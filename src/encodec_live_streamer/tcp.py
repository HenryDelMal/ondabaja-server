"""ELTCP v1: persistent, framed requests over ordinary TCP (docs/TCP_PROTOCOL.md)."""
from __future__ import annotations

import logging
import json
import queue
import itertools
import dataclasses
from datetime import datetime, timezone
import socket
import socketserver
import struct
import threading
import time

from .config import Config
from .manifest import ManifestStore, PublicationSnapshot, PublishedSegment


LOG = logging.getLogger(__name__)


class ProtocolError(ValueError):
    pass


def encode_varint(value: int) -> bytes:
    """Canonical unsigned LEB128, bounded to uint64."""
    if not 0 <= value < 1 << 64:
        raise ValueError("varint must fit uint64")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(connection: socket.socket) -> int:
    value = 0
    for index in range(10):
        raw = connection.recv(1)
        if not raw:
            raise ProtocolError("truncated varint")
        byte = raw[0]
        if index == 9 and byte > 1:
            raise ProtocolError("varint exceeds uint64")
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            if index and byte == 0:
                raise ProtocolError("noncanonical varint")
            return value
    raise ProtocolError("unterminated varint")


def send_payload(connection: socket.socket, kind: bytes, payload: bytes) -> None:
    # One write avoids Nagle/delayed-ACK stalls between header and payload.
    connection.sendall(payload_frame(kind, payload))


def payload_frame(kind: bytes, payload: bytes) -> bytes:
    return kind + encode_varint(len(payload)) + payload


def absolute_segment_frame(item: PublishedSegment, payload: bytes) -> bytes:
    """ELTCP v1 absolute-sequence reply: length, big-endian CRC32C, flags, ECDC."""
    flags = 1 if item.discontinuity else 0
    return (
        b"S"
        + encode_varint(len(payload))
        + struct.pack(">I", item.crc32c)
        + bytes((flags,))
        + payload
    )


class _RequestHandler(socketserver.BaseRequestHandler):
    def _snapshot(self) -> PublicationSnapshot:
        return self.server.store.snapshot(self._timing)

    def _wait(self, previous: bytes, interval: float) -> PublicationSnapshot:
        started = time.monotonic_ns()
        result = self.server.store.wait_snapshot(previous, interval, self.server.stopping)
        if self._timing is not None:
            self._timing["publication_wait_ms"] += (time.monotonic_ns() - started) / 1e6
        return result

    def _send(self, frame: bytes) -> None:
        utc = datetime.now(timezone.utc).isoformat() if self._timing is not None else None
        started = time.monotonic_ns()
        self.request.sendall(frame)
        finished = time.monotonic_ns()
        if self._timing is not None:
            record = dict(self._timing)
            record.update(response=frame[:1].decode("ascii"), response_bytes=len(frame),
                          write_start_utc=utc, write_complete_utc=datetime.now(timezone.utc).isoformat(),
                          write_ms=(finished-started)/1e6,
                          command_to_write_complete_ms=(finished-self._command_received)/1e6)
            record["processing_ms"] = record["command_to_write_complete_ms"] - record["publication_wait_ms"]
            self.server.diagnostic(record)

    def _segment_frame(self, item: PublishedSegment, absolute: bool) -> bytes | None:
        started = time.monotonic_ns()
        # Use the latest cache, rather than pinning frames from an old i/s window.
        cached = item if absolute else self._snapshot().retained_segments.get(item.sequence)
        frame = (cached.absolute_frame if absolute else cached.legacy_frame) if cached else None
        if self._timing is not None:
            self._timing["segment_lookup_ms"] += (time.monotonic_ns()-started)/1e6
            self._timing["segment_cache_hit"] = frame is not None
        if frame is not None:
            return frame
        started = time.monotonic_ns()
        try:
            payload = (self.server.store.output_dir / item.uri).read_bytes()
        except FileNotFoundError:
            return None
        finally:
            if self._timing is not None:
                self._timing["file_read_ms"] += (time.monotonic_ns()-started)/1e6
        if len(payload) != item.byte_length:
            return None
        started = time.monotonic_ns()
        frame = absolute_segment_frame(item, payload) if absolute else payload_frame(b"s", payload)
        if self._timing is not None:
            self._timing["frame_preparation_ms"] += (time.monotonic_ns()-started)/1e6
        return frame

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        connection = self.request
        connection.settimeout(server.config.tcp_idle_timeout)
        connection_id, accepted, accepted_utc = server._accepted[connection]
        self._timing = None
        window: PublicationSnapshot | None = None
        last_segment_response: bytes | None = None
        last_segment_sequence: int | None = None
        retries = 0
        try:
            while not server.stopping.is_set():
                command = connection.recv(1)
                if not command or command == b"q":
                    return
                if window is None and command != b"i":
                    raise ProtocolError("connection must begin with i")
                argument = read_varint(connection) if command in (b"s", b"f") else None
                self._command_received = time.monotonic_ns()
                if server.config.tcp_diagnostics:
                    self._timing = dict(connection_id=connection_id, command=command.decode("ascii", errors="replace"),
                        sequence=argument if command == b"f" else None,
                        request_bytes=1 + (len(encode_varint(argument)) if argument is not None else 0),
                        accepted_utc=accepted_utc, accept_to_command_ms=(self._command_received-accepted)/1e6,
                        snapshot_lock_wait_ms=0.0, snapshot_lookup_ms=0.0, manifest_cache_hit=False,
                        manifest_preparation_ms=0.0, segment_lookup_ms=0.0, segment_cache_hit=False,
                        file_read_ms=0.0, checksum_preparation_ms=0.0, frame_preparation_ms=0.0,
                        publication_wait_ms=0.0)
                if command in (b"i", b"m"):
                    last_segment_response = None
                    retries = 0
                    if command == b"i":
                        current = self._snapshot()
                    else:
                        assert window is not None
                        deadline = time.monotonic() + server.config.tcp_manifest_wait
                        current = self._snapshot()
                        while (
                            current.manifest == window.manifest
                            and not server.stopping.is_set()
                        ):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            interval = remaining
                            if server.config.tcp_heartbeat_enabled:
                                interval = min(
                                    interval,
                                    server.config.tcp_heartbeat_interval,
                                )
                            current = self._wait(window.manifest, interval)
                            if (
                                current.manifest == window.manifest
                                and not server.stopping.is_set()
                                and server.config.tcp_heartbeat_enabled
                                and time.monotonic() < deadline
                            ):
                                self._send(b"h")
                    if server.stopping.is_set():
                        return
                    if command == b"m" and current.manifest == window.manifest:
                        self._send(b"n")
                    else:
                        if self._timing is not None:
                            self._timing["manifest_cache_hit"] = True
                        self._send(current.manifest_frame)
                        # Retain only metadata for legacy index mapping, not old caches.
                        window = dataclasses.replace(current, retained_segments={})
                elif command == b"h":
                    # Client-initiated probe, valid between other requests.
                    # During an outstanding m, unsolicited h bytes are sent
                    # periodically instead; clients should not send commands then.
                    self._send(b"h")
                elif command == b"s":
                    assert window is not None
                    index = argument
                    last_segment_response = None
                    retries = 0
                    if index >= len(window.segments):
                        self._send(b"g")
                        continue
                    item = window.segments[index]
                    last_segment_sequence = item.sequence
                    if self._timing is not None:
                        self._timing["sequence"] = item.sequence
                    last_segment_response = self._segment_frame(item, False)
                    if last_segment_response is None:
                        self._send(b"g")
                        continue
                    self._send(last_segment_response)
                elif command == b"f":
                    # Fetch by absolute sequence without requiring a fresh manifest.
                    # Share the configured long-poll deadline with m. Heartbeats
                    # maintain liveness without extending that deadline.
                    sequence = argument
                    last_segment_sequence = sequence
                    last_segment_response = None
                    retries = 0
                    deadline = time.monotonic() + server.config.tcp_manifest_wait
                    next_heartbeat = time.monotonic() + server.config.tcp_heartbeat_interval
                    while True:
                        if server.stopping.is_set():
                            return
                        current = self._snapshot()
                        if sequence < current.next_sequence:
                            started = time.monotonic_ns()
                            item = current.retained_segments.get(sequence)
                            if self._timing is not None:
                                self._timing["segment_lookup_ms"] += (time.monotonic_ns()-started)/1e6
                            if item is None:
                                self._send(b"g")
                                break
                            last_segment_response = self._segment_frame(item, True)
                            if last_segment_response is None:
                                self._send(b"g")
                                break
                            self._send(last_segment_response)
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._send(b"n")
                            break
                        interval = remaining
                        if server.config.tcp_heartbeat_enabled:
                            interval = min(interval, max(0, next_heartbeat - time.monotonic()))
                        updated = self._wait(current.manifest, interval)
                        now = time.monotonic()
                        if (
                            server.config.tcp_heartbeat_enabled
                            and not server.stopping.is_set()
                            and updated.next_sequence <= sequence
                            and next_heartbeat <= now < deadline
                        ):
                            self._send(b"h")
                            next_heartbeat = now + server.config.tcp_heartbeat_interval
                elif command == b"e":
                    if last_segment_response is None:
                        raise ProtocolError("no segment available to retry")
                    if retries >= server.config.tcp_max_retries:
                        self._send(b"r")
                        return
                    retries += 1
                    if self._timing is not None:
                        self._timing["segment_cache_hit"] = True
                        self._timing["sequence"] = last_segment_sequence
                    self._send(last_segment_response)
                elif command == b"o":
                    # Optional success ACK. Next s/f/i/m also acknowledges it.
                    last_segment_response = None
                    retries = 0
                else:
                    raise ProtocolError("unknown command")
                # Idle clients retain only their legacy mapping and one retry
                # frame. Drop transient references to an obsolete shared cache.
                current = updated = item = None
        except ProtocolError as error:
            LOG.debug("TCP protocol error from %s: %s", self.client_address, error)
            try:
                connection.sendall(b"p")
            except OSError:
                pass
        except OSError as error:
            LOG.debug("TCP connection ended from %s: %s", self.client_address, error)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], config: Config, store: ManifestStore):
        self.config = config
        self.store = store
        self.stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(config.tcp_max_clients)
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._accepted: dict[socket.socket, tuple[int, int, str | None]] = {}
        self._connection_ids = itertools.count(1)
        self._diagnostics: queue.Queue = queue.Queue(maxsize=1024)
        self.diagnostics_dropped = 0
        self._diagnostic_stop = threading.Event()
        self._diagnostic_thread = None
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.request_queue_size = min(config.tcp_max_clients, 128)
        super().__init__(address, _RequestHandler)
        if config.tcp_diagnostics:
            self._diagnostic_thread = threading.Thread(target=self._log_diagnostics,
                name="eltcp-diagnostics", daemon=True)
            self._diagnostic_thread.start()

    def get_request(self):
        request, address = super().get_request()
        accepted = time.monotonic_ns()
        # Set before dispatch, including the capacity rejection response.
        request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        utc = datetime.now(timezone.utc).isoformat() if self.config.tcp_diagnostics else None
        identity = next(self._connection_ids)
        self._accepted[request] = (identity, accepted, utc)
        self.diagnostic(dict(event="accept", connection_id=identity, accepted_utc=utc))
        return request, address

    def diagnostic(self, record: dict) -> None:
        if not self.config.tcp_diagnostics:
            return
        try:
            self._diagnostics.put_nowait(record)
        except queue.Full:
            self.diagnostics_dropped += 1

    def _log_diagnostics(self) -> None:
        while not self._diagnostic_stop.is_set() or not self._diagnostics.empty():
            try:
                record = self._diagnostics.get(timeout=0.1)
            except queue.Empty:
                continue
            record["diagnostics_dropped"] = self.diagnostics_dropped
            LOG.info("ELTCP timing %s", json.dumps(record, separators=(",", ":")))

    def server_close(self) -> None:
        super().server_close()
        self._diagnostic_stop.set()
        if self._diagnostic_thread is not None:
            self._diagnostic_thread.join(timeout=0.5)

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"b")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
                self._accepted.pop(request, None)
            return
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._connections_lock:
                self._connections.discard(request)
            self._slots.release()
            self._accepted.pop(request, None)
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connections_lock:
                self._connections.discard(request)
            self._slots.release()
            self._accepted.pop(request, None)

    def stop_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class TcpServer:
    """Bounded thread-per-client server; encoding/publication never waits on clients."""

    def __init__(self, config: Config, store: ManifestStore) -> None:
        self._server = _Server((config.tcp_host, config.tcp_port), config, store)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple:
        return self._server.server_address

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("TCP server already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="encodec-live-tcp",
            daemon=True,
        )
        self._thread.start()
        LOG.info("ELTCP v1 listening on %s", self.address)

    def close(self) -> None:
        self._server.stopping.set()
        self._server.store.wake_snapshot_waiters()
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        self._server.stop_connections()
        self._server.server_close()
