"""Synchronous execution core intended to run inside one dedicated Qt worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Callable

from app.devices.anritsu.adapter import AnritsuAdapter, SpectrumTrace
from app.devices.keithley.adapter import KeithleyAdapter
from app.devices.rigol.adapter import RigolAdapter
from app.domain.errors import ExecutionError
from app.domain.models import ApplicationState, DeviceState, MeasurementPoint
from app.engine.compiler import ExecutionPlan, PlanAction
from app.storage.hdf5_writer import Hdf5RunWriter


EventCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class RunResult:
    state: ApplicationState
    completed_actions: int
    stored_points: int
    error: str | None = None


class RecipeRunner:
    """Execute an already preflighted plan and always attempt safe shutdown."""

    def __init__(
        self,
        *,
        rigol: RigolAdapter,
        keithley: KeithleyAdapter,
        anritsu: AnritsuAdapter,
        writer: Hdf5RunWriter,
        on_event: EventCallback | None = None,
    ) -> None:
        self._rigol = rigol
        self._keithley = keithley
        self._anritsu = anritsu
        self._writer = writer
        self._on_event = on_event or (lambda _name, _data: None)
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_requested = threading.Event()
        self._resume_requested.set()
        self._state = ApplicationState.SAFE

    @property
    def state(self) -> ApplicationState:
        return self._state

    def request_stop(self) -> None:
        self._stop_requested.set()

    def pause_after_point(self) -> None:
        self._pause_requested.set()

    def resume(self) -> None:
        self._pause_requested.clear()
        self._resume_requested.set()

    def run(self, plan: ExecutionPlan) -> RunResult:
        completed = 0
        stored = 0
        point_measurements: dict[str, float] = {}
        point_setpoints: dict[str, float] = {}
        self._state = ApplicationState.RUNNING
        try:
            self._emit("run_started", {"recipe": plan.recipe_name, "actions": len(plan.actions), "hash": plan.sha256})
            for action in plan.actions:
                self._raise_if_stop_requested()
                self._wait_if_paused()
                self._emit("action_started", {"node_id": action.node_id, "kind": action.kind})
                trace = self._execute(action, point_measurements)
                point_setpoints.update(action.setpoints_si)
                completed += 1
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
                        },
                    )
                    self._writer.append(point)
                    stored += 1
                    point_measurements.clear()
                    self._emit("compliance_detected", {"channels": compliance_channels, "point_index": stored - 1})
                    raise ExecutionError("Keithley osiągnął compliance; zapisano ostatni checkpoint i wyłączono wyjście.")
                if trace is not None:
                    point = MeasurementPoint(
                        index=stored,
                        setpoints=dict(point_setpoints),
                        measurements=dict(point_measurements),
                        metadata={"plan_node": action.node_id, "monotonic_s": time.monotonic()},
                    )
                    self._writer.append(point, trace)
                    stored += 1
                    point_measurements.clear()
                    self._pause_at_point_if_requested()
                self._emit("action_finished", {"node_id": action.node_id, "kind": action.kind})
            if not self._safe_shutdown():
                raise ExecutionError("Nie potwierdzono bezpiecznego shutdownu wszystkich urządzeń.")
            self._state = ApplicationState.SAFE
            self._emit("run_completed", {"completed_actions": completed, "stored_points": stored})
            self._writer.close("completed")
            return RunResult(self._state, completed, stored)
        except Exception as exc:
            if self._stop_requested.is_set():
                return self._abort_safely(plan, completed, stored, point_measurements, str(exc))
            self._state = ApplicationState.FAULT
            self._emit_after_fault("run_fault", {"error": str(exc)})
            self._safe_shutdown()
            self._state = ApplicationState.FAULT
            self._writer.close("faulted")
            return RunResult(self._state, completed, stored, str(exc))

    def _abort_safely(
        self,
        plan: ExecutionPlan,
        completed: int,
        stored: int,
        measurements: dict[str, float],
        reason: str,
    ) -> RunResult:
        """Run only preflight-approved cleanup actions after an operator stop."""

        cleanup_ok = True
        self._emit_after_fault("run_aborting", {"reason": reason})
        for action in plan.actions:
            if not action.is_finally:
                continue
            try:
                self._emit_after_fault("safe_finally_started", {"node_id": action.node_id, "kind": action.kind})
                self._execute(action, measurements)
                self._emit_after_fault("safe_finally_finished", {"node_id": action.node_id, "kind": action.kind})
            except Exception as exc:
                cleanup_ok = False
                self._emit_after_fault(
                    "safe_finally_error", {"node_id": action.node_id, "kind": action.kind, "error": str(exc)}
                )
        shutdown_ok = self._safe_shutdown()
        if cleanup_ok and shutdown_ok:
            self._state = ApplicationState.SAFE
            self._emit_after_fault("run_aborted", {"completed_actions": completed, "stored_points": stored})
            self._writer.close("aborted")
            return RunResult(self._state, completed, stored, reason)
        self._state = ApplicationState.FAULT
        self._emit_after_fault("run_fault", {"error": "Nie potwierdzono bezpiecznego zatrzymania: " + reason})
        self._writer.close("faulted")
        return RunResult(self._state, completed, stored, reason)

    def _execute(self, action: PlanAction, measurements: dict[str, float]) -> SpectrumTrace | None:
        payload = action.payload
        if action.kind == "configure_rigol":
            self._rigol.configure_channel(payload["config"])
        elif action.kind == "configure_keithley":
            self._keithley.configure_source(payload["request"])
        elif action.kind == "configure_anritsu":
            self._anritsu.configure_spectrum(payload["config"])
        elif action.kind == "set_rigol_output":
            self._rigol.set_output(payload["channel"], payload["enabled"])
        elif action.kind == "arm_rigol_output":
            self._rigol.arm_output(payload["channel"])
        elif action.kind == "set_keithley_output":
            self._keithley.set_output(payload["channel"], payload["enabled"])
        elif action.kind == "arm_keithley_output":
            self._keithley.arm_output(payload["channel"])
        elif action.kind == "ramp_keithley_to_zero":
            self._keithley.ramp_to_zero(payload["channel"], deadline_s=payload["deadline_s"])
        elif action.kind == "measure_keithley":
            result = self._keithley.measure(payload["channel"])
            prefix = f"keithley.{payload['channel']}"
            measurements[f"{prefix}.voltage_v"] = result.voltage_v
            measurements[f"{prefix}.current_a"] = result.current_a
            measurements[f"{prefix}.power_w"] = result.power_w
            measurements[f"{prefix}.compliance_detected"] = float(result.compliance_detected)
            measurements[f"{prefix}.compliance_stop_required"] = float(result.compliance_stop_required)
        elif action.kind == "acquire_spectrum":
            return self._anritsu.acquire_single_sweep(payload["trace"])
        elif action.kind == "wait":
            self._interruptible_wait(payload["duration_s"])
        else:
            raise ExecutionError(f"Runner nie obsługuje akcji {action.kind!r}.")
        return None

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
            raise ExecutionError("Operator zatrzymał pomiar.")

    def _wait_if_paused(self) -> None:
        while self._pause_requested.is_set():
            self._state = ApplicationState.PAUSED
            self._resume_requested.clear()
            if self._stop_requested.wait(0.05):
                self._raise_if_stop_requested()
            if self._resume_requested.is_set():
                self._state = ApplicationState.RUNNING
                return

    def _pause_at_point_if_requested(self) -> None:
        if self._pause_requested.is_set():
            self._emit("pause_pending", {})
            self._wait_if_paused()

    def _safe_shutdown(self) -> bool:
        """Attempt every shutdown action and report whether all states are confirmed."""

        self._state = ApplicationState.STOPPING
        confirmed = True
        for device in (self._keithley, self._rigol, self._anritsu):
            try:
                device.emergency_off()
                if device.state is DeviceState.UNKNOWN:
                    confirmed = False
                    self._emit_after_fault(
                        "shutdown_error",
                        {
                            "device": type(device).__name__,
                            "error": "Brak potwierdzenia bezpiecznego stanu urządzenia.",
                        },
                    )
            except Exception as exc:
                confirmed = False
                self._emit_after_fault("shutdown_error", {"device": type(device).__name__, "error": str(exc)})
        return confirmed

    @staticmethod
    def _compliance_channels(measurements: dict[str, float]) -> tuple[str, ...]:
        channels = []
        for key, value in measurements.items():
            if key.endswith(".compliance_stop_required") and value:
                channels.append(key.removesuffix(".compliance_stop_required"))
        return tuple(channels)

    def _emit(self, name: str, data: dict[str, object]) -> None:
        payload = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **data}
        severity = "error" if name in {"run_fault", "shutdown_error"} else "info"
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
