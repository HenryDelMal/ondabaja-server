import unittest

from encodec_live_streamer.ecdc import make_test_ecdc, parse_header


class EcdcTests(unittest.TestCase):
    def test_parse_official_header_shape(self) -> None:
        header = parse_header(make_test_ecdc(96_000, 8))
        self.assertEqual(header.model, "encodec_48khz")
        self.assertEqual(header.audio_length, 96_000)
        self.assertEqual(header.codebooks, 8)
        self.assertFalse(header.language_model)

    def test_rejects_truncation(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            parse_header(b"ECDC")

    def test_parse_24khz_header(self) -> None:
        header = parse_header(make_test_ecdc(24_000, 4, "encodec_24khz"))
        self.assertEqual(header.model, "encodec_24khz")
        self.assertEqual(header.audio_length, 24_000)
        self.assertEqual(header.codebooks, 4)
