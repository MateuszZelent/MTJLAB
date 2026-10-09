"""Synchronous execution core intended to run inside one dedicated Qt worker."""

from __future__ import annotations

from collections import deque
from contextlib import nullcontext, contextmanager, ExitStack
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import math
import threading
import time
from typing import Callable
from uuid import uuid4
from enum import Enum

from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter, SpectrumTrace
from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.calibration import MokeCalibration
from app.safety.moke_box import MokeVoltagePlan
from app.devices.lakeshore_475.adapter import LakeShore475Adapter
from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.domain.errors import ConfigurationError, DeviceError, ExecutionError
from app.domain.models import ApplicationState, DeviceState, MeasurementPoint
from app.domain.recipe_spectrum import RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole
from app.engine.compiler import ExecutionPlan, PlanAction, controlled_output_endpoints, required_devices_for_actions
from app.engine.policy import ExecutionPolicy
from app.engine.recovery import RecoveredReference
from app.spectrum import (
    LinearPowerAverager,
    apply_reference_operation,
    frequency_grids_match,
)
from app.storage.hdf5_writer import Hdf5RunWriter


EventCallback = Callable[[str, dict[str, object]], None]
TelemetryCallback = Callable[[str, dict[str, object]], None]


class ExecutionMode(str, Enum):
    """Hardware execution policy selected for one immutable run."""

    MEASUREMENT = "measurement"
    DRY_RUN = "dry_run"
    MANUAL_STEP = "manual_step"

    @classmethod
    def coerce(cls, value: "ExecutionMode | str") -> "ExecutionMode":
        """Read the current mode and the retired demo provenance value.

        ``demo_outputs_off`` may be present in immutable run files written by
        earlier releases.  It has exactly the same safety semantics as a dry
        run, so historical recovery must fail closed rather than fall back to
        a normal measurement.
        """

        if value == "demo_outputs_off":
            return cls.DRY_RUN
        return cls(value)


@dataclass(frozen=True, slots=True)
class RunResult:
    state: ApplicationState
    completed_actions: int
    stored_points: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _AcquiredSpectrum:
    raw: SpectrumTrace
    average_count: int = 1
    processed_values: tuple[float, ...] | None = None
    processed_unit: str | None = None
    processing_operation: str = "none"
    reference_index: int | None = None
    source_sweep_indices: tuple[int, ...] = ()
    processing_metadata: dict | None = None
    history_dbm: tuple = ()


class RecipeRunner:
    """Execute an already preflighted plan and always attempt safe shutdown."""

    def __init__(
        self,
        *,
        rigol: RigolAdapter,
        keithley: KeithleyAdapter,
        anritsu: AnritsuAdapter,
        moke_box: MokeBoxAdapter | None = None,
        lakeshore: LakeShore475Adapter | None = None,
        writer: Hdf5RunWriter,
        on_event: EventCallback | None = None,
        on_telemetry: TelemetryCallback | None = None,
        policy: ExecutionPolicy | None = None,
        execution_mode: ExecutionMode = ExecutionMode.MEASUREMENT,
    ) -> None:
        self._rigol = rigol
        self._keithley = keithley
        self._anritsu = anritsu
        self._moke_box = moke_box
        self._moke_voltage_plan: MokeVoltagePlan | None = None
        self._moke_calibration: MokeCalibration | None = None
        self._moke_owns_output = False
        self._moke_owned_channels: set[int] = set()
        self._lakeshore = lakeshore
        self._writer = writer
        self._on_event = on_event or (lambda _name, _data: None)
        self._on_telemetry = on_telemetry or (lambda _name, _data: None)
        self._policy = policy or ExecutionPolicy()
        self._execution_mode = ExecutionMode.coerce(execution_mode)
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_requested = threading.Event()
        self._resume_requested.set()
        self._manual_step_permit = threading.Event()
        self._state = ApplicationState.SAFE
        self._required_devices: frozenset[str] = frozenset()
        self._safe_shutdown_actions: tuple[str, ...] = ()
        self._shutdown_deadline: float | None = None
        self._active_safety_context: dict[str, dict[str, object]] = {}
        self._device_states: dict[str, dict[str, object]] = {}
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._output_status = self._unknown_output_status()
        self._keithley_zeroed = {"A": True, "B": True}
        self._anritsu_sg_output_active = False
        self._anritsu_owns_output = False
        self._last_safe_boundary_points = 0
        self._watchdog_lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        self._watchdog_timed_out = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_action: dict[str, object] | None = None
        self._watchdog_deadline_monotonic = 0.0
        self._watchdog_started_monotonic = 0.0
        self._correlation_id = ""
        self._reference_trace: SpectrumTrace | None = None
        self._reference_index: int | None = None
        self._reference_fingerprint = None
        self._reference_origin = {}
        # Last engine-confirmed value for each semantic axis.  Point metadata
        # uses this snapshot so requested/applied/readback provenance remains
        # explicit even when the checkpoint action itself is an acquisition.
        self._semantic_confirmations: dict[str, dict[str, object]] = {}
        self._setpoint_confirmed_at: dict[str, str] = {}
        self._executed_settling_times: dict[str, float] = {}

    @property
    def state(self) -> ApplicationState:
        return self._state

    def request_stop(self) -> None:
        self._stop_requested.set()
        self._manual_step_permit.set()

    def pause_after_point(self) -> None:
        self._pause_requested.set()

    def resume(self) -> None:
        self._pause_requested.clear()
        self._resume_requested.set()

    def advance_manual_step(self) -> None:
        """Permit exactly the stage currently presented to the operator."""

        if self._execution_mode is ExecutionMode.MANUAL_STEP:
            self._manual_step_permit.set()

    def run(
        self,
        plan: ExecutionPlan,
        *,
        start_action_index: int = 0,
        stored_points: int = 0,
        recovery_prelude: tuple[PlanAction, ...] = (),
        recovery_reference: RecoveredReference | None = None,
    ) -> RunResult:
        if start_action_index < 0 or start_action_index > len(plan.actions):
            raise ExecutionError("Invalid recovery action index.")
        if stored_points < 0 or stored_points > plan.total_points:
            raise ExecutionError("Invalid recovered point count.")
        completed = start_action_index
        completed_retained_outputs: set[str] = set()
        stored = stored_points
        point_measurements: dict[str, float] = {}
        point_setpoints: dict[str, float] = {}
        run_started_monotonic = time.monotonic()
        self._required_devices = (
            plan.required_devices | required_devices_for_actions(plan.actions)
        )
        self._resume_output_scope = controlled_output_endpoints(plan.actions)
        self._anritsu_owns_output = "anritsu.sg" in self._resume_output_scope
        self._safe_shutdown_actions = plan.safe_shutdown_actions
        self._active_safety_context = {}
        self._device_states = {}
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._output_status = {
            endpoint: state for endpoint, state in self._unknown_output_status().items()
            if endpoint.split(".", 1)[0] in self._required_devices
            and (not endpoint.startswith("anritsu.") or self._anritsu_owns_output)
        }
        self._keithley_zeroed = {"A": True, "B": True}
        self._anritsu_sg_output_active = False
        self._last_safe_boundary_points = stored_points
        self._watchdog_timed_out.clear()
        self._moke_voltage_plan = None
        self._moke_calibration = None
        self._moke_owns_output = False
        self._moke_owned_channels.clear()
        self._shutdown_deadline = None
        self._reference_trace = None
        self._reference_index = None
        self._reference_fingerprint = None
        self._reference_origin = {}
        self._semantic_confirmations = {}
        self._setpoint_confirmed_at = {}
        self._executed_settling_times = {}
        self._manual_step_permit.clear()
        self._correlation_id = str(uuid4())
        self._start_watchdog()
        self._state = ApplicationState.RUNNING
        current_action: PlanAction | None = None
        attempted_finally: set[str] = set()
        try:
            self._emit(
                "run_started",
                {
                    "recipe": plan.recipe_name,
                    "actions": len(plan.actions),
                    "hash": plan.sha256,
                    "start_action_index": start_action_index,
                    "stored_points": stored_points,
                    "execution_mode": self._execution_mode.value,
                    "outputs_forced_off": self.outputs_forced_off,
                    "output_guard_devices": self._output_guard_devices(),
                },
            )
            # Lock external inputs before any recipe action can energize a source.
            from app.storage.recipe_reference import file_sha256

            reference_available = recovery_reference is not None
            verified_assets = set()
            for planned in plan.actions[start_action_index:]:
                self._raise_if_stop_requested()
                if planned.kind == "acquire_reference":
                    reference_available = True
                if (planned.kind == "acquire_spectrum"
                        and planned.payload.get("reference_operation", "none") != "none"
                        and not reference_available):
                    raise ExecutionError("Resume requires a reference step before corrected acquisition; restart from a reference boundary.")
                if planned.payload.get("source_file"):
                    asset = (planned.payload["source_file"], planned.payload["source_sha256"])
                    if asset not in verified_assets and file_sha256(asset[0]) != asset[1]:
                        raise ExecutionError("Reference file changed after preflight; recompile the recipe.")
                    verified_assets.add(asset)
            if self.outputs_forced_off:
                self._confirm_dry_run_outputs_off()
            for action in recovery_prelude:
                if action.kind not in {
                    "configure_rigol",
                    "configure_rigol_output",
                    "configure_keithley",
                    "configure_anritsu",
                    "configure_anritsu_advanced",
                    "configure_anritsu_sg",
                }:
                    raise ExecutionError(
                        f"Unsafe recovery prelude action: {action.kind!r}."
                    )
                self._emit(
                    "recovery_prelude_started",
                    {"node_id": action.node_id, "kind": action.kind},
                )
                self._execute_with_policy(action, point_measurements)
                self._emit(
                    "recovery_prelude_finished",
                    {"node_id": action.node_id, "kind": action.kind},
                )
            if recovery_reference is not None:
                identity = self._read_spectrum_identity()
                if identity is None or identity[3] != recovery_reference.fingerprint:
                    raise ExecutionError("Recovered reference differs from the confirmed analyzer settings; acquire a new reference.")
                self._reference_trace = recovery_reference.trace
                self._reference_index = recovery_reference.index
                self._reference_fingerprint = recovery_reference.fingerprint
                self._reference_origin = dict(recovery_reference.origin)
                self._emit("reference_recovered", {"reference_index": self._reference_index,
                    "configuration_fingerprint": self._reference_fingerprint})
            for action_index, action in enumerate(
                plan.actions[start_action_index:], start=start_action_index
            ):
                self._raise_if_stop_requested()
                if action.fault_only:
                    completed = action_index + 1
                    self._emit("action_skipped", {"node_id": action.node_id, "kind": action.kind,
                                                  "reason": "fault/Stop cleanup overridden by successful final state"})
                    continue
                current_action = action
                if action.is_finally:
                    attempted_finally.add(action.node_id)
                if (
                    self._execution_mode is ExecutionMode.MANUAL_STEP
                    and not action.is_finally
                ):
                    self._wait_for_manual_step(action, action_index, len(plan.actions))
                started_event = {
                    "node_id": action.node_id,
                    "kind": action.kind,
                    **self._action_display_context(action),
                    "action_index": action_index,
                    "total_actions": len(plan.actions),
                    "setpoints_si": dict(action.setpoints_si),
                    "deadline_s": self._policy.deadline_for(action),
                    "cancellation_requested": self._stop_requested.is_set(),
                }
                if action.kind == "wait":
                    started_event["duration_s"] = float(action.payload["duration_s"])
                if action.semantic_id:
                    started_event["semantic_id"] = action.semantic_id
                    axis_context = self._axis_context_payload(action)
                    if axis_context is not None:
                        started_event["axis_context"] = axis_context
                    requested = (
                        action.payload.get("duration_s")
                        if action.kind == "wait"
                        else action.payload.get("requested_si")
                    )
                    if not isinstance(requested, (int, float)) and action.axis_context is not None:
                        requested = action.axis_context.value_si
                    if isinstance(requested, (int, float)):
                        started_event["requested_si"] = float(requested)
                self._emit("action_started", started_event)
                semantic_started = self._semantic_event_data(
                    action,
                    action_index=action_index,
                    total_actions=len(plan.actions),
                    phase="running",
                )
                if semantic_started is not None:
                    self._emit("semantic_operation_started", semantic_started)
                acquisition = self._execute_with_policy(action, point_measurements)
                if action.completion_only and action.retained_output is not None:
                    completed_retained_outputs.add(action.retained_output)
                if action.semantic_id:
                    applied_si, readback_si = self._confirmed_semantic_value(action)
                    semantic_applied = self._semantic_event_data(
                        action,
                        action_index=action_index,
                        total_actions=len(plan.actions),
                        phase="applied",
                        applied_si=applied_si,
                        readback_si=readback_si,
                        verification=(
                            "timed_wait" if action.kind == "wait"
                            else "readback" if readback_si is not None else "executed"
                        ),
                    )
                    if semantic_applied is not None:
                        self._remember_semantic_confirmation(action, semantic_applied)
                        self._emit("semantic_operation_applied", semantic_applied)
                point_setpoints.update(action.setpoints_si)
                self._apply_actual_setpoints(point_setpoints)
                completed = action_index + 1
                compliance_channels = self._compliance_channels(point_measurements)
                if compliance_channels:
                    point = MeasurementPoint(
                        index=stored,
                        setpoints=dict(point_setpoints),
                        measurements=dict(point_measurements),
                        status="compliance",
                        metadata={
                            "plan_node": action.node_id,
                            "monotonic_s": time.monotonic(),
                            "compliance_channels": compliance_channels,
                            "safety_context": self._safety_context_snapshot(),
                            "execution_mode": self._execution_mode.value,
                            "outputs_forced_off": self.outputs_forced_off,
                            "output_guard_devices": self._output_guard_devices(),
                            **self._semantic_point_metadata(action),
                        },
                    )
                    write_started = time.monotonic()
                    self._append_checkpoint(point)
                    write_elapsed = time.monotonic() - write_started
                    stored += 1
                    self._emit_point_stored(
                        point,
                        stored=stored,
                        run_started_monotonic=run_started_monotonic,
                        write_elapsed_s=write_elapsed,
                        spectrum_points=0,
                    )
                    point_measurements.clear()
                    self._emit("compliance_detected", {"channels": compliance_channels, "point_index": stored - 1})
                    raise ExecutionError("Keithley reached compliance; the final checkpoint was saved and output was disabled.")
                if acquisition is not None or action.kind == "checkpoint" or (
                    action.kind in {"measure_moke_hall", "measure_lakeshore_field"}
                    and bool(action.payload.get("checkpoint", True))
                ):
                    trace = acquisition.raw if acquisition is not None else None
                    point = MeasurementPoint(
                        index=stored,
                        setpoints=dict(point_setpoints),
                        measurements=dict(point_measurements),
                        metadata={
                            "plan_node": action.node_id,
                            "checkpoint_label": action.payload.get("label", action.node_id),
                            "monotonic_s": time.monotonic(),
                            "safety_context": self._safety_context_snapshot(),
                            "spectrum_processing": (
                                acquisition.processing_operation
                                if acquisition is not None
                                else "none"
                            ),
                            "spectrum_average_count": (
                                acquisition.average_count
                                if acquisition is not None
                                else 1
                            ),
                            "spectrum_processing_v1": acquisition.processing_metadata if acquisition is not None else None,
                            "raw_recipe_sweep_indices": (
                                acquisition.source_sweep_indices if acquisition is not None else ()
                            ),
                            "reference_index": (
                                acquisition.reference_index
                                if acquisition is not None
                                else None
                            ),
                            "execution_mode": self._execution_mode.value,
                            "outputs_forced_off": self.outputs_forced_off,
                            "output_guard_devices": self._output_guard_devices(),
                            **self._semantic_point_metadata(action),
                        },
                    )
                    write_started = time.monotonic()
                    if (
                        acquisition is not None
                        and acquisition.processed_values is not None
                    ):
                        self._append_checkpoint(
                            point, trace,
                            processed_values=acquisition.processed_values,
                            processed_unit=acquisition.processed_unit,
                            processing_operation=acquisition.processing_operation,
                        )
                    else:
                        # Keep the writer protocol compatible with lightweight
                        # diagnostic writers and existing raw-only storage.
                        self._append_checkpoint(point, trace)
                    write_elapsed = time.monotonic() - write_started
                    stored += 1
                    self._emit_point_stored(
                        point,
                        stored=stored,
                        run_started_monotonic=run_started_monotonic,
                        write_elapsed_s=write_elapsed,
                        spectrum_points=(len(trace.powers_dbm) if trace is not None else 0),
                    )
                    if trace is not None:
                        self._emit_spectrum_preview(trace, point.index, acquisition=acquisition)
                    point_measurements.clear()
                    self._pause_at_point_if_requested()
                self._emit(
                    "action_finished",
                    {
                        "node_id": action.node_id,
                        "kind": action.kind,
                        **self._action_display_context(action),
                        "semantic_id": action.semantic_id,
                        "axis_context": self._axis_context_payload(action),
                        **(
                            {"duration_s": float(action.payload["duration_s"])}
                            if action.kind == "wait"
                            else {}
                        ),
                    },
                )
                current_action = None
                self._record_safe_boundary_if_advanced(
                    stored_points=stored,
                    next_action_index=completed,
                    plan_hash=plan.sha256,
                )
            retained_outputs = plan.retained_outputs if not self.outputs_forced_off else frozenset()
            if retained_outputs and completed_retained_outputs != set(retained_outputs):
                raise ExecutionError("Every retained output requires a confirmed completion action in this run.")
            if not self._safe_shutdown(retained_outputs=retained_outputs, successful_completion=True):
                raise ExecutionError("Safe shutdown was not confirmed for every instrument.")
            self._state = (ApplicationState.HOLDING if retained_outputs else
                           ApplicationState.UNKNOWN if self._moke_owns_output else ApplicationState.SAFE)
            completion = self._emit("run_completed", {"completed_actions": completed, "stored_points": stored,
                                                       "retained_outputs": sorted(retained_outputs),
                                                       "storage_validation_pending": True}, notify=False)
            self._writer.close("completed")
            self._on_event("run_completed", {**completion, "storage_validation_pending": False})
            return RunResult(self._state, completed, stored)
        except Exception as exc:
            if self._stop_requested.is_set() and not self._watchdog_timed_out.is_set():
                return self._abort_safely(
                    plan,
                    completed,
                    stored,
                    point_measurements,
                    str(exc),
                    attempted_finally=attempted_finally,
                )
            self._state = ApplicationState.FAULT
            # A failed action leaves output state indeterminate until the
            # emergency-off sequence itself confirms a safe state.
            self._mark_output_unknown()
            if current_action is not None:
                semantic_failed = self._semantic_event_data(
                    current_action,
                    action_index=max(0, completed),
                    total_actions=len(plan.actions),
                    phase="failed",
                )
                if semantic_failed is not None:
                    semantic_failed["error"] = str(exc)
                    self._emit_after_fault("semantic_operation_failed", semantic_failed)
                self._emit_after_fault(
                    "action_failed",
                    {
                        "node_id": current_action.node_id,
                        "kind": current_action.kind,
                        "error": str(exc),
                    },
                )
            self._emit_after_fault("run_fault", {"error": str(exc)})
            self._run_pending_finally(
                plan,
                point_measurements,
                attempted_node_ids=attempted_finally,
            )
            self._safe_shutdown()
            self._state = ApplicationState.FAULT
            error = self._close_after_error("faulted", str(exc))
            return RunResult(self._state, completed, stored, error)
        finally:
            self._stop_watchdog()

    def _execute_with_policy(
        self,
        action: PlanAction,
        measurements: dict[str, float],
    ) -> _AcquiredSpectrum | None:
        if action.completion_only:
            self._raise_if_stop_requested()
        retries = self._policy.retry_count if self._can_retry(action) else 0
        for attempt in range(retries + 1):
            deadline_s = self._policy.deadline_for(action)
            self._begin_action_watchdog(
                action, attempt=attempt + 1, deadline_s=deadline_s
            )
            started = time.monotonic()
            try:
                affected_targets = self._mutated_setpoint_targets(action)
                for axis_id, confirmation in tuple(self._semantic_confirmations.items()):
                    if confirmation.get("target") in affected_targets:
                        del self._semantic_confirmations[axis_id]
                for target in affected_targets:
                    self._setpoint_confirmed_at.pop(target, None)
                    self._executed_settling_times.pop(target, None)
                adapter = self._adapter_for_action(action)
                if adapter is None:
                    result = self._execute(action, measurements)
                else:
                    scope = getattr(adapter, "operation_timeout", None)
                    remaining_s = deadline_s - (time.monotonic() - started)
                    if remaining_s <= 0:
                        raise ExecutionError("Action deadline expired before adapter dispatch.")
                    operation = scope(remaining_s) if callable(scope) else nullcontext()
                    with operation, adapter.io_timeout(self._policy.command_timeout_s):
                        result = self._execute(action, measurements)
                elapsed = time.monotonic() - started
                if elapsed > deadline_s:
                    self._flag_watchdog_timeout(action, attempt + 1, elapsed, deadline_s)
                    raise ExecutionError(
                        f"Action {action.node_id!r} exceeded its {deadline_s:.3g} s deadline."
                    )
                confirmed_at = datetime.now(timezone.utc).isoformat()
                for target in affected_targets:
                    if self._actual_setpoint_value(target) is not None:
                        self._setpoint_confirmed_at[target] = confirmed_at
                return result
            except DeviceError as exc:
                if self._watchdog_timed_out.is_set() or attempt >= retries:
                    raise
                self._emit(
                    "action_retry",
                    {
                        "node_id": action.node_id,
                        "kind": action.kind,
                        "attempt": attempt + 2,
                        "maximum_attempts": retries + 1,
                        "error": str(exc),
                        "backoff_s": self._policy.retry_backoff_s,
                    },
                )
                self._interruptible_wait(self._policy.retry_backoff_s)
            finally:
                self._clear_action_watchdog()
        raise ExecutionError(f"Action {action.node_id!r} exhausted its retry policy.")

    def _can_retry(self, action: PlanAction) -> bool:
        """Retry only operations that cannot create a second energizing transition."""

        if action.kind == "configure_rigol":
            return not self._rigol_output_active[action.payload["config"].channel]
        if action.kind == "configure_rigol_output":
            return not self._rigol_output_active[action.payload["config"].channel]
        if action.kind == "configure_keithley":
            return not self._keithley_output_active[action.payload["request"].channel]
        return self._policy.retry_candidate(action)

    def _adapter_for_action(self, action: PlanAction):
        if action.kind == "verify_connection":
            return {
                "rigol": self._rigol,
                "keithley": self._keithley,
                "anritsu": self._anritsu,
            }[str(action.payload["device"])]
        if "rigol" in action.kind:
            return self._rigol
        if "keithley" in action.kind:
            return self._keithley
        if "moke" in action.kind:
            return self._moke_box
        if "lakeshore" in action.kind:
            return self._lakeshore
        if "anritsu" in action.kind or action.kind in {
            "acquire_reference",
            "acquire_spectrum",
        }:
            return self._anritsu
        return None

    @staticmethod
    def _action_display_context(action: PlanAction) -> dict[str, object]:
        """Expose only routing metadata needed by the read-only device views."""
        kind = action.kind
        device = (
            "rigol"
            if "rigol" in kind
            else "keithley"
            if "keithley" in kind
            else "anritsu"
            if "anritsu" in kind or kind in {"acquire_reference", "acquire_spectrum"}
            else "moke_box"
            if "moke" in kind
            else "lakeshore"
            if "lakeshore" in kind
            else ""
        )
        context: dict[str, object] = {"device": device} if device else {}
        if kind.startswith("update_") and kind != "update_moke_voltage":
            context["transition_policy"] = "direct_setpoint"
        elif kind == "update_moke_voltage" or kind == "ramp_keithley_to_zero":
            context["transition_policy"] = "qualified_ramp"
        elif kind in {"configure_keithley", "configure_rigol", "configure_anritsu_sg"}:
            context["transition_policy"] = "configuration_with_output_off"
        channel = action.payload.get("channel")
        if channel is None:
            config = action.payload.get("config")
            request = action.payload.get("request")
            channel = getattr(config, "channel", getattr(request, "channel", None))
        if channel is not None:
            context["channel"] = channel
        return context

    @staticmethod
    def _axis_context_payload(action: PlanAction) -> dict[str, object] | None:
        context = action.axis_context
        if context is None:
            return None
        return {
            "axis_id": context.axis_id,
            "point_index": context.point_index,
            "point_count": context.point_count,
            "stage_index": context.stage_index,
            "value_si": context.value_si,
            "active_setpoints_si": dict(context.active_setpoints_si),
            "loop_path": list(context.loop_path),
        }

    @staticmethod
    def _mutated_setpoint_targets(action: PlanAction) -> set[str]:
        """Invalidate proofs only for physical fields this action can overwrite."""
        payload = action.payload
        if action.kind == "wait":
            target = payload.get("target")
            return {target} if isinstance(target, str) and target.endswith(".settling_time") else set()
        if action.kind == "configure_keithley":
            request = payload["request"]
            prefix = f"keithley.{request.channel}."
            fields = request.changed_fields
            tails = set()
            if fields is None or "mode" in fields or "level_si" in fields:
                tails.update(("current", "voltage", "level"))
            if fields is None or "mode" in fields or "compliance_si" in fields:
                tails.update(("compliance_voltage", "compliance_current"))
            if fields is None or "settle_time_s" in fields:
                tails.add("settling_time")
            return {prefix + tail for tail in tails}
        if action.kind in {"update_keithley_level", "update_keithley_compliance"}:
            tails = ("current", "voltage", "level") if action.kind.endswith("level") else ("compliance_voltage", "compliance_current")
            return {f"keithley.{payload['channel']}.{tail}" for tail in tails}
        if action.kind in {"configure_rigol", "update_rigol_frequency", "update_rigol_levels"}:
            channel = payload["config"].channel if action.kind == "configure_rigol" else payload["channel"]
            tails = {"frequency"} if action.kind.endswith("frequency") else {"high_level", "low_level", "amplitude", "offset"}
            if action.kind == "configure_rigol":
                tails.add("frequency")
            return {f"rigol.{channel}.{tail}" for tail in tails}
        if action.kind == "configure_anritsu":
            mask = payload["config"].changed_fields
            names = {"start_hz": "start_frequency", "stop_hz": "stop_frequency", "reference_level_dbm": "reference_level", "points": "points"}
            return {f"anritsu.spectrum.{tail}" for field, tail in names.items() if mask is None or field in mask}
        if action.kind in {"configure_anritsu_sg", "update_anritsu_sg"}:
            mask = payload["config"].changed_fields
            return {f"anritsu.sg.{tail}" for field, tail in (("frequency_hz", "frequency"), ("power_dbm", "power")) if mask is None or field in mask}
        if action.kind in {"configure_moke_box", "update_moke_voltage", "stop_moke_voltage"}:
            if action.kind == "stop_moke_voltage":
                if "channel" in payload:
                    return {f"moke_box.vout{payload['channel']}.voltage"}
                return {f"moke_box.vout{channel}.voltage" for channel in range(8)}
            channel = payload["profile"].channel if action.kind == "configure_moke_box" else payload["channel"]
            return {f"moke_box.vout{channel}.voltage"}
        return set()

    def _remember_semantic_confirmation(
        self,
        action: PlanAction,
        event: dict[str, object],
    ) -> None:
        """Cache one confirmed axis operation for the following checkpoint."""

        context = action.axis_context
        if context is None or not str(action.semantic_id or "").endswith(".set-roi-value"):
            return
        self._semantic_confirmations[context.axis_id] = {
            "target": action.payload.get("target"),
            "confirmed_at_utc": datetime.now(timezone.utc).isoformat(),
            "semantic_operation_id": action.semantic_id,
            "requested_si": event.get("requested_si"),
            "applied_si": event.get("applied_si"),
            "readback_si": event.get("readback_si"),
            "verification": event.get("verification"),
        }

    def _semantic_point_metadata(self, action: PlanAction) -> dict[str, object]:
        """Return additive, explicit provenance for a stored checkpoint."""

        context = action.axis_context
        metadata: dict[str, object] = {
            "semantic_operation_id": action.semantic_id,
            "axis_context": self._axis_context_payload(action),
            "setpoint_evidence_v1": self._setpoint_evidence(action.setpoints_si),
            "safety_sampling_policy": "active_smu_iv_before_each_raw_frame",
        }
        if context is None:
            return metadata
        metadata["axis_confirmations_v1"] = {
            axis_id: dict(confirmation)
            for axis_id, confirmation in self._semantic_confirmations.items()
            if confirmation.get("target") in context.active_setpoints_si
        }
        confirmation = self._semantic_confirmations.get(context.axis_id, {})
        metadata.update(
            {
                "axis_id": context.axis_id,
                "stage_index": context.stage_index,
                "point_index": context.point_index,
                "loop_path": list(context.loop_path),
                "requested_si": confirmation.get("requested_si", context.value_si),
                "applied_si": confirmation.get("applied_si"),
                "readback_si": confirmation.get("readback_si"),
                "verification": confirmation.get("verification"),
            }
        )
        # The axis operation is the semantic operation that established the
        # checkpoint setpoint.  Keep the checkpoint/action ID separately so
        # consumers can still trace the acquisition node when needed.
        operation_id = confirmation.get("semantic_operation_id")
        if isinstance(operation_id, str) and operation_id:
            metadata["semantic_operation_id"] = operation_id
            metadata["checkpoint_operation_id"] = action.semantic_id
        return metadata

    def _setpoint_evidence(self, requested: dict[str, float]) -> dict[str, dict[str, object]]:
        from app.recipes.parameter_registry import persisted_quantity_unit

        evidence = {}
        for target, value in requested.items():
            readback = self._actual_setpoint_value(target)
            applied = readback
            verification = "readback" if readback is not None else "unconfirmed"
            if target.startswith("moke_box.vout"):
                applied = self._active_safety_context.get("moke_box", {}).get("applied_v")
                verification = "dac_register_readback" if readback is not None else "unconfirmed"
            if target.endswith(".settling_time"):
                applied = self._executed_settling_times.get(target)
                readback = None
                verification = "executed_wait" if applied is not None else "unconfirmed"
            evidence[target] = {
                "requested_si": value, "applied_si": applied, "readback_si": readback,
                "unit": persisted_quantity_unit(target), "verification": verification,
                "confirmed_at_utc": self._setpoint_confirmed_at.get(target),
            }
        return evidence

    def _semantic_event_data(
        self,
        action: PlanAction,
        *,
        action_index: int,
        total_actions: int,
        phase: str,
        applied_si: float | None = None,
        readback_si: float | None = None,
        verification: str | None = None,
    ) -> dict[str, object] | None:
        if not action.semantic_id:
            return None
        requested = (
            action.payload.get("duration_s")
            if action.kind == "wait"
            else action.payload.get("requested_si")
        )
        if not isinstance(requested, (int, float)) and action.axis_context is not None:
            requested = action.axis_context.value_si
        data: dict[str, object] = {
            "semantic_id": action.semantic_id,
            "phase": phase,
            "node_id": action.node_id,
            "kind": action.kind,
            "action_index": action_index,
            "total_actions": total_actions,
            "setpoints_si": dict(action.setpoints_si),
        }
        data.update(self._action_display_context(action))
        for key in ("duration_s", "trace", "reference_operation"):
            if key in action.payload:
                data[key] = action.payload[key]
        axis_context = self._axis_context_payload(action)
        if axis_context is not None:
            data["axis_context"] = axis_context
        if isinstance(requested, (int, float)):
            data["requested_si"] = float(requested)
        if applied_si is not None:
            data["applied_si"] = float(applied_si)
        if readback_si is not None:
            data["readback_si"] = float(readback_si)
        if verification is not None:
            data["verification"] = verification
        return data

    def _confirmed_semantic_value(self, action: PlanAction) -> tuple[float | None, float | None]:
        """Read the applied/readback value from the confirmed runtime context."""

        if not action.semantic_id:
            return None, None
        requested = (
            action.payload.get("duration_s")
            if action.kind == "wait"
            else action.payload.get("requested_si")
        )
        if not isinstance(requested, (int, float)) and action.axis_context is not None:
            requested = action.axis_context.value_si
        target = str(action.payload.get("target", ""))
        if action.axis_context is not None and action.axis_context.loop_path:
            # The active setpoint map is explicit; select the axis target by
            # matching the requested value, with payload metadata as fallback.
            candidates = action.axis_context.active_setpoints_si
            target = str(action.payload.get("target", ""))
            if not target:
                for key, value in candidates.items():
                    if isinstance(requested, (int, float)) and math.isclose(float(value), float(requested), rel_tol=0.0, abs_tol=1e-15):
                        target = str(key)
                        break
        applied: object | None = None
        if action.kind == "update_keithley_level":
            applied = self._active_safety_context.get(
                f"keithley.{action.payload.get('channel')}", {}
            ).get("source_level_si", applied)
        elif action.kind == "update_keithley_compliance":
            applied = self._active_safety_context.get(
                f"keithley.{action.payload.get('channel')}", {}
            ).get("compliance_si", applied)
        elif action.kind == "update_rigol_frequency":
            applied = self._active_safety_context.get(
                f"rigol.{action.payload.get('channel')}", {}
            ).get("frequency_hz", applied)
        elif action.kind == "update_rigol_levels":
            actual = self._active_safety_context.get(
                f"rigol.{action.payload.get('channel')}", {}
            )
            if target.endswith(("amplitude", "offset")):
                applied = self._actual_setpoint_value(target)
            elif target.endswith("low_level"):
                applied = actual.get("low_level_v", applied)
            else:
                applied = actual.get("high_level_v", applied)
        elif action.kind in {"update_anritsu_sg", "configure_anritsu_sg"}:
            actual = self._active_safety_context.get("anritsu.sg", {})
            applied = actual.get(
                "power_dbm" if target.endswith("power") else "frequency_hz", applied
            )
        elif action.kind == "configure_anritsu":
            readback = self._actual_setpoint_value(target)
            if readback is not None:
                return readback, readback
        elif action.kind == "wait":
            applied = action.payload.get("duration_s", applied)
            return float(applied), None
        elif action.kind == "update_moke_voltage":
            actual = self._active_safety_context.get("moke_box", {})
            return actual.get("applied_v"), actual.get("actual_v")
        if isinstance(applied, (int, float)):
            return float(applied), float(applied)
        planned = action.payload.get("applied_si", requested)
        return (float(planned) if isinstance(planned, (int, float)) else None), None

    def _start_watchdog(self) -> None:
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="recipe-run-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread, self._watchdog_thread = self._watchdog_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, self._policy.heartbeat_interval_s * 2))
        self._clear_action_watchdog()

    def _begin_action_watchdog(
        self, action: PlanAction, *, attempt: int, deadline_s: float
    ) -> None:
        now = time.monotonic()
        with self._watchdog_lock:
            self._watchdog_action = {
                "node_id": action.node_id,
                "kind": action.kind,
                "attempt": attempt,
                "deadline_s": deadline_s,
            }
            self._watchdog_started_monotonic = now
            self._watchdog_deadline_monotonic = now + deadline_s

    def _clear_action_watchdog(self) -> None:
        with self._watchdog_lock:
            self._watchdog_action = None
            self._watchdog_deadline_monotonic = 0.0
            self._watchdog_started_monotonic = 0.0

    def _watchdog_loop(self) -> None:
        interval = self._policy.heartbeat_interval_s
        while not self._watchdog_stop.wait(interval):
            with self._watchdog_lock:
                action = dict(self._watchdog_action) if self._watchdog_action else None
                deadline = self._watchdog_deadline_monotonic
                started = self._watchdog_started_monotonic
            if action is None:
                continue
            now = time.monotonic()
            elapsed = max(0.0, now - started)
            self._emit_telemetry(
                "runner_heartbeat",
                {
                    **action,
                    "elapsed_s": elapsed,
                    "remaining_s": max(0.0, deadline - now),
                },
            )
            if now >= deadline:
                self._flag_watchdog_timeout(
                    PlanAction(
                        node_id=str(action["node_id"]),
                        kind=str(action["kind"]),
                        payload={},
                        setpoints_si={},
                    ),
                    int(action["attempt"]),
                    elapsed,
                    float(action["deadline_s"]),
                )

    def _flag_watchdog_timeout(
        self,
        action: PlanAction,
        attempt: int,
        elapsed_s: float,
        deadline_s: float,
    ) -> None:
        if self._watchdog_timed_out.is_set():
            return
        self._watchdog_timed_out.set()
        self._stop_requested.set()
        self._emit_telemetry(
            "watchdog_timeout",
            {
                "node_id": action.node_id,
                "kind": action.kind,
                "attempt": attempt,
                "elapsed_s": elapsed_s,
                "deadline_s": deadline_s,
            },
        )

    def _emit_telemetry(self, name: str, data: dict[str, object]) -> None:
        payload = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **data}
        try:
            self._on_telemetry(name, payload)
        except Exception:
            pass

    def _emit_point_stored(
        self,
        point: MeasurementPoint,
        *,
        stored: int,
        run_started_monotonic: float,
        write_elapsed_s: float,
        spectrum_points: int,
    ) -> None:
        elapsed = max(time.monotonic() - run_started_monotonic, 1e-12)
        self._emit(
            "point_stored",
            {
                "point_index": point.index,
                "stored_points": stored,
                "status": point.status,
                "setpoints_si": dict(point.setpoints),
                "measurements_si": dict(point.measurements),
                "spectrum_points": spectrum_points,
                "write_elapsed_s": write_elapsed_s,
                "average_write_rate_points_per_s": stored / elapsed,
            },
        )

    def _emit_spectrum_preview(
        self,
        trace: SpectrumTrace,
        point_index: int,
        *,
        preview_kind: str = "measurement",
        acquisition: _AcquiredSpectrum | None = None,
    ) -> None:
        from app.spectrum.processing import peak_preserving_indices

        processed = acquisition.processed_values if acquisition is not None else None
        indices = peak_preserving_indices(processed if processed is not None else trace.powers_dbm, 1_000)
        self._emit_telemetry(
            "reference_preview" if preview_kind == "reference" else "spectrum_preview",
            {
                "point_index": point_index,
                "preview_kind": preview_kind,
                "trace_name": trace.trace_name,
                "frequency_hz": tuple(trace.frequencies_hz[index] for index in indices),
                "power_dbm": tuple(trace.powers_dbm[index] for index in indices),
                "source_points": len(trace.powers_dbm),
                "processed_values": tuple(processed[index] for index in indices) if processed is not None else None,
                "processed_unit": acquisition.processed_unit if acquisition is not None else None,
                "processing_operation": acquisition.processing_operation if acquisition is not None else None,
            },
        )

    def _abort_safely(
        self,
        plan: ExecutionPlan,
        completed: int,
        stored: int,
        measurements: dict[str, float],
        reason: str,
        *,
        attempted_finally: set[str] | None = None,
    ) -> RunResult:
        """Run only preflight-selected cleanup actions after an operator stop."""

        self._emit_after_fault("run_aborting", {"reason": reason})
        cleanup_ok = self._run_pending_finally(
            plan,
            measurements,
            attempted_node_ids=attempted_finally,
        )
        shutdown_ok = self._safe_shutdown()
        if cleanup_ok and shutdown_ok:
            self._state = ApplicationState.UNKNOWN if self._moke_owns_output else ApplicationState.SAFE
            self._emit_after_fault("run_aborted", {"completed_actions": completed, "stored_points": stored})
            error = self._close_after_error("aborted", reason)
            return RunResult(self._state, completed, stored, error)
        self._state = ApplicationState.FAULT
        self._emit_after_fault("run_fault", {"error": "Safe stop was not confirmed: " + reason})
        error = self._close_after_error("faulted", reason)
        return RunResult(self._state, completed, stored, error)

    def _close_after_error(self, status: str, reason: str) -> str:
        """Retain the original fault when closing/validation also fails."""
        try:
            self._writer.close(status)
        except Exception as exc:
            self._state = ApplicationState.FAULT
            original_reason = reason
            reason += f"\nArchive close failed: {exc}"
            # The HDF5 handle may already be closed. Report to the UI/audit
            # callback without attempting another write to that handle.
            try:
                self._on_event("storage_close_failed", {
                    "error": str(exc), "original_error": original_reason,
                    "correlation_id": self._correlation_id,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "state_snapshot": self._runtime_state_snapshot(),
                })
            except Exception:
                pass
        return reason

    def _run_pending_finally(
        self,
        plan: ExecutionPlan,
        measurements: dict[str, float],
        *,
        attempted_node_ids: set[str] | None = None,
    ) -> bool:
        """Attempt each not-yet-started selected Finally action exactly once."""

        attempted = attempted_node_ids if attempted_node_ids is not None else set()
        cleanup_ok = True
        for action in plan.actions:
            if not action.is_finally or action.completion_only or action.node_id in attempted:
                continue
            attempted.add(action.node_id)
            try:
                self._emit_after_fault(
                    "safe_finally_started",
                    {"node_id": action.node_id, "kind": action.kind},
                )
                self._execute(action, measurements)
                self._emit_after_fault(
                    "safe_finally_finished",
                    {"node_id": action.node_id, "kind": action.kind},
                )
            except Exception as exc:
                cleanup_ok = False
                self._emit_after_fault(
                    "safe_finally_error",
                    {
                        "node_id": action.node_id,
                        "kind": action.kind,
                        "error": str(exc),
                    },
                )
        return cleanup_ok

    def _execute(
        self, action: PlanAction, measurements: dict[str, float],
    ) -> _AcquiredSpectrum | None:
        adapter = self._adapter_for_action(action) if action.is_finally and not action.completion_only else None
        if adapter is not None:
            # Reserve time for the final fallback OFF operations as well.
            with self._cleanup_operation(adapter, slots=2):
                return self._execute_impl(action, measurements)
        return self._execute_impl(action, measurements)

    def _execute_impl(
        self, action: PlanAction, measurements: dict[str, float]
    ) -> _AcquiredSpectrum | None:
        payload = action.payload
        if action.kind == "assert_output_on":
            device = str(payload.get("device", ""))
            channel = str(payload.get("channel", ""))
            expected_output = not self.outputs_forced_off
            confirmed = self._assert_physical_output_state(
                device, channel, expected_enabled=expected_output
            )
            if not self.outputs_forced_off and not confirmed:
                raise ExecutionError(
                    f"Continuous OUTPUT for {device}.{channel} was not confirmed ON "
                    "by an earlier action in this run."
                )
            if self.outputs_forced_off:
                self._emit(
                    "dry_run_output_continuity_suppressed",
                    {
                        "node_id": action.node_id,
                        "device": device,
                        "channel": channel,
                        "requested_enabled": True,
                        "actual_enabled": False,
                        "reason": "Dry run keeps continuous-output assertions logically scoped while OUTPUT remains OFF.",
                    },
                )
            context_key = (
                f"{device}.{channel}"
                if device != "anritsu_sg"
                else "anritsu.sg"
            )
            actual_state = self._active_safety_context.get(context_key, {})
            expected_state = payload.get("expected_state", {})
            if not isinstance(expected_state, dict):
                raise ExecutionError("Continuous-output expected state is malformed.")
            mismatches: list[str] = []
            for name, expected in expected_state.items():
                actual = actual_state.get(name)
                if isinstance(expected, (int, float)) and isinstance(
                    actual, (int, float)
                ):
                    abs_tol = (
                        1.0
                        if name.endswith("frequency_hz")
                        else 0.01
                        if name.endswith("power_dbm")
                        else 1e-12
                    )
                    matches = math.isclose(
                        float(actual), float(expected), rel_tol=1e-9, abs_tol=abs_tol
                    )
                else:
                    matches = actual == expected
                if not matches:
                    mismatches.append(name)
            if mismatches:
                raise ExecutionError(
                    f"Continuous OUTPUT for {device}.{channel} cannot inherit a "
                    "different configuration; mismatched fields: "
                    + ", ".join(mismatches)
                    + "."
                )
        elif action.kind == "configure_rigol":
            config = payload["config"]
            self._rigol.configure_channel(config)
            applied = self._rigol.last_channel_config(config.channel)
            self._rigol_output_active[applied.channel] = False
            self._confirm_output_state(f"rigol.{applied.channel}", False)
            self._active_safety_context[f"rigol.{applied.channel}"] = {
                "frequency_hz": applied.frequency_hz,
                "high_level_v": applied.high_level_v,
                "low_level_v": applied.low_level_v,
                "output_load": applied.output_load,
                "waveform": applied.waveform,
                "phase_deg": applied.phase_deg,
                "square_duty_percent": applied.square_duty_percent,
                "ramp_symmetry_percent": applied.ramp_symmetry_percent,
                "pulse_width_s": applied.pulse_width_s,
                "pulse_leading_s": applied.pulse_leading_s,
                "pulse_trailing_s": applied.pulse_trailing_s,
            }
            self._record_device_state(
                "rigol", f"channel_{config.channel}", requested=config,
                actual=self._active_safety_context[f"rigol.{config.channel}"],
            )
        elif action.kind == "configure_rigol_output":
            config = payload["config"]
            applied = self._rigol.configure_output(config) or config
            self._rigol_output_active[config.channel] = False
            self._confirm_output_state(f"rigol.{config.channel}", False)
            context = self._active_safety_context.get(
                f"rigol.{config.channel}", {}
            )
            context["output_path"] = {
                "output_load": applied.output_load,
                "polarity": applied.polarity,
                "mode": applied.mode,
                "gate_polarity": applied.gate_polarity,
                "sync_enabled": applied.sync_enabled,
                "sync_polarity": applied.sync_polarity,
                "sync_delay_s": applied.sync_delay_s,
            }
            self._active_safety_context[f"rigol.{config.channel}"] = context
            self._record_device_state(
                "rigol", f"channel_{config.channel}", requested=config, actual=context
            )
        elif action.kind == "configure_keithley":
            request = payload["request"]
            self._keithley.configure_source(request)
            applied = self._keithley.last_source_request(request.channel)
            self._keithley_output_active[applied.channel] = False
            self._confirm_output_state(f"keithley.{applied.channel}", False)
            self._keithley_zeroed[applied.channel] = abs(applied.level_si) <= 1e-15
            self._active_safety_context[f"keithley.{applied.channel}"] = {
                "mode": applied.mode,
                "source_level_si": applied.level_si,
                "compliance_si": applied.compliance_si,
                "nplc": applied.nplc,
                "settle_time_s": applied.settle_time_s,
                "sense_mode": applied.sense_mode,
                "source_autorange": applied.source_autorange,
                "source_range_si": applied.source_range_si,
                "measure_voltage_autorange": applied.measure_voltage_autorange,
                "measure_voltage_range_si": applied.measure_voltage_range_si,
                "measure_current_autorange": applied.measure_current_autorange,
                "measure_current_range_si": applied.measure_current_range_si,
            }
            range_reader = getattr(self._keithley, "last_range_readback", None)
            range_readback = range_reader(request.channel) if callable(range_reader) else None
            if isinstance(range_readback, dict):
                self._active_safety_context[f"keithley.{request.channel}"]["range_readback"] = range_readback
            self._record_device_state(
                "keithley", f"channel_{request.channel}", requested=request,
                actual=self._active_safety_context[f"keithley.{request.channel}"],
            )
        elif action.kind == "update_keithley_level":
            actual = self._keithley.update_source_level(
                payload["channel"],
                mode=payload["mode"],
                level_si=payload["level_si"],
            )
            key = f"keithley.{payload['channel']}"
            context = self._active_safety_context.get(key, {})
            context["source_level_si"] = actual
            self._active_safety_context[key] = context
            self._record_device_state("keithley", f"channel_{payload['channel']}", requested=payload, actual=context)
        elif action.kind == "update_keithley_compliance":
            actual = self._keithley.update_source_compliance(
                payload["channel"],
                mode=payload["mode"],
                compliance_si=payload["compliance_si"],
            )
            key = f"keithley.{payload['channel']}"
            context = self._active_safety_context.get(key, {})
            context["compliance_si"] = actual
            self._active_safety_context[key] = context
            self._record_device_state("keithley", f"channel_{payload['channel']}", requested=payload, actual=context)
        elif action.kind == "configure_anritsu":
            config = payload["config"]
            actual = self._anritsu.configure_spectrum(config)
            if config.changed_fields is None and self._anritsu_owns_output and actual is not None:
                # The qualified full-mode transition queries and confirms RF
                # OFF. A selected-field patch carries no such output proof.
                self._anritsu_sg_output_active = False
                self._confirm_output_state("anritsu.sg", False)
            if actual is None:
                # Qualified AnritsuAdapter implementations return a verified
                # snapshot. Keep passive test/dry-run doubles compatible while
                # never weakening the production adapter contract.
                actual = config
            self._active_safety_context["anritsu"] = {
                "start_hz": getattr(actual, "start_hz", config.start_hz),
                "stop_hz": getattr(actual, "stop_hz", config.stop_hz),
                "reference_level_dbm": getattr(
                    actual, "reference_level_dbm", config.reference_level_dbm
                ),
                "points": getattr(actual, "points", config.points),
                "instrument_mode": getattr(actual, "instrument_mode", ""),
            }
            self._record_device_state(
                "anritsu", "spectrum", requested=config,
                actual=self._active_safety_context["anritsu"],
            )
        elif action.kind == "configure_anritsu_advanced":
            config = payload["config"]
            actual = self._anritsu.configure_advanced_spectrum(config)
            self._active_safety_context["anritsu.advanced"] = {
                "vbw_filter_mode": actual.vbw_filter_mode,
                "rbw_auto": actual.rbw_auto,
                "rbw_hz": actual.rbw_hz,
                "vbw_mode": actual.vbw_mode,
                "vbw_hz": actual.vbw_hz,
                "detector": actual.detector,
                "attenuation_auto": actual.attenuation_auto,
                "attenuation_db": actual.attenuation_db,
                "preamplifier_enabled": actual.preamplifier_enabled,
                "sweep_time_auto": actual.sweep_time_auto,
                "sweep_time_s": actual.sweep_time_s,
            }
            self._record_device_state(
                "anritsu", "advanced_spectrum", requested=config,
                actual=self._active_safety_context["anritsu.advanced"],
            )
        elif action.kind == "configure_anritsu_sg":
            config = payload["config"]
            actual = self._anritsu.configure_signal_generator(config)
            self._anritsu_sg_output_active = actual.output_enabled
            self._confirm_output_state("anritsu.sg", actual.output_enabled)
            self._active_safety_context["anritsu.sg"] = {
                "frequency_hz": actual.frequency_hz,
                "power_dbm": actual.power_dbm,
                "output_enabled": actual.output_enabled,
                "instrument_mode": actual.instrument_mode,
            }
            self._record_device_state(
                "anritsu", "signal_generator", requested=config,
                actual=self._active_safety_context["anritsu.sg"],
            )
        elif action.kind == "update_anritsu_sg":
            config = payload["config"]
            expected_output = self._anritsu_sg_output_active
            actual = self._anritsu.update_signal_generator(config)
            if actual.output_enabled != expected_output:
                raise ExecutionError(
                    "Anritsu SG RF OUTPUT continuity was lost during a live update."
                )
            self._anritsu_sg_output_active = actual.output_enabled
            self._confirm_output_state("anritsu.sg", actual.output_enabled)
            self._active_safety_context["anritsu.sg"] = {
                "frequency_hz": actual.frequency_hz,
                "power_dbm": actual.power_dbm,
                "output_enabled": actual.output_enabled,
                "instrument_mode": actual.instrument_mode,
            }
            self._record_device_state(
                "anritsu",
                "signal_generator",
                requested=config,
                actual=self._active_safety_context["anritsu.sg"],
            )
        elif action.kind == "update_rigol_frequency":
            actual = self._rigol.update_frequency(
                payload["channel"], payload["frequency_hz"]
            )
            key = f"rigol.{payload['channel']}"
            context = self._active_safety_context.get(key, {})
            context["frequency_hz"] = actual
            self._active_safety_context[key] = context
            self._record_device_state("rigol", f"channel_{payload['channel']}", requested=payload, actual=context)
        elif action.kind == "update_rigol_levels":
            actual_high, actual_low = self._rigol.update_levels(
                payload["channel"],
                high_level_v=payload["high_level_v"],
                low_level_v=payload["low_level_v"],
            )
            key = f"rigol.{payload['channel']}"
            context = self._active_safety_context.get(key, {})
            context["high_level_v"] = actual_high
            context["low_level_v"] = actual_low
            self._active_safety_context[key] = context
            self._record_device_state("rigol", f"channel_{payload['channel']}", requested=payload, actual=context)
        elif action.kind == "set_rigol_output":
            requested_enabled = bool(payload["enabled"])
            effective_enabled = requested_enabled and not self.outputs_forced_off
            actual_enabled = self._rigol.set_output(
                payload["channel"], effective_enabled
            )
            self._rigol_output_active[payload["channel"]] = actual_enabled
            self._confirm_output_state(
                f"rigol.{payload['channel']}", actual_enabled
            )
            context = self._active_safety_context.get(f"rigol.{payload['channel']}", {})
            context["output_enabled"] = actual_enabled
            self._active_safety_context[f"rigol.{payload['channel']}"] = context
            self._record_device_state("rigol", f"channel_{payload['channel']}", requested=payload, actual=context)
            self._emit_dry_run_suppression(action, requested_enabled, actual_enabled)
        elif action.kind == "set_keithley_output":
            requested_enabled = bool(payload["enabled"])
            effective_enabled = requested_enabled and not self.outputs_forced_off
            actual_enabled = self._keithley.set_output(
                payload["channel"], effective_enabled
            )
            self._keithley_output_active[payload["channel"]] = actual_enabled
            self._confirm_output_state(
                f"keithley.{payload['channel']}", actual_enabled
            )
            context = self._active_safety_context.get(f"keithley.{payload['channel']}", {})
            context["output_enabled"] = actual_enabled
            self._active_safety_context[f"keithley.{payload['channel']}"] = context
            self._record_device_state("keithley", f"channel_{payload['channel']}", requested=payload, actual=context)
            self._emit_dry_run_suppression(action, requested_enabled, actual_enabled)
        elif action.kind == "set_anritsu_sg_output":
            requested_enabled = bool(payload["enabled"])
            effective_enabled = requested_enabled and not self.outputs_forced_off
            actual_enabled = self._anritsu.set_signal_generator_output(
                effective_enabled
            )
            if not isinstance(actual_enabled, bool):
                raise ExecutionError(
                    "Anritsu SG output transition did not return a confirmed boolean readback."
                )
            self._anritsu_sg_output_active = actual_enabled
            self._confirm_output_state("anritsu.sg", actual_enabled)
            context = self._active_safety_context.get("anritsu.sg", {})
            context["output_enabled"] = actual_enabled
            self._active_safety_context["anritsu.sg"] = context
            self._record_device_state("anritsu", "signal_generator", requested=payload, actual=context)
            self._emit_dry_run_suppression(
                action, requested_enabled, actual_enabled
            )
        elif action.kind == "ramp_keithley_to_zero":
            self._keithley.ramp_to_zero(payload["channel"], deadline_s=payload["deadline_s"])
            channel = payload["channel"]
            self._keithley_zeroed[channel] = True
            # A successful adapter ramp confirms both the zero level and OFF.
            # Publish that state, rather than retaining the last sweep setpoint
            # in the terminal snapshot and the device-card mirror.
            self._keithley_output_active[channel] = False
            self._confirm_output_state(f"keithley.{channel}", False)
            context = self._active_safety_context.setdefault(f"keithley.{channel}", {})
            if context.get("mode") in {"current", "voltage"}:
                context["source_level_si"] = 0.0
            context["output_enabled"] = False
            self._record_device_state("keithley", f"channel_{channel}", requested=payload, actual=context)
        elif action.kind == "measure_keithley":
            self._sample_keithley(payload["channel"], measurements)
        elif action.kind == "configure_moke_box":
            if self._moke_box is None:
                raise ExecutionError("MOKE voltage control requires an available adapter.")
            self._moke_voltage_plan = payload["plan"]
            self._moke_calibration = payload.get("calibration")
            self._moke_box.configure_voltage_plan(self._moke_voltage_plan)
            self._active_safety_context["moke_box"] = {
                "channel": self._moke_voltage_plan.channel,
                "minimum_v": self._moke_voltage_plan.minimum_v,
                "maximum_v": self._moke_voltage_plan.maximum_v,
                "profile_fingerprint": self._moke_voltage_plan.profile_fingerprint,
                "calibration_id": self._moke_calibration.calibration_id if self._moke_calibration else None,
                "calibration_snapshot": self._jsonable(self._moke_calibration) if self._moke_calibration else None,
                "field_source": "calibrated_branch_curves" if self._moke_calibration else "uncalibrated",
                "conditioning_verified": False,
            }
            projection = {key: value for key, value in payload.items() if key != "plan"}
            projection["trajectory_points"] = len(self._moke_voltage_plan.targets_v)
            self._record_device_state("moke_box", "voltage_control", requested=projection,
                                      actual=self._active_safety_context["moke_box"])
            self._emit("moke_plan_prepared", {"plan": self._jsonable(self._moke_voltage_plan)})
        elif action.kind == "arm_moke_voltage":
            if self._moke_box is None or payload["plan"] != self._moke_voltage_plan:
                raise ExecutionError("MOKE arm permission does not match its prepared plan.")
            if not self.outputs_forced_off:
                self._moke_box.arm_voltage_plan(payload["plan"])
                self._moke_owns_output = True
                self._moke_owned_channels.add(int(payload["plan"].channel))
                self._output_status["moke_box.field"] = "unknown"
        elif action.kind == "update_moke_voltage":
            if self._moke_box is None or self._moke_voltage_plan is None:
                raise ExecutionError("Prepare the MOKE voltage trajectory before applying a point.")
            target = float(payload["voltage_v"])
            if self.outputs_forced_off:
                # Suppressed trajectory points are never passed to SET_VOUT.
                actual = self._moke_box.read_vouts()[int(payload["channel"])]
                applied = None  # No DAC mutation was applied in dry run.
            else:
                self._moke_owned_channels.add(int(payload["channel"]))
                self._moke_owns_output = True
                result = self._moke_box.ramp_vout(int(payload["channel"]), target, cancel=self._stop_requested,
                                                  progress=self._moke_ramp_progress)
                actual, applied = result.actual_v, result.applied_v
            measurements["moke_box.vout_voltage_v"] = actual
            context = self._active_safety_context["moke_box"]
            context.update(requested_v=target, applied_v=applied, actual_v=actual,
                           mutation_suppressed=self.outputs_forced_off)
            for direction in ("ascending", "descending"):
                key = f"moke_box.field_estimated_{direction}_t"
                measurements.pop(key, None)
                if self._moke_calibration is not None:
                    try:
                        branch = getattr(self._moke_calibration, direction)
                        measurements[key] = branch.estimate(actual)
                    except ConfigurationError:
                        context["field_validity"] = "outside_calibrated_range"
                    else:
                        context["field_validity"] = "branch_predictions_history_unverified"
            self._record_device_state("moke_box", "voltage_control", requested=payload, actual=context)
        elif action.kind == "stop_moke_voltage":
            if self._moke_box is None:
                raise ExecutionError("MOKE DAC shutdown requires an available adapter.")
            if not self.outputs_forced_off:
                self._moke_owns_output = True
                channel = payload.get("channel")
                self._stop_owned_moke_channels(self._moke_box, channels=[channel] if channel is not None else None)
            self._moke_voltage_plan = None
        elif action.kind == "measure_moke_hall":
            if self._moke_box is None:
                raise ExecutionError("MOKE Hall measurement was requested but MOKE Box is unavailable.")
            result = self._moke_box.read_hall_voltage()
            measurements["moke_box.hall1_voltage_v"] = result.voltage_v
            measurements["moke_box.hall1_stddev_v"] = result.stddev_v
            measurements["moke_box.hall1_raw_ad7734"] = float(result.raw_codes[0])
            self._record_device_state(
                "moke_box",
                "hall_readback",
                requested={"sample_count": 1},
                actual={
                    "voltage_v": result.voltage_v,
                    "stddev_v": result.stddev_v,
                    "samples": result.samples,
                    "raw_ad7734": result.raw_codes[0],
                    "timestamp_utc": result.timestamp_utc.isoformat(),
                },
            )
        elif action.kind == "measure_lakeshore_field":
            if self._lakeshore is None:
                raise ExecutionError("Lake Shore field measurement was requested but the adapter is unavailable.")
            result = self._lakeshore.read_measurement()
            snapshot = result.snapshot
            if result.field_t is not None:
                measurements["lakeshore.field_t"] = result.field_t
            if result.frequency_hz is not None:
                measurements["lakeshore.frequency_hz"] = result.frequency_hz
            if result.negative_peak_t is not None:
                measurements["lakeshore.negative_peak_t"] = result.negative_peak_t
            if result.positive_peak_t is not None:
                measurements["lakeshore.positive_peak_t"] = result.positive_peak_t
            measurements["lakeshore.mode_code"] = float(snapshot.mode_code)
            measurements["lakeshore.unit_code"] = float(snapshot.unit_code)
            measurements["lakeshore.range_code"] = float(snapshot.range_code)
            measurements["lakeshore.autorange_enabled"] = float(snapshot.autorange_enabled)
            measurements["lakeshore.probe_type_code"] = float(snapshot.probe_type_code)
            self._record_device_state(
                "lakeshore",
                "measurement",
                requested={"mode": snapshot.mode.value},
                actual={
                    "mode": result.mode.value,
                    "unit": result.unit.value,
                    "mode_code": snapshot.mode_code,
                    "unit_code": snapshot.unit_code,
                    "range_code": snapshot.range_code,
                    "autorange_enabled": snapshot.autorange_enabled,
                    "probe_type_code": snapshot.probe_type_code,
                    "field_t": result.field_t,
                    "frequency_hz": result.frequency_hz,
                    "negative_peak_t": result.negative_peak_t,
                    "positive_peak_t": result.positive_peak_t,
                    "timestamp_utc": result.timestamp_utc.isoformat(),
                },
            )
        elif action.kind == "acquire_reference":
            average_count = payload.get("average_count", 1)
            self._reference_origin = {}
            if payload.get("source_file"):
                from app.storage.recipe_reference import (
                    file_sha256,
                    load_recipe_reference,
                    verify_recipe_reference,
                )

                path = payload["source_file"]
                if file_sha256(path) != payload["source_sha256"]:
                    raise ExecutionError("Reference file changed after preflight; recompile the recipe.")
                reference, average_count, evidence = load_recipe_reference(path, payload["file_kind"])
                identity = self._read_spectrum_identity()
                if identity is None:
                    raise ExecutionError("Imported reference requires qualified analyzer readback.")
                full, advanced, idn, fingerprint = identity
                verify_recipe_reference(evidence, full, advanced, idn, fingerprint)
                if file_sha256(path) != payload["source_sha256"]:
                    raise ExecutionError("Reference file changed while loading.")
                block = _AcquiredSpectrum(reference, average_count)
                self._reference_fingerprint = fingerprint
                self._reference_origin = {key: payload[key] for key in ("source_file", "source_sha256", "file_kind")}
                if payload["file_kind"] == "background":
                    context, profile = evidence
                    self._writer.store_background_profile(context, profile)
                    self._reference_origin.update(profile_id=profile.profile_id, profile_hash=profile.content_hash)
            else:
                block = self._acquire_averaged_spectrum(
                    payload["trace"], average_count, action=action, role=SpectrumFrameRole.REFERENCE, measurements=measurements,
                )
                if block is None:
                    return None
                self._reference_fingerprint = (block.processing_metadata or {}).get("configuration_fingerprint")
            reference = block.raw
            average_count = block.average_count
            reference_options = {}
            if payload.get("minimum_duration_s") or "purpose" in payload:
                reference_options["acquisition_metadata"] = {
                    **(block.processing_metadata or {}),
                    "purpose": payload.get("purpose", "reference"),
                    "recipe_node_id": action.node_id,
                    "requested_minimum_sweeps": payload.get("average_count", 1),
                    "safety_context": self._safety_context_snapshot(),
                    "device_states": self._jsonable(self._device_states),
                    "output_status": dict(self._output_status),
                }
            stored_reference_index = self._writer.store_reference(
                reference,
                kind="loaded" if payload.get("source_file") else "single" if average_count == 1 else "averaged",
                average_count=average_count,
                source_sweep_indices=block.source_sweep_indices,
                **reference_options,
            )
            self._reference_trace = reference
            self._reference_index = (
                stored_reference_index
                if isinstance(stored_reference_index, int)
                else None
            )
            self._emit(
                "reference_stored",
                {
                    "trace": reference.trace_name,
                    "points": len(reference.powers_dbm),
                    "average_count": average_count,
                    "reference_index": self._reference_index,
                    "acquired_at_utc": reference.acquired_at_utc.isoformat(),
                    "raw_recipe_sweep_indices": block.source_sweep_indices,
                    "reference_origin": self._reference_origin,
                    "configuration_fingerprint": self._reference_fingerprint,
                    "acquisition_metadata": reference_options.get("acquisition_metadata", {}),
                },
            )
            self._emit_spectrum_preview(
                reference,
                self._reference_index if self._reference_index is not None else -1,
                preview_kind="reference",
            )
        elif action.kind == "acquire_spectrum":
            from app.recipes.spectrum_processing import parse_processing
            from app.spectrum.analysis import clean_spectrum_pipeline

            operation = str(payload.get("reference_operation", "none"))
            filters, parameters = parse_processing(payload.get("processing"))
            if operation != "none" and self._reference_trace is None:
                raise ExecutionError("Acquire or load a matching reference after the last analyzer configuration.")
            average_count = payload.get("average_count", 1)
            block = self._acquire_averaged_spectrum(
                payload["trace"],
                average_count,
                action=action, role=SpectrumFrameRole.SIGNAL, measurements=measurements,
            )
            if block is None:
                return None
            trace = block.raw
            operation = str(payload.get("reference_operation", "none"))
            processed: tuple[float, ...] | None = None
            processed_unit: str | None = None
            metadata = {"schema": "recipe-spectrum-processing-v1", "processing": payload.get("processing", {}),
                        "reference_operation": operation, "reference_origin": dict(self._reference_origin) if operation != "none" else {},
                        "configuration_fingerprint": (block.processing_metadata or {}).get("configuration_fingerprint"),
                        "uncertainty_qualified": False}
            if operation != "none":
                reference = self._reference_trace
                if reference is None:
                    raise ExecutionError(
                        "Reference processing was requested, but no reference "
                        "spectrum is active in this run."
                    )
                if not frequency_grids_match(
                    trace.frequencies_hz, reference.frequencies_hz
                ):
                    raise ExecutionError(
                        "The acquired spectrum frequency grid differs from the reference."
                    )
                fingerprint = (block.processing_metadata or {}).get("configuration_fingerprint")
                if self._reference_fingerprint is not None and fingerprint != self._reference_fingerprint:
                    raise ExecutionError("Analyzer input path or bandwidth changed since reference acquisition.")
                processed, processed_unit = apply_reference_operation(
                    trace.powers_dbm,
                    reference.powers_dbm,
                    operation,
                )
            if filters:
                history = block.history_dbm
                if operation != "none" and "emi_reject" in filters:
                    history = tuple(apply_reference_operation(row, self._reference_trace.powers_dbm, operation)[0] for row in history)
                cleaned = clean_spectrum_pipeline(processed if processed is not None else trace.powers_dbm,
                    unit=processed_unit or "dBm", modes=filters, parameters=parameters,
                    frequencies_hz=trace.frequencies_hz, history=history)
                if set(cleaned.applied_modes) != set(filters):
                    raise ExecutionError("Requested recipe filters could not be applied: " + "; ".join(cleaned.notes))
                processed, processed_unit = cleaned.values, cleaned.unit
                metadata.update(method=cleaned.method, notes=cleaned.notes)
                operation = "recipe_filtered_v1"
            if processed is not None and not all(math.isfinite(value) for value in processed):
                raise ExecutionError("Reference subtraction produced non-positive log power. Use Remove background — signed W to retain all bins.")
            if not bool(payload.get("store_processed", operation != "none")):
                processed = None
                processed_unit = None
                operation = "none"
            return _AcquiredSpectrum(
                raw=trace,
                average_count=average_count,
                processed_values=processed,
                processed_unit=processed_unit,
                processing_operation=operation,
                reference_index=self._reference_index if payload.get("reference_operation", "none") != "none" else None,
                source_sweep_indices=block.source_sweep_indices,
                processing_metadata=metadata,
            )
        elif action.kind == "checkpoint":
            pass
        elif action.kind == "verify_connection":
            device_name = str(payload["device"])
            device = {
                "rigol": self._rigol,
                "keithley": self._keithley,
                "anritsu": self._anritsu,
            }[device_name]
            if device.state in {
                DeviceState.DISCONNECTED,
                DeviceState.UNKNOWN,
                DeviceState.FAULT,
            }:
                raise ExecutionError(
                    f"Recipe connection verification failed for {device_name}: "
                    f"{device.state.value}."
                )
        elif action.kind == "wait":
            self._interruptible_wait(payload["duration_s"])
            target = str(payload.get("target", ""))
            if target.startswith("keithley.") and target.endswith(".settling_time"):
                channel = target.split(".")[1]
                self._active_safety_context.setdefault(f"keithley.{channel}", {})["settle_time_s"] = payload["duration_s"]
                self._executed_settling_times[target] = float(payload["duration_s"])
        elif action.kind in {"upload_to_elab", "upload_elab"}:
            self._emit(
                "elab_upload_scheduled",
                {
                    "node_id": action.node_id,
                    "template_id": payload.get("template_id"),
                    "template_name": payload.get("template_name"),
                },
            )
        else:
            raise ExecutionError(f"The runner does not support action {action.kind!r}.")
        return None

    def _sample_keithley(self, channel: str, measurements: dict[str, float]) -> None:
        """Read IV/compliance and validate measured trips without changing settings."""
        result = self._keithley.measure(channel)
        prefix = f"keithley.{channel}"
        measurements[f"{prefix}.voltage_v"] = result.voltage_v
        measurements[f"{prefix}.current_a"] = result.current_a
        measurements[f"{prefix}.power_w"] = result.power_w
        measurements[f"{prefix}.output_enabled"] = float(result.output_enabled)
        measurements[f"{prefix}.measurement_path_connected"] = float(
            result.measurement_path_connected
        )
        measurements[f"{prefix}.compliance_detected"] = float(result.compliance_detected)
        measurements[f"{prefix}.compliance_stop_required"] = float(result.compliance_stop_required)
        self._keithley_output_active[result.channel] = result.output_enabled
        self._confirm_output_state(prefix, result.output_enabled)
        self._record_device_state(
            "keithley",
            f"measurement_{result.channel}",
            requested={"channel": result.channel},
            actual=result,
        )

    def _moke_ramp_progress(self, sample) -> None:
        """Project confirmed DAC steps without adding I/O or durable event traffic."""
        context = self._active_safety_context.get("moke_box", {})
        context.update(actual_v=sample.actual_v, ramp_phase=sample.phase, ramp_elapsed_s=sample.elapsed_s)
        self._record_device_state("moke_box", "voltage_control", requested={"target_v": sample.target_v}, actual=context)
        self._emit_telemetry("moke_ramp_progress", {"state_snapshot": self._runtime_state_snapshot(),
                                                    "progress": self._jsonable(sample)})

    def _acquire_averaged_spectrum(
        self,
        trace_name: str,
        average_count: int,
        *, action: PlanAction, role: SpectrumFrameRole, measurements: dict[str, float],
    ) -> _AcquiredSpectrum | None:
        """Acquire complete, grid-matched traces and average in linear power."""

        if type(average_count) is not int or not 1 <= average_count <= 9999:
            raise ExecutionError("Recipe spectrum average_count must be an integer in 1..9999.")

        before = self._read_spectrum_identity()
        if before is not None:
            self._record_device_state("anritsu", "spectrum",
                requested={"operation": "acquisition_readback"}, actual=before[0])
            self._record_device_state("anritsu", "advanced_spectrum",
                requested={"operation": "acquisition_readback"}, actual=before[1])
        if (role == SpectrumFrameRole.SIGNAL
                and action.payload.get("reference_operation", "none") != "none"
                and self._reference_fingerprint is not None
                and (before is None or before[3] != self._reference_fingerprint)):
            raise ExecutionError("Analyzer settings changed since reference; acquire or load a matching reference.")
        averager = LinearPowerAverager()
        history = deque(maxlen=24)
        first: SpectrumTrace | None = None
        latest: SpectrumTrace | None = None
        source_indices = []
        delay = float(action.payload.get("inter_sweep_delay_s", 0.0))
        if not math.isfinite(delay) or not 0 <= delay <= 3600:
            raise ExecutionError("Invalid spectrum inter-sweep delay.")
        minimum_duration = float(action.payload.get("minimum_duration_s", 0.0))
        if not math.isfinite(minimum_duration) or not 0 <= minimum_duration <= 3600:
            raise ExecutionError("Invalid minimum spectrum collection duration.")
        if minimum_duration and role != SpectrumFrameRole.REFERENCE:
            raise ExecutionError("Timed collection requires a reference/background acquisition.")
        collection_started = time.monotonic()
        for _index in range(9999 if minimum_duration else average_count):
            self._raise_if_stop_requested()
            if _index > 0 and delay > 0:
                self._interruptible_wait(delay)
                self._raise_if_stop_requested()
            safety_channels = []
            for channel, enabled in tuple(self._keithley_output_active.items()):
                if enabled:
                    self._sample_keithley(channel, measurements)
                    safety_channels.append(channel)
            if self._compliance_channels(measurements):
                return None
            safety_sampled_at = datetime.now(timezone.utc).timestamp() if safety_channels else None
            latest = self._anritsu.acquire_single_sweep(trace_name, restore_continuous=False)
            setpoint_evidence = self._setpoint_evidence(action.setpoints_si)
            # Commit before requesting another sweep or publishing a mean.
            # Storage failure propagates through the established runner fault
            # path and finally/shutdown; it cannot silently lose raw sources.
            source_index = self._writer.store_recipe_spectrum_sweep(RecipeSpectrumSweep(
                latest.frequencies_hz, latest.powers_dbm, action.node_id, self._correlation_id,
                role, _index, None if minimum_duration else average_count, latest.acquired_at_utc.timestamp(),
                latest.configuration_generation, latest.sweep_evidence, latest.sweep_id,
                latest.acquisition_started_at_utc.timestamp() if latest.acquisition_started_at_utc else None,
                latest.acquisition_completed_at_utc.timestamp() if latest.acquisition_completed_at_utc else None,
                tuple(sorted((name, float(value)) for name, value in action.setpoints_si.items())),
                setpoint_metadata_version=2,
                requested_setpoints_si=tuple(sorted((name, float(value)) for name, value in action.setpoints_si.items())),
                applied_setpoints_si=tuple(sorted((name, float(value["applied_si"])) for name, value in setpoint_evidence.items() if value["applied_si"] is not None)),
                readback_setpoints_si=tuple(sorted((name, float(value["readback_si"])) for name, value in setpoint_evidence.items() if value["readback_si"] is not None)),
                inter_sweep_delay_s=delay,
                safety_measurements_si=tuple(sorted((name, value) for name, value in measurements.items()
                    if any(name.startswith(f"keithley.{channel}.") for channel in safety_channels))),
                safety_sampled_at_s=safety_sampled_at,
                minimum_duration_s=minimum_duration,
            ))
            source_indices.append(source_index)
            if first is None:
                first = latest
            elif not frequency_grids_match(
                first.frequencies_hz, latest.frequencies_hz
            ):
                raise ExecutionError(
                    "Anritsu averaging aborted because the frequency grid changed "
                    "between complete spectra."
                )
            elif latest.configuration_generation != first.configuration_generation:
                raise ExecutionError("Anritsu averaging aborted because its acquisition configuration changed.")
            averager.add(latest.powers_dbm)
            history.append(latest.powers_dbm)
            if len(source_indices) >= average_count and time.monotonic() - collection_started >= minimum_duration:
                break
        elapsed_s = time.monotonic() - collection_started
        if elapsed_s < minimum_duration:
            raise ExecutionError("Timed reference reached 9999 sweeps before the requested duration; RAW sources were retained.")
        if first is None or latest is None:
            raise ExecutionError("Anritsu averaging requires at least one spectrum.")
        averaged = SpectrumTrace(
            frequencies_hz=first.frequencies_hz,
            powers_dbm=averager.result(),
            acquired_at_utc=latest.acquired_at_utc,
            trace_name=latest.trace_name,
            configuration_generation=latest.configuration_generation,
            sweep_evidence=latest.sweep_evidence,
            acquisition_started_at_utc=first.acquisition_started_at_utc,
            acquisition_completed_at_utc=latest.acquisition_completed_at_utc,
        )
        after = self._read_spectrum_identity()
        if before is not None and (after is None or before[3] != after[3]):
            raise ExecutionError("Analyzer settings changed during the acquisition block; RAW sources were retained.")
        return _AcquiredSpectrum(averaged, len(source_indices), source_sweep_indices=tuple(source_indices),
            processing_metadata={"configuration_fingerprint": before[3] if before else None,
                                 "inter_sweep_delay_s": delay,
                                 "minimum_duration_s": minimum_duration,
                                 "collection_elapsed_s": elapsed_s,
                                 "safety_sampling": "active_smu_iv_before_each_raw_frame"}, history_dbm=tuple(history))

    def _read_spectrum_identity(self):
        # Read through the adapter contract, including the owning-thread proxy.
        # Missing/failed readback must fail acquisition, never silently remove
        # configuration qualification for a whole GUI run.
        from app.devices.anritsu_ms2830a.acquisition_context import (
            spectrum_configuration_fingerprint,
        )

        full, advanced = self._anritsu.read_acquisition_configuration()
        idn = self._anritsu.identity.idn
        return full, advanced, idn, spectrum_configuration_fingerprint(full, advanced, idn)

    def _interruptible_wait(self, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        while True:
            self._raise_if_stop_requested()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    def _raise_if_stop_requested(self) -> None:
        if self._stop_requested.is_set():
            raise ExecutionError("The operator stopped the measurement.")

    def _wait_if_paused(self) -> None:
        while self._pause_requested.is_set():
            self._state = ApplicationState.PAUSED
            self._resume_requested.clear()
            if self._stop_requested.wait(0.05):
                self._raise_if_stop_requested()
            if self._resume_requested.is_set():
                self._state = ApplicationState.RUNNING
                return

    def _wait_for_manual_step(
        self, action: PlanAction, action_index: int, total_actions: int
    ) -> None:
        """Wait cooperatively before one visible recipe stage.

        This is a UI pacing gate only.  It does not replace adapter completion,
        deadlines, readback or the normal safe-finally path.  Cleanup actions
        deliberately bypass the gate so Stop/E-STOP cannot wait for a click.
        """

        self._manual_step_permit.clear()
        self._state = ApplicationState.PAUSED
        self._emit(
            "manual_stage_waiting",
            {
                "node_id": action.node_id,
                "kind": action.kind,
                "action_index": action_index,
                "total_actions": total_actions,
                "setpoints_si": dict(action.setpoints_si),
                "deadline_s": self._policy.deadline_for(action),
            },
        )
        while not self._manual_step_permit.wait(0.05):
            self._raise_if_stop_requested()
        self._raise_if_stop_requested()
        self._manual_step_permit.clear()
        self._state = ApplicationState.RUNNING
        self._emit(
            "manual_stage_confirmed",
            {
                "node_id": action.node_id,
                "kind": action.kind,
                "action_index": action_index,
                "total_actions": total_actions,
            },
        )

    def _assert_physical_output_state(
        self, device: str, channel: str, *, expected_enabled: bool
    ) -> bool:
        """Read back the output and configuration at every continuity boundary."""

        if device == "keithley":
            if channel not in {"A", "B"}:
                raise ExecutionError(
                    f"Unknown continuous-output endpoint {device}.{channel}."
                )
            actual = self._keithley.assert_output_state(
                channel, expected_enabled=expected_enabled
            )
            self._keithley_output_active[channel] = actual
            self._confirm_output_state(f"keithley.{channel}", actual)
            return actual
        if device == "rigol":
            try:
                channel_number = int(channel)
            except ValueError as exc:
                raise ExecutionError(
                    f"Unknown continuous-output endpoint {device}.{channel}."
                ) from exc
            if channel_number not in {1, 2}:
                raise ExecutionError(
                    f"Unknown continuous-output endpoint {device}.{channel}."
                )
            actual = self._rigol.assert_output_state(
                channel_number, expected_enabled=expected_enabled
            )
            self._rigol_output_active[channel_number] = actual
            self._confirm_output_state(f"rigol.{channel_number}", actual)
            return actual
        if device == "anritsu_sg" and channel == "RF":
            actual = self._anritsu.assert_signal_generator_output_state(
                expected_enabled=expected_enabled
            )
            self._anritsu_sg_output_active = actual
            self._confirm_output_state("anritsu.sg", actual)
            return actual
        raise ExecutionError(
            f"Unknown continuous-output endpoint {device}.{channel}."
        )

    def _pause_at_point_if_requested(self) -> None:
        if self._pause_requested.is_set():
            self._emit("pause_pending", {})
            self._wait_if_paused()

    def _safe_shutdown(self, *, retained_outputs: frozenset[str] = frozenset(),
                       successful_completion: bool = False) -> bool:
        """Confirm owned output states; preserve receiver sweeps after success."""

        self._state = ApplicationState.STOPPING
        confirmed = True
        devices = {
            "keithley": self._keithley,
            "rigol": self._rigol,
            "anritsu": self._anritsu,
        }
        if self._moke_owns_output and self._moke_box is not None:
            devices["moke_box"] = self._moke_box
        default_actions = {
            "keithley": "keithley.outputs_off",
            "rigol": "rigol.outputs_off",
            "anritsu": "anritsu.rf_off_and_abort" if self._anritsu_owns_output else "anritsu.abort_acquisition",
            "moke_box": "moke_box.dac_zero_or_unknown",
        }
        shutdown_scope = set(self._output_guard_devices())
        if successful_completion and not self._anritsu_owns_output:
            # Legacy manifests must not stop a receiver after successful data collection.
            shutdown_scope.discard("anritsu")
        if self._moke_owns_output:
            shutdown_scope.add("moke_box")
        actions = self._safe_shutdown_actions or tuple(
            default_actions[name]
            for name in ("keithley", "rigol", "anritsu")
            if name in shutdown_scope
        ) + ("storage.flush_checkpoint",)
        attempted_devices: set[str] = set()
        for action in actions:
            if action.startswith("anritsu."):
                action = default_actions["anritsu"]
            # Older compiled manifests can contain station-wide shutdowns.
            # They do not grant ownership of unrelated connected instruments.
            if action != "storage.flush_checkpoint" and action.split(".", 1)[0] not in shutdown_scope:
                continue
            self._emit_after_fault("shutdown_action_started", {"action": action})
            try:
                if action == "storage.flush_checkpoint":
                    flush = getattr(self._writer, "flush_checkpoint", None)
                    if callable(flush):
                        flush()
                else:
                    name = action.split(".", 1)[0]
                    if name == "moke_box" and not self._moke_owns_output:
                        self._emit_after_fault("shutdown_action_finished", {
                            "action": action, "mutation_suppressed": True,
                        })
                        continue
                    device = devices[name]
                    attempted_devices.add(name)
                    with self._cleanup_operation(device, slots=max(1, len(shutdown_scope - attempted_devices) + 1)):
                        self._shutdown_owned_device(name, device, retained_outputs=retained_outputs)
            except Exception as exc:
                confirmed = False
                self._emit_after_fault(
                    "shutdown_error",
                    {"action": action, "error": str(exc)},
                )
            else:
                if action != "storage.flush_checkpoint" and name != "moke_box" and not any(
                    endpoint.startswith(name + ".") for endpoint in retained_outputs
                ) and (name != "anritsu" or self._anritsu_owns_output):
                    self._confirm_device_outputs_off(name)
                self._emit_after_fault("shutdown_action_finished", {"action": action})
        # A malformed manually-created plan must not be able to omit OFF for a
        # required device. Compiled plans already contain this fallback.
        fallback_scope = shutdown_scope
        for name in sorted(fallback_scope - attempted_devices):
            action = default_actions[name]
            try:
                attempted_devices.add(name)
                with self._cleanup_operation(devices[name], slots=max(1, len(fallback_scope - attempted_devices) + 1)):
                    self._shutdown_owned_device(name, devices[name], retained_outputs=retained_outputs)
            except Exception as exc:
                confirmed = False
                self._emit_after_fault(
                    "shutdown_error",
                    {"action": action, "error": str(exc), "fallback": True},
                )
            else:
                if name != "moke_box" and not any(endpoint.startswith(name + ".") for endpoint in retained_outputs) and (name != "anritsu" or self._anritsu_owns_output):
                    self._confirm_device_outputs_off(name)
        return confirmed

    @contextmanager
    def _cleanup_operation(self, adapter, *, slots: int = 1):
        now = time.monotonic()
        if self._shutdown_deadline is None:
            self._shutdown_deadline = now + self._policy.shutdown_timeout_s
            self._emit_after_fault("shutdown_budget_started", {
                "timeout_s": self._policy.shutdown_timeout_s,
                "scope": "instrument_finally_and_fallback",
            })
        remaining = self._shutdown_deadline - time.monotonic()
        action = PlanAction("shutdown", "shutdown", {}, {}, is_finally=True)
        if remaining <= 0:
            self._flag_watchdog_timeout(action, 1, self._policy.shutdown_timeout_s - remaining,
                                        self._policy.shutdown_timeout_s)
            raise ExecutionError("Shared instrument shutdown deadline expired; safe state is unconfirmed.")
        budget = remaining / max(1, slots)
        self._begin_action_watchdog(action, attempt=1, deadline_s=budget)
        try:
            with ExitStack() as stack:
                operation = getattr(adapter, "operation_timeout", None)
                io = getattr(adapter, "io_timeout", None)
                if callable(operation):
                    stack.enter_context(operation(budget))
                if callable(io):
                    stack.enter_context(io(min(budget, self._policy.command_timeout_s)))
                yield
            elapsed = time.monotonic() - now
            if elapsed > budget:
                self._flag_watchdog_timeout(action, 1, elapsed, budget)
                raise ExecutionError("Instrument shutdown exceeded its allocated deadline.")
        finally:
            self._clear_action_watchdog()

    def _shutdown_owned_device(self, name, device, *, retained_outputs=frozenset()) -> None:
        retained = {endpoint for endpoint in retained_outputs if endpoint.startswith(name + ".")}
        if retained and name in {"keithley", "rigol"}:
            errors = []
            for channel in (("A", "B") if name == "keithley" else (1, 2)):
                endpoint = f"{name}.{channel}"
                try:
                    if endpoint in retained:
                        actual = device.assert_output_state(channel, expected_enabled=True)
                        if actual is not True:
                            raise ExecutionError("Retained output was not confirmed ON.")
                    else:
                        actual = device.set_output(channel, False)
                        if actual is not False:
                            raise ExecutionError("Output OFF was not confirmed.")
                    self._confirm_output_state(endpoint, actual)
                    self._emit_after_fault("completion_output_confirmed", {"endpoint": endpoint, "enabled": actual})
                except Exception as exc:
                    self._mark_output_unknown(endpoint)
                    errors.append(f"{endpoint}: {exc}")
            if errors:
                raise ExecutionError("; ".join(errors))
            return
        if name == "anritsu" and not self._anritsu_owns_output:
            if device.abort_acquisition() is not True:
                raise ExecutionError("Anritsu acquisition abort was not confirmed.")
            return
        if name == "moke_box":
            self._stop_owned_moke_channels(device, exclude={int(endpoint.rsplit("vout", 1)[1]) for endpoint in retained})
            return
        confirmed = device.emergency_off()
        if confirmed is not True and not (
            confirmed is None and device.state == DeviceState.OUTPUT_OFF
        ):
            raise ExecutionError("The instrument did not confirm a safe state.")

    def _stop_owned_moke_channels(self, device, *, channels=None, exclude=frozenset()) -> None:
        channels = sorted(self._moke_owned_channels) if channels is None else channels
        if not channels and self._moke_voltage_plan is not None:
            channels = [self._moke_voltage_plan.channel]
        # An explicitly authored Stop with no preceding plan retains the
        # adapter's qualified current-channel semantics.
        targets = [channel for channel in (channels or [None]) if channel not in exclude]
        errors = []
        self._output_status["moke_box.field"] = "unknown"
        for channel in targets:
            if channel is not None:
                self._moke_owned_channels.add(channel)
            pending = {"channel": channel, "actual_v": None, "safe_target_confirmed": False}
            self._record_device_state("moke_box", "dac_shutdown",
                                      requested={"channel": channel}, actual=pending)
            if channel is not None:
                self._record_device_state("moke_box", f"dac_shutdown_{channel}",
                                          requested={"channel": channel}, actual=pending)
            try:
                result = device.stop_vout() if channel is None else device.stop_vout(channel)
                self._moke_owned_channels.add(result.channel)
                self._record_device_state("moke_box", "dac_shutdown",
                                          requested={"channel": channel}, actual=result)
                self._record_device_state("moke_box", f"dac_shutdown_{result.channel}",
                                          requested={"channel": channel}, actual=result)
                self._emit_after_fault("moke_dac_shutdown", {
                    "channel": result.channel, "dac_zero_confirmed": result.safe_target_confirmed,
                    "kepco_power_state": "unknown", "actual_v": result.actual_v,
                })
                if not result.safe_target_confirmed:
                    raise ExecutionError("MOKE programming-signal zero was not confirmed.")
            except Exception as exc:
                errors.append(f"VOUT {channel}: {exc}")
        if errors:
            raise ExecutionError("; ".join(errors))

    @staticmethod
    def _compliance_channels(measurements: dict[str, float]) -> tuple[str, ...]:
        channels = []
        for key, value in measurements.items():
            if key.endswith(".compliance_stop_required") and value:
                channels.append(key.removesuffix(".compliance_stop_required"))
        return tuple(channels)

    def _safety_context_snapshot(self) -> dict[str, dict[str, object]]:
        """Copy the active, JSON-safe physical envelope into a checkpoint."""

        return {name: dict(values) for name, values in self._active_safety_context.items()}

    @staticmethod
    def _jsonable(value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            return RecipeRunner._jsonable(asdict(value))
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {str(key): RecipeRunner._jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [RecipeRunner._jsonable(item) for item in value]
        return value

    def _record_device_state(
        self, device: str, section: str, *, requested: object, actual: object
    ) -> None:
        self._device_states.setdefault(device, {})[section] = {
            "requested": self._jsonable(requested),
            "actual": self._jsonable(actual),
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _apply_actual_setpoints(self, setpoints: dict[str, float]) -> None:
        """Replace requested sweep floats with the last readback-confirmed values."""

        for name in tuple(setpoints):
            actual = self._actual_setpoint_value(name)
            if actual is not None and math.isfinite(actual):
                setpoints[name] = actual

    def _actual_setpoint_value(self, name: str) -> float | None:
        parts = name.split(".")
        context: dict[str, object] | None = None
        field: str | None = None
        if len(parts) == 3 and parts[0] == "keithley":
            channel, axis = parts[1], parts[2]
            context = self._active_safety_context.get(f"keithley.{channel}")
            mode = context.get("mode") if context is not None else None
            if axis in {"current", "voltage", "level"} and (
                (axis == "level" and mode == "current") or mode == axis
            ):
                field = "source_level_si"
            elif axis == "compliance_voltage" and mode == "current":
                field = "compliance_si"
            elif axis == "compliance_current" and mode == "voltage":
                field = "compliance_si"
            elif axis == "settling_time":
                field = "settle_time_s"
        elif len(parts) == 3 and parts[0] == "rigol":
            context = self._active_safety_context.get(f"rigol.{parts[1]}")
            field = {
                "frequency": "frequency_hz",
                "high_level": "high_level_v",
                "low_level": "low_level_v",
            }.get(parts[2])
            if parts[2] == "amplitude" and context is not None:
                high = context.get("high_level_v")
                low = context.get("low_level_v")
                if isinstance(high, (int, float)) and isinstance(low, (int, float)):
                    return float(high) - float(low)
            if parts[2] == "offset" and context is not None:
                high = context.get("high_level_v")
                low = context.get("low_level_v")
                if isinstance(high, (int, float)) and isinstance(low, (int, float)):
                    return (float(high) + float(low)) / 2.0
        elif len(parts) == 3 and parts[0] == "moke_box" and parts[2] == "voltage":
            context = self._active_safety_context.get("moke_box")
            if context is not None and parts[1] == f"vout{context.get('channel')}":
                field = "actual_v"
        elif parts == ["anritsu", "spectrum", "start_frequency"]:
            context = self._active_safety_context.get("anritsu")
            field = "start_hz"
        elif parts == ["anritsu", "spectrum", "stop_frequency"]:
            context = self._active_safety_context.get("anritsu")
            field = "stop_hz"
        elif parts == ["anritsu", "spectrum", "reference_level"]:
            context = self._active_safety_context.get("anritsu")
            field = "reference_level_dbm"
        elif parts == ["anritsu", "spectrum", "points"]:
            context = self._active_safety_context.get("anritsu")
            field = "points"
        elif parts == ["anritsu", "sg", "frequency"]:
            context = self._active_safety_context.get("anritsu.sg")
            field = "frequency_hz"
        elif parts == ["anritsu", "sg", "power"]:
            context = self._active_safety_context.get("anritsu.sg")
            field = "power_dbm"
        if context is None or field is None:
            return None
        value = context.get(field)
        return float(value) if isinstance(value, (int, float)) else None

    def _device_state_snapshot(self) -> dict[str, dict[str, object]]:
        return self._jsonable(self._device_states)  # type: ignore[return-value]

    def _append_checkpoint(
        self,
        point: MeasurementPoint,
        trace: SpectrumTrace | None = None,
        *,
        processed_values: tuple[float, ...] | None = None,
        processed_unit: str | None = None,
        processing_operation: str = "none",
    ) -> object:
        """Write once, with state evidence; any writer exception fails the run."""

        kwargs = {
            "processed_values": processed_values,
            "processed_unit": processed_unit,
            "processing_operation": processing_operation,
            "device_states": self._device_state_snapshot(),
        }
        if processed_values is None:
            kwargs.pop("processed_values")
            kwargs.pop("processed_unit")
            kwargs.pop("processing_operation")
        return self._writer.append(point, trace, **kwargs)

    def _runtime_state_snapshot(self) -> dict[str, object]:
        """Return a query-free state snapshot safe to attach to every audit event."""

        return {
            "application": self._state.value,
            "execution_mode": self._execution_mode.value,
            "outputs_forced_off": self.outputs_forced_off,
            "devices": {
                "rigol": self._rigol.state.value,
                "keithley": self._keithley.state.value,
                "anritsu": self._anritsu.state.value,
            },
            "rigol_outputs": dict(self._rigol_output_active),
            "keithley_outputs": dict(self._keithley_output_active),
            "anritsu_sg_output": self._anritsu_sg_output_active,
            "output_status": dict(self._output_status),
            "device_states": self._device_state_snapshot(),
            "reference": {"index": self._reference_index,
                          "fingerprint": self._reference_fingerprint,
                          "origin": dict(self._reference_origin)},
        }

    @staticmethod
    def _unknown_output_status() -> dict[str, str]:
        return {
            "rigol.1": "unknown",
            "rigol.2": "unknown",
            "keithley.A": "unknown",
            "keithley.B": "unknown",
            "anritsu.sg": "unknown",
        }

    def _confirm_output_state(self, endpoint: str, enabled: bool) -> None:
        """Record only an adapter-confirmed output state for the UI/event log."""

        self._output_status[endpoint] = "on" if enabled else "off"

    def _mark_output_unknown(self, endpoint: str | None = None) -> None:
        if endpoint is not None:
            self._output_status[endpoint] = "unknown"
            return
        for key in self._output_status:
            self._output_status[key] = "unknown"

    def _confirm_device_outputs_off(self, device: str) -> None:
        prefix = f"{device}."
        for endpoint in self._output_status:
            if endpoint.startswith(prefix):
                self._confirm_output_state(endpoint, False)

    @property
    def outputs_forced_off(self) -> bool:
        return self._execution_mode is ExecutionMode.DRY_RUN

    def _output_guard_devices(self) -> tuple[str, ...]:
        """Return the core devices whose outputs belong to this run's scope."""

        core = ("keithley", "rigol", "anritsu")
        return tuple(name for name in core if name in self._required_devices)

    def _confirm_dry_run_outputs_off(self) -> None:
        """Establish and confirm the dry-run invariant before recipe actions."""

        errors: list[str] = []
        devices = {
            "keithley": self._keithley,
            "rigol": self._rigol,
            "anritsu": self._anritsu,
        }
        for name in self._output_guard_devices():
            if name == "anritsu" and not self._anritsu_owns_output:
                continue
            device = devices[name]
            self._emit("dry_run_output_guard_started", {"device": name})
            try:
                self._shutdown_owned_device(name, device)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._emit_after_fault(
                    "dry_run_output_guard_error",
                    {"device": name, "error": str(exc)},
                )
            else:
                self._emit("dry_run_output_guard_confirmed", {"device": name})
        if errors:
            raise ExecutionError(
                "Dry run could not confirm all outputs OFF: " + "; ".join(errors)
            )
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._anritsu_sg_output_active = False
        for device in self._output_guard_devices():
            if device == "anritsu" and not self._anritsu_owns_output:
                continue
            self._confirm_device_outputs_off(device)

    def _emit_dry_run_suppression(
        self, action: PlanAction, requested_enabled: bool, actual_enabled: bool
    ) -> None:
        if not self.outputs_forced_off:
            return
        if actual_enabled is not False:
            raise ExecutionError(
                f"Dry run output guard failed for {action.node_id!r}: OUTPUT OFF was not confirmed."
            )
        if not requested_enabled:
            return
        self._emit(
            "dry_run_output_action_suppressed",
            {
                "node_id": action.node_id,
                "kind": action.kind,
                "requested_enabled": True,
                "actual_enabled": False,
                "reason": "Dry run replaced OUTPUT ON with confirmed OUTPUT OFF.",
            },
        )

    def _record_safe_boundary_if_advanced(
        self,
        *,
        stored_points: int,
        next_action_index: int,
        plan_hash: str,
    ) -> None:
        if stored_points <= self._last_safe_boundary_points:
            return
        rigol_safe = not any(self._rigol_output_active.values())
        keithley_safe = not any(self._keithley_output_active.values())
        anritsu_safe = not self._anritsu_sg_output_active
        if not (rigol_safe and keithley_safe and anritsu_safe):
            return
        scope = getattr(self, "_resume_output_scope", set(self._output_status))
        if any(self._output_status.get(endpoint) != "off" for endpoint in scope):
            return
        self._emit(
            "safe_resume_boundary",
            {
                "stored_points": stored_points,
                "next_action_index": next_action_index,
                "plan_sha256": plan_hash,
                "rigol_outputs": dict(self._rigol_output_active),
                "keithley_outputs": dict(self._keithley_output_active),
                "keithley_zeroed": dict(self._keithley_zeroed),
                "anritsu_sg_output": self._anritsu_sg_output_active,
                "output_status": dict(self._output_status),
                "confirmed_output_scope": sorted(scope),
            },
        )
        self._last_safe_boundary_points = stored_points

    def _emit(self, name: str, data: dict[str, object], *, notify=True) -> dict[str, object]:
        payload = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **data,
            "correlation_id": self._correlation_id,
            "cancellation_token_id": self._correlation_id,
            "cancellation_requested": self._stop_requested.is_set(),
            "state_snapshot": self._runtime_state_snapshot(),
        }
        severity = (
            "error"
            if name in {
                "action_failed",
                "dry_run_output_guard_error",
                "run_fault",
                "shutdown_error",
            }
            else "info"
        )
        self._writer.append_event(name, payload, severity=severity)
        if notify:
            self._on_event(name, payload)
        return payload

    def _emit_after_fault(self, name: str, data: dict[str, object]) -> None:
        """Best-effort diagnostics that can never stop the shutdown sequence."""

        try:
            self._emit(name, data)
        except Exception:
            # A storage failure is itself already the originating fault.  The
            # hardware must still receive every emergency-off command.
            pass

