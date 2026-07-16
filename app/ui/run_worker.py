"""Dedicated Qt worker for a complete multi-device measurement run."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import re

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from ruamel.yaml import YAML

from app.devices.anritsu import AnritsuAdapter
from app.devices.keithley import KeithleyAdapter
from app.devices.rigol import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.engine.compiler import ExecutionPlan
from app.engine.runner import RecipeRunner
from app.settings.models import StationSettings
from app.storage import Hdf5RunWriter


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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_path = settings_path
        self._plan = plan
        self._simulation = simulation
        self._runner: RecipeRunner | None = None

    @Slot()
    def run(self) -> None:
        rigol = RigolAdapter(
            self._settings,
            session_factory=SimulatedVisaFactory("rigol") if self._simulation else None,
        )
        keithley = KeithleyAdapter(
            self._settings,
            session_factory=SimulatedVisaFactory("keithley") if self._simulation else None,
        )
        anritsu = AnritsuAdapter(
            self._settings,
            session_factory=SimulatedVisaFactory("anritsu") if self._simulation else None,
        )
        writer: Hdf5RunWriter | None = None
        try:
            identities = {
                "rigol": rigol.connect().idn,
                "keithley": keithley.connect().idn,
                "anritsu": anritsu.connect().idn,
            }
            output_dir = Path(str(self._settings.storage.get("output_directory", "./measurements")))
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self._plan.recipe_name).strip("_") or "run"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_stem = f"{timestamp}_{safe_name}"
            writer = Hdf5RunWriter(
                output_dir / f"{run_stem}.h5",
                recipe_source=self._plan.recipe_source,
                settings_source=self._settings_snapshot(),
                plan_hash=self._plan.sha256,
                device_idn=identities,
                device_capabilities={
                    "rigol": rigol.capabilities,
                    "keithley": keithley.capabilities,
                    "anritsu": anritsu.capabilities,
                },
                csv_summary_path=output_dir / f"{run_stem}.csv" if self._settings.storage.get("write_csv_summary") else None,
            )
            self._runner = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
                on_event=lambda name, data: self.event.emit(name, data),
            )
            result = self._runner.run(self._plan)
            self.finished.emit({"result": result, "path": str(writer.path)})
        except Exception as exc:
            if writer is not None:
                try:
                    writer.close("faulted")
                except Exception:
                    pass
            self.failed.emit(str(exc))
        finally:
            self._runner = None
            # A run owns separate VISA sessions.  Release all of them on both
            # success and failure; RecipeRunner has already requested its
            # ordered emergency-off policy before this cleanup.
            for device in (keithley, rigol, anritsu):
                try:
                    device.emergency_off()
                    device.disconnect()
                except Exception:
                    pass

    def _settings_snapshot(self) -> str:
        if not self._simulation:
            return self._settings_path.read_text(encoding="utf-8")
        stream = StringIO()
        stream.write("# In-memory simulation profile; not persisted to settings.yml.\n")
        YAML().dump(self._settings.model_dump(mode="python"), stream)
        return stream.getvalue()

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

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self, settings: StationSettings, settings_path: Path, plan: ExecutionPlan, *, simulation: bool = False
    ) -> None:
        if self.running:
            raise RuntimeError("A measurement is already running.")
        self._thread = QThread(self)
        self._worker = RunWorker(settings, settings_path, plan, simulation)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self.event)
        self._worker.finished.connect(self._finished)
        self._worker.failed.connect(self._failed)
        self._thread.start()
        self.started.emit()

    def request_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()

    def request_pause(self) -> None:
        if self._worker is not None:
            self._worker.request_pause()

    def request_resume(self) -> None:
        if self._worker is not None:
            self._worker.request_resume()

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
        self.finished.emit(result)
        self._dispose()

    def _failed(self, error: str) -> None:
        self.failed.emit(error)
        self._dispose()

    def _dispose(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5_000)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def close(self) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5_000)
        self._worker = None
        self._thread = None
        if self._emergency_thread is not None:
            self._emergency_thread.quit()
            self._emergency_thread.wait(5_000)
            self._emergency_worker = None
            self._emergency_thread = None
