from pathlib import Path
import asyncio
from dataclasses import replace
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from encodec_live_streamer.config import Config
from encodec_live_streamer.ecdc import make_test_ecdc
from encodec_live_streamer.manifest import ManifestStore, crc32c
from encodec_live_streamer.mux import OndaBajaTcpServer, Radio
from encodec_live_streamer import mux
from encodec_live_streamer.ondabaja import OndaBajaService, load_ondabaja_config
from encodec_live_streamer.tcp import encode_varint, read_varint


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def payload(sock, kind):
    assert exact(sock, 1) == kind
    return exact(sock, read_varint(sock))


def startup_response(sock):
    assert exact(sock, 1) == b'B'
    selection_id = read_varint(sock)
    manifest = exact(sock, read_varint(sock))
    marker = exact(sock, 1)
    if marker == b'n':
        return selection_id, manifest, None, None, None, None
    assert marker == b'S'
    sequence = read_varint(sock)
    segment_length = read_varint(sock)
    checksum = struct.unpack('>I', exact(sock, 4))[0]
    flags = exact(sock, 1)[0]
    segment = exact(sock, segment_length)
    return selection_id, manifest, sequence, checksum, flags, segment


def burst_reply(sock):
    kind = exact(sock, 1)
    assert kind in (b'D', b'E')
    burst_id, sequence = read_varint(sock), read_varint(sock)
    if kind == b'E':
        return kind, burst_id, sequence, exact(sock, 1)
    size = read_varint(sock)
    checksum = struct.unpack('>I', exact(sock, 4))[0]
    flags = exact(sock, 1)[0]
    return kind, burst_id, sequence, checksum, flags, exact(sock, size)


class StationFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'stations').mkdir()
        (self.root / 'server.toml').write_text('[server]\nstations_dir="stations"\n')

    def station(self, filename, station_id, title):
        path = self.root / 'stations' / filename
        path.write_text(f'id="{station_id}"\ntitle="{title}"\n'
                        f'input="unused"\noutput_dir="output-{filename}"\n')
        return path

    def load(self):
        return load_ondabaja_config(self.root / 'server.toml')

    def test_directory_catalog_order_ecng_and_revision(self):
        self.station('b.toml', 'b', 'Second')
        first = self.station('a.toml', 'a', 'First')
        config, streams = self.load()
        self.assertEqual([(s.id, s.title) for s in config.stations], [('a', 'First'), ('b', 'Second')])
        self.assertTrue(all(s.codec == 'ECNG' and not s.tcp_enabled for s in streams))
        self.assertEqual(streams[0].output_dir, (self.root / 'stations/output-a.toml').resolve())
        self.assertEqual(self.load()[0].revision, config.revision)
        first.write_text(first.read_text().replace('First', 'Renamed'))
        self.assertEqual(self.load()[0].revision, config.revision + 1)

    def test_global_defaults_paths_and_station_overrides(self):
        (self.root / 'server.toml').write_text(
            '[server]\nstations_dir="stations"\noutput_root="published"\n'
            '[stream_defaults]\nmodel_dir="models"\n'
            'native_encoder="bin/encodec-live-native"\nbandwidth_kbps=1.5\n')
        (self.root / 'stations/a.toml').write_text(
            'id="a"\ntitle="A"\ninput="unused"\n')
        (self.root / 'stations/b.toml').write_text(
            'id="b"\ntitle="B"\ninput="unused"\n'
            'model_dir="private-models"\nbandwidth_kbps=3\n')
        _, streams = self.load()
        self.assertEqual(streams[0].model_dir, (self.root / 'models').resolve())
        self.assertEqual(streams[0].output_dir, (self.root / 'published/a').resolve())
        self.assertEqual(streams[0].native_encoder,
                         str((self.root / 'bin/encodec-live-native').resolve()))
        self.assertEqual(streams[0].bandwidth_kbps, 1.5)
        self.assertEqual(streams[1].model_dir, (self.root / 'stations/private-models').resolve())
        self.assertEqual(streams[1].bandwidth_kbps, 3)

    def test_duplicate_ids_rejected(self):
        self.station('a.toml', 'same', 'First')
        self.station('b.toml', 'same', 'Second')
        with self.assertRaisesRegex(ValueError, 'duplicate station id'):
            self.load()

    def test_flat_station_infers_id_and_enforces_global_tcp_and_greedy_encoder(self):
        path = self.root / 'stations' / 'demo-station.toml'
        text = ('title="Demo Radio"\ninput="unused"\n'
                'input_options=["-rw_timeout", "15000000"]\n'
                'output_options=["-af", "volume=0.9"]\n'
                'samplerate=24\nbandwidth_kbps=1.5\nsegment_duration=4.96\n'
                'window_segments=12\nstale_grace_segments=6\nthreads=1\nbeam_size=8\n')
        path.write_text(text)
        config, streams = self.load()
        self.assertEqual(config.stations[0].id, 'demo-station')
        self.assertEqual(config.stations[0].title, 'Demo Radio')
        self.assertEqual(streams[0].beam_size, 1)
        self.assertEqual(streams[0].input_options, ('-rw_timeout', '15000000'))
        self.assertEqual(streams[0].output_options, ('-af', 'volume=0.9'))
        self.assertEqual(streams[0].output_dir,
                         (self.root / 'public/demo-station').resolve())
        path.write_text(text + 'tcp_port=9001\n')
        with self.assertRaisesRegex(ValueError, 'TCP settings are global'):
            self.load()
        path.write_text(text + 'refinement_passes=2\n')
        with self.assertRaisesRegex(ValueError, 'refinement_passes is no longer supported'):
            self.load()

    def test_http_directory_and_tcp_catalog_match(self):
        (self.root / 'server.toml').write_text(
            '[server]\nstations_dir="stations"\noutput_root="published"\n'
            'public_base_url="https://radio.example.com/radios/"\n')
        (self.root / 'stations/a.toml').write_text(
            'id="radio a"\ntitle="A"\ninput="unused"\n')
        config, streams = self.load()
        self.assertEqual(streams[0].output_dir, (self.root / 'published/radio a').resolve())
        self.assertEqual(config.stations[0].manifest_url,
                         'https://radio.example.com/radios/radio%20a/stream.json')
        self.assertEqual(config.revision_file.parent, config.output_root)
        service = OndaBajaService(replace(config, port=0), streams)
        self.addCleanup(service.tcp.close)
        service.publish_catalog()
        self.assertTrue(streams[0].output_dir.is_dir())
        self.assertEqual((config.output_root / 'catalog.pb').read_bytes(), service.tcp.catalog)
        document = json.loads((config.output_root / 'catalog.json').read_text())
        self.assertEqual(document['revision'], config.revision)
        self.assertEqual(document['updated_at_unix_ms'], service.tcp.catalog_updated_at_unix_ms)
        self.assertEqual(document['stations'][0]['manifest_url'], config.stations[0].manifest_url)
        service.tcp.start()
        client = socket.create_connection(service.tcp.address, timeout=2)
        self.addCleanup(client.close)
        client.sendall(b'C\x00')
        self.assertEqual(payload(client, b'c'), (config.output_root / 'catalog.pb').read_bytes())

    def test_http_base_validation_and_station_override(self):
        path = self.station('a.toml', 'a', 'A')
        path.write_text(path.read_text().replace('title="A"',
            'title="A"\nmanifest_url="https://fallback.example.com/a/stream.json"'))
        (self.root / 'server.toml').write_text(
            '[server]\nstations_dir="stations"\npublic_base_url="https://radio.example.com/radios"\n')
        config, _ = self.load()
        self.assertEqual(config.stations[0].manifest_url,
                         'https://fallback.example.com/a/stream.json')
        original_revision = config.revision
        path.write_text(path.read_text().replace(
            'manifest_url="https://fallback.example.com/a/stream.json"\n', ''))
        self.assertGreater(self.load()[0].revision, original_revision)
        config_path = self.root / 'server.toml'
        config_path.write_text(config_path.read_text().replace('/radios"', '/radios?token=private"'))
        with self.assertRaisesRegex(ValueError, 'public_base_url'):
            self.load()

    def test_explicit_encodec_rejected(self):
        path = self.station('a.toml', 'a', 'First')
        path.write_text(path.read_text().replace('input="unused"',
            'input="unused"\ncodec="EnCodec"'))
        with self.assertRaisesRegex(ValueError, 'require codec=ECNG'):
            self.load()

    def test_clients_share_port_with_independent_selection_and_interrupt(self):
        stores = [ManifestStore(Config(input='unused', codec='ECNG',
                    output_dir=self.root / name, fsync=False,
                    segment_duration=duration)) for name, duration in [('a', 1), ('b', 2)]]
        radios = tuple(Radio(name, name.upper(), '', store)
                       for name, store in zip(('a', 'b'), stores))
        server = OndaBajaTcpServer('127.0.0.1', 0, radios, 7, wait_seconds=2)
        server.start()
        self.addCleanup(server.close)
        clients = [socket.create_connection(server.address, timeout=1) for _ in range(2)]
        for client in clients:
            self.addCleanup(client.close)
            client.sendall(b'C' + encode_varint(0))
            self.assertIn(b'A', payload(client, b'c'))
        for client, name, store in zip(clients, (b'a', b'b'), stores):
            client.sendall(b'R\x01' + name)
            self.assertEqual(payload(client, b'm'), store.snapshot().manifest)
        clients[0].sendall(b'f' + encode_varint(999) + b'R\x01b')
        self.assertEqual(payload(clients[0], b'm'), stores[1].snapshot().manifest)
        clients[1].sendall(b'M')
        self.assertEqual(payload(clients[1], b'm'), stores[1].snapshot().manifest)
        clients[0].sendall(b'C' + encode_varint(7))
        self.assertEqual(exact(clients[0], 1), b'n')

    def test_station_switch_preempts_pending_fetch_during_publication(self):
        stores = [ManifestStore(Config(input='unused', codec='ECNG',
                    output_dir=self.root / name, fsync=False)) for name in ('slow', 'live')]
        radios = tuple(Radio(name, name.upper(), '', store)
                       for name, store in zip(('slow', 'live'), stores))
        server = OndaBajaTcpServer('127.0.0.1', 0, radios, 1, wait_seconds=1.0)
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=1)
        self.addCleanup(client.close)
        client.settimeout(1)
        client.sendall(b'R\x04slow')
        self.assertEqual(payload(client, b'm'), stores[0].snapshot().manifest)

        # Leave the old station's request waiting, then make an old-station
        # publication race the new station selection.
        client.sendall(b'f\x00')
        time.sleep(0.05)

        def publish_old_station():
            time.sleep(0.005)
            store = stores[0]
            sample_count = store.config.samples_per_segment
            encoded = make_test_ecdc(sample_count, store.config.codebooks,
                                     store.config.model)
            store.publish_segment(
                encoded, sample_count=sample_count, pts_samples=0,
                program_date_time='2026-01-01T00:00:00Z', epoch='old-station',
                discontinuity=True,
            )

        publisher = threading.Thread(target=publish_old_station)
        publisher.start()
        switch_started = time.monotonic()
        client.sendall(b'R\x04live')
        self.assertEqual(payload(client, b'm'), stores[1].snapshot().manifest)
        self.assertLess(time.monotonic() - switch_started, 0.5)
        publisher.join(timeout=1)
        self.assertFalse(publisher.is_alive())

        # No stale S/n response from the interrupted fetch may be queued ahead
        # of later commands on this still-open connection.
        client.sendall(b'M')
        self.assertEqual(payload(client, b'm'), stores[1].snapshot().manifest)
        client.sendall(b'H')
        self.assertEqual(exact(client, 1), b'h')

    def test_bundled_startup_selects_lookback_and_frames_crc(self):
        store = ManifestStore(Config(input='unused', codec='ECNG',
                    output_dir=self.root / 'startup', fsync=False,
                    segment_duration=1, window_segments=12))
        payloads = []
        sample_count = store.config.samples_per_segment
        for sequence in range(12):
            encoded = make_test_ecdc(sample_count, store.config.codebooks,
                                     store.config.model)
            payloads.append(encoded)
            store.publish_segment(
                encoded, sample_count=sample_count,
                pts_samples=sequence * sample_count,
                program_date_time='2026-01-01T00:00:00Z', epoch='startup',
                discontinuity=sequence == 0,
            )
        radio = Radio('startup', 'Startup', '', store)
        server = OndaBajaTcpServer('127.0.0.1', 0, (radio,), 1)
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=1)
        self.addCleanup(client.close)
        client.settimeout(1)

        # A 1 s target chooses the newest segment; the two-entry retention
        # margin is a minimum index, matching LiveSequenceTracker.
        client.sendall(b'B' + encode_varint(101) + encode_varint(7) +
                       b'startup' + encode_varint(1000))
        selection_id, manifest, sequence, checksum, flags, segment = startup_response(client)
        self.assertEqual(selection_id, 101)
        self.assertEqual(manifest, store.snapshot().manifest)
        self.assertEqual(sequence, 11)
        self.assertEqual(segment, payloads[11])
        self.assertEqual(checksum, crc32c(segment))
        self.assertEqual(flags, 0)

        # Accumulate actual segment durations backward to the 5 s target.
        client.sendall(b'B' + encode_varint(102) + encode_varint(7) +
                       b'startup' + encode_varint(5000))
        selection_id, _, sequence, checksum, flags, segment = startup_response(client)
        self.assertEqual(selection_id, 102)
        self.assertEqual(sequence, 7)
        self.assertEqual(segment, payloads[7])
        self.assertEqual(checksum, crc32c(segment))
        self.assertEqual(flags, 0)

    def test_bundled_startup_empty_and_unknown_station_responses(self):
        store = ManifestStore(Config(input='unused', codec='ECNG',
                                     output_dir=self.root / 'empty', fsync=False))
        server = OndaBajaTcpServer('127.0.0.1', 0,
                                   (Radio('empty', 'Empty', '', store),), 1)
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=1)
        self.addCleanup(client.close)
        client.settimeout(1)

        client.sendall(b'B' + encode_varint(1) + encode_varint(5) +
                       b'empty' + encode_varint(30000))
        selection_id, manifest, sequence, checksum, flags, segment = startup_response(client)
        self.assertEqual(selection_id, 1)
        self.assertEqual(manifest, store.snapshot().manifest)
        self.assertIsNone(sequence)
        self.assertIsNone(checksum)
        self.assertIsNone(flags)
        self.assertIsNone(segment)

        client.sendall(b'B' + encode_varint(2) + encode_varint(7) +
                       b'missing' + encode_varint(30000))
        self.assertEqual(exact(client, 1), b'x')
        client.sendall(b'H')
        self.assertEqual(exact(client, 1), b'h')

    def test_bundled_startup_preempts_pending_fetch_during_publication(self):
        stores = [ManifestStore(Config(input='unused', codec='ECNG',
                    output_dir=self.root / name, fsync=False,
                    segment_duration=1)) for name in ('pending', 'new')]
        sample_count = stores[1].config.samples_per_segment
        encoded = make_test_ecdc(sample_count, stores[1].config.codebooks,
                                 stores[1].config.model)
        stores[1].publish_segment(
            encoded, sample_count=sample_count, pts_samples=0,
            program_date_time='2026-01-01T00:00:00Z', epoch='new-station',
            discontinuity=True,
        )
        radios = tuple(Radio(name, name.upper(), '', store)
                       for name, store in zip(('pending', 'new'), stores))
        server = OndaBajaTcpServer('127.0.0.1', 0, radios, 1, wait_seconds=2.0)
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=1)
        self.addCleanup(client.close)
        client.settimeout(1)
        client.sendall(b'R' + encode_varint(7) + b'pending')
        self.assertEqual(payload(client, b'm'), stores[0].snapshot().manifest)
        client.sendall(b'f\x00')
        time.sleep(0.05)

        def publish_pending_station():
            time.sleep(0.005)
            store = stores[0]
            samples = store.config.samples_per_segment
            old_encoded = make_test_ecdc(samples, store.config.codebooks,
                                         store.config.model)
            store.publish_segment(
                old_encoded, sample_count=samples, pts_samples=0,
                program_date_time='2026-01-01T00:00:00Z', epoch='old-station',
                discontinuity=True,
            )

        publisher = threading.Thread(target=publish_pending_station)
        publisher.start()
        started = time.monotonic()
        client.sendall(b'B' + encode_varint(99) + encode_varint(3) +
                       b'new' + encode_varint(30000))
        selection_id, manifest, sequence, checksum, flags, segment = startup_response(client)
        self.assertEqual(selection_id, 99)
        self.assertEqual(manifest, stores[1].snapshot().manifest)
        self.assertEqual(sequence, 0)
        self.assertEqual(segment, encoded)
        self.assertEqual(checksum, crc32c(encoded))
        self.assertEqual(flags, 1)
        self.assertLess(time.monotonic() - started, 0.5)
        publisher.join(timeout=1)
        self.assertFalse(publisher.is_alive())
        client.sendall(b'H')
        self.assertEqual(exact(client, 1), b'h')

    def test_bundled_startup_retry_is_processed_during_preparation(self):
        store = ManifestStore(Config(input='unused', codec='ECNG',
                    output_dir=self.root / 'retry', fsync=False))
        sample_count = store.config.samples_per_segment
        encoded = make_test_ecdc(sample_count, store.config.codebooks, store.config.model)
        store.publish_segment(
            encoded, sample_count=sample_count, pts_samples=0,
            program_date_time='2026-01-01T00:00:00Z', epoch='retry',
            discontinuity=True,
        )
        server = OndaBajaTcpServer('127.0.0.1', 0,
                                   (Radio('retry', 'Retry', '', store),), 1)
        original_startup_response = server._startup_response

        async def delayed_startup_response(radio, selection_id, lookback_ms):
            await asyncio.sleep(0.1)
            return await original_startup_response(radio, selection_id, lookback_ms)

        server._startup_response = delayed_startup_response
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=1)
        self.addCleanup(client.close)
        client.settimeout(1)
        request = b'B' + encode_varint(55) + encode_varint(5) + b'retry' + encode_varint(30000)
        client.sendall(request)
        time.sleep(0.02)
        client.sendall(request)
        selection_id, manifest, sequence, checksum, flags, segment = startup_response(client)
        self.assertEqual(selection_id, 55)
        self.assertEqual(manifest, store.snapshot().manifest)
        self.assertEqual(sequence, 0)
        self.assertEqual(segment, encoded)
        self.assertEqual(checksum, crc32c(encoded))
        self.assertEqual(flags, 1)
        client.sendall(b'H')
        self.assertEqual(exact(client, 1), b'h')

    def test_async_listener_handles_many_idle_connections_on_one_thread(self):
        store = ManifestStore(Config(input='unused', codec='ECNG',
                                     output_dir=self.root / 'many', fsync=False))
        server = OndaBajaTcpServer('127.0.0.1', 0,
                                   (Radio('a', 'A', '', store),), 1,
                                   max_clients=256)
        server.start()
        self.addCleanup(server.close)
        clients = [socket.create_connection(server.address, timeout=2) for _ in range(256)]
        for client in clients:
            self.addCleanup(client.close)
            client.settimeout(2)
            client.sendall(b'H')
        self.assertEqual(len([thread for thread in threading.enumerate()
                              if thread.name == 'ondabaja-mux-asyncio']), 1)
        for client in clients:
            self.assertEqual(exact(client, 1), b'h')


class BurstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def store(self, name, count=0, **options):
        store = ManifestStore(Config(input='unused', codec='ECNG', fsync=False,
            output_dir=self.root / name, segment_duration=1, **options),
            segment_cache_enabled=False)
        for _ in range(count):
            self.publish(store)
        return store

    def publish(self, store):
        samples = store.config.samples_per_segment
        encoded = make_test_ecdc(samples, store.config.codebooks, store.config.model)
        sequence = store.snapshot().next_sequence
        store.publish_segment(encoded, sample_count=samples,
            pts_samples=sequence * samples, program_date_time='2026-01-01T00:00:00Z',
            epoch='burst', discontinuity=sequence == 0)
        return encoded

    def connect(self, *stores):
        server = OndaBajaTcpServer('127.0.0.1', 0,
            tuple(Radio(s.output_dir.name, s.output_dir.name, '', s) for s in stores),
            1, wait_seconds=5)
        server.start()
        self.addCleanup(server.close)
        client = socket.create_connection(server.address, timeout=2)
        self.addCleanup(client.close)
        client.settimeout(2)
        station_id = stores[0].output_dir.name.encode()
        client.sendall(b'R' + encode_varint(len(station_id)) + station_id)
        self.assertEqual(payload(client, b'm'), stores[0].snapshot().manifest)
        return client, server

    def request(self, client, burst_id, sequence, count):
        client.sendall(b'F' + encode_varint(burst_id) + encode_varint(sequence) +
                       encode_varint(count))

    def test_six_segments_are_tagged_and_individually_framed(self):
        store = self.store('radio', 8)
        client, _ = self.connect(store)
        self.request(client, 130, 0, 6)
        for sequence in range(6):
            kind, burst_id, actual, checksum, flags, segment = burst_reply(client)
            self.assertEqual((kind, burst_id, actual, flags),
                             (b'D', 130, sequence, int(sequence == 0)))
            self.assertEqual(checksum, crc32c(segment))
            item = store.snapshot().retained_segments[sequence]
            self.assertEqual(segment, (store.output_dir / item.uri).read_bytes())
        self.assertEqual(burst_reply(client), (b'E', 130, 6, b'n'))
        # Burst D frames are never the normal f/S retry target.
        client.sendall(b'eH')
        self.assertEqual(exact(client, 2), b'rh')
        self.request(client, 131, 6, 6)
        self.assertEqual(burst_reply(client)[:3], (b'D', 131, 6))
        self.assertEqual(burst_reply(client)[:3], (b'D', 131, 7))
        self.assertEqual(burst_reply(client), (b'E', 131, 8, b'n'))

    def test_empty_future_stale_and_missing_file_end_without_polling(self):
        empty = self.store('empty')
        client, _ = self.connect(empty)
        started = time.monotonic()
        self.request(client, 1, 0, 6)
        self.assertEqual(burst_reply(client), (b'E', 1, 0, b'n'))
        self.assertLess(time.monotonic() - started, 0.5)
        store = self.store('retained', 4, window_segments=2, stale_grace_segments=0)
        client, _ = self.connect(store)
        for burst_id, sequence, status in ((2, 0, b'g'), (3, 100, b'n')):
            self.request(client, burst_id, sequence, 6)
            self.assertEqual(burst_reply(client), (b'E', burst_id, sequence, status))
        (store.output_dir / store.snapshot().retained_segments[3].uri).unlink()
        self.request(client, 4, 2, 6)
        self.assertEqual(burst_reply(client)[:3], (b'D', 4, 2))
        self.assertEqual(burst_reply(client), (b'E', 4, 3, b'g'))

    def test_snapshot_excludes_publications_during_burst(self):
        store = self.store('snapshot', 1)
        original = mux._segment_frame

        async def publish_during_read(current_store, item, snapshot=None):
            frame = await original(current_store, item, snapshot)
            self.publish(current_store)
            return frame

        with patch.object(mux, '_segment_frame', publish_during_read):
            client, _ = self.connect(store)
            self.request(client, 1, 0, 6)
            self.assertEqual(burst_reply(client)[:3], (b'D', 1, 0))
            self.assertEqual(burst_reply(client), (b'E', 1, 1, b'n'))
        self.request(client, 2, 1, 1)
        self.assertEqual(burst_reply(client)[:3], (b'D', 2, 1))
        self.assertEqual(burst_reply(client), (b'E', 2, 2, b'n'))

    def test_first_segment_streams_before_slow_disk_read_and_b_supersedes(self):
        old, new = self.store('old', 2), self.store('new', 1)
        entered, release = threading.Event(), threading.Event()
        item = old.snapshot().retained_segments[1]
        blocked_path = old.output_dir / item.uri
        original_read = Path.read_bytes

        def slow_disk(path):
            if path == blocked_path:
                entered.set()
                release.wait(3)
            return original_read(path)

        with patch.object(Path, 'read_bytes', slow_disk):
            client, _ = self.connect(old, new)
            self.request(client, 7, 0, 6)
            self.assertEqual(burst_reply(client)[:3], (b'D', 7, 0))
            try:
                self.assertTrue(entered.wait(1))
                client.sendall(b'H')
                self.assertEqual(exact(client, 1), b'h')
                started = time.monotonic()
                client.sendall(b'B' + encode_varint(99) + b'\x03new' + encode_varint(30000))
                response = startup_response(client)
                self.assertEqual(response[:3], (99, new.snapshot().manifest, 0))
                self.assertLess(time.monotonic() - started, 0.5)
                client.sendall(b'H')
                self.assertEqual(exact(client, 1), b'h')
            finally:
                release.set()
        # The abandoned disk read must never send old D/E afterward.
        client.sendall(b'M')
        self.assertEqual(payload(client, b'm'), new.snapshot().manifest)

    def test_r_and_q_interrupt_segment_preparation(self):
        original = mux._segment_frame
        for command in (b'R\x03new', b'Q'):
            with self.subTest(command=command):
                entered = threading.Event()
                old = self.store('old-' + str(command[0]), 1)
                new = self.store('new', 1)

                async def blocked(current_store, item, snapshot=None):
                    if current_store is old:
                        entered.set()
                        await asyncio.Event().wait()
                    return await original(current_store, item, snapshot)

                with patch.object(mux, '_segment_frame', blocked):
                    client, _ = self.connect(old, new)
                    self.request(client, 1, 0, 1)
                    self.assertTrue(entered.wait(1))
                    client.sendall(command)
                    if command == b'Q':
                        self.assertEqual(client.recv(1), b'')
                    else:
                        self.assertEqual(payload(client, b'm'), new.snapshot().manifest)
                        client.sendall(b'H')
                        self.assertEqual(exact(client, 1), b'h')

    def test_invalid_burst_fields_close_connection(self):
        store = self.store('validation')
        for fields in ((0, 0, 1), (1, 0, 0), (1, 0, 7)):
            with self.subTest(fields=fields):
                client, _ = self.connect(store)
                self.request(client, *fields)
                self.assertEqual(client.recv(1), b'')
        client, _ = self.connect(store)
        client.sendall(b'F\x81\x00\x00\x01')  # Noncanonical burst ID.
        self.assertEqual(client.recv(1), b'')

    def test_new_b_selection_supersedes_older_preparation(self):
        old, new = self.store('old', 1), self.store('new', 1)
        client, server = self.connect(old, new)
        entered = threading.Event()
        original = server._startup_response

        async def blocked(radio, selection_id, lookback_ms):
            if selection_id == 1:
                entered.set()
                await asyncio.Event().wait()
            return await original(radio, selection_id, lookback_ms)

        server._startup_response = blocked
        client.sendall(b'B\x01\x03old' + encode_varint(30000))
        self.assertTrue(entered.wait(1))
        client.sendall(b'B\x02\x03new' + encode_varint(30000))
        self.assertEqual(startup_response(client)[:3], (2, new.snapshot().manifest, 0))
        client.sendall(b'H')
        self.assertEqual(exact(client, 1), b'h')

    def test_selection_interrupts_socket_drain_after_one_complete_data_frame(self):
        old, new = self.store('draining', 6), self.store('selected', 1)
        server = OndaBajaTcpServer('127.0.0.1', 0,
            (Radio('draining', 'Old', '', old), Radio('selected', 'New', '', new)), 1)

        async def exercise():
            reader = asyncio.StreamReader()
            draining = asyncio.Event()
            frames = []

            class SlowWriter:
                def write(self, frame):
                    frames.append(frame)

                async def drain(self):
                    if frames[-1].startswith(b'D'):
                        draining.set()
                        await asyncio.Event().wait()

            burst = asyncio.create_task(server._burst(
                reader, SlowWriter(), server.radios[0], 77, 0, 6))
            try:
                await asyncio.wait_for(draining.wait(), 1)
                reader.feed_data(b'B\x7b\x08selected' + encode_varint(30000))
                selected = await asyncio.wait_for(burst, 1)
                self.assertEqual(selected.id, 'selected')
                self.assertEqual(len(frames), 2)
                self.assertTrue(frames[0].startswith(b'D' + encode_varint(77) + b'\x00'))
                self.assertTrue(frames[1].startswith(b'B' + encode_varint(123)))
            finally:
                if not burst.done():
                    burst.cancel()
                await asyncio.gather(burst, return_exceptions=True)

        asyncio.run(exercise())
