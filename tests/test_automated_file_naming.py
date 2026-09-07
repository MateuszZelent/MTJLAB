"""Tests for automated run file naming and sample target synchronization."""

from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest

from app.inventory.models import ActiveSampleTarget
from app.settings.models import StationSettings
from app.ui.run_worker import automated_run_file_stem, planned_run_paths, sanitize_run_file_stem


def _dummy_settings(
    output_dir: str = "./measurements",
    write_csv: bool = True,
    pattern: str | None = None,
) -> StationSettings:
    storage = {
        "output_directory": output_dir,
        "write_csv_summary": write_csv,
    }
    if pattern:
        storage["filename_pattern"] = pattern

    class _MockSettings:
        def __init__(self, st: dict[str, object]) -> None:
            self.storage = st

    return _MockSettings(storage)  # type: ignore[return-value]


class AutomatedFileNamingTests(unittest.TestCase):
    def test_sanitize_run_file_stem(self) -> None:
        self.assertEqual(sanitize_run_file_stem("valid_name"), "valid_name")
        self.assertEqual(sanitize_run_file_stem("name with spaces"), "name_with_spaces")
        self.assertEqual(sanitize_run_file_stem("bad/path:chars*"), "path_chars")
        self.assertEqual(sanitize_run_file_stem("bad:path:chars*"), "bad_path_chars")
        self.assertEqual(sanitize_run_file_stem("file.h5"), "file")
        self.assertEqual(sanitize_run_file_stem(""), "run")

    def test_automated_file_stem_no_sample_target(self) -> None:
        stem = automated_run_file_stem("transfer_curve")
        self.assertEqual(stem, "transfer_curve")

        stem_fallback = automated_run_file_stem("")
        self.assertEqual(stem_fallback, "run")

    def test_automated_file_stem_with_active_sample(self) -> None:
        target = ActiveSampleTarget(
            sample_id="INL_MTJ_02.2026",
            sample_name="INL MTJ Wedge Sample (02.2026)",
            row="20",
            col="1",
            device_label="100 nm (P1)",
            row_label="l20 (Rep 1)",
            col_label="100 nm (P1)",
        )
        stem = automated_run_file_stem("transfer_curve", sample_target=target)
        self.assertEqual(stem, "INL_MTJ_02.2026_R20C1_100_nm_P1_transfer_curve")

    def test_automated_file_stem_coordinate_only_when_no_label(self) -> None:
        target = ActiveSampleTarget(
            sample_id="SAMPLE1",
            row="5",
            col="2",
            device_label="",
        )
        stem = automated_run_file_stem("sweep", sample_target=target)
        self.assertEqual(stem, "SAMPLE1_R5C2_sweep")

    def test_automated_file_stem_with_override_plain_text(self) -> None:
        target = ActiveSampleTarget(
            sample_id="INL_MTJ_02.2026",
            row="20",
            col="1",
            device_label="100 nm",
        )
        stem = automated_run_file_stem(
            "transfer_curve",
            sample_target=target,
            file_stem_override="manual_override_run",
        )
        self.assertEqual(stem, "manual_override_run")

    def test_automated_file_stem_with_override_template(self) -> None:
        target = ActiveSampleTarget(
            sample_id="INL_MTJ_02.2026",
            row="20",
            col="1",
            device_label="100 nm",
        )
        stem = automated_run_file_stem(
            "transfer_curve",
            sample_target=target,
            file_stem_override="Custom_{sample_id}_{coord}_test",
        )
        self.assertEqual(stem, "Custom_INL_MTJ_02.2026_R20C1_test")

    def test_automated_file_stem_with_settings_pattern(self) -> None:
        target = ActiveSampleTarget(
            sample_id="INL_MTJ_02.2026",
            row="20",
            col="1",
            device_label="100 nm",
        )
        stem = automated_run_file_stem(
            "bias_sweep",
            sample_target=target,
            pattern="{recipe}_{sample_id}_{coord}",
        )
        self.assertEqual(stem, "bias_sweep_INL_MTJ_02.2026_R20C1")

    def test_planned_run_paths_automated_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _dummy_settings(output_dir=temp_dir, write_csv=True)
            fixed_time = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
            target = ActiveSampleTarget(
                sample_id="INL_MTJ_02.2026",
                row="20",
                col="1",
                device_label="100 nm",
            )
            h5_path, csv_path = planned_run_paths(
                settings,
                "transfer_curve",
                timestamp=fixed_time,
                sample_target=target,
            )
            self.assertEqual(
                h5_path.name,
                "20260906T120000.000000Z_INL_MTJ_02.2026_R20C1_100_nm_transfer_curve.h5",
            )
            self.assertIsNotNone(csv_path)
            self.assertEqual(
                csv_path.name,
                "20260906T120000.000000Z_INL_MTJ_02.2026_R20C1_100_nm_transfer_curve.csv",
            )


if __name__ == "__main__":
    unittest.main()

