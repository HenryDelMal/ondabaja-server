import dataclasses
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from encodec_live_streamer.config import Config
from encodec_live_streamer.ecdc import make_test_ecdc
from encodec_live_streamer.manifest import ManifestStore, crc32c
from encodec_live_streamer.tcp import TcpServer, encode_varint, read_varint, _RequestHandler


def receive_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        block = connection.recv(count - len(result))
        if not block:
            raise EOFError("connection closed within a payload")
        result.extend(block)
    return bytes(result)


def receive_payload(connection, expected):
    kind = receive_exact(connection, 1)
    if kind != expected:
        raise AssertionError(f"expected {expected!r}, received {kind!r}")
    return receive_exact(connection, read_varint(connection))


def receive_absolute_segment(connection):
    kind = receive_exact(connection, 1)
    while kind == b"h":
        kind = receive_exact(connection, 1)
    if kind != b"S":
        raise AssertionError("expected absolute-sequence segment response")
    length = read_varint(connection)
    checksum = struct.unpack(">I", receive_exact(connection, 4))[0]
    flags = receive_exact(connection, 1)[0]
    return receive_exact(connection, length), checksum, flags


class TcpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Only the test harness uses port 0 to ask the OS for an unused port.
        # User-facing TOML validation requires a fixed port in 1..65535.
        self.config = Config(
            input="unused", output_dir=Path(self.directory.name),
            fsync=False, tcp_enabled=True, tcp_port=0, tcp_manifest_wait=0.2,
            tcp_idle_timeout=2, tcp_max_clients=2, tcp_max_retries=2,
            tcp_heartbeat_enabled=True, tcp_heartbeat_interval=0.05,
            window_segments=2, stale_grace_segments=0,
        )
        self.store = ManifestStore(self.config)
        self.store.write_manifest()
        self.server = TcpServer(self.config, self.store)
        self.server.start()
        self.addCleanup(self.server.close)

    def connect(self):
        connection = socket.create_connection(self.server.address, timeout=2)
        self.addCleanup(connection.close)
        return connection

    def initialize(self, connection):
        connection.sendall(b"i")
        return receive_payload(connection, b"m")

    def publish(self, padding=0):
        samples = self.config.samples_per_segment
        sequence = self.store.next_sequence
        payload = make_test_ecdc(samples, self.config.codebooks, self.config.model)
        payload += bytes(padding)
        self.store.publish_segment(
            payload, sample_count=samples, pts_samples=sequence * samples,
            program_date_time="2026-01-01T00:00:00Z", epoch="test",
            epoch_start_unix_ms=1767225600000, discontinuity=sequence == 0,
        )
        return payload

    def test_binary_frames_and_coalesced_optional_ack(self):
        payload = self.publish()
        connection = self.connect()
        self.assertEqual(self.initialize(connection), self.store.protobuf_path.read_bytes())
        # TCP writes can split commands or combine several commands.
        connection.sendall(b"s")
        connection.sendall(b"\x00")
        self.assertEqual(receive_payload(connection, b"s"), payload)
        connection.sendall(b"os\x00")
        self.assertEqual(receive_payload(connection, b"s"), payload)

    def test_init_empty_and_existing_fetch_use_published_frames(self):
        connection = self.connect()
        with mock.patch('encodec_live_streamer.manifest.manifest_protobuf', side_effect=AssertionError('serialized during init')):
            self.assertEqual(self.initialize(connection), self.store.snapshot().manifest)
        for accepted in tuple(self.server._server._connections):
            # Some kernels return a nonzero flag mask rather than literal 1.
            self.assertTrue(accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        payload = self.publish()
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('disk read on cache hit')):
            connection.sendall(b'f\x00')
            self.assertEqual(receive_absolute_segment(connection)[0], payload)

    def test_reconnect_init_refreshes_index_mapping(self):
        self.publish()
        first = self.connect()
        old = self.initialize(first)
        first.close()
        self.publish()
        self.publish()
        second = self.connect()
        current = self.initialize(second)
        self.assertNotEqual(current, old)
        second.sendall(b's\x00')
        body = receive_payload(second, b's')
        self.assertEqual(body, (self.config.output_dir / 'segment-000000000001.ecdc').read_bytes())

    def test_cache_budget_and_grace_retention(self):
        self.store.config = dataclasses.replace(self.config, stale_grace_segments=1,
                                                tcp_segment_cache_bytes=4096)
        payload = self.publish()
        self.publish()
        self.publish()
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b'f\x00')
        self.assertEqual(receive_absolute_segment(connection)[0], payload)

        self.assertLessEqual(self.store._cache_bytes, 4096)
        self.assertLessEqual(len(self.store._frames), 3)
        self.publish()
        connection.sendall(b'f\x00')
        self.assertEqual(receive_exact(connection, 1), b'g')
        # Zero cache budget exercises disk fallback, with identical wire bytes.
        self.store.config = dataclasses.replace(self.store.config, tcp_segment_cache_bytes=0)
        self.store._frames.clear()
        self.store._cache_bytes = 0
        self.store.write_manifest()
        connection.sendall(b'f\x03')
        self.assertEqual(receive_absolute_segment(connection)[0], payload)

    def test_cache_byte_eviction_and_restart_preload(self):
        self.store.config = dataclasses.replace(self.config, tcp_segment_cache_bytes=2500)
        self.publish(padding=970)
        latest = self.publish(padding=970)
        self.assertEqual(len(self.store._frames), 1)
        self.assertLessEqual(self.store._cache_bytes, 2500)
        connection = self.connect()
        self.initialize(connection)
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected disk read')) as read:
            # Latest segment is cached despite oldest-byte-budget eviction.
            connection.sendall(b'f\x01')
            self.assertEqual(receive_absolute_segment(connection)[0], latest)
            read.assert_not_called()
        restored = ManifestStore(self.store.config)
        self.assertIsNotNone(restored.snapshot().retained_segments[1].absolute_frame)

    def test_actual_slow_reader_does_not_block_other_client(self):
        self.publish(padding=256*1024)
        slow = self.connect()
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        self.initialize(slow)
        for accepted in tuple(self.server._server._connections):
            accepted.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        entered = threading.Event()
        finished = threading.Event()
        original = _RequestHandler._send
        def observed_send(handler, frame):
            if frame[:1] == b'S':
                entered.set()
                try:
                    return original(handler, frame)
                finally:
                    finished.set()
            return original(handler, frame)
        with mock.patch.object(_RequestHandler, '_send', observed_send):
            slow.sendall(b'f\x00')
            self.assertTrue(entered.wait(1))
            fast = self.connect()
            fast.settimeout(.5)
            self.assertEqual(self.initialize(fast), self.store.snapshot().manifest)
            self.assertFalse(finished.is_set(), 'large send should still be blocked by unread socket')
            self.publish()
            fast.sendall(b'i')
            self.assertEqual(receive_payload(fast, b'm'), self.store.snapshot().manifest)
            slow.close()
            self.assertTrue(finished.wait(1))

    def test_init_during_publication_io_returns_previous_complete_snapshot(self):
        previous = self.store.snapshot().manifest
        entered = threading.Event()
        release = threading.Event()
        from encodec_live_streamer.manifest import atomic_write
        def slow_write(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return atomic_write(*args, **kwargs)
        with mock.patch('encodec_live_streamer.manifest.atomic_write', side_effect=slow_write):
            publisher = threading.Thread(target=self.publish)
            publisher.start()
            try:
                self.assertTrue(entered.wait(1))
                connection = self.connect()
                connection.settimeout(.5)
                self.assertEqual(self.initialize(connection), previous)
            finally:
                release.set()
                publisher.join(2)
        self.assertFalse(publisher.is_alive())

    def test_blocked_writer_does_not_block_publication_or_another_init(self):
        self.publish()
        slow = self.connect()
        self.initialize(slow)
        entered = threading.Event()
        release = threading.Event()
        original = _RequestHandler._send
        def blocked_send(handler, frame):
            if frame[:1] == b'S' and not entered.is_set():
                entered.set()
                release.wait(2)
            return original(handler, frame)
        with mock.patch.object(_RequestHandler, '_send', blocked_send):
            slow.sendall(b'f\x00')
            try:
                self.assertTrue(entered.wait(1))
                # Publication and a second client's i both complete while the
                # first writer is deliberately blocked at the socket-write boundary.
                self.publish()
                fast = self.connect()
                fast.settimeout(.5)
                self.assertEqual(self.initialize(fast), self.store.snapshot().manifest)
            finally:
                release.set()
            receive_absolute_segment(slow)

    def test_diagnostics_separate_publication_wait_and_do_not_block_on_full_queue(self):
        server = self.server._server
        server.config = dataclasses.replace(server.config, tcp_diagnostics=True)
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b'f\x00')
        response = receive_exact(connection, 1)
        while response == b'h':
            response = receive_exact(connection, 1)
        self.assertEqual(response, b'n')
        records = []
        while not server._diagnostics.empty():
            records.append(server._diagnostics.get_nowait())
        init = next(record for record in records if record.get('command') == 'i')
        final = next(record for record in records if record.get('response') == 'n')
        self.assertEqual(init['publication_wait_ms'], 0)
        self.assertTrue(init['manifest_cache_hit'])
        self.assertGreater(final['publication_wait_ms'], 150)
        self.assertLess(final['processing_ms'], 100)
        self.assertIn('write_complete_utc', final)
        for _ in range(server._diagnostics.maxsize):
            server._diagnostics.put_nowait({})
        connection.sendall(b'i')
        receive_payload(connection, b'm')
        self.assertGreater(server.diagnostics_dropped, 0)

    def test_client_heartbeat_probe_is_echoed(self):
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"h")
        self.assertEqual(receive_exact(connection, 1), b"h")

    def test_no_change_waits_and_publication_wakes_waiters(self):
        self.publish()
        connection = self.connect()
        previous = self.initialize(connection)
        started = time.monotonic()
        connection.sendall(b"m")
        heartbeats = 0
        response = receive_exact(connection, 1)
        while response == b"h":
            heartbeats += 1
            response = receive_exact(connection, 1)
        self.assertEqual(response, b"n")
        self.assertGreater(heartbeats, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        connection.sendall(b"m")
        self.publish()
        current = receive_payload(connection, b"m")
        self.assertNotEqual(current, previous)
        self.assertEqual(current, self.store.protobuf_path.read_bytes())

    def test_manifest_change_replaces_heartbeat_with_manifest(self):
        self.publish()
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"m")
        self.assertEqual(receive_exact(connection, 1), b"h")
        self.publish()
        self.assertEqual(receive_payload(connection, b"m"), self.store.snapshot().manifest)

    def test_index_stays_relative_to_last_sent_manifest_after_rollover(self):
        payload = self.publish()
        connection = self.connect()
        self.initialize(connection)
        self.publish()
        connection.sendall(b"s\x00")
        self.assertEqual(receive_payload(connection, b"s"), payload)
        self.publish()  # Sequence 0 is now deleted by cleanup.
        connection.sendall(b"s\x00")
        self.assertEqual(receive_exact(connection, 1), b"g")
        connection.sendall(b"m")
        self.assertEqual(receive_payload(connection, b"m"), self.store.snapshot().manifest)
        connection.sendall(b"s\x00")
        self.assertEqual(receive_payload(connection, b"s"), payload)

    def test_absolute_fetch_needs_no_manifest_and_carries_crc_and_discontinuity(self):
        connection = self.connect()
        self.initialize(connection)
        payload = self.publish()  # The init manifest did not include this sequence.
        connection.sendall(b"f" + encode_varint(0))
        body, checksum, flags = receive_absolute_segment(connection)
        self.assertEqual(body, payload)
        self.assertEqual(checksum, crc32c(payload))
        self.assertEqual(flags, 1)
        # Retry preserves the same response framing and metadata.
        connection.sendall(b"e")
        self.assertEqual(receive_absolute_segment(connection), (payload, checksum, flags))

    def test_absolute_fetch_waits_briefly_for_next_sequence(self):
        self.publish()
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"f" + encode_varint(1))
        self.assertEqual(receive_exact(connection, 1), b"h")
        publisher = threading.Thread(target=self.publish)
        publisher.start()
        body, checksum, flags = receive_absolute_segment(connection)
        publisher.join(timeout=1)
        self.assertFalse(publisher.is_alive())
        self.assertEqual(body, (self.config.output_dir / "segment-000000000001.ecdc").read_bytes())
        self.assertEqual(checksum, crc32c(body))
        self.assertEqual(flags, 0)

    def test_absolute_fetch_reports_stale_or_not_yet_published(self):
        self.publish()
        connection = self.connect()
        self.initialize(connection)
        self.publish()
        self.publish()  # Sequence 0 is now outside the retained window.
        connection.sendall(b"f" + encode_varint(0))
        self.assertEqual(receive_exact(connection, 1), b"g")
        connection.sendall(b"f" + encode_varint(3))
        started = time.monotonic()
        response = receive_exact(connection, 1)
        heartbeats = 0
        while response == b"h":
            heartbeats += 1
            response = receive_exact(connection, 1)
        self.assertEqual(response, b"n")
        self.assertGreater(heartbeats, 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertLess(time.monotonic() - started, 1)

    def test_absolute_wait_without_heartbeats_returns_only_final_status(self):
        self.server._server.config = dataclasses.replace(
            self.config, tcp_heartbeat_enabled=False
        )
        connection = self.connect()
        self.initialize(connection)
        started = time.monotonic()
        connection.sendall(b"f\x00")
        self.assertEqual(receive_exact(connection, 1), b"n")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_shutdown_wakes_absolute_fetch_waiter(self):
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"f\x00")
        self.assertEqual(receive_exact(connection, 1), b"h")
        self.server.close()
        self.assertEqual(connection.recv(1), b"")

    def test_retry_uses_cached_bytes_even_after_cleanup_then_caps_retries(self):
        payload = self.publish()
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"s\x00")
        self.assertEqual(receive_payload(connection, b"s"), payload)
        self.publish()
        self.publish()
        self.assertFalse((self.config.output_dir / "segment-000000000000.ecdc").exists())
        for _ in range(2):
            connection.sendall(b"e")
            self.assertEqual(receive_payload(connection, b"s"), payload)
        connection.sendall(b"e")
        self.assertEqual(receive_exact(connection, 1), b"r")
        self.assertEqual(connection.recv(1), b"")

    def test_invalid_commands_indexes_and_varints(self):
        connection = self.connect()
        connection.sendall(b"s")  # Invalid before init; no trailing unread bytes.
        self.assertEqual(receive_exact(connection, 1), b"p")
        self.assertEqual(connection.recv(1), b"")
        for request in (b"x", b"e", b"s\x80\x00", b"s" + b"\xff" * 10):
            connection = self.connect()
            self.initialize(connection)
            connection.sendall(request)
            self.assertEqual(receive_exact(connection, 1), b"p")
            self.assertEqual(connection.recv(1), b"")
            connection.close()
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"s" + encode_varint((1 << 64) - 1))
        self.assertEqual(receive_exact(connection, 1), b"g")

    def test_max_clients_rejects_extra_connections(self):
        self.initialize(self.connect())
        self.initialize(self.connect())
        connection = self.connect()
        self.assertEqual(receive_exact(connection, 1), b"b")
        self.assertEqual(connection.recv(1), b"")

    def test_shutdown_wakes_manifest_requests_and_closes_sockets(self):
        connection = self.connect()
        self.initialize(connection)
        connection.sendall(b"m")
        thread = threading.Thread(target=self.server.close)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        # Either close immediately or finish an already-expiring no-change reply.
        self.assertIn(connection.recv(1), (b"", b"n"))


if __name__ == "__main__":
    unittest.main()
