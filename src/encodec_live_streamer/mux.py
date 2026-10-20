"""OndaBaja radio-directory and stream-selection TCP protocol (separate port)."""
from __future__ import annotations

import dataclasses
import logging
import select
import socket
import socketserver
import threading
import time
from datetime import datetime, timezone

from .config import Config
from .manifest import ManifestStore, PublishedSegment
from .tcp import absolute_segment_frame, encode_varint, payload_frame, read_varint

LOG = logging.getLogger(__name__)
MAX_CATALOG_BYTES = 256 * 1024
MAX_STATION_ID_BYTES = 128


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


class _MuxHandler(socketserver.BaseRequestHandler):
    def _read_exact(self, count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            chunk = self.request.recv(count - len(result))
            if not chunk:
                raise ConnectionError("peer closed during request")
            result.extend(chunk)
        return bytes(result)

    def _select_radio(self, raw_id: bytes) -> Radio | None:
        if not raw_id or len(raw_id) > MAX_STATION_ID_BYTES:
            raise ValueError("invalid station ID length")
        try:
            station_id = raw_id.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("station ID is not UTF-8") from error
        radio = self.server.radios_by_id.get(station_id)
        if radio is None:
            self.request.sendall(b"x")
            LOG.debug("unknown station ID %r from %s", station_id, self.client_address)
            return None
        self.radio = radio
        self.generation += 1
        self.last_frame = None
        self.request.sendall(radio.store.snapshot().manifest_frame)
        return radio

    def _read_optional_command(self) -> tuple[bytes, bytes | int | None] | None:
        """Read a command already queued while an f publication wait is pending."""
        ready, _, _ = select.select([self.request], [], [], 0)
        if not ready:
            return None
        command = self._read_exact(1)
        if command == b"R":
            size = read_varint(self.request)
            if size > MAX_STATION_ID_BYTES:
                raise ValueError("station ID exceeds protocol limit")
            return command, self._read_exact(size)
        if command == b"f":
            return command, read_varint(self.request)
        return command, None

    def _fetch(self, sequence: int) -> bool:
        """Return false if a queued station switch superseded this fetch."""
        assert self.radio is not None
        radio = self.radio
        generation = self.generation
        deadline = time.monotonic() + self.server.wait_seconds
        while not self.server.stopping.is_set() and generation == self.generation:
            queued = self._read_optional_command()
            if queued is not None:
                command, argument = queued
                if command == b"R":
                    self._select_radio(argument)  # type: ignore[arg-type]
                    return False
                if command == b"H":
                    self.request.sendall(b"h")
                    continue
                raise ValueError("only R or H may interrupt a pending f request")

            snapshot = radio.store.snapshot()
            if sequence < snapshot.next_sequence:
                item = snapshot.retained_segments.get(sequence)
                if item is None:
                    self.request.sendall(b"g")
                    return True
                frame = self._segment_frame(radio.store, item)
                if frame is None:
                    self.request.sendall(b"g")
                else:
                    self.last_frame = frame
                    self.request.sendall(frame)
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.request.sendall(b"n")
                return True
            # Short slices let us observe R and cancel the publication wait.
            radio.store.wait_snapshot(snapshot.manifest, min(remaining, 0.05), self.server.stopping)
        return False

    @staticmethod
    def _segment_frame(store: ManifestStore, item: PublishedSegment) -> bytes | None:
        cached = store.snapshot().retained_segments.get(item.sequence)
        payload = None
        if cached is not None and cached.absolute_frame is not None:
            return cached.absolute_frame
        try:
            payload = (store.output_dir / item.uri).read_bytes()
        except OSError:
            return None
        if len(payload) != item.byte_length:
            return None
        return absolute_segment_frame(item, payload)

    def handle(self) -> None:
        self.request.settimeout(self.server.idle_timeout)
        self.radio: Radio | None = None
        self.generation = 0
        self.last_frame: bytes | None = None
        try:
            while not self.server.stopping.is_set():
                command = self._read_exact(1)
                if command in (b"Q", b"q"):
                    return
                if command == b"R":
                    size = read_varint(self.request)
                    if size > MAX_STATION_ID_BYTES:
                        raise ValueError("station ID exceeds protocol limit")
                    self._select_radio(self._read_exact(size))
                elif command == b"C":
                    revision = read_varint(self.request)
                    if revision == self.server.revision:
                        self.request.sendall(b"n")
                    else:
                        self.request.sendall(payload_frame(b"c", self.server.catalog))
                elif command == b"M":
                    if self.radio is None:
                        self.request.sendall(b"x")
                    else:
                        self.request.sendall(self.radio.store.snapshot().manifest_frame)
                elif command == b"f":
                    if self.radio is None:
                        self.request.sendall(b"x")
                    else:
                        sequence = read_varint(self.request)
                        self.last_frame = None
                        self._fetch(sequence)
                elif command == b"e":
                    if self.last_frame is None:
                        self.request.sendall(b"r")
                    else:
                        self.request.sendall(self.last_frame)
                elif command == b"H":
                    self.request.sendall(b"h")
                else:
                    raise ValueError(f"unknown OndaBaja command {command!r}")
        except (OSError, ConnectionError, ValueError) as error:
            LOG.debug("OndaBaja TCP connection from %s ended: %s", self.client_address, error)


class _MuxSocketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, radios, revision, idle_timeout, wait_seconds, max_clients):
        self.radios_by_id = {radio.id: radio for radio in radios}
        self.revision = revision
        updated_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.catalog_updated_at_unix_ms = updated_ms
        self.catalog = _catalog_bytes(revision, updated_ms, tuple(radios))
        self.idle_timeout = idle_timeout
        self.wait_seconds = wait_seconds
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(max_clients)
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        super().__init__(address, _MuxHandler)
        self.request_queue_size = min(max_clients, 128)

    def get_request(self):
        request, address = super().get_request()
        request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return request, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"b")
            finally:
                self.shutdown_request(request)
            return
        with self.connections_lock:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.connections_lock:
                self.connections.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connections_lock:
                self.connections.discard(request)
            self.slots.release()

    def stop_connections(self):
        with self.connections_lock:
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class OndaBajaTcpServer:
    def __init__(self, host: str, port: int, radios: tuple[Radio, ...], revision: int,
                 idle_timeout: float = 60.0, wait_seconds: float = 15.0,
                 max_clients: int = 32):
        self._server = _MuxSocketServer((host, port), radios, revision, idle_timeout,
                                        wait_seconds, max_clients)
        self._thread: threading.Thread | None = None

    @property
    def catalog(self) -> bytes:
        return self._server.catalog

    @property
    def catalog_updated_at_unix_ms(self) -> int:
        return self._server.catalog_updated_at_unix_ms

    @property
    def address(self):
        return self._server.server_address

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("OndaBaja TCP service already started")
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1},
                                        name="ondabaja-mux-tcp", daemon=True)
        self._thread.start()
        LOG.info("OndaBaja multiplexed TCP listening on %s", self.address)

    def close(self) -> None:
        self._server.stopping.set()
        for radio in self._server.radios_by_id.values():
            radio.store.wake_snapshot_waiters()
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        self._server.stop_connections()
        self._server.server_close()
