import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from encodec_live_streamer.cli import main
from encodec_live_streamer.ecdc import make_test_ecdc


class CliTests(unittest.TestCase):
    def test_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ecdc"
            path.write_bytes(make_test_ecdc(48_000, 4))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["inspect", str(path)]), 0)
            self.assertIn('"audio_length": 48000', output.getvalue())
