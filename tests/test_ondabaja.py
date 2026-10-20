from pathlib import Path
import socket
import tempfile
import unittest

from encodec_live_streamer.config import Config
from encodec_live_streamer.manifest import ManifestStore
from encodec_live_streamer.mux import OndaBajaTcpServer, Radio
from encodec_live_streamer.ondabaja import load_ondabaja_config
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


class StationFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'stations').mkdir()
        (self.root / 'server.toml').write_text('[server]\nstations_dir="stations"\n')

    def station(self, filename, station_id, title):
        path = self.root / 'stations' / filename
        path.write_text(f'[station]\nid="{station_id}"\ntitle="{title}"\n'
                        f'[stream]\ninput="unused"\noutput_dir="output-{filename}"\n')
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
            '[station]\nid="a"\ntitle="A"\n[stream]\ninput="unused"\n')
        (self.root / 'stations/b.toml').write_text(
            '[station]\nid="b"\ntitle="B"\n[stream]\ninput="unused"\n'
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

    def test_explicit_encodec_rejected(self):
        path = self.station('a.toml', 'a', 'First')
        path.write_text(path.read_text().replace('[stream]', '[stream]\ncodec="EnCodec"'))
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
