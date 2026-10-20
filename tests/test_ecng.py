from pathlib import Path
import tempfile
import unittest

from encodec_live_streamer.config import Config
from encodec_live_streamer.ecdc import make_test_ecdc, parse_header
from encodec_live_streamer.encoder import native_command
from encodec_live_streamer.manifest import ManifestStore


class EcngTests(unittest.TestCase):
    def test_profile_and_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x",
                output_dir=Path(directory),
                model_dir=Path(directory) / "models",
                codec="ECNG",
                samplerate=24,
                bandwidth_kbps=1.5,
            ).validate()
            self.assertEqual(config.model, "ecng_24khz")
            self.assertEqual(config.sample_rate, 24_000)
            self.assertEqual(config.channels, 1)
            self.assertEqual(config.codebooks, 2)
            self.assertEqual(config.model_path.name, "ecng-24khz-encoder-f32.bin")

    def test_ecng_rejects_unsupported_sample_rate_and_bitrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "samplerate=24"):
                Config(input="x", output_dir=Path(directory), codec="ECNG", samplerate=48).validate()
            with self.assertRaisesRegex(ValueError, "bandwidth_kbps"):
                Config(
                    input="x", output_dir=Path(directory), codec="ECNG", bandwidth_kbps=6
                ).validate()

    def test_native_command_uses_ecng_and_greedy_rvq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x", output_dir=Path(directory), codec="ECNG", beam_size=16
            ).validate()
            command = native_command(config)
            self.assertEqual(command[command.index("--codec") + 1], "ECNG")
            self.assertEqual(command[command.index("--beam-size") + 1], "1")

    def test_ecng_stream_publishes_ecng_named_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="x", output_dir=Path(directory), codec="ECNG", fsync=False
            ).validate()
            store = ManifestStore(config)
            payload = make_test_ecdc(
                config.samples_per_segment, config.codebooks, config.model
            )
            segment = store.publish_segment(
                payload,
                sample_count=config.samples_per_segment,
                pts_samples=0,
                program_date_time="2026-01-01T00:00:00Z",
                epoch="test",
                discontinuity=True,
            )
            self.assertEqual(segment["uri"], "segment-000000000000.ecng")
            self.assertTrue((Path(directory) / segment["uri"]).is_file())
            self.assertEqual(parse_header(payload).model, "ecng_24khz")


if __name__ == "__main__":
    unittest.main()
