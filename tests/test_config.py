from pathlib import Path
import tempfile
import unittest

from encodec_live_streamer.config import Config
from encodec_live_streamer.encoder import native_command
from encodec_live_streamer.ffmpeg import command


class ConfigTests(unittest.TestCase):
    def test_default_is_24khz_mono_and_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(input="x", output_dir=Path(directory)).validate()
            self.assertAlmostEqual(config.segment_duration, 1.9866666666666666)
            self.assertEqual(config.sample_rate, 24_000)
            self.assertEqual(config.channels, 1)
            self.assertEqual(config.codebooks, 4)
            self.assertEqual(config.window_segments, 16)
            self.assertEqual(config.stale_grace_segments, 6)
            self.assertTrue(config.segment_is_aligned)

    def test_24khz_3kbps_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x",
                output_dir=Path(directory),
                samplerate=24,
                bandwidth_kbps=3,
            ).validate()
            self.assertEqual(config.model, "encodec_24khz")
            self.assertEqual(config.sample_rate, 24_000)
            self.assertEqual(config.channels, 1)
            self.assertEqual(config.codebooks, 4)
            self.assertEqual(config.bytes_per_sample_frame, 4)
            self.assertTrue(config.segment_is_aligned)

    def test_two_second_segments_are_not_48khz_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x",
                output_dir=Path(directory),
                samplerate=48,
                bandwidth_kbps=12,
                segment_duration=2.0,
            ).validate()
            self.assertFalse(config.segment_is_aligned)

    def test_ffmpeg_command_orders_input_options_before_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="hw:0",
                input_format="alsa",
                input_options=("-thread_queue_size", "1024"),
                output_dir=Path(directory),
                samplerate=48,
                bandwidth_kbps=12,
            ).validate()
            argv = command(config)
            self.assertLess(argv.index("-thread_queue_size"), argv.index("-i"))
            self.assertEqual(argv[argv.index("-f") + 1], "alsa")
            self.assertEqual(argv[argv.index("-ac") + 1], "2")
            self.assertEqual(argv[argv.index("-ar") + 1], "48000")
            self.assertEqual(argv[-2:], ["f32le", "pipe:1"])

    def test_24khz_ffmpeg_command_is_mono(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x", output_dir=Path(directory), samplerate=24, bandwidth_kbps=3
            ).validate()
            argv = command(config)
            self.assertEqual(argv[argv.index("-ac") + 1], "1")
            self.assertEqual(argv[argv.index("-ar") + 1], "24000")

    def test_rejects_invalid_bandwidth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "bandwidth"):
                Config(input="x", output_dir=Path(directory), bandwidth_kbps=5).validate()

    def test_24khz_accepts_1_5_kbps_but_48khz_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Config(
                input="x", output_dir=Path(directory), samplerate=24, bandwidth_kbps=1.5
            ).validate()
            with self.assertRaisesRegex(ValueError, "bandwidth"):
                Config(
                    input="x", output_dir=Path(directory), samplerate=48, bandwidth_kbps=1.5
                ).validate()

    def test_rejects_invalid_samplerate_threads_and_beam_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "samplerate"):
                Config(input="x", output_dir=Path(directory), samplerate=32).validate()
            with self.assertRaisesRegex(ValueError, "threads"):
                Config(input="x", output_dir=Path(directory), threads=0).validate()
            for value in (0, 65, True):
                with self.assertRaisesRegex(ValueError, "beam_size"):
                    Config(input="x", output_dir=Path(directory), beam_size=value).validate()

    def test_native_command_includes_beam_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x", output_dir=Path(directory), beam_size=4
            ).validate()
            argv = native_command(config)
            self.assertEqual(argv[argv.index("--beam-size") + 1], "4")

    def test_toml_reports_missing_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text("[stream]\nbandwidth_kbps = 12\n")
            with self.assertRaisesRegex(ValueError, "input, output_dir"):
                Config.from_toml(path)

    def test_legacy_title_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.toml"
            path.write_text(
                '[stream]\ninput = "x"\noutput_dir = "public"\n'
                'title = "Bio Bio Santiago"\n'
            )
            with self.assertLogs("encodec_live_streamer.config", level="WARNING"):
                config = Config.from_toml(path)
            self.assertFalse(hasattr(config, "title"))

    def test_tcp_settings_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.toml"
            path.write_text(
                '[stream]\ninput = "x"\noutput_dir = "public"\n'
                'tcp_enabled = true\ntcp_host = "0.0.0.0"\ntcp_port = 9900\n'
            )
            config = Config.from_toml(path)
            self.assertTrue(config.tcp_enabled)
            self.assertEqual(config.tcp_host, "0.0.0.0")
            self.assertEqual(config.tcp_port, 9900)
            self.assertFalse(config.tcp_heartbeat_enabled)
            self.assertEqual(config.tcp_heartbeat_interval, 3.5)

    def test_tcp_limits_and_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for values in (
                {"tcp_enabled": 1}, {"tcp_host": ""}, {"tcp_port": 0},
                {"tcp_port": 65536}, {"tcp_port": True}, {"tcp_max_clients": 0},
                {"tcp_max_retries": -1}, {"tcp_idle_timeout": float("nan")},
                {"tcp_manifest_wait": 0}, {"tcp_manifest_wait": 60},
                {"tcp_heartbeat_enabled": 1}, {"tcp_heartbeat_interval": 0},
            ):
                with self.subTest(values=values), self.assertRaisesRegex(ValueError, "tcp_"):
                    Config(input="x", output_dir=Path(directory), **values).validate()
