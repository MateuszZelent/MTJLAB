"""Synchronous execution core intended to run inside one dedicated Qt worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Callable, Literal

from app.devices.anritsu.adapter import AnritsuAdapter, SpectrumTrace
from app.devices.keithley.adapter import KeithleyAdapter
from app.devices.rigol.adapter import RigolAdapter
from app.domain.errors import ExecutionError
from app.domain.models import ApplicationState, MeasurementPoint
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
        self._emit("run_started", {"recipe": plan.recipe_name, "actions": len(plan.actions), "hash": plan.sha256})
        try:
            for action in plan.actions:
                self._raise_if_stop_requested()
                self._wait_if_paused()
                self._emit("action_started", {"node_id": action.node_id, "kind": action.kind})
                trace = self._execute(action, point_measurements)
                point_setpoints.update(action.setpoints_si)
                completed += 1
                if trace is not None:
                    point = MeasurementPoint(
                        index=stored,
                        setpoints=dict(point_setpoints),
                        measurements=dict(point_measurements),
                        metadata={"plan_node": action.node_id},
                    )
                    self._writer.append(point, trace)
                    stored += 1
                    point_measurements.clear()
                    self._pause_at_point_if_requested()
                self._emit("action_finished", {"node_id": action.node_id, "kind": action.kind})
            self._safe_shutdown()
            self._writer.close("completed")
            self._state = ApplicationState.SAFE
            return RunResult(self._state, completed, stored)
        except Exception as exc:
            self._state = ApplicationState.FAULT
            self._emit("run_fault", {"error": str(exc)})
            self._safe_shutdown()
            self._writer.close("aborted" if self._stop_requested.is_set() else "faulted")
            return RunResult(self._state, completed, stored, str(exc))

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
        elif action.kind == "set_keithley_output":
            self._keithley.set_output(payload["channel"], payload["enabled"])
        elif action.kind == "measure_keithley":
            result = self._keithley.measure(payload["channel"])
            prefix = f"keithley.{payload['channel']}"
            measurements[f"{prefix}.voltage_v"] = result.voltage_v
            measurements[f"{prefix}.current_a"] = result.current_a
            measurements[f"{prefix}.power_w"] = result.power_w
        elif action.kind == "acquire_spectrum":
            return self._anritsu.fetch_trace(payload["trace"])
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

    def _safe_shutdown(self) -> None:
        self._state = ApplicationState.STOPPING
        for device in (self._keithley, self._rigol, self._anritsu):
            try:
                device.emergency_off()
            except Exception as exc:
                self._emit("shutdown_error", {"device": type(device).__name__, "error": str(exc)})

    def _emit(self, name: str, data: dict[str, object]) -> None:
        self._on_event(name, {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **data})

