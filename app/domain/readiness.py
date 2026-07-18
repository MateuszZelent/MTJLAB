"""Pure station-readiness model used by the dashboard and run preflight UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import tempfile
from typing import Mapping

from app.engine.compiler import ExecutionPlan
from app.engine.estimation import PlanEstimate
from app.settings.models import StationSettings

_ENERGIZING_OUTPUT_ACTIONS = frozenset(
    {"set_rigol_output", "set_keithley_output", "set_anritsu_sg_output"}
)


class ReadinessLevel(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ReadinessItem:
    key: str
    label: str
    detail: str
    level: ReadinessLevel


@dataclass(frozen=True, slots=True)
class StationReadiness:
    items: tuple[ReadinessItem, ...]

    @property
    def blocking_items(self) -> tuple[ReadinessItem, ...]:
        return tuple(item for item in self.items if item.level is ReadinessLevel.FAIL)

    @property
    def ready(self) -> bool:
        return not self.blocking_items


def _probe_output_directory(raw_path: object) -> tuple[bool, str]:
    path = Path(str(raw_path or "./measurements")).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".labcontrol-probe-", dir=path, delete=True):
            pass
    except OSError as exc:
        return False, f"{path}: {exc}"
    return True, str(path.resolve())


def evaluate_station_readiness(
    settings: StationSettings,
    *,
    device_states: Mapping[str, str],
    verified_resources: Mapping[str, str],
    audit_healthy: bool,
    device_errors: Mapping[str, str] | None = None,
    plan: ExecutionPlan | None = None,
    estimate: PlanEstimate | None = None,
) -> StationReadiness:
    """Build a deterministic, presentation-independent readiness checklist."""

    items: list[ReadinessItem] = []
    approved = not settings.outputs_locked
    items.append(
        ReadinessItem(
            "profile",
            "Safety profile",
            (
                f"Approved: {settings.profile.name}"
                if approved
                else f"{settings.profile.state.title()}; energy-producing operations are locked"
            ),
            ReadinessLevel.PASS if approved else ReadinessLevel.FAIL,
        )
    )
    items.append(
        ReadinessItem(
            "audit",
            "Durable audit log",
            "Writable and active" if audit_healthy else "Unavailable; ARM and new runs are locked",
            ReadinessLevel.PASS if audit_healthy else ReadinessLevel.FAIL,
        )
    )

    required = plan.required_devices if plan is not None else frozenset()
    errors = device_errors or {}
    devices = (
        ("rigol", settings.rigol, settings.rigol.connection.resource),
        ("keithley", settings.keithley, settings.keithley.connection.resource),
        ("anritsu", settings.anritsu, settings.anritsu.connection.resource),
        ("moke_box", settings.moke_box, settings.moke_box.endpoint),
        (
            "lakeshore_gaussmeter",
            settings.lakeshore_gaussmeter,
            settings.lakeshore_gaussmeter.resource,
        ),
    )
    for device, configured, resource in devices:
        state = str(device_states.get(device, "disconnected"))
        is_required = device in required
        if not configured.enabled:
            level = ReadinessLevel.FAIL if is_required else ReadinessLevel.INFO
            detail = "Disabled in the current profile"
        elif not resource:
            level = ReadinessLevel.FAIL if is_required else ReadinessLevel.WARNING
            detail = "No connection resource assigned"
        elif state in {"output_on", "armed", "compliance", "fault", "unknown"}:
            level = ReadinessLevel.FAIL
            detail = f"Unsafe/manual state: {state.replace('_', ' ')}"
        elif device in errors:
            level = ReadinessLevel.FAIL if is_required else ReadinessLevel.WARNING
            detail = f"Last communication error: {errors[device]}"
        elif verified_resources.get(device) == resource:
            level = ReadinessLevel.PASS
            detail = f"Identity verified for {resource}; current state: {state.replace('_', ' ')}"
        elif is_required:
            level = ReadinessLevel.WARNING
            detail = f"Assigned to {resource}; Run Engine will re-verify identity before execution"
        else:
            level = ReadinessLevel.INFO
            detail = f"Assigned to {resource}; not required by the current plan"
        items.append(ReadinessItem(f"device.{device}", configured.display_name, detail, level))

    storage_ok, storage_detail = _probe_output_directory(
        settings.storage.get("output_directory", "./measurements")
    )
    items.append(
        ReadinessItem(
            "storage",
            "Results directory",
            storage_detail if storage_ok else f"Not writable: {storage_detail}",
            ReadinessLevel.PASS if storage_ok else ReadinessLevel.FAIL,
        )
    )

    if plan is None or estimate is None:
        items.append(
            ReadinessItem(
                "plan",
                "Measurement plan",
                "No compiled recipe; compile one to calculate point, time and disk estimates",
                ReadinessLevel.INFO,
            )
        )
    else:
        items.append(
            ReadinessItem(
                "plan",
                "Measurement plan",
                f"{len(plan.actions)} actions, {plan.total_points} checkpoints, "
                f"{plan.total_spectra} spectra, hash {plan.sha256[:12]}",
                ReadinessLevel.PASS,
            )
        )
        items.append(
            ReadinessItem(
                "estimate",
                "Run estimate",
                f"{estimate.nominal_duration_s:.2f} s nominal, "
                f"{estimate.total_upper_bytes / (1024 * 1024):.2f} MiB uncompressed upper model",
                ReadinessLevel.WARNING if estimate.warnings else ReadinessLevel.PASS,
            )
        )
        energized = any(
            action.kind in _ENERGIZING_OUTPUT_ACTIONS
            and bool(action.payload.get("enabled"))
            for action in plan.actions
        )
        items.append(
            ReadinessItem(
                "dut",
                "DUT safety declaration",
                (
                    "Compiler validated the recipe DUT envelope against approved laboratory limits"
                    if energized
                    else "Plan contains no OUTPUT ON action"
                ),
                ReadinessLevel.PASS,
            )
        )
    return StationReadiness(tuple(items))
