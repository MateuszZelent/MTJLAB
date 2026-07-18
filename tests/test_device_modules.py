from __future__ import annotations

import unittest

from app.devices.lakeshore_gaussmeter import (
    GaussmeterConfig,
    LakeShore475Adapter,
    MeasurementMode,
)
from app.devices.lakeshore_gaussmeter.simulator import simulated_475_session
from app.devices.moke_box import MokeBoxAdapter, MokeBoxConfig
from app.devices.registry import built_in_device_registry
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ConfigurationError, DeviceError
from tests.helpers import loaded_settings


class _MokeTransport:
    def __init__(self) -> None:
        self.connected = False

    def connect(self, endpoint: str, timeout_s: float) -> None:
        self.connected = bool(endpoint) and timeout_s > 0

    def identify(self) -> str:
        return "MOKE Box,SIM,1"

    def read_signal(self) -> float:
        return 0.125

    def close(self) -> None:
        self.connected = False


class _OfficialModel425Bridge:
    """Minimal official-driver stand-in; it only sees the read-only proxy."""

    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.idn = connection.query("*IDN?")  # type: ignore[attr-defined]


class DeviceModuleTests(unittest.TestCase):
    def test_registry_exposes_current_and_prepared_modules(self) -> None:
        registry = built_in_device_registry()
        self.assertEqual(
            {module.key for module in registry.all_modules()},
            {"rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"},
        )
        self.assertEqual(
            {module.key for module in registry.enabled_modules()},
            {"rigol", "keithley", "anritsu"},
        )
        self.assertTrue(all(module.page_factory is not None for module in registry.enabled_modules()))
        self.assertIsNotNone(registry.get("moke_box").page_factory)
        self.assertIsNone(registry.get("lakeshore_gaussmeter").page_factory)

    def test_unconfigured_future_module_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            built_in_device_registry().get("moke_box").create_adapter(None, simulation=False)  # type: ignore[arg-type]

    def test_future_device_profile_slots_are_present_and_fail_closed(self) -> None:
        settings = loaded_settings()

        self.assertFalse(settings.moke_box.enabled)
        self.assertFalse(settings.lakeshore_gaussmeter.enabled)

    def test_recipe_controls_are_aggregated_from_enabled_module_manifests(self) -> None:
        definitions = built_in_device_registry().recipe_parameter_definitions()

        self.assertEqual(
            {definition["target"] for definition in definitions},
            {
                "keithley.A.current", "keithley.A.voltage",
                "keithley.B.current", "keithley.B.voltage",
                "rigol.1.frequency", "rigol.1.high_level", "rigol.1.low_level",
                "rigol.2.frequency", "rigol.2.high_level", "rigol.2.low_level",
                "anritsu.sg.frequency", "anritsu.sg.power",
                "anritsu.spectrum.start_frequency",
                "anritsu.spectrum.stop_frequency",
                "anritsu.spectrum.reference_level",
            },
        )
        self.assertEqual(
            built_in_device_registry().get("moke_box").recipe_extension.parameter_definitions,
            ({
                "device": "MOKE Box",
                "label": "Magnetic field target",
                "target": "moke_box.field_target",
                "dimension": "magnetic_field",
            },),
        )

    def test_lakeshore_475_reads_dc_gauss_through_official_read_only_bridge(self) -> None:
        session = simulated_475_session(field=312.5, unit_code="1", mode_code="1")
        bridge: list[_OfficialModel425Bridge] = []
        adapter = LakeShore475Adapter(
            GaussmeterConfig(resource="SIM::LAKESHORE::INSTR"),
            session_factory=FakeVisaSessionFactory(session),
            official_model_factory=lambda connection: bridge.append(
                _OfficialModel425Bridge(connection)
            ) or bridge[-1],
        )
        identity = adapter.connect()
        reading = adapter.read_measurement()

        self.assertEqual(identity.model, "MODEL475")
        self.assertEqual(reading.mode, MeasurementMode.DC)
        self.assertEqual(reading.field_t, 0.03125)
        self.assertIsNone(reading.frequency_hz)
        self.assertEqual(len(bridge), 1)
        self.assertIn("RDGFIELD?", session.writes)
        self.assertTrue(set(session.writes) <= {
            "*IDN?", "UNIT?", "RDGMODE?", "RANGE?", "AUTO?", "TYPE?", "RDGFIELD?",
        })

    def test_lakeshore_475_rejects_h_units_without_assuming_b_conversion(self) -> None:
        session = simulated_475_session(field=12.0, unit_code="3", mode_code="1")
        adapter = LakeShore475Adapter(
            GaussmeterConfig(resource="SIM::LAKESHORE::INSTR"),
            session_factory=FakeVisaSessionFactory(session),
            official_model_factory=_OfficialModel425Bridge,
        )
        adapter.connect()

        with self.assertRaisesRegex(DeviceError, "Oersted"):
            adapter.read_measurement()

    def test_moke_adapter_exposes_only_measurement_path(self) -> None:
        transport = _MokeTransport()
        adapter = MokeBoxAdapter(MokeBoxConfig(endpoint="moke://sim"), transport)

        adapter.connect()
        self.assertEqual(adapter.read_signal().signal, 0.125)
        adapter.emergency_off()
        adapter.disconnect()
        self.assertFalse(transport.connected)
