import json
import stat
from pathlib import Path
import tempfile
import unittest

from encodec_live_streamer.config import Config
from encodec_live_streamer.ecdc import make_test_ecdc
from encodec_live_streamer.manifest import ManifestStore, crc32c


def publish(store: ManifestStore, sequence_in_epoch: int, discontinuity: bool = False) -> None:
    samples = store.config.samples_per_segment
    store.publish_segment(
        make_test_ecdc(samples, store.config.codebooks, store.config.model),
        sample_count=samples,
        pts_samples=sequence_in_epoch * samples,
        program_date_time="2026-01-01T00:00:00Z",
        epoch="test-epoch",
        discontinuity=discontinuity,
    )


class ManifestTests(unittest.TestCase):
    def test_title_and_internal_checksum_are_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = ManifestStore(
                Config(input="unused", output_dir=path, fsync=False).validate()
            )
            publish(store, 0)
            document = json.loads(store.path.read_text())
            self.assertNotIn("title", document)
            self.assertNotIn("_crc32c", document["segments"][0])
            self.assertEqual(store.snapshot().manifest, store.protobuf_path.read_bytes())

    def test_crc32c_known_vectors(self) -> None:
        self.assertEqual(crc32c(b""), 0)
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)

    def test_rejects_short_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(input="unused", output_dir=Path(directory), fsync=False).validate()
            store = ManifestStore(config)
            with self.assertRaisesRegex(ValueError, "fixed segment duration"):
                store.publish_segment(
                    make_test_ecdc(24_000, config.codebooks, config.model),
                    sample_count=24_000, pts_samples=0,
                    program_date_time="2026-01-01T00:00:00Z",
                    epoch="short", discontinuity=True,
                )

    def test_rolls_manifest_and_cleans_with_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = Config(
                input="unused",
                output_dir=path,
                bandwidth_kbps=12,
                window_segments=3,
                stale_grace_segments=1,
                fsync=False,
            ).validate()
            store = ManifestStore(config)
            for value in range(5):
                publish(store, value, discontinuity=value == 0)

            document = json.loads((path / "stream.json").read_text())
            self.assertEqual(document["media_sequence"], 2)
            self.assertEqual([item["sequence"] for item in document["segments"]], [2, 3, 4])
            self.assertEqual(document["discontinuity_sequence"], 1)
            self.assertFalse((path / "segment-000000000000.ecdc").exists())
            self.assertTrue((path / "segment-000000000001.ecdc").exists())
            self.assertEqual(
                stat.S_IMODE((path / "stream.json").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE((path / "segment-000000000004.ecdc").stat().st_mode),
                0o644,
            )

    def test_restart_resumes_sequence_and_retains_compatible_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(input="unused", output_dir=Path(directory), fsync=False).validate()
            first = ManifestStore(config)
            publish(first, 0, discontinuity=True)
            second = ManifestStore(config)
            self.assertEqual(second.next_sequence, 1)
            self.assertEqual(len(second.segments), 1)
            publish(second, 0, discontinuity=True)
            self.assertEqual([item["sequence"] for item in second.segments], [0, 1])
            self.assertTrue(second.segments[-1]["discontinuity"])

    def test_24khz_manifest_init_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                input="unused",
                output_dir=Path(directory),
                samplerate=24,
                bandwidth_kbps=3,
                fsync=False,
            ).validate()
            store = ManifestStore(config)
            publish(store, 0, discontinuity=True)
            document = json.loads((Path(directory) / "stream.json").read_text())
            self.assertEqual(document["init"]["model"], "encodec_24khz")
            self.assertEqual(document["init"]["sample_rate"], 24_000)
            self.assertEqual(document["init"]["channels"], 1)
            self.assertEqual(document["init"]["codebooks"], 4)
            self.assertEqual(
                document["segments"][0]["duration"], config.segment_duration
            )

    def test_restart_discards_a_window_with_sequence_holes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(input="unused", output_dir=Path(directory), fsync=False).validate()
            store = ManifestStore(config)
            for index in range(3):
                publish(store, index)
            (config.output_dir / "segment-000000000001.ecdc").unlink()
            restarted = ManifestStore(config)
            self.assertEqual(restarted.next_sequence, 3)
            self.assertEqual(restarted.snapshot().segments, ())

    def test_rejects_wrong_segment_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(input="unused", output_dir=Path(directory), fsync=False).validate()
            store = ManifestStore(config)
            with self.assertRaisesRegex(ValueError, "does not match"):
                store.publish_segment(
                    make_test_ecdc(24_000, config.codebooks, config.model),
                    sample_count=48_000,
                    pts_samples=0,
                    program_date_time="2026-01-01T00:00:00Z",
                    epoch="x",
                    discontinuity=True,
                )
