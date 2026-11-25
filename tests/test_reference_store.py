from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.devices.anritsu_ms2830a import ReferenceSpectrum, SpectrumTrace
from app.domain.errors import ExecutionError
from app.storage import Hdf5RunReader, ReferenceHdf5Store
from app.storage.pythat_bridge import open_measurement_tree


class ReferenceStoreTests(unittest.TestCase):
    def test_reference_round_trip_preserves_trace_and_provenance_and_opens_in_pythat(self) -> None:
        acquired = datetime.now(UTC)
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
            vbw_filter_mode="POW",
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
            self.assertEqual(loaded.vbw_filter_mode, "POW")
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

            detail = Hdf5RunReader.detail(path)
            self.assertIsNone(detail.simulation_metadata["enabled"])
            self.assertEqual(detail.simulation_metadata["mode"], "unknown")
            self.assertIsNone(ReferenceHdf5Store.read_simulation_enabled(path))

    def test_simulation_provenance_round_trip_and_mismatch_rejection(self) -> None:
        acquired = datetime.now(UTC)
        reference = ReferenceSpectrum(
            trace=SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=acquired,
                trace_name="TRAC1",
            ),
            kind="single",
            average_count=1,
            acquired_at_utc=acquired,
            source_device_idn="ANRITSU,MS2830A,SIM,7.03.00",
            timing_context="test-timing",
        )
        with tempfile.TemporaryDirectory() as temporary:
            sim_path = Path(temporary) / "sim_ref.h5"
            hw_path = Path(temporary) / "hw_ref.h5"
            unknown_path = Path(temporary) / "unknown_ref.h5"

            ReferenceHdf5Store.save(sim_path, reference, simulation_mode=True)
            ReferenceHdf5Store.save(hw_path, reference, simulation_mode=False)
            ReferenceHdf5Store.save(unknown_path, reference)

            self.assertEqual(
                Hdf5RunReader.detail(sim_path).simulation_metadata,
                {"enabled": True, "mode": "simulation", "mode_source": "application_runtime"},
            )
            self.assertEqual(
                Hdf5RunReader.detail(hw_path).simulation_metadata,
                {"enabled": False, "mode": "hardware", "mode_source": "application_runtime"},
            )
            self.assertEqual(
                Hdf5RunReader.detail(unknown_path).simulation_metadata,
                {"enabled": None, "mode": "unknown", "mode_source": "not_provided"},
            )

            loaded_sim, sim_flag = ReferenceHdf5Store.load_with_provenance(
                sim_path, expected_simulation_mode=True
            )
            self.assertTrue(sim_flag)
            self.assertEqual(loaded_sim.timing_context, "test-timing")

            loaded_hw, hw_flag = ReferenceHdf5Store.load_with_provenance(
                hw_path, expected_simulation_mode=False
            )
            self.assertFalse(hw_flag)
            self.assertEqual(loaded_hw.timing_context, "test-timing")

            with self.assertRaisesRegex(ExecutionError, "simulated reference.*hardware"):
                ReferenceHdf5Store.load(sim_path, expected_simulation_mode=False)

            with self.assertRaisesRegex(ExecutionError, "hardware reference.*simulation"):
                ReferenceHdf5Store.load(hw_path, expected_simulation_mode=True)

            # Unknown provenance is permitted by default for backward compatibility,
            # and rejected when strict mode is requested.
            loaded_unknown, unknown_flag = ReferenceHdf5Store.load_with_provenance(
                unknown_path, expected_simulation_mode=False
            )
            self.assertIsNone(unknown_flag)
            self.assertEqual(loaded_unknown.grid_hash, reference.grid_hash)
            with self.assertRaisesRegex(ExecutionError, "does not record"):
                ReferenceHdf5Store.load(
                    unknown_path,
                    expected_simulation_mode=False,
                    allow_unknown_simulation=False,
                )

            with self.assertRaises(TypeError):
                ReferenceHdf5Store.save(Path(temporary) / "bad.h5", reference, simulation_mode=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
