from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import h5py

from app.devices.anritsu import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunWriter


class Hdf5WriterTests(unittest.TestCase):
    def test_writer_flushes_a_point_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={"rigol": "RIGOL,DG1032Z"},
            )
            point = MeasurementPoint(
                index=0,
                setpoints={"keithley.B.current": 0.001},
                measurements={"keithley.B.voltage_v": 0.01},
            )
            trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            self.assertEqual(writer.append(point, trace), 0)
            writer.close("completed")
            with h5py.File(path, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertEqual(tuple(file["spectra/0/frequency_hz"][:]), trace.frequencies_hz)
                self.assertEqual(tuple(file["spectra/0/power_dbm"][:]), trace.powers_dbm)


if __name__ == "__main__":
    unittest.main()
