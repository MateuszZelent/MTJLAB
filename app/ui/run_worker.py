"""Dedicated Qt worker for a complete multi-device measurement run."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from app.devices.anritsu import AnritsuAdapter
from app.devices.keithley import KeithleyAdapter
from app.devices.rigol import RigolAdapter
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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_path = settings_path
        self._plan = plan
        self._runner: RecipeRunner | None = None

    @Slot()
    def run(self) -> None:
        rigol = RigolAdapter(self._settings)
        keithley = KeithleyAdapter(self._settings)
        anritsu = AnritsuAdapter(self._settings)
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
            writer = Hdf5RunWriter(
                output_dir / f"{timestamp}_{safe_name}.h5",
                recipe_source=self._plan.recipe_source,
                settings_source=self._settings_path.read_text(encoding="utf-8"),
                plan_hash=self._plan.sha256,
                device_idn=identities,
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
            for device in (keithley, rigol, anritsu):
                try:
                    device.emergency_off()
                    device.disconnect()
                except Exception:
                    pass
            if writer is not None:
                try:
                    writer.close("faulted")
                except Exception:
                    pass
            self.failed.emit(str(exc))
        finally:
            self._runner = None

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


class RunController(QObject):
    event = Signal(str, object)
    finished = Signal(object)
    failed = Signal(str)
    started = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: RunWorker | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, settings: StationSettings, settings_path: Path, plan: ExecutionPlan) -> None:
        if self.running:
            raise RuntimeError("Pomiar już trwa.")
        self._thread = QThread(self)
        self._worker = RunWorker(settings, settings_path, plan)
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

