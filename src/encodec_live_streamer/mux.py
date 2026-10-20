"""OndaBaja radio-directory and stream-selection TCP protocol (separate port)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from datetime import datetime, timezone

from .config import Config
from .manifest import ManifestStore, PublishedSegment
from .tcp import absolute_segment_frame, encode_varint, payload_frame, read_varint

LOG = logging.getLogger(__name__)
MAX_CATALOG_BYTES = 256 * 1024
MAX_STATION_ID_BYTES = 128
_CATALOG_RELOADED = object()
_CLOSE_CONNECTION = object()


def _pb_uint(field: int, value: int) -> bytes:
    return encode_varint(field << 3) + encode_varint(value)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return encode_varint((field << 3) | 2) + encode_varint(len(value)) + value


def _catalog_bytes(revision: int, updated_ms: int, stations: tuple["Radio", ...]) -> bytes:
    result = bytearray(_pb_uint(1, revision) + _pb_uint(2, updated_ms))
    for radio in stations:
        message = b"".join((
            _pb_bytes(1, radio.id.encode("utf-8")),
            _pb_bytes(2, radio.title.encode("utf-8")),
            _pb_bytes(3, radio.manifest_url.encode("utf-8")),
        ))
        result.extend(_pb_bytes(3, message))
    if len(result) > MAX_CATALOG_BYTES:
        raise ValueError("station catalog exceeds the protocol size limit")
    return bytes(result)


@dataclasses.dataclass(frozen=True)
class Radio:
    id: str
    title: str
    manifest_url: str
    store: ManifestStore


async def _read_varint(reader: asyncio.StreamReader, timeout: float) -> int:
    value = 0
    for index in range(10):
        raw = await asyncio.wait_for(reader.readexactly(1), timeout)
        byte = raw[0]
        if index == 9 and byte > 1:
            raise ValueError("varint exceeds uint64")
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            if index and byte == 0:
                raise ValueError("noncanonical varint")
            return value
    raise ValueError("unterminated varint")


async def _write(writer: asyncio.StreamWriter, frame: bytes) -> None:
    writer.write(frame)
    await writer.drain()


async def _segment_frame(store: ManifestStore, item: PublishedSegment,
                         snapshot=None) -> bytes | None:
    view = snapshot if snapshot is not None else store.snapshot()
    cached = view.retained_segments.get(item.sequence)
    if cached is not None and cached.absolute_frame is not None:
        return cached.absolute_frame
    try:
        payload = await asyncio.to_thread((store.output_dir / item.uri).read_bytes)
    except OSError:
        return None
    if len(payload) != item.byte_length:
        return None
    return absolute_segment_frame(item, payload)


class OndaBajaTcpServer:
    """Async ELTCP listener; station publishers remain in their worker threads."""

    def __init__(self, host: str, port: int, radios: tuple[Radio, ...], revision: int,
                 idle_timeout: float = 60.0, wait_seconds: float = 15.0,
                 max_clients: int = 4096):
        self.host = host
        self.port = port
        self.radios = tuple(radios)
        self.radios_by_id = {radio.id: radio for radio in radios}
        self.revision = revision
        self.catalog_updated_at_unix_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.catalog = _catalog_bytes(revision, self.catalog_updated_at_unix_ms, self.radios)
        self.idle_timeout = idle_timeout
        self.wait_seconds = wait_seconds
        self.max_clients = max_clients
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._started = False
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._client_tasks: set[asyncio.Task] = set()
        self._client_changes: dict[asyncio.StreamWriter, asyncio.Event] = {}
        self._stopping = threading.Event()

    @property
    def address(self):
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()
        return self.host, self.port

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._thread_ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _start_server(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
            backlog=min(self.max_clients, 4096), limit=MAX_CATALOG_BYTES,
        )

    def start(self) -> None:
        if self._thread is not None or self._started:
            raise RuntimeError("OndaBaja TCP service already started")
        self._thread = threading.Thread(target=self._run_loop,
                                        name="ondabaja-mux-asyncio", daemon=True)
        self._thread.start()
        self._thread_ready.wait()
        try:
            asyncio.run_coroutine_threadsafe(self._start_server(), self._loop).result()
        except BaseException:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._thread = None
            raise
        self._started = True
        LOG.info("OndaBaja async TCP listening on %s", self.address)

    async def _update_catalog(self, radios: tuple[Radio, ...], revision: int) -> None:
        self.radios = tuple(radios)
        self.radios_by_id = {radio.id: radio for radio in radios}
        self.revision = revision
        self.catalog_updated_at_unix_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.catalog = _catalog_bytes(revision, self.catalog_updated_at_unix_ms, self.radios)

    async def _notify_catalog_changed(self) -> None:
        for changed in tuple(self._client_changes.values()):
            changed.set()

    def update_catalog(self, radios: tuple[Radio, ...], revision: int) -> None:
        """Replace station metadata without rebinding the TCP listener."""
        if self._loop is None or self._thread is None:
            raise RuntimeError("OndaBaja TCP listener is not running")
        asyncio.run_coroutine_threadsafe(
            self._update_catalog(radios, revision), self._loop
        ).result(timeout=10)

    def notify_catalog_changed(self) -> None:
        if self._loop is None or self._thread is None:
            raise RuntimeError("OndaBaja TCP listener is not running")
        asyncio.run_coroutine_threadsafe(
            self._notify_catalog_changed(), self._loop
        ).result(timeout=10)

    async def _read_exactly(self, reader: asyncio.StreamReader, count: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(count), self.idle_timeout)

    async def _select_radio(self, writer: asyncio.StreamWriter, raw_id: bytes) -> Radio | None:
        if not raw_id or len(raw_id) > MAX_STATION_ID_BYTES:
            raise ValueError("invalid station ID length")
        station_id = raw_id.decode("utf-8")
        radio = self.radios_by_id.get(station_id)
        if radio is None:
            await _write(writer, b"x")
            return None
        await _write(writer, radio.store.snapshot().manifest_frame)
        return radio

    async def _startup_response(self, radio: Radio, selection_id: int,
                                lookback_ms: int) -> bytes:
        """Build a B selection response from one immutable station snapshot."""
        snapshot = radio.store.snapshot()
        manifest = snapshot.manifest
        prefix = b"B" + encode_varint(selection_id) + encode_varint(len(manifest)) + manifest
        if not snapshot.segments:
            return prefix + b"n"

        segments = snapshot.segments
        index = len(segments) - 1
        buffered_ms = int(segments[index].duration * 1000)
        target_ms = max(1, lookback_ms)
        while index > 0 and buffered_ms < target_ms:
            index -= 1
            buffered_ms += int(segments[index].duration * 1000)
        # Match LiveSequenceTracker: retain a two-segment deletion margin for
        # useful windows (8+ entries), while allowing smaller windows whole.
        if len(segments) >= 8:
            index = max(index, 2)

        selected = segments[index]
        frame = await _segment_frame(radio.store, selected, snapshot)
        if frame is None or not frame.startswith(b"S"):
            return prefix + b"n"
        return prefix + b"S" + encode_varint(selected.sequence) + frame[1:]

    async def _read_station_request_data(self, reader, command: bytes):
        selection_id = None
        if command == b"B":
            selection_id = await _read_varint(reader, self.idle_timeout)
            if selection_id == 0:
                raise ValueError("B selection ID must be positive")
        size = await _read_varint(reader, self.idle_timeout)
        if size == 0 or size > MAX_STATION_ID_BYTES:
            raise ValueError("invalid station ID length")
        raw_id = await self._read_exactly(reader, size)
        lookback_ms = (await _read_varint(reader, self.idle_timeout)
                       if command == b"B" else None)
        return raw_id, selection_id, lookback_ms

    async def _startup_exchange(self, reader, writer, radio: Radio,
                                selection_id: int, lookback_ms: int) -> Radio | object:
        """Prepare B while still accepting a same-socket retry or switch."""
        request_id = selection_id
        request_station = radio.id
        request_lookback = lookback_ms
        preparation = asyncio.create_task(
            self._startup_response(radio, request_id, request_lookback))
        incoming = asyncio.create_task(reader.read(1))
        try:
            while True:
                done, _ = await asyncio.wait((preparation, incoming),
                                             return_when=asyncio.FIRST_COMPLETED)
                # Process a command first if it arrived in the same event-loop
                # turn as preparation completion. This makes B retries and R
                # switches deterministic and prevents an abandoned B reply.
                if incoming in done:
                    command = incoming.result()
                    if not command or command in (b"Q", b"q"):
                        preparation.cancel()
                        await asyncio.gather(preparation, return_exceptions=True)
                        return _CLOSE_CONNECTION
                    if command == b"H":
                        await _write(writer, b"h")
                        incoming = asyncio.create_task(reader.read(1))
                        continue
                    if command == b"R":
                        preparation.cancel()
                        await asyncio.gather(preparation, return_exceptions=True)
                        raw_id, _, _ = await self._read_station_request_data(reader, command)
                        return await self._select_radio(writer, raw_id)
                    if command != b"B":
                        raise ValueError("only B, R, H, or Q may interrupt B preparation")

                    raw_id, retry_id, retry_lookback = await self._read_station_request_data(
                        reader, command)
                    station_id = raw_id.decode("utf-8")
                    if (retry_id == request_id and station_id == request_station
                            and retry_lookback == request_lookback):
                        # The in-flight response satisfies this retry because
                        # the client correlates it with selectionId.
                        incoming = asyncio.create_task(reader.read(1))
                        continue

                    preparation.cancel()
                    await asyncio.gather(preparation, return_exceptions=True)
                    radio = self.radios_by_id.get(station_id)
                    if radio is None:
                        await _write(writer, b"x")
                        return None
                    request_id = retry_id
                    request_station = station_id
                    request_lookback = retry_lookback or 0
                    preparation = asyncio.create_task(
                        self._startup_response(radio, request_id, request_lookback))
                    incoming = asyncio.create_task(reader.read(1))
                    continue

                response = preparation.result()
                if not incoming.done():
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
                    incoming = None
                await _write(writer, response)
                return radio
        finally:
            if not preparation.done():
                preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
            if incoming is not None and not incoming.done():
                incoming.cancel()
            if incoming is not None:
                await asyncio.gather(incoming, return_exceptions=True)

    async def _read_station_request(self, reader, writer,
                                    command: bytes) -> Radio | object | None:
        raw_id, selection_id, lookback_ms = await self._read_station_request_data(
            reader, command)
        if command == b"R":
            return await self._select_radio(writer, raw_id)

        station_id = raw_id.decode("utf-8")
        radio = self.radios_by_id.get(station_id)
        if radio is None:
            await _write(writer, b"x")
            return None
        return await self._startup_exchange(reader, writer, radio, selection_id,
                                            lookback_ms or 0)

    async def _fetch(self, reader, writer, radio: Radio, sequence: int,
                     catalog_changed: asyncio.Event):
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def notify() -> None:
            try:
                loop.call_soon_threadsafe(changed.set)
            except RuntimeError:
                pass

        radio.store.subscribe_publication(notify)
        deadline = loop.time() + self.wait_seconds
        # Keep the read itself as the task. Wrapping a freshly-created read
        # coroutine in wait_for can leave it unawaited if this task is
        # cancelled before wait_for gets its first event-loop turn.
        incoming = asyncio.create_task(reader.read(1))
        incoming_deadline = loop.time() + self.idle_timeout
        publication = None
        catalog_notification = None

        async def handle_interrupt(command: bytes):
            nonlocal incoming, incoming_deadline
            if not command:
                return "close", None
            if command == b"H":
                await _write(writer, b"h")
                incoming = asyncio.create_task(reader.read(1))
                incoming_deadline = loop.time() + self.idle_timeout
                return "continue", None
            if command in (b"R", b"B"):
                selected = await self._read_station_request(reader, writer, command)
                if selected is _CLOSE_CONNECTION:
                    return "close", None
                return "selected", selected
            if command in (b"Q", b"q"):
                return "close", None
            raise ValueError("only R, B, H, or Q may interrupt a pending f request")

        async def handle_ready_interrupt():
            # Let the socket reader run once before committing a segment or
            # timeout response. If R arrived in the same wake-up window, it
            # must win so the old fetch cannot consume its command byte.
            await asyncio.sleep(0)
            if incoming is None or not incoming.done():
                return None, None
            command = incoming.result()
            return await handle_interrupt(command)

        try:
            while not self._stopping.is_set():
                if incoming.done():
                    action, selected = await handle_interrupt(incoming.result())
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    return None, None, True
                if catalog_changed.is_set():
                    action, selected = await handle_ready_interrupt()
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    if action == "close":
                        return None, None, True
                    if incoming is not None and not incoming.done():
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        incoming = None
                    catalog_changed.clear()
                    await _write(writer, b"u")
                    return _CATALOG_RELOADED, None, False
                changed.clear()
                snapshot = radio.store.snapshot()
                if sequence < snapshot.next_sequence:
                    item = snapshot.retained_segments.get(sequence)
                    if item is None:
                        await _write(writer, b"g")
                        return None, None, None
                    frame = await _segment_frame(radio.store, item)
                    action, selected = await handle_ready_interrupt()
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    if action == "close":
                        return None, None, True
                    if incoming is not None and not incoming.done():
                        # Stop the speculative read before replying. Bytes
                        # arriving after this point remain buffered for the
                        # next command loop instead of being lost here.
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        incoming = None
                    await _write(writer, frame if frame is not None else b"g")
                    return None, frame, False

                remaining = deadline - loop.time()
                if remaining <= 0:
                    await _write(writer, b"n")
                    return None, None, False
                publication = asyncio.create_task(changed.wait())
                catalog_notification = asyncio.create_task(catalog_changed.wait())
                read_remaining = incoming_deadline - loop.time()
                if read_remaining <= 0:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait((incoming, publication, catalog_notification),
                                             timeout=min(remaining, read_remaining),
                                             return_when=asyncio.FIRST_COMPLETED)
                if incoming in done:
                    publication.cancel()
                    await asyncio.gather(publication, return_exceptions=True)
                    publication = None
                    catalog_notification.cancel()
                    await asyncio.gather(catalog_notification, return_exceptions=True)
                    catalog_notification = None
                    action, selected = await handle_interrupt(incoming.result())
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    return None, None, True
                if catalog_notification in done:
                    action, selected = await handle_ready_interrupt()
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    if action == "close":
                        return None, None, True
                    if incoming is not None and not incoming.done():
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        incoming = None
                    catalog_notification = None
                    catalog_changed.clear()
                    await _write(writer, b"u")
                    return _CATALOG_RELOADED, None, False
                if publication in done:
                    publication = None
                    continue
                if loop.time() >= deadline:
                    action, selected = await handle_ready_interrupt()
                    if action == "continue":
                        continue
                    if action == "selected":
                        return selected, None, False
                    if action == "close":
                        return None, None, True
                    if incoming is not None and not incoming.done():
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        incoming = None
                    await _write(writer, b"n")
                    return None, None, False
                raise asyncio.TimeoutError
            return None, None, True
        finally:
            radio.store.unsubscribe_publication(notify)
            if publication is not None and not publication.done():
                publication.cancel()
                await asyncio.gather(publication, return_exceptions=True)
            if catalog_notification is not None and not catalog_notification.done():
                catalog_notification.cancel()
                await asyncio.gather(catalog_notification, return_exceptions=True)
            if incoming is not None and not incoming.done():
                incoming.cancel()
            if incoming is not None:
                await asyncio.gather(incoming, return_exceptions=True)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if len(self._writers) >= self.max_clients:
            try:
                await _write(writer, b"b")
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            return
        if task is not None:
            self._client_tasks.add(task)
        self._writers.add(writer)
        catalog_changed = asyncio.Event()
        self._client_changes[writer] = catalog_changed
        peer = writer.get_extra_info("peername")
        radio: Radio | None = None
        last_frame: bytes | None = None
        try:
            while not self._stopping.is_set():
                command_read = asyncio.create_task(self._read_exactly(reader, 1))
                change_wait = asyncio.create_task(catalog_changed.wait())
                done, _ = await asyncio.wait((command_read, change_wait),
                                             return_when=asyncio.FIRST_COMPLETED)
                if change_wait in done and command_read not in done:
                    catalog_changed.clear()
                    await _write(writer, b"u")
                    change_wait.cancel()
                    await asyncio.gather(change_wait, return_exceptions=True)
                    # Keep the pending socket read for the next loop iteration.
                    command = await command_read
                else:
                    change_wait.cancel()
                    await asyncio.gather(change_wait, return_exceptions=True)
                    command = command_read.result()
                if radio is not None:
                    radio = self.radios_by_id.get(radio.id)
                if command in (b"Q", b"q"):
                    return
                if command in (b"R", b"B"):
                    selected = await self._read_station_request(reader, writer, command)
                    if selected is _CLOSE_CONNECTION:
                        return
                    if selected is not None:
                        radio = selected
                        last_frame = None
                elif command == b"C":
                    revision = await _read_varint(reader, self.idle_timeout)
                    await _write(writer, b"n" if revision == self.revision else
                                 payload_frame(b"c", self.catalog))
                elif command == b"M":
                    await _write(writer, b"x" if radio is None else
                                 radio.store.snapshot().manifest_frame)
                elif command == b"f":
                    sequence = await _read_varint(reader, self.idle_timeout)
                    last_frame = None
                    if radio is None:
                        await _write(writer, b"x")
                        continue
                    selected, frame, close = await self._fetch(
                        reader, writer, radio, sequence, catalog_changed)
                    if close:
                        return
                    if selected is _CATALOG_RELOADED:
                        continue
                    if selected is not None:
                        radio = selected
                        last_frame = None
                    else:
                        last_frame = frame
                elif command == b"e":
                    await _write(writer, last_frame if last_frame is not None else b"r")
                elif command == b"H":
                    await _write(writer, b"h")
                else:
                    raise ValueError(f"unknown OndaBaja command {command!r}")
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionError, UnicodeDecodeError, ValueError) as error:
            LOG.debug("OndaBaja TCP connection from %s ended: %s", peer, error)
        finally:
            self._writers.discard(writer)
            self._client_changes.pop(writer, None)
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _close_server(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        tasks = tuple(task for task in self._client_tasks
                      if task is not asyncio.current_task() and not task.done())
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()


    def close(self) -> None:
        if self._thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_server(), self._loop).result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            self._thread = None
