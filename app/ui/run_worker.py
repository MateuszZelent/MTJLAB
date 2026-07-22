"""Dedicated Qt worker for a complete multi-device measurement run."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import re
import secrets

from PySide6.QtCore import QObject, QThread, Signal, Slot
from ruamel.yaml import YAML

from app.bootstrap import StationComposition
from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.base import DeviceAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.moke_box import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.lakeshore_475 import LakeShore475Adapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.devices.simulation import SimulationContext
from app.engine.compiler import ExecutionPlan, required_devices_for_actions
from app.engine.policy import ExecutionPolicy
from app.engine.recovery import RecoveryCheckpoint
from app.engine.runner import ExecutionMode, RecipeRunner
from app.settings.models import StationSettings
from app.storage import Hdf5RunWriter


def serialize_settings_snapshot(
    settings: StationSettings,
    settings_path: Path,
    *,
    simulation: bool,
) -> str:
    """Return the exact settings provenance used for new and resumed runs."""

    if not simulation:
        return settings_path.read_text(encoding="utf-8")
    stream = StringIO()
    stream.write("# SIMULATION: in-memory profile; not persisted to settings.yml.\n")
    YAML().dump(settings.model_dump(mode="python"), stream)
    return stream.getvalue()


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
        self._outputs_forced_off = bool(outputs_forced_off)
        self._execution_mode = ExecutionMode(execution_mode)
        self._runner: RecipeRunner | None = None

    @Slot()
    def run(self) -> None:
        simulation_context = (
            SimulationContext(
                seed=(self._simulation_seed if self._simulation_seed is not None else secrets.randbits(64))
            )
            if self._simulation
            else None
        )
        rigol = RigolAdapter(
            self._settings,
            session_factory=(
                SimulatedVisaFactory("rigol", context=simulation_context)
                if self._simulation
                else None
            ),
        )
        keithley = KeithleyAdapter(
            self._settings,
            session_factory=(
                SimulatedVisaFactory("keithley", context=simulation_context)
                if self._simulation
                else None
            ),
        )
        anritsu = AnritsuAdapter(
            self._settings,
            session_factory=(
                SimulatedVisaFactory("anritsu", context=simulation_context)
                if self._simulation
                else None
            ),
        )
        writer: Hdf5RunWriter | None = None
        devices: dict[str, DeviceAdapter] = {
            "rigol": rigol,
            "keithley": keithley,
            "anritsu": anritsu,
        }
        moke_box: MokeBoxAdapter | None = None
        lakeshore: LakeShore475Adapter | None = None
        completion: dict[str, object] | None = None
        failure: str | None = None
        try:
            required_by_plan = set(
                self._plan.required_devices
                or required_devices_for_actions(self._plan.actions)
            )
            if "moke_box" in required_by_plan:
                candidate = (
                    MokeBoxAdapter(
                        MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM"),
                        SimulatedMokeBoxTransport(simulation_context),
                    )
                    if simulation_context is not None
                    else StationComposition(self._settings, simulation=False).create_adapter("moke_box")
                )
                if not isinstance(candidate, MokeBoxAdapter):
                    raise RuntimeError(
                        "MOKE Hall measurement requires an enabled, protocol-qualified "
                        "MOKE Box profile and is unavailable in simulation."
                    )
                moke_box = candidate
                devices["moke_box"] = moke_box
            if "lakeshore_gaussmeter" in required_by_plan:
                candidate = StationComposition(
                    self._settings, simulation=self._simulation
                ).create_adapter("lakeshore_gaussmeter")
                if not isinstance(candidate, LakeShore475Adapter):
                    raise RuntimeError(
                        "Lake Shore measurement requires an enabled 475 VISA profile."
                    )
                lakeshore = candidate
                devices["lakeshore_gaussmeter"] = lakeshore
            # A recipe owns the complete station for its lifetime. Connecting
            # every configured device lets the final emergency-off sequence
            # confirm that *all* outputs are disabled after success, stop or
            # any fault, not just the devices named by the recipe.
            required = set(devices)
            identities = {name: devices[name].connect().idn for name in sorted(required)}
            output_dir = Path(str(self._settings.storage.get("output_directory", "./measurements")))
            settings_source = self._settings_snapshot()
            if self._recovery is None:
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self._plan.recipe_name).strip("_") or "run"
                # Microseconds make accidental collisions across fast repeated
                # runs practically impossible; the writer additionally refuses
                # to overwrite an existing artefact.
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                run_stem = f"{timestamp}_{safe_name}"
                writer = Hdf5RunWriter(
                    output_dir / f"{run_stem}.h5",
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
                    csv_summary_path=output_dir / f"{run_stem}.csv" if self._settings.storage.get("write_csv_summary") else None,
                )
            else:
                writer = Hdf5RunWriter.resume(
                    self._recovery.path,
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
            self._runner = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                moke_box=moke_box,
                lakeshore=lakeshore,
                writer=writer,
                on_event=lambda name, data: self.event.emit(name, data),
                on_telemetry=lambda name, data: self.event.emit(name, data),
                policy=ExecutionPolicy.from_settings(self._settings),
                execution_mode=self._execution_mode,
            )
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
            )
            completion = {"result": result, "path": str(writer.path)}
        except Exception as exc:
            failure = str(exc)
            if writer is not None:
                try:
                    writer.close("faulted")
                except Exception as close_exc:
                    failure += f"; failed to close run file: {close_exc}"
        finally:
            runner_owned_shutdown = self._runner is not None
            self._runner = None
            cleanup_errors: list[str] = []
            # RecipeRunner owns the ordered, confirmed shutdown once it has
            # been constructed. If setup failed earlier, issue emergency OFF
            # here before releasing any session. In either case, do not tell
            # the GUI that the run ended until every session is disconnected.
            for name, device in reversed(tuple(devices.items())):
                if not device.connected:
                    continue
                if not runner_owned_shutdown:
                    try:
                        device.emergency_off()
                    except Exception as cleanup_exc:
                        cleanup_errors.append(
                            f"{name} emergency OFF: {cleanup_exc}"
                        )
                try:
                    device.disconnect()
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"{name} disconnect: {cleanup_exc}")
            if cleanup_errors:
                self.event.emit(
                    "worker_cleanup_warning",
                    {
                        "errors": tuple(cleanup_errors),
                        "runner_owned_shutdown": runner_owned_shutdown,
                    },
                )
                if not runner_owned_shutdown:
                    detail = "; ".join(cleanup_errors)
                    failure = (
                        f"{failure}; emergency cleanup incomplete: {detail}"
                        if failure
                        else f"Emergency cleanup incomplete: {detail}"
                    )

        if failure is not None:
            self.failed.emit(failure)
        elif completion is not None:
            self.finished.emit(completion)
        else:
            self.failed.emit("Run worker ended without a result.")

    def _required_devices(self) -> set[str]:
        return set(
            self._plan.required_devices or required_devices_for_actions(self._plan.actions)
        )

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
        }

    # These methods intentionally only set thread-safe Event flags on
    # RecipeRunner; they are safe to call directly from the GUI thread while
    # this worker's event loop is occupied by a run.
    def request_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_stop()

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
    action.  It never configures a source or enables an output.
    """

    finished = Signal(object)

    def __init__(self, settings: StationSettings, *, simulation: bool) -> None:
        super().__init__()
        raw = settings.model_dump(mode="python")
        raw["devices"]["rigol"]["safety"]["outputs_off_on_connect"] = True
        raw["devices"]["keithley"]["safety"]["outputs_off_on_connect"] = True
        self._settings = StationSettings.model_validate(raw)
        self._simulation = simulation

    @Slot()
    def run(self) -> None:
        devices = (
            KeithleyAdapter(
                self._settings,
                session_factory=SimulatedVisaFactory("keithley") if self._simulation else None,
            ),
            RigolAdapter(
                self._settings,
                session_factory=SimulatedVisaFactory("rigol") if self._simulation else None,
            ),
            AnritsuAdapter(
                self._settings,
                session_factory=SimulatedVisaFactory("anritsu") if self._simulation else None,
            ),
        )
        errors: list[str] = []
        for device in devices:
            try:
                device.connect()
                device.emergency_off()
            except Exception as exc:
                errors.append(f"{type(device).__name__}: {exc}")
            finally:
                try:
                    device.disconnect()
                except Exception as exc:
                    errors.append(f"{type(device).__name__} disconnect: {exc}")
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
        self._watchdog_estop_started = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

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
    ) -> None:
        if self.running:
            raise RuntimeError("A measurement is already running.")
        self._run_settings = settings
        self._run_simulation = simulation
        self._run_outputs_forced_off = bool(outputs_forced_off)
        self._watchdog_estop_started = False
        self._thread = QThread(self)
        self._worker = RunWorker(
            settings,
            settings_path,
            plan,
            simulation,
            recovery,
            operator_context=operator_context,
            outputs_forced_off=outputs_forced_off,
            execution_mode=execution_mode,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self._worker_event)
        self._worker.finished.connect(self._finished)
        self._worker.failed.connect(self._failed)
        self._thread.start()
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

    def request_emergency_stop(self, settings: StationSettings, *, simulation: bool = False) -> None:
        """Request cooperative stop and concurrently issue a best-effort OFF."""

        self.request_stop()
        if self._emergency_thread is not None and self._emergency_thread.isRunning():
            return
        thread = QThread(self)
        worker = EmergencyStopWorker(settings, simulation=simulation)
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

