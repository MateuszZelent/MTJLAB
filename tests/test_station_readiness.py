from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from app.domain.readiness import ReadinessLevel, evaluate_station_readiness
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.estimation import PlanEstimate
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import SETTINGS_TEMPLATE


class StationReadinessTests(unittest.TestCase):
    def _settings(self, output_directory: Path) -> StationSettings:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["storage"]["output_directory"] = str(output_directory)
        raw["devices"]["rigol"]["connection"]["resource"] = "USB0::rigol::INSTR"
        raw["devices"]["keithley"]["connection"]["resource"] = "GPIB0::22::INSTR"
        raw["devices"]["anritsu"]["connection"]["resource"] = "GPIB0::23::INSTR"
        return StationSettings.model_validate(raw)

    @staticmethod
    def _plan() -> ExecutionPlan:
        action = PlanAction("connect-rigol", "verify_connection", {"device": "rigol"}, {})
        return ExecutionPlan(
            "readiness",
            (action,),
            0,
            "a" * 64,
            "name: readiness",
            frozenset({"rigol"}),
            0,
        )

    @staticmethod
    def _estimate() -> PlanEstimate:
        return PlanEstimate(1.0, 1.5, 1024, 0, 0, 0, 0, ())

    def test_configured_safe_station_is_ready_and_reports_plan_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            readiness = evaluate_station_readiness(
                settings,
                device_states={name: "disconnected" for name in ("rigol", "keithley", "anritsu")},
                verified_resources={"rigol": "USB0::rigol::INSTR"},
                audit_healthy=True,
                plan=self._plan(),
                estimate=self._estimate(),
            )
        self.assertTrue(readiness.ready)
        self.assertIn("plan", {item.key for item in readiness.items})
        self.assertEqual(
            next(item.level for item in readiness.items if item.key == "device.rigol"),
            ReadinessLevel.PASS,
        )

    def test_required_unassigned_device_and_unhealthy_audit_are_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            raw = settings.model_dump(mode="python")
            raw["devices"]["rigol"]["connection"]["resource"] = None
            settings = StationSettings.model_validate(raw)
            readiness = evaluate_station_readiness(
                settings,
                device_states={},
                verified_resources={},
                audit_healthy=False,
                plan=self._plan(),
                estimate=self._estimate(),
            )
        self.assertFalse(readiness.ready)
        self.assertEqual(
            {item.key for item in readiness.blocking_items},
            {"audit", "device.rigol"},
        )

    def test_manual_output_on_blocks_even_when_device_is_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            readiness = evaluate_station_readiness(
                settings,
                device_states={"keithley": "output_on"},
                verified_resources={},
                audit_healthy=True,
                plan=self._plan(),
                estimate=self._estimate(),
            )
        self.assertIn("device.keithley", {item.key for item in readiness.blocking_items})

    def test_required_moke_and_lakeshore_devices_are_reported_as_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            plan = ExecutionPlan(
                "readiness",
                (),
                0,
                "b" * 64,
                "name: readiness",
                frozenset({"moke_box", "lakeshore_gaussmeter"}),
                0,
            )
            readiness = evaluate_station_readiness(
                settings,
                device_states={},
                verified_resources={},
                audit_healthy=True,
                plan=plan,
                estimate=self._estimate(),
            )
        self.assertEqual(
            {item.key for item in readiness.blocking_items},
            {"device.moke_box", "device.lakeshore_gaussmeter"},
        )

    def test_moke_endpoint_and_lakeshore_resource_can_be_identity_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            raw = settings.model_dump(mode="python")
            raw["devices"]["moke_box"].update(
                {"enabled": True, "endpoint": "192.0.2.10:5000"}
            )
            raw["devices"]["lakeshore_gaussmeter"].update(
                {"enabled": True, "resource": "ASRL3::INSTR"}
            )
            settings = StationSettings.model_validate(raw)
            plan = ExecutionPlan(
                "readiness",
                (),
                0,
                "c" * 64,
                "name: readiness",
                frozenset({"moke_box", "lakeshore_gaussmeter"}),
                0,
            )
            readiness = evaluate_station_readiness(
                settings,
                device_states={
                    "moke_box": "disconnected",
                    "lakeshore_gaussmeter": "disconnected",
                },
                verified_resources={
                    "moke_box": "192.0.2.10:5000",
                    "lakeshore_gaussmeter": "ASRL3::INSTR",
                },
                audit_healthy=True,
                plan=plan,
                estimate=self._estimate(),
            )
        levels = {item.key: item.level for item in readiness.items}
        self.assertEqual(levels["device.moke_box"], ReadinessLevel.PASS)
        self.assertEqual(levels["device.lakeshore_gaussmeter"], ReadinessLevel.PASS)

    def test_energized_plan_reports_legacy_dut_limits_as_not_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory) / "results")
            plan = ExecutionPlan(
                "readiness",
                (
                    PlanAction(
                        "rf-on",
                        "set_anritsu_sg_output",
                        {"enabled": True},
                        {},
                    ),
                ),
                0,
                "d" * 64,
                "name: readiness",
                frozenset({"anritsu"}),
                0,
                recipe_dut_limits={
                    "anritsu": {"max_signal_generator_output": "-10 dBm"}
                },
            )
            readiness = evaluate_station_readiness(
                settings,
                device_states={"anritsu": "disconnected"},
                verified_resources={},
                audit_healthy=True,
                plan=plan,
                estimate=self._estimate(),
            )
        item = next(item for item in readiness.items if item.key == "dut")
        self.assertEqual(item.level, ReadinessLevel.WARNING)
        self.assertIn("not enforced", item.detail.lower())
        self.assertIn("station", item.detail.lower())
        self.assertNotIn("validated", item.detail.lower())


if __name__ == "__main__":
    unittest.main()
