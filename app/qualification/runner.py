"""Passive qualification runner using production adapters and safe state paths."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Any, Iterable
from uuid import uuid4

from app.audit import AuditLogger
from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulation import SimulationContext
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.domain.errors import AuthorizationError
from app.engine.compiler import RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from app.security import AccessPolicy, Permission
from app.settings import SettingsRepository
from app.storage import Hdf5RunWriter
from app.qualification.report import CaseResult, CaseStatus, QualificationReport, RiskLevel


ENERGIZED_CONFIRMATION = "I CONFIRM DUMMY LOAD AND PHYSICAL INTERLOCK"
ENERGIZED_HIL_ENVIRONMENT = "LAB_CONTROL_ENABLE_ENERGIZED_HIL"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity_document(identity: object) -> dict[str, Any]:
    return {
        key: getattr(identity, key, None)
        for key in ("resource", "idn", "manufacturer", "model", "serial", "firmware")
    }


class QualificationRunner:
    """Run a bounded passive qualification session and persist signed evidence."""

    def __init__(
        self,
        settings_path: str | Path,
        *,
        output_directory: str | Path,
        simulation: bool = False,
    ) -> None:
        self.settings_path = Path(settings_path).expanduser()
        loaded = SettingsRepository(self.settings_path).load()
        self.settings = loaded.settings
        self._raw_settings = loaded.raw
        self.output_directory = Path(output_directory).expanduser()
        self.simulation = simulation
        self.policy = AccessPolicy.from_settings(self.settings, simulation=simulation)
        if not simulation:
            self.policy.require(Permission.SERVICE_DIAGNOSTICS, action="service_diagnostics")
        profile = self._raw_settings.get("profile", {})
        self.profile_state = str(profile.get("state", "unverified")) if isinstance(profile, dict) else "unverified"
        self.settings_sha256 = hashlib.sha256(self.settings_path.read_bytes()).hexdigest()

    def run_passive(
        self,
        *,
        devices: Iterable[str] | None = None,
        read_anritsu_trace: bool = False,
    ) -> Path:
        started = _utc_now()
        qualification_id = f"HIL-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        operator = {
            "username": self.policy.identity.username,
            "provider": self.policy.identity.provider,
            "host": self.policy.identity.host,
            "roles": sorted(role.value for role in self.policy.identity.roles),
        }
        report = QualificationReport(
            qualification_id=qualification_id,
            started_at_utc=started,
            settings_path=str(self.settings_path),
            settings_sha256=self.settings_sha256,
            profile_id=self.settings.profile.id,
            profile_state=self.profile_state,
            operator=operator,
            simulation=self.simulation,
            cases=[],
        )
        audit = AuditLogger(
            self.output_directory / "audit",
            profile_id=self.settings.profile.id,
            simulation=self.simulation,
            actor=self.policy.identity.username,
            actor_roles=tuple(operator["roles"]),
        )
        runtime_settings = simulated_station_settings(self.settings) if self.simulation else self.settings
        context = SimulationContext(seed=0x48494C, model_version="qualification")
        selected = tuple(devices or ("rigol", "keithley", "anritsu"))
        try:
            self._case(
                report,
                audit,
                "profile.validated",
                "Validated station profile",
                lambda: {
                    "simulation_profile_isolated": self.simulation,
                    "profile_state": self.profile_state,
                    "settings_sha256": self.settings_sha256,
                },
            )
            for device in selected:
                if device not in {"rigol", "keithley", "anritsu"}:
                    self._record_case(
                        report,
                        audit,
                        "device.unknown",
                        f"Unknown device {device}",
                        CaseStatus.BLOCKED,
                        RiskLevel.PASSIVE,
                        error=f"Unknown qualification device {device!r}.",
                    )
                    continue
                self._qualify_device(
                    report,
                    audit,
                    runtime_settings,
                    context,
                    device,
                    read_anritsu_trace=read_anritsu_trace,
                )
        finally:
            report.finish()
            report_path = report.write_atomic(
                self.output_directory / f"{qualification_id}.json"
            )
            audit.close()
        return report_path

    def run_recipe(
        self,
        recipe_path: str | Path,
        *,
        authorization,
        devices: Iterable[str] | None = None,
    ) -> Path:
        """Compile and execute one explicitly authorized qualification recipe.

        This is intentionally a narrow release gate around the production
        compiler, runner and HDF5 writer.  It never interprets YAML as raw
        instrument commands, and it refuses simulation or unsupported device
        families before opening a VISA session.
        """

        source_path = Path(recipe_path).expanduser()
        started = _utc_now()
        qualification_id = (
            f"HIL-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid4().hex[:8]}"
        )
        operator = {
            "username": self.policy.identity.username,
            "provider": self.policy.identity.provider,
            "host": self.policy.identity.host,
            "roles": sorted(role.value for role in self.policy.identity.roles),
        }
        report = QualificationReport(
            qualification_id=qualification_id,
            started_at_utc=started,
            settings_path=str(self.settings_path),
            settings_sha256=self.settings_sha256,
            profile_id=self.settings.profile.id,
            profile_state=self.profile_state,
            operator=operator,
            simulation=self.simulation,
            cases=[],
        )
        audit = AuditLogger(
            self.output_directory / "audit",
            profile_id=self.settings.profile.id,
            simulation=self.simulation,
            actor=self.policy.identity.username,
            actor_roles=tuple(operator["roles"]),
        )
        report_path = self.output_directory / f"{qualification_id}.json"

        def finish() -> Path:
            report.finish()
            path = report.write_atomic(report_path)
            audit.close()
            return path

        try:
            try:
                authorization.validate(
                    self.settings,
                    self.policy,
                    simulation=self.simulation,
                    environment=dict(os.environ),
                )
            except Exception as exc:
                self._record_case(
                    report,
                    audit,
                    "recipe.authorization",
                    "Energized recipe authorization",
                    CaseStatus.BLOCKED,
                    RiskLevel.ENERGIZED,
                    error=str(exc),
                )
                return finish()

            try:
                source = source_path.read_text(encoding="utf-8")
                recipe = parse_recipe_text(source, origin=str(source_path))
                plan = RecipeCompiler(self.settings).compile(recipe)
                requested = set(devices or plan.required_devices)
                unsupported = set(plan.required_devices) - {
                    "rigol",
                    "keithley",
                    "anritsu",
                }
                missing_from_selection = set(plan.required_devices) - requested
                if unsupported:
                    raise AuthorizationError(
                        "Energized qualification does not yet support these device "
                        f"families: {', '.join(sorted(unsupported))}."
                    )
                if missing_from_selection:
                    raise AuthorizationError(
                        "The selected qualification devices omit required recipe "
                        f"dependencies: {', '.join(sorted(missing_from_selection))}."
                    )
                self._record_case(
                    report,
                    audit,
                    "recipe.compile",
                    "Compile qualification recipe",
                    CaseStatus.PASSED,
                    RiskLevel.ENERGIZED,
                    evidence={
                        "recipe": recipe.name,
                        "plan_sha256": plan.sha256,
                        "required_devices": sorted(plan.required_devices),
                        "actions": len(plan.actions),
                        "points": plan.total_points,
                    },
                )
            except Exception as exc:
                self._record_case(
                    report,
                    audit,
                    "recipe.compile",
                    "Compile qualification recipe",
                    CaseStatus.FAILED,
                    RiskLevel.ENERGIZED,
                    error=str(exc),
                )
                return finish()

            adapters = {
                "rigol": RigolAdapter(self.settings),
                "keithley": KeithleyAdapter(self.settings),
                "anritsu": AnritsuAdapter(self.settings),
            }
            connected: list[str] = []
            writer: Hdf5RunWriter | None = None
            result = None
            try:
                identities: dict[str, str] = {}
                capabilities: dict[str, object] = {}
                for device in sorted(plan.required_devices):
                    adapter = adapters[device]
                    identity = adapter.connect()
                    connected.append(device)
                    identities[device] = identity.idn
                    capabilities[device] = adapter.capabilities
                result_path = (
                    self.output_directory
                    / "runs"
                    / f"{qualification_id}.h5"
                )
                writer = Hdf5RunWriter(
                    result_path,
                    recipe_source=source,
                    settings_source=self.settings_path.read_text(encoding="utf-8"),
                    plan_hash=plan.sha256,
                    device_idn=identities,
                    device_capabilities=capabilities,
                    expected_points=plan.total_points,
                    operator_context={
                        **self.policy.identity.as_context(),
                        "qualification_id": qualification_id,
                        "dummy_load_id": authorization.dummy_load_id,
                    },
                    run_attributes={
                        "qualification_id": qualification_id,
                        "risk_level": RiskLevel.ENERGIZED.value,
                        "dummy_load_id": authorization.dummy_load_id,
                    },
                )
                runner = RecipeRunner(
                    rigol=adapters["rigol"],
                    keithley=adapters["keithley"],
                    anritsu=adapters["anritsu"],
                    writer=writer,
                    policy=ExecutionPolicy.from_settings(self.settings),
                    on_event=lambda name, data: audit.record(
                        f"Qualification recipe event: {name}",
                        category="qualification",
                        event_type=name,
                        context=data,
                        critical=name in {"run_fault", "shutdown_error"},
                    ),
                )
                result = runner.run(plan)
                state = getattr(result.state, "value", str(result.state))
                status = (
                    CaseStatus.PASSED
                    if state == "safe"
                    else CaseStatus.FAILED
                )
                self._record_case(
                    report,
                    audit,
                    "recipe.execute",
                    "Execute qualification recipe",
                    status,
                    RiskLevel.ENERGIZED,
                    evidence={
                        "state": state,
                        "completed_actions": result.completed_actions,
                        "stored_points": result.stored_points,
                        "result_path": str(result_path),
                    },
                    error=result.error,
                )
            except Exception as exc:
                if writer is not None:
                    try:
                        writer.close("faulted")
                    except Exception:
                        pass
                self._record_case(
                    report,
                    audit,
                    "recipe.execute",
                    "Execute qualification recipe",
                    CaseStatus.FAILED,
                    RiskLevel.ENERGIZED,
                    error=str(exc),
                )
            finally:
                for device in reversed(connected):
                    adapter = adapters[device]
                    try:
                        if result is None:
                            adapter.emergency_off()
                    finally:
                        try:
                            adapter.disconnect()
                        except Exception as exc:
                            audit.record(
                                f"Qualification disconnect failed: {device}",
                                category="qualification",
                                event_type="disconnect_error",
                                context={"device": device, "error": str(exc)},
                                critical=True,
                            )
            return finish()
        except Exception:
            audit.close()
            raise

    def _case(
        self,
        report: QualificationReport,
        audit: AuditLogger,
        case_id: str,
        name: str,
        operation,
    ) -> None:
        started = time.monotonic()
        started_at = _utc_now()
        try:
            evidence = operation()
        except Exception as exc:
            self._record_case(
                report,
                audit,
                case_id,
                name,
                CaseStatus.FAILED,
                RiskLevel.PASSIVE,
                evidence={},
                error=str(exc),
                started_at=started_at,
                duration_s=time.monotonic() - started,
            )
        else:
            self._record_case(
                report,
                audit,
                case_id,
                name,
                CaseStatus.PASSED,
                RiskLevel.PASSIVE,
                evidence=evidence if isinstance(evidence, dict) else {"value": evidence},
                started_at=started_at,
                duration_s=time.monotonic() - started,
            )

    def _record_case(
        self,
        report: QualificationReport,
        audit: AuditLogger,
        case_id: str,
        name: str,
        status: CaseStatus,
        risk: RiskLevel,
        *,
        evidence: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: str | None = None,
        duration_s: float = 0.0,
    ) -> None:
        started_at = started_at or _utc_now()
        report.cases.append(
            CaseResult(
                case_id,
                name,
                risk,
                status,
                started_at,
                _utc_now(),
                duration_s,
                evidence or {},
                error,
            )
        )
        audit.record(
            f"Qualification case {case_id}: {status.value}",
            category="qualification",
            event_type="case_result",
            context={"case_id": case_id, "status": status.value, "error": error or ""},
            critical=True,
        )

    def _qualify_device(self, report, audit, settings, context, device, *, read_anritsu_trace):
        config = getattr(settings, device)
        if not config.enabled:
            for suffix in ("connect", "safe_shutdown"):
                self._record_case(
                    report, audit, f"{device}.{suffix}", f"{device} {suffix}",
                    CaseStatus.INCOMPLETE, RiskLevel.PASSIVE,
                    error=f"{device} is disabled in the station profile.",
                )
            return
        factory = SimulatedVisaFactory(device, context=context) if self.simulation else None
        adapter = {
            "rigol": RigolAdapter(settings, session_factory=factory),
            "keithley": KeithleyAdapter(settings, session_factory=factory),
            "anritsu": AnritsuAdapter(settings, session_factory=factory),
        }[device]
        connected = False
        try:
            def connect():
                nonlocal connected
                identity = adapter.connect()
                connected = True
                return {"identity": _identity_document(identity), "state": adapter.state.value}

            self._case(report, audit, f"{device}.connect", f"{device} connect", connect)
            if not connected:
                self._record_case(
                    report, audit, f"{device}.safe_shutdown", f"{device} safe shutdown",
                    CaseStatus.FAILED, RiskLevel.PASSIVE,
                    error="Safe shutdown was not attempted because connection failed.",
                )
                return
            if device == "keithley":
                self._case(
                    report, audit, "keithley.read_configuration", "Keithley read configuration",
                    lambda: {"configuration": str(adapter.read_configuration())},
                )
            if device == "anritsu":
                self._case(
                    report, audit, "anritsu.read_configuration", "Anritsu read configuration",
                    lambda: {"configuration": str(adapter.read_current_configuration())},
                )
                if read_anritsu_trace:
                    self._case(
                        report, audit, "anritsu.read_current_trace", "Anritsu read current trace",
                        lambda: {"points": len(adapter.fetch_current_trace().powers_dbm)},
                    )
            self._case(
                report,
                audit,
                f"{device}.safe_shutdown",
                f"{device} safe shutdown",
                lambda: self._safe_shutdown(adapter),
            )
        finally:
            try:
                adapter.disconnect()
            except Exception:
                # The safe-shutdown case already records the operational fault;
                # do not hide later devices behind a cleanup exception.
                pass

    @staticmethod
    def _safe_shutdown(adapter) -> dict[str, Any]:
        adapter.emergency_off()
        return {"state": adapter.state.value, "output_off": adapter.state.value in {"output_off", "verified"}}
