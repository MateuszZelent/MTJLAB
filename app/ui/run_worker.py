"""Dedicated Qt worker for a complete multi-device measurement run."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from ruamel.yaml import YAML

from app.bootstrap import StationComposition
from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.moke_box import MokeBoxAdapter
from app.devices.moke_box.module import create_simulated_moke_adapter
from app.devices.lakeshore_475 import LakeShore475Adapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.devices.simulation import SimulationContext
from app.domain.models import DeviceState, ApplicationState
from app.engine.compiler import ExecutionPlan, required_devices_for_actions, controlled_output_endpoints
from app.engine.policy import ExecutionPolicy
from app.engine.estimation import PlanEstimator, require_storage_capacity
from app.engine.recovery import RecoveryCheckpoint
from app.engine.runner import ExecutionMode, RecipeRunner
from app.settings.models import StationSettings
from app.storage import Hdf5RunWriter
from app.ui.workers import DeviceController


from app.storage.naming import automated_run_file_stem, sanitize_run_file_stem

__all__ = [
    "RunController",
    "RunWorker",
    "automated_run_file_stem",
    "planned_run_paths",
    "sanitize_run_file_stem",
    "serialize_settings_snapshot",
]


def planned_run_paths(
    settings: StationSettings,
    recipe_name: str,
    *,
    output_dir_override: str | Path | None = None,
    file_stem_override: str | None = None,
    timestamp: datetime | None = None,
    sample_target: object | None = None,
    reserve_directory: bool = False,
) -> tuple[Path, Path | None]:
    """Return the HDF5 and optional CSV path for a new run."""

    raw_output_dir = output_dir_override or settings.storage.get(
        "output_directory", "./measurements"
    )
    output_dir = Path(str(raw_output_dir)).expanduser()
    pattern = None
    if hasattr(settings, "storage") and isinstance(settings.storage, Mapping):
        pattern = settings.storage.get("filename_pattern")
    base_name = automated_run_file_stem(
        recipe_name,
        sample_target=sample_target,
        file_stem_override=file_stem_override,
        pattern=pattern,
    )
    from app.storage.run_bundle import bundle_directory
    output_dir = bundle_directory(
        output_dir, base_name if file_stem_override else recipe_name,
        timestamp or datetime.now(timezone.utc), reserve=reserve_directory,
    )
    csv_path = (
        output_dir / "data.csv"
        if hasattr(settings, "storage") and settings.storage.get("write_csv_summary")
        else None
    )
    return output_dir / "data.h5", csv_path


def serialize_settings_snapshot(
    settings: StationSettings,
    settings_path: Path,
    *,
    simulation: bool,
) -> str:
    """Return the exact settings provenance used for new and resumed runs."""

    stream = StringIO()
    if simulation:
        stream.write("# SIMULATION: in-memory profile; not persisted to settings.yml.\n")
    YAML().dump(settings.model_dump(mode="python"), stream)
    return stream.getvalue()


class RunTelemetryCoalescer:
    """Forward safety events immediately and bound high-rate UI telemetry.

    Spectrum previews and point checkpoints are presentation data, not the
    durable measurement record.  A large sweep can produce one of each per
    point, and emitting all of them into Qt's GUI event queue makes the
    application appear frozen even though the run worker is healthy.  Keep
    only the newest frame per telemetry stream during the short display
    interval. In pull mode the GUI owns that cadence: a busy GUI cannot
    accumulate queued frames. Watchdog and safety events bypass the buffer;
    duplicate technical boundaries already represented by semantic events
    are omitted from presentation only.

    The runner's watchdog callback can arrive from a helper thread, therefore
    the pending-frame bookkeeping is protected independently of the Qt signal
    dispatch.  The callback itself is always invoked outside the lock.
    """

    _COALESCED_NAMES = frozenset(
        {
            "runner_heartbeat",
            "point_stored",
            "spectrum_preview",
            "reference_preview",
            "moke_ramp_progress",
        }
    )
    _SEMANTIC_NAMES = frozenset(
        {
            "semantic_operation_started",
            "semantic_operation_applied",
            "semantic_operation_failed",
        }
    )

    def __init__(
        self,
        emit: Callable[[str, object], None],
        *,
        interval_s: float = 0.1,
        pull_mode: bool = False,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("Telemetry coalescing interval must be positive.")
        self._emit = emit
        self._interval_s = float(interval_s)
        self._pull_mode = pull_mode
        self._lock = threading.Lock()
        self._last_emit: dict[str, float] = {}
        self._pending: dict[str, tuple[str, object]] = {}
        self._pending_semantic: dict[
            tuple[str, str], tuple[str, object, int]
        ] = {}

    def submit(self, name: str, data: object) -> None:
        if self._pull_mode and name in {
            "watchdog_timeout", "run_fault", "run_aborting", "run_aborted",
            "action_failed", "compliance_detected", "semantic_operation_failed",
            "safe_finally_started", "shutdown_action_started",
        }:
            # A fault/stop supersedes pending normal presentation, never the
            # durable engine record. Do not repaint stale RUNNING afterwards.
            with self._lock:
                self._pending.clear()
                self._pending_semantic.clear()
        # Heartbeats keep all presentation streams moving during a long wait
        # or acquisition. Previously only another event of the SAME stream
        # could release its last frame, leaving the UI seconds behind.
        if not self._pull_mode:
            self._flush_due()
        if self._pull_mode and name in {"action_started", "action_finished"} and isinstance(data, Mapping) and data.get("semantic_id"):
            # Already persisted by Runner and represented by the semantic
            # stream. Do not put the duplicate snapshot into Qt's queue.
            return
        if name in self._SEMANTIC_NAMES:
            self._submit_semantic(name, data)
            return
        if name not in self._COALESCED_NAMES:
            self._emit(name, data)
            return
        now = time.monotonic()
        ready: tuple[str, object] | None = None
        with self._lock:
            self._pending[name] = (name, data)
            last = self._last_emit.get(name)
            if not self._pull_mode and (last is None or now - last >= self._interval_s):
                ready = self._pending.pop(name)
                self._last_emit[name] = now
        if ready is not None:
            self._emit(*ready)

    def _submit_semantic(self, name: str, data: object) -> None:
        """Coalesce visual semantic states while retaining event counts.

        A long sweep emits a start and confirmation for every point.  The
        runner persists each event before this method is called, so the UI can
        receive the newest state for each semantic row at a bounded cadence.
        ``_coalesced_count`` lets the monitor retain truthful ingress metrics
        without replaying thousands of stale Qt signal deliveries.
        """

        if not isinstance(data, Mapping):
            self._emit(name, data)
            return
        semantic_id = str(data.get("semantic_id", "")).strip()
        if not semantic_id:
            self._emit(name, data)
            return
        if name == "semantic_operation_failed":
            # Fault feedback is a safety boundary. Flush visual states queued
            # before the failure, then forward the fault without delay.
            with self._lock:
                ready = tuple(self._pending_semantic.values())
                self._pending_semantic.clear()
                self._last_emit["semantic"] = time.monotonic()
            for pending_name, pending_data, count in ready:
                self._emit_semantic(pending_name, pending_data, count)
            self._emit(name, data)
            return

        now = time.monotonic()
        ready: tuple[tuple[str, object, int], ...] = ()
        key = (name, semantic_id)
        with self._lock:
            previous = self._pending_semantic.get(key)
            count = (previous[2] if previous is not None else 0) + 1
            self._pending_semantic.pop(key, None)
            self._pending_semantic[key] = (name, data, count)
            last = self._last_emit.get("semantic")
            if not self._pull_mode and (last is None or now - last >= self._interval_s):
                ready = tuple(self._pending_semantic.values())
                self._pending_semantic.clear()
                self._last_emit["semantic"] = now
        for pending_name, pending_data, count in ready:
            self._emit_semantic(pending_name, pending_data, count)

    def _emit_semantic(self, name: str, data: object, count: int) -> None:
        if count <= 1 or not isinstance(data, Mapping):
            self._emit(name, data)
            return
        payload = dict(data)
        payload["_coalesced_count"] = count
        self._emit(name, payload)

    def _flush_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            pending = []
            for name in tuple(self._pending):
                if now - self._last_emit.get(name, now) >= self._interval_s:
                    pending.append(self._pending.pop(name))
                    self._last_emit[name] = now
            semantic = ()
            if self._pending_semantic and now - self._last_emit.get("semantic", now) >= self._interval_s:
                semantic = tuple(self._pending_semantic.values())
                self._pending_semantic.clear()
                self._last_emit["semantic"] = now
        for name, data in pending:
            self._emit(name, data)
        for name, data, count in semantic:
            self._emit_semantic(name, data, count)

    def flush(self) -> None:
        """Forward the newest frame from every pending stream before shutdown."""

        with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
            pending_semantic = tuple(self._pending_semantic.values())
            self._pending_semantic.clear()
            now = time.monotonic()
            for name, _data in pending:
                self._last_emit[name] = now
            if pending_semantic:
                self._last_emit["semantic"] = now
        for name, data in pending:
            self._emit(name, data)
        for name, data, count in pending_semantic:
            self._emit_semantic(name, data, count)



class RunWorker(QObject):
    event = Signal(str, object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        settings: StationSettings,
        settings_path: Path,
        plan: ExecutionPlan,
        simulation: bool = False,
        recovery: RecoveryCheckpoint | None = None,
        operator_context: dict[str, object] | None = None,
        simulation_seed: int | None = None,
        outputs_forced_off: bool = False,
        execution_mode: str = ExecutionMode.MEASUREMENT.value,
        output_dir_override: str | None = None,
        file_stem_override: str | None = None,
        device_controllers: Mapping[str, DeviceController] | None = None,
        sample_target: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_path = settings_path
        self._plan = plan
        self._simulation = simulation
        self._recovery = recovery
        self._operator_context = dict(operator_context or {})
        self._simulation_seed = simulation_seed
        self._execution_mode = ExecutionMode.coerce(execution_mode)
        self._output_dir_override = output_dir_override
        self._file_stem_override = file_stem_override
        self._device_controllers = dict(device_controllers or {})
        self._run_leases: dict[str, object] = {}
        self._sample_target = deepcopy(sample_target)
        if outputs_forced_off:
            self._execution_mode = ExecutionMode.DRY_RUN
        self._outputs_forced_off = self._execution_mode is ExecutionMode.DRY_RUN
        self._early_stop_requested = threading.Event()
        self._runner: RecipeRunner | None = None
        self._telemetry_forwarder = RunTelemetryCoalescer(
            lambda name, data: self.event.emit(name, data),
            pull_mode=True,
        )

    def _forward_run_event(self, name: str, data: object) -> None:
        """Preserve terminal ordering when a checkpoint is still pending."""

        if name == "run_completed":
            self._telemetry_forwarder.flush()
        self._telemetry_forwarder.submit(name, data)
        # The simulation backend performs deterministic HDF5/event work in
        # this worker thread.  On Windows, a long sequence of Python and
        # native calls can otherwise retain the interpreter lock long enough
        # to starve the GUI event loop even though the window itself is doing
        # bounded work.  A zero-duration sleep is a cooperative scheduler
        # yield; it has no effect on hardware timing and is restricted to the
        # synthetic run path used by UI qualification/training.
        if self._simulation:
            time.sleep(0)

    @Slot()
    def run(self) -> None:
        # Adapter creation/lease lookup is fallible too. A terminal signal must
        # reach RunController even before the regular cleanup scope exists.
        try:
            self._run_impl()
        except Exception as exc:
            errors = self._release_run_leases()
            detail = f"; {'; '.join(errors)}" if errors else ""
            self.failed.emit(f"Run worker initialization or cleanup failed: {exc}{detail}")

    def _run_impl(self) -> None:
        if self._early_stop_requested.is_set():
            raise RuntimeError("Run was stopped by operator before initialization.")
        retained_devices = {endpoint.split(".", 1)[0] for endpoint in self._plan.retained_outputs}
        if self._execution_mode is not ExecutionMode.DRY_RUN and retained_devices - self._device_controllers.keys():
            raise RuntimeError("Holding final outputs requires persistent device-page controllers for: "
                               + ", ".join(sorted(retained_devices - self._device_controllers.keys())))
        simulation_context = (
            SimulationContext(
                seed=(self._simulation_seed if self._simulation_seed is not None else secrets.randbits(64))
            )
            if self._simulation
            else None
        )
        rigol = self._adapter_for_run(
            "rigol",
            lambda: RigolAdapter(
                self._settings,
                session_factory=(
                    SimulatedVisaFactory("rigol", context=simulation_context)
                    if self._simulation
                    else None
                ),
            ),
        )
        keithley = self._adapter_for_run(
            "keithley",
            lambda: KeithleyAdapter(
                self._settings,
                session_factory=(
                    SimulatedVisaFactory("keithley", context=simulation_context)
                    if self._simulation
                    else None
                ),
            ),
        )
        anritsu = self._adapter_for_run(
            "anritsu",
            lambda: AnritsuAdapter(
                self._settings,
                session_factory=(
                    SimulatedVisaFactory("anritsu", context=simulation_context)
                    if self._simulation
                    else None
                ),
            ),
        )
        writer: Hdf5RunWriter | None = None
        devices: dict[str, object] = {
            "rigol": rigol,
            "keithley": keithley,
            "anritsu": anritsu,
        }
        moke_box: MokeBoxAdapter | None = None
        lakeshore: LakeShore475Adapter | None = None
        completion: dict[str, object] | None = None
        failure: str | None = None
        bundle = None
        try:
            required_by_plan = set(
                self._plan.required_devices
                | required_devices_for_actions(self._plan.actions)
            )
            if "moke_box" in required_by_plan:
                candidate = self._adapter_for_run(
                    "moke_box",
                    lambda: (
                        create_simulated_moke_adapter(simulation_context, self._settings)
                        if simulation_context is not None
                        else StationComposition(self._settings, simulation=False).create_adapter("moke_box")
                    ),
                )
                if "moke_box" not in self._device_controllers and not isinstance(candidate, MokeBoxAdapter):
                    raise RuntimeError(
                        "MOKE Hall measurement requires an enabled, protocol-qualified "
                        "MOKE Box profile and is unavailable in simulation."
                    )
                moke_box = candidate
                devices["moke_box"] = moke_box
            if "lakeshore_gaussmeter" in required_by_plan:
                candidate = self._adapter_for_run(
                    "lakeshore_gaussmeter",
                    lambda: StationComposition(
                        self._settings, simulation=self._simulation, simulation_context=simulation_context
                    ).create_adapter("lakeshore_gaussmeter"),
                )
                if (
                    "lakeshore_gaussmeter" not in self._device_controllers
                    and not isinstance(candidate, LakeShore475Adapter)
                ):
                    raise RuntimeError(
                        "Lake Shore measurement requires an enabled 475 VISA profile."
                    )
                lakeshore = candidate
                devices["lakeshore_gaussmeter"] = lakeshore
            # Connection and cleanup share the immutable plan's device scope.
            missing = required_by_plan - set(devices)
            if missing:
                raise RuntimeError(
                    "The execution plan references unavailable devices: "
                    + ", ".join(sorted(missing))
                )
            required = required_by_plan
            if self._early_stop_requested.is_set():
                raise RuntimeError("Run was stopped by operator before device connection.")
            if self._recovery is None:
                result_path, csv_summary_path = planned_run_paths(
                    self._settings, self._plan.recipe_name,
                    output_dir_override=self._output_dir_override,
                    file_stem_override=self._file_stem_override,
                    sample_target=self._sample_target,
                    reserve_directory=True,
                )
            else:
                result_path = self._recovery.path
                csv_summary_path = None
            estimate = PlanEstimator(self._settings).estimate(self._plan)
            settings_source = self._settings_snapshot()
            from app.storage.run_bundle import RunBundle
            if self._recovery is None:
                bundle = RunBundle(result_path)
                bundle.initialize(
                    plan=self._plan, settings_source=settings_source,
                    operator_context=self._operator_context, sample_target=self._sample_target,
                    simulation_metadata=self._execution_metadata(simulation_context, required),
                    display_name=automated_run_file_stem(self._plan.recipe_name,
                        sample_target=self._sample_target, file_stem_override=self._file_stem_override,
                        pattern=self._settings.storage.get("filename_pattern")),
                )
            elif (result_path.parent / "metadata.json").is_file():
                bundle = RunBundle(result_path)
                bundle.verify_identity(self._plan)
            require_storage_capacity(result_path, estimate.total_upper_bytes)
            from app.storage.resource_budget import resolve_import_memory_budget, require_public_import_capacity
            import_budget = resolve_import_memory_budget(self._settings.storage.get("validation_memory_budget_bytes"))
            require_public_import_capacity(estimate.public_import_upper_bytes, budget_bytes=import_budget)
            if self._recovery is not None:
                import h5py
                from app.storage.resource_budget import persisted_validation_memory_budget
                with h5py.File(result_path, "r") as stored:
                    Hdf5RunWriter.verify_resume_identity(stored, self._plan.recipe_source,
                                                        settings_source, self._plan.sha256)
                    require_public_import_capacity(estimate.public_import_upper_bytes,
                        budget_bytes=persisted_validation_memory_budget(stored))
            identities = {}
            for name in sorted(required):
                if self._early_stop_requested.is_set():
                    raise RuntimeError("Run was stopped by operator during device connection.")
                # A run borrows existing sessions; a start is not a reconnect.
                if devices[name].connected:
                    identities[name] = devices[name].identity.idn
                else:
                    identities[name] = devices[name].connect().idn
            if self._early_stop_requested.is_set():
                raise RuntimeError("Run was stopped by operator before storage initialization.")
            sample_attrs: dict[str, object] = {}
            if self._sample_target is not None:
                if hasattr(self._sample_target, "is_active") and self._sample_target.is_active:
                    sample_attrs = {
                        "sample_id": str(self._sample_target.sample_id),
                        "sample_name": str(self._sample_target.sample_name or self._sample_target.sample_id),
                        "sample_row": str(self._sample_target.row or ""),
                        "sample_col": str(self._sample_target.col or ""),
                        "sample_coordinate_label": str(self._sample_target.device_label or ""),
                        "sample_row_label": str(getattr(self._sample_target, "row_label", "") or ""),
                        "sample_col_label": str(getattr(self._sample_target, "col_label", "") or ""),
                        "sample_description": str(getattr(self._sample_target, "description", "") or ""),
                        "sample_tags": list(getattr(self._sample_target, "tags", ()) or ()),
                        "sample_cell_notes": str(getattr(self._sample_target, "notes", "") or ""),
                        "sample_device_settings": dict(getattr(self._sample_target, "device_settings", {}) or {}),
                    }
                elif isinstance(self._sample_target, Mapping) and self._sample_target.get("sample_id"):
                    sample_attrs = {
                        "sample_id": str(self._sample_target.get("sample_id")),
                        "sample_name": str(self._sample_target.get("sample_name") or self._sample_target.get("sample_id")),
                        "sample_row": str(self._sample_target.get("row") or ""),
                        "sample_col": str(self._sample_target.get("col") or ""),
                        "sample_coordinate_label": str(self._sample_target.get("device_label") or ""),
                        "sample_row_label": str(self._sample_target.get("row_label") or ""),
                        "sample_col_label": str(self._sample_target.get("col_label") or ""),
                        "sample_description": str(self._sample_target.get("description") or ""),
                        "sample_tags": list(self._sample_target.get("tags") or ()),
                        "sample_cell_notes": str(self._sample_target.get("notes") or ""),
                        "sample_device_settings": dict(self._sample_target.get("device_settings") or {}),
                    }

            if self._recovery is None:
                writer = Hdf5RunWriter(
                    result_path,
                    isolate_validation=True,
                    recipe_source=self._plan.recipe_source,
                    settings_source=settings_source,
                    plan_hash=self._plan.sha256,
                    device_idn=identities,
                    device_capabilities={
                        name: devices[name].capabilities for name in sorted(required)
                    },
                    expected_points=self._plan.total_points,
                    operator_context=self._operator_context,
                    simulation_metadata=self._execution_metadata(
                        simulation_context, required
                    ),
                    csv_summary_path=csv_summary_path,
                    run_attributes=sample_attrs or None,
                    validation_memory_budget_bytes=import_budget,
                )
            else:
                writer = Hdf5RunWriter.resume(
                    self._recovery.path,
                    isolate_validation=True,
                    recipe_source=self._plan.recipe_source,
                    settings_source=settings_source,
                    plan_hash=self._plan.sha256,
                    checkpoint_count=self._recovery.stored_points,
                    expected_points=self._plan.total_points,
                    csv_summary_path=(
                        self._recovery.path.with_suffix(".csv")
                        if self._settings.storage.get("write_csv_summary")
                        else None
                    ),
                    operator_context=self._operator_context,
                )
            if bundle is not None:
                bundle.mark_running()
            self._runner = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                moke_box=moke_box,
                lakeshore=lakeshore,
                writer=writer,
                on_event=self._forward_run_event,
                on_telemetry=self._telemetry_forwarder.submit,
                policy=ExecutionPolicy.from_settings(self._settings),
                execution_mode=self._execution_mode,
            )
            if self._early_stop_requested.is_set():
                self._runner.request_stop()
            result = self._runner.run(
                self._plan,
                start_action_index=(
                    self._recovery.next_action_index if self._recovery is not None else 0
                ),
                stored_points=(
                    self._recovery.stored_points if self._recovery is not None else 0
                ),
                recovery_prelude=(
                    self._recovery.prelude_actions if self._recovery is not None else ()
                ),
                recovery_reference=(self._recovery.reference if self._recovery is not None else None),
            )
            if bundle is not None:
                bundle.finalize(execution_state=result.state.value, error=result.error)
            completion = {
                "result": result,
                "path": str(writer.path),
                "sample_target": self._sample_target,
            }
        except Exception as exc:
            failure = str(exc)
        finally:
            runner_owned_shutdown = self._runner is not None and failure is None
            self._completion_retained_devices = (retained_devices if completion is not None
                and completion["result"].state is ApplicationState.HOLDING and failure is None else set())
            self._runner = None
            # A normally returned RecipeRunner result owns its ordered shutdown.
            # Setup or an unexpected escaping runner exception requires OFF
            # here before releasing any session. Report incomplete cleanup
            # as a fault before telling the GUI that the worker has ended.
            cleanup_errors = self._cleanup_devices(devices, runner_owned_shutdown=runner_owned_shutdown)
            # A slow/failing filesystem must not delay the first hardware
            # cleanup attempt after initialization or an unexpected runner fault.
            if failure is not None and writer is not None:
                try:
                    writer.close("faulted")
                except Exception as close_exc:
                    failure += f"; failed to close run file: {close_exc}"
            cleanup_errors.extend(self._release_run_leases())
            if cleanup_errors:
                if self._completion_retained_devices:
                    self._completion_retained_devices = set()
                    fallback_devices = {
                        name: self._device_controllers[name].adapter_for_run() if name in self._device_controllers else device
                        for name, device in devices.items()
                    }
                    cleanup_errors.extend(self._cleanup_devices(fallback_devices, runner_owned_shutdown=False))
                self.event.emit(
                    "worker_cleanup_warning",
                    {
                        "errors": tuple(cleanup_errors),
                        "runner_owned_shutdown": runner_owned_shutdown,
                        "path": completion.get("path") if completion is not None else None,
                    },
                )
                detail = "; ".join(cleanup_errors)
                if completion is not None:
                    run_result = completion["result"]
                    reason = f"Device cleanup incomplete: {detail}"
                    completion["result"] = replace(
                        run_result, state=ApplicationState.FAULT,
                        error=f"{run_result.error}; {reason}" if run_result.error else reason,
                    )
                    completion["cleanup_errors"] = tuple(cleanup_errors)
                else:
                    failure = (
                        f"{failure}; emergency cleanup incomplete: {detail}"
                        if failure
                        else f"Emergency cleanup incomplete: {detail}"
                    )

            # Do not leave the last visible spectrum frame behind merely
            # because the worker finished between two telemetry intervals.
            self._telemetry_forwarder.flush()
            if bundle is not None and (failure is not None or cleanup_errors):
                try:
                    bundle.fail(failure or "; ".join(cleanup_errors))
                except Exception as metadata_exc:
                    failure = f"{failure or 'Run cleanup failed'}; bundle metadata update failed: {metadata_exc}"

        if failure is not None:
            self.failed.emit(failure)
        elif completion is not None:
            self.finished.emit(completion)
        else:
            self.failed.emit("Run worker ended without a result.")

    def _cleanup_devices(self, devices: Mapping[str, Any], *, runner_owned_shutdown: bool) -> list[str]:
        errors: list[str] = []
        try:
            timeout = ExecutionPolicy.from_settings(self._settings).shutdown_timeout_s
        except Exception as exc:
            # An invalid setup must not prevent the emergency attempt itself.
            timeout = ExecutionPolicy().shutdown_timeout_s
            errors.append(f"Cleanup policy invalid: {exc}")
        deadline = time.monotonic() + timeout
        required = self._required_devices()
        pending = [(name, device) for name, device in reversed(tuple(devices.items())) if name in required]
        for index, (name, device) in enumerate(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(f"{name} cleanup deadline expired; device state unconfirmed.")
                continue
            # Reserve time for the remaining devices after an individual fault.
            budget = remaining / (len(pending) - index)
            started = time.monotonic()
            try:
                operation = getattr(device, "operation_timeout", None)
                with operation(budget) if callable(operation) else nullcontext():
                    self._cleanup_device(name, device, runner_owned_shutdown, errors)
                if time.monotonic() - started > budget:
                    raise TimeoutError("Device cleanup exceeded its operation deadline.")
            except Exception as exc:
                errors.append(f"{name} cleanup: {exc}")
        return errors

    def _cleanup_device(self, name: str, device: Any, runner_owned_shutdown: bool, errors: list[str]) -> None:
        # Ownership outlives the reservation: another device's release error
        # can trigger emergency cleanup after this lease was already released.
        borrowed_session = name in self._run_leases or name in self._device_controllers
        if name == "moke_box" and not any(
            endpoint.startswith("moke_box.") for endpoint in controlled_output_endpoints(self._plan.actions)
        ):
            if borrowed_session:
                # A read-only run does not own the page's pre-existing DAC
                # state. Even on startup/storage failure, release only its
                # reservation: disconnect can zero a previously used adapter.
                self.event.emit("readonly_session_preserved", {"device": name})
            else:
                # Standalone workers own their transport, not a persistent UI
                # session. They never changed this adapter's DAC output.
                device.disconnect()
            return
        try:
            connected = device.connected
        except Exception as exc:
            # UNKNOWN is not proof of OFF; still try shutdown and disconnect.
            connected = True
            errors.append(f"{name} connection state: {exc}")
        if not connected:
            if name in getattr(self, "_completion_retained_devices", set()):
                errors.append(f"{name}: retained output lost its persistent connection.")
            return
        if runner_owned_shutdown and (borrowed_session or name in getattr(self, "_completion_retained_devices", set())):
            # Runner confirmed the recipe's final state. The page still owns
            # borrowed sessions, even when their outputs are now OFF.
            return
        if not runner_owned_shutdown:
            try:
                if name == "anritsu" and "anritsu.sg" not in controlled_output_endpoints(self._plan.actions):
                    if device.abort_acquisition() is not True:
                        raise RuntimeError("Acquisition abort was not confirmed.")
                else:
                    confirmed = device.emergency_off()
                    if name in {"rigol", "keithley", "anritsu"} and (
                        confirmed is False or (confirmed is not True and device.state != DeviceState.OUTPUT_OFF)
                    ):
                        raise RuntimeError("Physical OUTPUT OFF was not confirmed.")
            except Exception as exc:
                errors.append(f"{name} emergency OFF: {exc}")
        if borrowed_session:
            # On failure, perform shutdown above but preserve the page's
            # transport. Releasing the run reservation is not Disconnect.
            return
        try:
            device.disconnect()
        except Exception as exc:
            errors.append(f"{name} disconnect: {exc}")

    def _required_devices(self) -> set[str]:
        return set(
            self._plan.required_devices | required_devices_for_actions(self._plan.actions)
        )

    def _adapter_for_run(self, name: str, create: object) -> object:
        controller = self._device_controllers.get(name) if name in self._required_devices() else None
        if controller is not None:
            if name not in self._run_leases:
                self._run_leases[name] = controller.acquire_run_lease()
            return self._run_leases[name]
        if not callable(create):
            raise TypeError(f"Run adapter factory for {name!r} is not callable.")
        return create()

    def _release_run_leases(self) -> list[str]:
        errors = []
        for name, lease in reversed(tuple(self._run_leases.items())):
            try:
                lease.release()
            except Exception as exc:
                # Keep an uncertain/busy reservation held rather than allowing
                # new manual mutations while an old call may still be running.
                errors.append(f"{name} reservation remains held: {exc}")
            else:
                del self._run_leases[name]
        return errors

    def _settings_snapshot(self) -> str:
        return serialize_settings_snapshot(
            self._settings,
            self._settings_path,
            simulation=self._simulation,
        )

    def _execution_metadata(
        self,
        simulation_context: SimulationContext | None,
        required_devices: set[str],
    ) -> dict[str, object]:
        metadata = (
            simulation_context.metadata(tuple(sorted(required_devices)))
            if simulation_context is not None
            else {"enabled": False}
        )
        return {
            **metadata,
            "execution_mode": self._execution_mode.value,
            "outputs_forced_off": self._outputs_forced_off,
            "output_guard_devices": (
                tuple(
                    name
                    for name in ("keithley", "rigol", "anritsu")
                    if name in required_devices
                    and (name != "anritsu" or "anritsu.sg" in controlled_output_endpoints(self._plan.actions))
                )
            ),
        }

    # These methods intentionally only set thread-safe Event flags on
    # RecipeRunner; they are safe to call directly from the GUI thread while
    # this worker's event loop is occupied by a run.
    def request_stop(self) -> None:
        self._early_stop_requested.set()
        # Retain a local reference: cleanup can clear the attribute concurrently.
        runner = self._runner
        if runner is not None:
            runner.request_stop()

    def request_pause(self) -> None:
        if self._runner is not None:
            self._runner.pause_after_point()

    def request_resume(self) -> None:
        if self._runner is not None:
            self._runner.resume()

    def advance_manual_step(self) -> None:
        if self._runner is not None:
            self._runner.advance_manual_step()


class EmergencyStopWorker(QObject):
    """Best-effort out-of-band OFF/ABORT for a blocked run worker.

    The normal run owns its VISA sessions in one worker thread.  During an
    E-STOP it may be blocked in a long instrument query, so this worker opens
    separate short-lived sessions and sends only each adapter's fixed emergency
    action concurrently.  It never configures a source or enables an output.
    """

    finished = Signal(object)

    def __init__(self, settings: StationSettings, *, simulation: bool,
                 device_names: frozenset[str] | None = None, anritsu_rf_output: bool = True) -> None:
        super().__init__()
        raw = settings.model_dump(mode="python")
        raw["devices"]["rigol"]["safety"]["outputs_off_on_connect"] = True
        raw["devices"]["keithley"]["safety"]["outputs_off_on_connect"] = True
        self._settings = StationSettings.model_validate(raw)
        self._simulation = simulation
        self._device_names = device_names
        self._anritsu_rf_output = anritsu_rf_output

    @Slot()
    def run(self) -> None:
        scope = self._device_names
        errors: list[str] = []
        devices = []
        for name, adapter in (("keithley", KeithleyAdapter), ("rigol", RigolAdapter), ("anritsu", AnritsuAdapter)):
            if scope is not None and name not in scope:
                continue
            try:
                devices.append((name, adapter(self._settings,
                    session_factory=SimulatedVisaFactory(name) if self._simulation else None)))
            except Exception as exc:
                errors.append(f"{name} emergency session: {exc}")
        if (scope is None or "moke_box" in scope) and self._settings.moke_box.enabled and self._settings.moke_box.allow_vout_control:
            try:
                devices.append(("moke_box", StationComposition(self._settings, simulation=self._simulation).create_adapter("moke_box")))
            except Exception as exc:
                errors.append(f"MOKE Box emergency session: {exc}")

        deadline = time.monotonic() + ExecutionPolicy.from_settings(self._settings).shutdown_timeout_s

        def stop_device(named_device: tuple[str, object]) -> list[str]:
            name, device = named_device
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return [f"{name}: emergency session deadline expired before connection; state unknown."]
            operation = getattr(device, "operation_timeout", None)
            try:
                with operation(remaining) if callable(operation) else nullcontext():
                    errors = stop_device_in_scope(named_device)
                if time.monotonic() > deadline:
                    errors.append(f"{name}: emergency session exceeded shutdown deadline.")
                return errors
            except Exception as exc:
                return [f"{name}: emergency session deadline/context failed: {exc}"]

        def stop_device_in_scope(named_device: tuple[str, object]) -> list[str]:
            name, device = named_device
            dev_errors: list[str] = []
            try:
                connect_fn = getattr(device, "connect", None)
                if callable(connect_fn):
                    connect_fn()
                method = "abort_acquisition" if name == "anritsu" and not self._anritsu_rf_output else "emergency_off"
                emergency_fn = getattr(device, method, None)
                if not callable(emergency_fn):
                    raise RuntimeError(f"{name} does not implement {method}")
                if callable(emergency_fn):
                    confirmed = emergency_fn()
                    if isinstance(device, MokeBoxAdapter):
                        if not device.safe_target_confirmed:
                            dev_errors.append("MOKE Box: qualified DAC zero was not confirmed.")
                        dev_errors.append("MOKE Box: Kepco power state remains unknown; DAC zero does not prove amplifier power OFF.")
                    elif method == "abort_acquisition":
                        if confirmed is not True:
                            dev_errors.append("Anritsu: acquisition abort was not confirmed.")
                    elif confirmed is False or (confirmed is not True and device.state != DeviceState.OUTPUT_OFF):
                        dev_errors.append(f"{type(device).__name__}: physical OUTPUT OFF was not confirmed.")
            except Exception as exc:
                dev_errors.append(f"{type(device).__name__}: {exc}")
            finally:
                try:
                    disconnect_fn = getattr(device, "disconnect", None)
                    if callable(disconnect_fn):
                        disconnect_fn()
                except Exception as exc:
                    dev_errors.append(f"{type(device).__name__} disconnect: {exc}")
            return dev_errors

        with ThreadPoolExecutor(max_workers=max(1, len(devices))) as executor:
            futures = [executor.submit(stop_device, d) for d in devices]
            for f in futures:
                errors.extend(f.result())

        self.finished.emit(tuple(errors))


class RunController(QObject):
    event = Signal(str, object)
    finished = Signal(object)
    failed = Signal(str)
    started = Signal()
    emergency_completed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: RunWorker | None = None
        self._emergency_thread: QThread | None = None
        self._emergency_worker: EmergencyStopWorker | None = None
        self._run_settings: StationSettings | None = None
        self._run_simulation = False
        self._run_outputs_forced_off = False
        self._run_emergency_devices: frozenset[str] = frozenset()
        self._run_anritsu_rf_output = False
        self._watchdog_estop_started = False
        self._emergency_latch = False
        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.setInterval(100)
        self._telemetry_timer.timeout.connect(self._flush_telemetry)

    def _flush_telemetry(self) -> None:
        # RunWorker.run occupies its thread's event loop. Poll the thread-safe
        # presentation buffer here, without making any instrument calls.
        if self._worker is not None:
            self._worker._telemetry_forwarder.flush()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def is_emergency_latched(self) -> bool:
        return self._emergency_latch

    def reset_emergency_latch(self) -> None:
        self._emergency_latch = False

    def start(
        self,
        settings: StationSettings,
        settings_path: Path,
        plan: ExecutionPlan,
        *,
        simulation: bool = False,
        recovery: RecoveryCheckpoint | None = None,
        operator_context: dict[str, object] | None = None,
        outputs_forced_off: bool = False,
        execution_mode: str = ExecutionMode.MEASUREMENT.value,
        output_dir_override: str | None = None,
        file_stem_override: str | None = None,
        device_controllers: Mapping[str, DeviceController] | None = None,
        sample_target: Any | None = None,
    ) -> None:
        if self.running:
            raise RuntimeError("A measurement is already running.")
        if self._emergency_thread is not None and self._emergency_thread.isRunning():
            raise RuntimeError("Cannot start a measurement while emergency stop is running.")
        if self._emergency_latch:
            raise RuntimeError(
                "Emergency stop is latched. Reset emergency stop before starting a new run."
            )
        self._run_settings = settings
        self._run_emergency_devices = plan.required_devices | required_devices_for_actions(plan.actions)
        self._run_anritsu_rf_output = "anritsu.sg" in controlled_output_endpoints(plan.actions)
        self._run_simulation = simulation
        # A caller that only supplies the former boolean policy must still get
        # the fail-closed dry-run runner, rather than the default measurement
        # enum.  The enum is the single authoritative runtime policy.
        selected_mode = ExecutionMode.coerce(execution_mode)
        if outputs_forced_off:
            selected_mode = ExecutionMode.DRY_RUN
        self._run_outputs_forced_off = selected_mode is ExecutionMode.DRY_RUN
        if self._run_outputs_forced_off or not any(
            action.kind in {"arm_moke_voltage", "update_moke_voltage", "stop_moke_voltage"}
            for action in plan.actions
        ):
            self._run_emergency_devices -= {"moke_box"}
        self._watchdog_estop_started = False
        self._thread = QThread(self)
        self._worker = RunWorker(
            settings,
            settings_path,
            plan,
            simulation,
            recovery,
            operator_context=operator_context,
            outputs_forced_off=self._run_outputs_forced_off,
            execution_mode=selected_mode.value,
            output_dir_override=output_dir_override,
            file_stem_override=file_stem_override,
            device_controllers=device_controllers,
            sample_target=sample_target,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self._worker_event)
        self._worker.finished.connect(self._finished)
        self._worker.failed.connect(self._failed)
        self._thread.start()
        self._telemetry_timer.start()
        self.started.emit()

    @Slot(str, object)
    def _worker_event(self, name: str, data: object) -> None:
        self.event.emit(name, data)
        if (
            name == "watchdog_timeout"
            and not self._watchdog_estop_started
            and self._run_settings is not None
        ):
            self._watchdog_estop_started = True
            self.request_emergency_stop(
                self._run_settings,
                simulation=self._run_simulation,
                device_names=self._run_emergency_devices,
                anritsu_rf_output=self._run_anritsu_rf_output,
            )

    def request_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()

    def request_pause(self) -> None:
        if self._worker is not None:
            self._worker.request_pause()

    def request_resume(self) -> None:
        if self._worker is not None:
            self._worker.request_resume()

    def advance_manual_step(self) -> None:
        if self._worker is not None:
            self._worker.advance_manual_step()

    def request_emergency_stop(self, settings: StationSettings, *, simulation: bool = False,
                               device_names: frozenset[str] | None = None, anritsu_rf_output: bool = True) -> None:
        """Request cooperative stop and concurrently issue a best-effort OFF."""

        self._emergency_latch = True
        self.request_stop()
        if self._emergency_thread is not None and self._emergency_thread.isRunning():
            return
        thread = QThread(self)
        worker = EmergencyStopWorker(settings, simulation=simulation, device_names=device_names,
                                     anritsu_rf_output=anritsu_rf_output)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._emergency_finished)
        thread.finished.connect(lambda completed=thread: self._emergency_thread_finished(completed))
        self._emergency_thread = thread
        self._emergency_worker = worker
        thread.start()

    def _emergency_finished(self, errors: object) -> None:
        self.emergency_completed.emit(errors)
        if self._emergency_thread is not None:
            self._emergency_thread.quit()

    def _emergency_thread_finished(self, completed: QThread) -> None:
        """Release only a thread which has actually stopped running."""

        if self._emergency_thread is completed:
            self._emergency_worker = None
            self._emergency_thread = None
        completed.deleteLater()

    def _finished(self, result: object) -> None:
        if self._dispose():
            self.finished.emit(result)
        else:
            self.failed.emit(
                "The run finished, but its worker thread did not stop within 5 seconds. "
                "The application remains in a fault state."
            )

    def _failed(self, error: str) -> None:
        stopped = self._dispose()
        self.failed.emit(
            error
            if stopped
            else error
            + "; the run worker thread did not stop within 5 seconds."
        )

    def _dispose(self, *, timeout_ms: int = 5_000) -> bool:
        self._telemetry_timer.stop()
        thread = self._thread
        if thread is not None:
            thread.quit()
            if thread.isRunning() and not thread.wait(timeout_ms):
                return False
        if self._worker is not None:
            self._worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._worker = None
        self._thread = None
        self._run_settings = None
        self._run_outputs_forced_off = False
        return True

    def close(self, *, timeout_ms: int = 5_000) -> bool:
        """Stop owned workers without discarding a still-running QThread."""

        self.request_stop()
        if self.running and self._run_settings is not None:
            self.request_emergency_stop(
                self._run_settings,
                simulation=self._run_simulation,
            )
        run_stopped = self._dispose(timeout_ms=timeout_ms)

        emergency_stopped = True
        emergency_thread = self._emergency_thread
        if emergency_thread is not None:
            emergency_thread.quit()
            emergency_stopped = (
                not emergency_thread.isRunning()
                or emergency_thread.wait(timeout_ms)
            )
        if emergency_stopped and emergency_thread is not None:
            emergency_thread.deleteLater()
            if self._emergency_worker is not None:
                self._emergency_worker.deleteLater()
            self._emergency_worker = None
            self._emergency_thread = None
        return run_stopped and emergency_stopped

