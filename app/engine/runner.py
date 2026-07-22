"""Synchronous execution core intended to run inside one dedicated Qt worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Callable
from uuid import uuid4
from enum import Enum

from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter, SpectrumTrace
from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import hall_field_from_voltage
from app.devices.lakeshore_475.adapter import LakeShore475Adapter
from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.domain.errors import DeviceError, ExecutionError
from app.domain.models import ApplicationState, DeviceState, MeasurementPoint
from app.engine.compiler import ExecutionPlan, PlanAction, required_devices_for_actions
from app.engine.policy import ExecutionPolicy
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
    DEMO_OUTPUTS_OFF = "demo_outputs_off"
    MANUAL_STEP = "manual_step"


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
        self._lakeshore = lakeshore
        self._writer = writer
        self._on_event = on_event or (lambda _name, _data: None)
        self._on_telemetry = on_telemetry or (lambda _name, _data: None)
        self._policy = policy or ExecutionPolicy()
        self._execution_mode = ExecutionMode(execution_mode)
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_requested = threading.Event()
        self._resume_requested.set()
        self._manual_step_permit = threading.Event()
        self._state = ApplicationState.SAFE
        self._required_devices: frozenset[str] = frozenset()
        self._safe_shutdown_actions: tuple[str, ...] = ()
        self._active_safety_context: dict[str, dict[str, object]] = {}
        self._device_states: dict[str, dict[str, object]] = {}
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._output_status = self._unknown_output_status()
        self._keithley_zeroed = {"A": True, "B": True}
        self._anritsu_sg_output_active = False
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
    ) -> RunResult:
        if start_action_index < 0 or start_action_index > len(plan.actions):
            raise ExecutionError("Invalid recovery action index.")
        if stored_points < 0 or stored_points > plan.total_points:
            raise ExecutionError("Invalid recovered point count.")
        completed = start_action_index
        stored = stored_points
        point_measurements: dict[str, float] = {}
        point_setpoints: dict[str, float] = {}
        run_started_monotonic = time.monotonic()
        self._required_devices = (
            plan.required_devices or required_devices_for_actions(plan.actions)
        )
        self._safe_shutdown_actions = plan.safe_shutdown_actions
        self._active_safety_context = {}
        self._device_states = {}
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._output_status = self._unknown_output_status()
        self._keithley_zeroed = {"A": True, "B": True}
        self._anritsu_sg_output_active = False
        self._last_safe_boundary_points = stored_points
        self._watchdog_timed_out.clear()
        self._reference_trace = None
        self._reference_index = None
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
                },
            )
            if self.outputs_forced_off:
                self._confirm_demo_outputs_off()
            for action in recovery_prelude:
                if action.kind not in {
                    "configure_rigol",
                    "configure_keithley",
                    "configure_anritsu",
                    "configure_anritsu_advanced",
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
            for action_index, action in enumerate(
                plan.actions[start_action_index:], start=start_action_index
            ):
                self._raise_if_stop_requested()
                current_action = action
                if action.is_finally:
                    attempted_finally.add(action.node_id)
                if (
                    self._execution_mode is ExecutionMode.MANUAL_STEP
                    and not action.is_finally
                ):
                    self._wait_for_manual_step(action, action_index, len(plan.actions))
                self._emit(
                    "action_started",
                    {
                        "node_id": action.node_id,
                        "kind": action.kind,
                        **self._action_display_context(action),
                        "action_index": action_index,
                        "setpoints_si": dict(action.setpoints_si),
                        "deadline_s": self._policy.deadline_for(action),
                        "cancellation_requested": self._stop_requested.is_set(),
                    },
                )
                acquisition = self._execute_with_policy(action, point_measurements)
                point_setpoints.update(action.setpoints_si)
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
                            "reference_index": (
                                acquisition.reference_index
                                if acquisition is not None
                                else None
                            ),
                            "execution_mode": self._execution_mode.value,
                            "outputs_forced_off": self.outputs_forced_off,
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
                        self._emit_spectrum_preview(trace, point.index)
                    point_measurements.clear()
                    self._pause_at_point_if_requested()
                self._emit(
                    "action_finished",
                    {
                        "node_id": action.node_id,
                        "kind": action.kind,
                        **self._action_display_context(action),
                    },
                )
                current_action = None
                self._record_safe_boundary_if_advanced(
                    stored_points=stored,
                    next_action_index=completed,
                    plan_hash=plan.sha256,
                )
            if not self._safe_shutdown():
                raise ExecutionError("Safe shutdown was not confirmed for every instrument.")
            self._state = ApplicationState.SAFE
            self._emit("run_completed", {"completed_actions": completed, "stored_points": stored})
            self._writer.close("completed")
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
            self._writer.close("faulted")
            return RunResult(self._state, completed, stored, str(exc))
        finally:
            self._stop_watchdog()

    def _execute_with_policy(
        self,
        action: PlanAction,
        measurements: dict[str, float],
    ) -> _AcquiredSpectrum | None:
        retries = self._policy.retry_count if self._can_retry(action) else 0
        for attempt in range(retries + 1):
            deadline_s = self._policy.deadline_for(action)
            self._begin_action_watchdog(
                action, attempt=attempt + 1, deadline_s=deadline_s
            )
            started = time.monotonic()
            try:
                adapter = self._adapter_for_action(action)
                if adapter is None:
                    result = self._execute(action, measurements)
                else:
                    with adapter.io_timeout(self._policy.command_timeout_s):
                        result = self._execute(action, measurements)
                elapsed = time.monotonic() - started
                if elapsed > deadline_s:
                    self._flag_watchdog_timeout(action, attempt + 1, elapsed, deadline_s)
                    raise ExecutionError(
                        f"Action {action.node_id!r} exceeded its {deadline_s:.3g} s deadline."
                    )
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
        if action.kind == "set_rigol_output":
            return not bool(action.payload["enabled"])
        if action.kind == "set_keithley_output":
            return not bool(action.payload["enabled"])
        if action.kind == "set_anritsu_sg_output":
            return not bool(action.payload["enabled"])
        return action.kind in {
            "configure_anritsu",
            "configure_anritsu_advanced",
            "configure_anritsu_sg",
            "measure_keithley",
            "acquire_reference",
            "acquire_spectrum",
            "ramp_keithley_to_zero",
        }

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
        channel = action.payload.get("channel")
        if channel is None:
            config = action.payload.get("config")
            request = action.payload.get("request")
            channel = getattr(config, "channel", getattr(request, "channel", None))
        if channel is not None:
            context["channel"] = channel
        return context

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
    ) -> None:
        maximum_preview_points = 1_000
        step = max(
            1,
            (len(trace.powers_dbm) + maximum_preview_points - 1) // maximum_preview_points,
        )
        self._emit_telemetry(
            "reference_preview" if preview_kind == "reference" else "spectrum_preview",
            {
                "point_index": point_index,
                "preview_kind": preview_kind,
                "trace_name": trace.trace_name,
                "frequency_hz": trace.frequencies_hz[::step],
                "power_dbm": trace.powers_dbm[::step],
                "source_points": len(trace.powers_dbm),
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
            self._state = ApplicationState.SAFE
            self._emit_after_fault("run_aborted", {"completed_actions": completed, "stored_points": stored})
            self._writer.close("aborted")
            return RunResult(self._state, completed, stored, reason)
        self._state = ApplicationState.FAULT
        self._emit_after_fault("run_fault", {"error": "Safe stop was not confirmed: " + reason})
        self._writer.close("faulted")
        return RunResult(self._state, completed, stored, reason)

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
            if not action.is_finally or action.node_id in attempted:
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
        self, action: PlanAction, measurements: dict[str, float]
    ) -> _AcquiredSpectrum | None:
        payload = action.payload
        if action.kind == "configure_rigol":
            config = payload["config"]
            self._rigol.configure_channel(config)
            self._rigol_output_active[config.channel] = False
            self._confirm_output_state(f"rigol.{config.channel}", False)
            envelope = config.dut_envelope
            self._active_safety_context[f"rigol.{config.channel}"] = {
                "minimum_impedance_ohm": (
                    envelope.minimum_impedance_ohm
                    if envelope is not None
                    else config.dut_min_impedance_ohm
                ),
                "max_abs_current_a": (
                    envelope.max_abs_current_a if envelope is not None else None
                ),
                "max_abs_power_w": (
                    envelope.max_abs_power_w if envelope is not None else None
                ),
                "frequency_hz": config.frequency_hz,
                "high_level_v": config.high_level_v,
                "low_level_v": config.low_level_v,
                "output_load": config.output_load,
            }
            self._record_device_state(
                "rigol", f"channel_{config.channel}", requested=config,
                actual=self._active_safety_context[f"rigol.{config.channel}"],
            )
        elif action.kind == "configure_rigol_output":
            config = payload["config"]
            self._rigol.configure_output(config)
            self._rigol_output_active[config.channel] = False
            self._confirm_output_state(f"rigol.{config.channel}", False)
            context = self._active_safety_context.get(
                f"rigol.{config.channel}", {}
            )
            context["output_path"] = {
                "polarity": config.polarity,
                "mode": config.mode,
                "gate_polarity": config.gate_polarity,
                "sync_enabled": config.sync_enabled,
                "sync_polarity": config.sync_polarity,
                "sync_delay_s": config.sync_delay_s,
            }
            self._active_safety_context[f"rigol.{config.channel}"] = context
            self._record_device_state(
                "rigol", f"channel_{config.channel}", requested=config, actual=context
            )
        elif action.kind == "configure_keithley":
            request = payload["request"]
            self._keithley.configure_source(request)
            self._keithley_output_active[request.channel] = False
            self._confirm_output_state(f"keithley.{request.channel}", False)
            self._keithley_zeroed[request.channel] = abs(request.level_si) <= 1e-15
            envelope = request.dut_envelope
            self._active_safety_context[f"keithley.{request.channel}"] = {
                "mode": request.mode,
                "source_level_si": request.level_si,
                "compliance_si": request.compliance_si,
                "current_min_a": envelope.current_min_a if envelope is not None else None,
                "current_max_a": envelope.current_max_a if envelope is not None else None,
                "voltage_min_v": envelope.voltage_min_v if envelope is not None else None,
                "voltage_max_v": envelope.voltage_max_v if envelope is not None else None,
                "max_abs_power_w": envelope.max_abs_power_w if envelope is not None else None,
            }
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
            self._active_safety_context["anritsu"] = {
                "start_hz": actual.start_hz,
                "stop_hz": actual.stop_hz,
                "reference_level_dbm": actual.reference_level_dbm,
                "points": actual.points,
                "instrument_mode": actual.instrument_mode,
                "max_expected_input_dbm": config.dut_max_expected_input_dbm,
            }
            self._record_device_state(
                "anritsu", "spectrum", requested=config,
                actual=self._active_safety_context["anritsu"],
            )
        elif action.kind == "configure_anritsu_advanced":
            config = payload["config"]
            actual = self._anritsu.configure_advanced_spectrum(config)
            self._active_safety_context["anritsu.advanced"] = {
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
            self._emit_demo_suppression(action, requested_enabled, actual_enabled)
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
            self._emit_demo_suppression(action, requested_enabled, actual_enabled)
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
            self._emit_demo_suppression(
                action, requested_enabled, actual_enabled
            )
        elif action.kind == "ramp_keithley_to_zero":
            self._keithley.ramp_to_zero(payload["channel"], deadline_s=payload["deadline_s"])
            self._keithley_zeroed[payload["channel"]] = True
        elif action.kind == "measure_keithley":
            result = self._keithley.measure(payload["channel"])
            prefix = f"keithley.{payload['channel']}"
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
        elif action.kind == "measure_moke_hall":
            if self._moke_box is None:
                raise ExecutionError("MOKE Hall measurement was requested but MOKE Box is unavailable.")
            result = self._moke_box.read_hall_voltage()
            measurements["moke_box.hall1_voltage_v"] = result.voltage_v
            measurements["moke_box.hall1_field_t"] = hall_field_from_voltage(result.voltage_v)
            measurements["moke_box.hall1_stddev_v"] = result.stddev_v
            measurements["moke_box.hall1_raw_ad7734"] = float(result.raw_codes[0])
            self._record_device_state(
                "moke_box",
                "hall_readback",
                requested={"sample_count": 1},
                actual={
                    "voltage_v": result.voltage_v,
                    "field_t": measurements["moke_box.hall1_field_t"],
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
            average_count = int(payload.get("average_count", 1))
            reference = self._acquire_averaged_spectrum(
                payload["trace"],
                payload.get("dut_max_expected_input_dbm"),
                average_count,
            )
            stored_reference_index = self._writer.store_reference(
                reference,
                kind="single" if average_count == 1 else "averaged",
                average_count=average_count,
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
                },
            )
            self._emit_spectrum_preview(
                reference,
                self._reference_index if self._reference_index is not None else -1,
                preview_kind="reference",
            )
        elif action.kind == "acquire_spectrum":
            average_count = int(payload.get("average_count", 1))
            trace = self._acquire_averaged_spectrum(
                payload["trace"],
                payload.get("dut_max_expected_input_dbm"),
                average_count,
            )
            operation = str(payload.get("reference_operation", "none"))
            processed: tuple[float, ...] | None = None
            processed_unit: str | None = None
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
                processed, processed_unit = apply_reference_operation(
                    trace.powers_dbm,
                    reference.powers_dbm,
                    operation,
                )
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
                reference_index=(
                    self._reference_index if operation != "none" else None
                ),
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
        else:
            raise ExecutionError(f"The runner does not support action {action.kind!r}.")
        return None

    def _acquire_averaged_spectrum(
        self,
        trace_name: str,
        dut_max_expected_input_dbm: float | None,
        average_count: int,
    ) -> SpectrumTrace:
        """Acquire complete, grid-matched traces and average in linear power."""

        averager = LinearPowerAverager()
        first: SpectrumTrace | None = None
        latest: SpectrumTrace | None = None
        for _index in range(average_count):
            self._raise_if_stop_requested()
            latest = self._anritsu.acquire_single_sweep(
                trace_name, dut_max_expected_input_dbm
            )
            if first is None:
                first = latest
            elif not frequency_grids_match(
                first.frequencies_hz, latest.frequencies_hz
            ):
                raise ExecutionError(
                    "Anritsu averaging aborted because the frequency grid changed "
                    "between complete spectra."
                )
            averager.add(latest.powers_dbm)
        if first is None or latest is None:
            raise ExecutionError("Anritsu averaging requires at least one spectrum.")
        return SpectrumTrace(
            frequencies_hz=first.frequencies_hz,
            powers_dbm=averager.result(),
            acquired_at_utc=latest.acquired_at_utc,
            trace_name=latest.trace_name,
        )

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

    def _pause_at_point_if_requested(self) -> None:
        if self._pause_requested.is_set():
            self._emit("pause_pending", {})
            self._wait_if_paused()

    def _safe_shutdown(self) -> bool:
        """Attempt every shutdown action and report whether all states are confirmed."""

        self._state = ApplicationState.STOPPING
        confirmed = True
        devices = {
            "keithley": self._keithley,
            "rigol": self._rigol,
            "anritsu": self._anritsu,
        }
        default_actions = {
            "keithley": "keithley.outputs_off",
            "rigol": "rigol.outputs_off",
            "anritsu": "anritsu.rf_off_and_abort",
        }
        actions = self._safe_shutdown_actions or tuple(
            default_actions[name] for name in ("keithley", "rigol", "anritsu")
        ) + ("storage.flush_checkpoint",)
        attempted_devices: set[str] = set()
        for action in actions:
            self._emit_after_fault("shutdown_action_started", {"action": action})
            try:
                if action == "storage.flush_checkpoint":
                    flush = getattr(self._writer, "flush_checkpoint", None)
                    if callable(flush):
                        flush()
                else:
                    name = action.split(".", 1)[0]
                    device = devices[name]
                    attempted_devices.add(name)
                    device.emergency_off()
                    if device.state is DeviceState.UNKNOWN:
                        raise ExecutionError(
                            "The instrument did not confirm a safe state."
                        )
            except Exception as exc:
                confirmed = False
                self._emit_after_fault(
                    "shutdown_error",
                    {"action": action, "error": str(exc)},
                )
            else:
                if action != "storage.flush_checkpoint":
                    self._confirm_device_outputs_off(name)
                self._emit_after_fault("shutdown_action_finished", {"action": action})
        # A malformed manually-created plan must not be able to omit OFF for a
        # required device. Compiled plans already contain this fallback.
        for name in sorted(set(devices) - attempted_devices):
            action = default_actions[name]
            try:
                devices[name].emergency_off()
                if devices[name].state is DeviceState.UNKNOWN:
                    raise ExecutionError(
                        "The instrument did not confirm a safe state."
                    )
            except Exception as exc:
                confirmed = False
                self._emit_after_fault(
                    "shutdown_error",
                    {"action": action, "error": str(exc), "fallback": True},
                )
            else:
                self._confirm_device_outputs_off(name)
        return confirmed

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
        }

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
        """Write a stateful checkpoint while retaining legacy diagnostic writers."""

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
        try:
            return self._writer.append(point, trace, **kwargs)
        except TypeError as exc:
            if "device_states" not in str(exc):
                raise
            kwargs.pop("device_states")
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
        return self._execution_mode is ExecutionMode.DEMO_OUTPUTS_OFF

    def _confirm_demo_outputs_off(self) -> None:
        """Establish and confirm the demo invariant before any recipe action."""

        errors: list[str] = []
        for name, device in (
            ("keithley", self._keithley),
            ("rigol", self._rigol),
            ("anritsu", self._anritsu),
        ):
            self._emit("demo_output_guard_started", {"device": name})
            try:
                device.emergency_off()
                if device.state is DeviceState.UNKNOWN:
                    raise ExecutionError(
                        "The instrument did not confirm OUTPUT OFF."
                    )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self._emit_after_fault(
                    "demo_output_guard_error",
                    {"device": name, "error": str(exc)},
                )
            else:
                self._emit("demo_output_guard_confirmed", {"device": name})
        self._rigol_output_active = {1: False, 2: False}
        self._keithley_output_active = {"A": False, "B": False}
        self._anritsu_sg_output_active = False
        for device in ("keithley", "rigol", "anritsu"):
            self._confirm_device_outputs_off(device)
        if errors:
            raise ExecutionError(
                "Demo mode could not confirm all outputs OFF: " + "; ".join(errors)
            )

    def _emit_demo_suppression(
        self, action: PlanAction, requested_enabled: bool, actual_enabled: bool
    ) -> None:
        if not (self.outputs_forced_off and requested_enabled):
            return
        if actual_enabled:
            raise ExecutionError(
                f"Demo mode output guard failed for {action.node_id!r}: OUTPUT is ON."
            )
        self._emit(
            "demo_output_action_suppressed",
            {
                "node_id": action.node_id,
                "kind": action.kind,
                "requested_enabled": True,
                "actual_enabled": False,
                "reason": "Demo mode replaced OUTPUT ON with confirmed OUTPUT OFF.",
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
        if not (rigol_safe and keithley_safe):
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
            },
        )
        self._last_safe_boundary_points = stored_points

    def _emit(self, name: str, data: dict[str, object]) -> None:
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
                "demo_output_guard_error",
                "run_fault",
                "shutdown_error",
            }
            else "info"
        )
        self._writer.append_event(name, payload, severity=severity)
        self._on_event(name, payload)

    def _emit_after_fault(self, name: str, data: dict[str, object]) -> None:
        """Best-effort diagnostics that can never stop the shutdown sequence."""

        try:
            self._emit(name, data)
        except Exception:
            # A storage failure is itself already the originating fault.  The
            # hardware must still receive every emergency-off command.
            pass

