from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ruamel.yaml import YAML

from app.domain.errors import AuthorizationError, ConfigurationError, SafetyViolation
from app.qualification import (
    CaseResult,
    CaseStatus,
    EnergizedAuthorization,
    QualificationReport,
    QualificationRunner,
    RiskLevel,
)
from app.qualification.runner import ENERGIZED_CONFIRMATION, ENERGIZED_HIL_ENVIRONMENT
from app.qualification.__main__ import main as qualification_main
from app.security import AccessPolicy, AuthenticatedIdentity, Role
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import SETTINGS_TEMPLATE, simulation_settings


class QualificationEvidenceTests(unittest.TestCase):
    def test_report_digest_detects_tampering(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        report = QualificationReport(
            qualification_id="HIL-test",
            started_at_utc=now,
            settings_path="settings.yml",
            settings_sha256="a" * 64,
            profile_id="test",
            profile_state="unverified",
            operator={"username": "tester", "roles": ["service"]},
            simulation=True,
            cases=[
                CaseResult(
                    "profile.validated",
                    "Profile",
                    RiskLevel.PASSIVE,
                    CaseStatus.PASSED,
                    now,
                    now,
                    0.01,
                )
            ],
        )
        report.finish()
        with tempfile.TemporaryDirectory() as directory:
            path = report.write_atomic(Path(directory) / "evidence.json")
            verified = QualificationReport.verify_file(path)
            self.assertEqual(verified["overall_status"], "simulation_passed")
            document = json.loads(path.read_text(encoding="utf-8"))
            document["overall_status"] = "passed"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                QualificationReport.verify_file(path)

    def test_energized_authorization_requires_every_physical_gate(self) -> None:
        settings = simulation_settings(approved=True)
        policy = AccessPolicy(
            AuthenticatedIdentity(
                username="LAB\\service",
                provider="operating_system",
                host="station",
                roles=frozenset({Role.SERVICE}),
            )
        )
        authorization = EnergizedAuthorization(
            allow_energized=True,
            dummy_load_id="LOAD-50OHM-001",
            interlock_confirmed=True,
            confirmation=ENERGIZED_CONFIRMATION,
        )
        with self.assertRaises(SafetyViolation):
            authorization.validate(settings, policy, simulation=False, environment={})
        authorization.validate(
            settings,
            policy,
            simulation=False,
            environment={ENERGIZED_HIL_ENVIRONMENT: "YES"},
        )

    def test_passive_simulation_writes_verified_evidence(self) -> None:
        settings = simulation_settings(anritsu_enabled=True)
        raw = settings.model_dump(mode="python")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.yml"
            with settings_path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)
            runner = QualificationRunner(
                settings_path,
                output_directory=root / "qualification",
                simulation=True,
            )
            report_path = runner.run_passive(read_anritsu_trace=True)
            document = QualificationReport.verify_file(report_path)
            self.assertEqual(document["overall_status"], "simulation_passed")
            statuses = {item["case_id"]: item["status"] for item in document["cases"]}
            self.assertEqual(statuses["rigol.connect"], "passed")
            self.assertEqual(statuses["keithley.connect"], "passed")
            self.assertEqual(statuses["anritsu.read_configuration"], "passed")
            self.assertEqual(statuses["anritsu.read_current_trace"], "passed")
            self.assertEqual(statuses["rigol.safe_shutdown"], "passed")
            self.assertTrue(tuple((root / "qualification" / "audit").glob("*.jsonl")))

    def test_simulation_is_isolated_from_physical_resources_and_serial_binding(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["profile"]["state"] = "unverified"
        raw["devices"]["rigol"]["connection"]["resource"] = "USB0::PHYSICAL::INSTR"
        raw["devices"]["rigol"]["identity"]["require_serial_match"] = True
        raw["devices"]["rigol"]["identity"]["expected_serial"] = "PHYSICAL-SERIAL"
        raw["devices"]["keithley"]["connection"]["resource"] = None
        raw["devices"]["anritsu"]["connection"]["resource"] = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.yml"
            with settings_path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)

            runner = QualificationRunner(
                settings_path,
                output_directory=root / "qualification",
                simulation=True,
            )
            report_path = runner.run_passive(read_anritsu_trace=True)
            document = QualificationReport.verify_file(report_path)

            self.assertEqual(document["overall_status"], "simulation_passed")
            self.assertEqual(document["profile_state"], "unverified")
            cases = {item["case_id"]: item for item in document["cases"]}
            self.assertTrue(cases["profile.validated"]["evidence"]["simulation_profile_isolated"])
            self.assertEqual(
                cases["rigol.connect"]["evidence"]["identity"]["resource"],
                "SIM::RIGOL::INSTR",
            )
            self.assertEqual(cases["keithley.connect"]["status"], "passed")
            self.assertEqual(cases["anritsu.connect"]["status"], "passed")

    def test_physical_runner_is_service_only(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["access_control"]["default_roles"] = ["operator"]
        settings = StationSettings.model_validate(raw)
        self.assertEqual(settings.access_control.default_roles, ("operator",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.yml"
            with path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)
            with self.assertRaises(AuthorizationError) as caught:
                QualificationRunner(path, output_directory=Path(directory) / "qualification")
            self.assertIn("service_diagnostics", str(caught.exception))

    def test_cli_returns_nonzero_for_an_incomplete_qualification_report(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        for device_name in ("rigol", "keithley", "anritsu"):
            raw["devices"][device_name]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.yml"
            with settings_path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)

            exit_code = qualification_main(
                [
                    "--settings",
                    str(settings_path),
                    "--output-directory",
                    str(root / "qualification"),
                    "--simulate",
                    "passive",
                ]
            )

            self.assertEqual(exit_code, 1)
            report_path = next((root / "qualification").glob("HIL-*.json"))
            document = QualificationReport.verify_file(report_path)
            self.assertEqual(document["overall_status"], "incomplete")

    def test_recipe_cli_writes_a_blocked_report_without_all_energized_gates(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.yml"
            recipe_path = root / "qualification.yml"
            with settings_path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)
            recipe_path.write_text(
                """schema_version: 1
name: qualification-gate
root:
  id: checkpoint
  type: checkpoint
""",
                encoding="utf-8",
            )

            exit_code = qualification_main(
                [
                    "--settings",
                    str(settings_path),
                    "--output-directory",
                    str(root / "qualification"),
                    "--simulate",
                    "recipe",
                    str(recipe_path),
                    "--allow-energized",
                    "--dummy-load-id",
                    "LOAD-TEST",
                    "--interlock-confirmed",
                    "--confirmation",
                    ENERGIZED_CONFIRMATION,
                ]
            )

            self.assertEqual(exit_code, 1)
            report_path = next((root / "qualification").glob("HIL-*.json"))
            document = QualificationReport.verify_file(report_path)
            self.assertEqual(document["overall_status"], "blocked")
            self.assertEqual(
                {item["case_id"] for item in document["cases"]},
                {"recipe.authorization"},
            )

    def test_authorized_recipe_uses_production_runner_and_persists_result(self) -> None:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["profile"]["state"] = "approved"
        raw["access_control"]["default_roles"] = ["service"]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {ENERGIZED_HIL_ENVIRONMENT: "YES"}, clear=False
        ):
            root = Path(directory)
            settings_path = root / "settings.yml"
            recipe_path = root / "qualification.yml"
            with settings_path.open("w", encoding="utf-8") as stream:
                YAML().dump(raw, stream)
            recipe_path.write_text(
                """schema_version: 1
name: qualification-checkpoint
root:
  id: checkpoint
  type: checkpoint
""",
                encoding="utf-8",
            )
            runner = QualificationRunner(
                settings_path,
                output_directory=root / "qualification",
            )
            report_path = runner.run_recipe(
                recipe_path,
                authorization=EnergizedAuthorization(
                    allow_energized=True,
                    dummy_load_id="LOAD-TEST",
                    interlock_confirmed=True,
                    confirmation=ENERGIZED_CONFIRMATION,
                ),
            )

            document = QualificationReport.verify_file(report_path)
            self.assertEqual(document["overall_status"], "passed")
            execute = next(item for item in document["cases"] if item["case_id"] == "recipe.execute")
            result_path = Path(execute["evidence"]["result_path"])
            self.assertTrue(result_path.is_file())


if __name__ == "__main__":
    unittest.main()
