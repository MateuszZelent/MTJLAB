from __future__ import annotations

import unittest

from app.devices.anritsu_ms2830a import frequency_option_for, parse_anritsu_option_response


class AnritsuHardwareTests(unittest.TestCase):
    def test_option_response_is_normalized_and_deduplicated(self) -> None:
        self.assertEqual(
            parse_anritsu_option_response("MS2830A-041, 008,MS2830A-041"),
            ("041", "008"),
        )

    def test_frequency_profile_uses_detected_option(self) -> None:
        profile = frequency_option_for(("008", "041"))

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.code, "041")
        self.assertEqual(profile.maximum_stop_hz, 6.1e9)
        self.assertEqual(profile.default_sweep_time_s, 2e-3)

    def test_empty_option_response_is_supported(self) -> None:
        self.assertEqual(parse_anritsu_option_response("0"), ())
        self.assertIsNone(frequency_option_for(()))


if __name__ == "__main__":
    unittest.main()
