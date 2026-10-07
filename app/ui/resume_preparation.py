"""Read-only recovery preparation, isolated from the GUI and instruments."""
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.engine.recovery import RunRecoveryManager
from app.engine.runner import ExecutionMode
from app.recipes import parse_recipe_text
from app.storage import Hdf5RunReader
from app.ui.results.workers import ResultReadTask
from app.ui.run_worker import serialize_settings_snapshot


@dataclass(frozen=True)
class PreparedResume:
    plan: object
    checkpoint: object
    mode: ExecutionMode
    settings: object
    simulation: bool


def prepare_resume(path, settings, settings_path, simulation, registry):
    detail = Hdf5RunReader.detail(path)
    mode = ExecutionMode.coerce(str(detail.simulation_metadata.get("execution_mode", "measurement")))
    if detail.simulation_metadata.get("outputs_forced_off", False):
        mode = ExecutionMode.DRY_RUN
    source = serialize_settings_snapshot(settings, settings_path, simulation=simulation)
    if source != detail.settings_yaml:
        raise ConfigurationError("The current settings differ from the immutable run snapshot. "
                                 "Restore the exact station configuration before resuming.")
    recipe = parse_recipe_text(detail.recipe_yaml, origin=str(path))
    plan = RecipeCompiler(settings, outputs_forced_off=mode is ExecutionMode.DRY_RUN,
                          device_registry=registry).compile(recipe)
    checkpoint = RunRecoveryManager().inspect(path, plan)
    if checkpoint.stored_points >= plan.total_points and checkpoint.next_action_index >= len(plan.actions):
        raise ConfigurationError("The selected run has no remaining actions.")
    return PreparedResume(plan, checkpoint, mode, settings, simulation)


class ResumePreparation(QObject):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._generation = 0
        self._tasks = {}
        self._closed = False

    def start(self, path, settings, settings_path, simulation, registry):
        if self._closed:
            if not self._pool.waitForDone(0):
                return False
            self._closed = False
        for task in self._tasks.values():
            task.cancel()
        self._generation += 1
        task = ResultReadTask(self._generation, prepare_resume, path, settings.model_copy(deep=True),
                              settings_path, simulation, registry)
        self._tasks[self._generation] = task
        task.signals.loaded.connect(self._loaded)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._finished)
        self._pool.start(task)
        return True

    def _loaded(self, generation, result):
        if not self._closed and generation == self._generation:
            self.ready.emit(result)

    def _failed(self, generation, message):
        if not self._closed and generation == self._generation:
            self.failed.emit(message)

    def _finished(self, generation):
        self._tasks.pop(generation, None)

    def close(self):
        self._closed = True
        for task in self._tasks.values():
            task.cancel()
        return self._pool.waitForDone(0)
