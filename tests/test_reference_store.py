from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from app.devices.anritsu import ReferenceSpectrum, SpectrumTrace
from app.storage import ReferenceHdf5Store
from app.storage.pythat_bridge import open_measurement_tree


class ReferenceStoreTests(unittest.TestCase):
    def test_reference_round_trip_preserves_trace_and_provenance_and_opens_in_pythat(self) -> None:
        acquired = datetime.now(timezone.utc)
        reference = ReferenceSpectrum(
            trace=SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=acquired,
                trace_name="TRAC1_REFAVG200",
            ),
            kind="averaged",
            average_count=200,
            acquired_at_utc=acquired,
            source_device_idn="ANRITSU,MS2830A,6201514799,7.03.00",
            firmware="7.03.00",
            hardware_options=("041",),
            reference_level_dbm=-10.0,
            advanced_configuration_known=True,
            rbw_auto=False,
            rbw_hz=10e3,
            vbw_mode="manual",
            vbw_hz=3e3,
            detector="RMS",
            attenuation_auto=False,
            attenuation_db=20.0,
            preamplifier_enabled=False,
            sweep_time_auto=False,
            sweep_time_s=0.2,
            notes="fixture reference",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reference.h5"
            saved = ReferenceHdf5Store.save(path, reference)
            loaded = ReferenceHdf5Store.load(path)

            self.assertTrue(saved.saved_to_file)
            self.assertEqual(loaded.kind, "averaged")
            self.assertEqual(loaded.average_count, 200)
            self.assertEqual(loaded.trace.frequencies_hz, reference.trace.frequencies_hz)
            self.assertEqual(loaded.trace.powers_dbm, reference.trace.powers_dbm)
            self.assertEqual(loaded.grid_hash, reference.grid_hash)
            self.assertEqual(loaded.hardware_options, ("041",))
            self.assertTrue(loaded.advanced_configuration_known)
            self.assertFalse(loaded.rbw_auto)
            self.assertEqual(loaded.rbw_hz, 10e3)
            self.assertEqual(loaded.vbw_mode, "manual")
            self.assertEqual(loaded.vbw_hz, 3e3)
            self.assertEqual(loaded.detector, "RMS")
            self.assertFalse(loaded.attenuation_auto)
            self.assertEqual(loaded.attenuation_db, 20.0)
            self.assertFalse(loaded.preamplifier_enabled)
            self.assertFalse(loaded.sweep_time_auto)
            self.assertEqual(loaded.sweep_time_s, 0.2)
            self.assertEqual(loaded.notes, "fixture reference")

            tree = open_measurement_tree(path)
            self.assertEqual(tree.dataset.sizes["Checkpoint"], 1)
            self.assertEqual(tree.dataset.sizes["Frequency"], 3)
            self.assertIn("Spectrum", tree.dataset.data_vars)


if __name__ == "__main__":
    unittest.main()
