from __future__ import annotations

import unittest

from app.devices.keithley_2600 import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol_dg1000z import RigolAdapter, RigolChannelConfig
from app.devices.simulators import KeithleySimulator, RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_VOLTAGE
from app.safety.keithley import quantize_keithley_value
from app.safety.rigol_current import quantize_rigol_voltage
from tests.helpers import simulation_settings


class InstrumentPrecisionTests(unittest.TestCase):
    def test_documented_resolution_rounds_long_sweep_values(self) -> None:
        long_value = 0.01111111111111111111111111111111

        self.assertAlmostEqual(quantize_rigol_voltage(long_value), 0.0111)
        self.assertAlmostEqual(
            quantize_keithley_value(long_value, DIMENSION_VOLTAGE), 0.01111
        )
        self.assertAlmostEqual(
            quantize_keithley_value(long_value, DIMENSION_CURRENT), 0.011111
        )

    def test_rigol_sweep_level_is_quantized_before_scpi_write(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                channel=1,
                waveform="SIN",
                frequency_hz=1_000.0,
                high_level_v=0.001,
                low_level_v=-0.001,
            )
        )

        actual = adapter.update_high_level(
            1, 0.01111111111111111111111111111111
        )

        self.assertAlmostEqual(actual, 0.0111)
        self.assertIn(":SOUR1:VOLT:HIGH 0.0111", session.commands)
        self.assertAlmostEqual(
            adapter.last_channel_config(1).high_level_v, 0.0111
        )

    def test_keithley_sweep_level_is_quantized_before_tsp_write(self) -> None:
        session = KeithleySimulator()
        adapter = KeithleyAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest(
                channel="B",
                mode="voltage",
                level_si=0.01,
                compliance_si=0.001,
            )
        )

        actual = adapter.update_source_level(
            "B",
            mode="voltage",
            level_si=0.01111111111111111111111111111111,
        )

        self.assertAlmostEqual(actual, 0.01111)
        self.assertIn("smub.source.levelv = 0.01111", session.commands)
        self.assertAlmostEqual(
            adapter.quick_control_snapshot()["keithley.B.voltage"], 0.01111
        )

    def test_rigol_quantized_duplicate_frequency_skips_second_scpi_write(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                channel=1,
                waveform="SIN",
                frequency_hz=1_000.0,
                high_level_v=0.001,
                low_level_v=-0.001,
            )
        )
        initial_writes = session.commands.count(":SOUR1:FREQ 1000")

        actual = adapter.update_frequency(
            1, 1_000.0000000000001
        )

        self.assertAlmostEqual(actual, 1_000.0)
        self.assertEqual(session.commands.count(":SOUR1:FREQ 1000"), initial_writes)

        changed = adapter.update_frequency(1, 1_000.0005)

        self.assertAlmostEqual(changed, 1_000.0005)
        self.assertIn(":SOUR1:FREQ 1000.0005", session.commands)

    def test_keithley_quantized_duplicate_level_skips_second_tsp_write(self) -> None:
        session = KeithleySimulator()
        adapter = KeithleyAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest(
                channel="B",
                mode="voltage",
                level_si=0.01,
                compliance_si=0.001,
            )
        )
        initial_writes = session.commands.count("smub.source.levelv = 0.01")

        actual = adapter.update_source_level(
            "B",
            mode="voltage",
            level_si=0.01000000000000000000000000000001,
        )

        self.assertAlmostEqual(actual, 0.01)
        self.assertEqual(
            session.commands.count("smub.source.levelv = 0.01"), initial_writes
        )

    def test_rigol_configure_channel_writes_clean_scpi_voltages(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        # 0.015 - 0.005 in Python float arithmetic is 0.009999999999999998
        adapter.configure_channel(
            RigolChannelConfig(
                channel=1,
                waveform="SIN",
                frequency_hz=13_000.0,
                high_level_v=0.015,
                low_level_v=0.005,
            )
        )

        self.assertIn(":SOUR1:VOLT 0.01", session.commands)
        self.assertIn(":SOUR1:VOLT:OFFS 0.01", session.commands)
        self.assertNotIn(":SOUR1:VOLT 0.009999999999999998", session.commands)

    def test_rigol_voltage_formatting_preserves_units_and_quantizes(self) -> None:
        from app.devices.rigol_dg1000z.ui.page import RigolPage
        from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

        for fmt in (RigolPage._format_voltage, RigolNodeEditorDialog._format_voltage):
            # Unit preservation when 0
            self.assertEqual(fmt(0.0, preferred_unit="mV"), "0 mV")
            self.assertEqual(fmt(0.0, preferred_unit="V"), "0 V")
            self.assertEqual(fmt(-0.0, preferred_unit="mV"), "0 mV")
            self.assertEqual(fmt(-0.0, preferred_unit="V"), "0 V")

            # Preserves V even when < 1 V
            self.assertEqual(fmt(0.005, preferred_unit="V"), "0.005 V")
            self.assertEqual(fmt(0.01, preferred_unit="V"), "0.01 V")

            # Quantizes float noise
            self.assertEqual(fmt(0.015 - 0.005, preferred_unit="V"), "0.01 V")
            self.assertEqual(fmt(0.015 - 0.005, preferred_unit="mV"), "10 mV")

            # Default heuristic when preferred_unit is None
            self.assertEqual(fmt(0.005), "5 mV")
            self.assertEqual(fmt(1.5), "1.5 V")
            self.assertEqual(fmt(0.0), "0 V")

    def test_rigol_configure_dc_waveform_sets_offset(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            simulation_settings(),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                channel=1,
                waveform="DC",
                frequency_hz=1.0,
                high_level_v=0.007,
                low_level_v=0.007,
            )
        )
        self.assertIn(":SOUR1:APPL:DC 0.007", session.commands)
        self.assertIn(":SOUR1:VOLT:OFFS 0.007", session.commands)
        self.assertNotIn(":SOUR1:APPL:DC DEF,DEF,0.007", session.commands)
        self.assertEqual(session.query(":SOUR1:VOLT:OFFS?"), "0.007")


if __name__ == "__main__":
    unittest.main()
