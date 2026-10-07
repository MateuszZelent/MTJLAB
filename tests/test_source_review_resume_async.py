"""Recovery preparation is detached, asynchronous, and ignores stale jobs."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from PySide6.QtCore import QThread, QTimer

from app.ui import resume_preparation
from app.ui.shell.main_window import MainWindow
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_resume_preparation_is_off_gui_coalesces_and_closes_without_wait(shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    seen, ready, ticks = [], [], []
    settings = simulation_settings()

    def prepare(path, snapshot, *args):
        assert QThread.currentThread() != app.thread()
        assert snapshot is not settings and snapshot.execution is not settings.execution
        seen.append(path)
        if path == "old":
            entered.set()
            assert release.wait(5)
        return path

    monkeypatch.setattr(resume_preparation, "prepare_resume", prepare)
    controller = resume_preparation.ResumePreparation()
    controller.ready.connect(ready.append)
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        controller.start("old", settings, "settings.yml", True, None)
        timer.start(5)
        wait_until(app, lambda: entered.is_set() and len(ticks) >= 3)
        controller.start("middle", settings, "settings.yml", True, None)
        controller.start("latest", settings, "settings.yml", True, None)
        release.set()
        wait_until(app, lambda: ready == ["latest"])
        assert seen == ["old", "latest"]
        entered.clear()
        release.clear()
        controller.start("old", settings, "settings.yml", True, None)
        wait_until(app, entered.is_set)
        started = time.monotonic()
        assert not controller.close()
        assert time.monotonic() - started < .2
        release.set()
        wait_until(app, controller.close)
        app.processEvents()
        assert ready == ["latest"]
    finally:
        release.set()
        timer.stop()
        assert controller._pool.waitForDone(3000)
        controller.close()


def test_changed_settings_after_preparation_cannot_open_resume_confirmation():
    error = Mock()
    window = SimpleNamespace(_run_controller=SimpleNamespace(running=False), _settings="new",
                             _simulation=True, _resume_preparation_failed=error)
    MainWindow._resume_prepared(window, SimpleNamespace(settings="old", simulation=True))
    error.assert_called_once()
    assert "changed" in error.call_args.args[0]


def test_preparation_reads_real_archive_compiles_and_preserves_file(tmp_path):
    from app.domain.errors import ConfigurationError
    from app.engine.compiler import RecipeCompiler
    from app.recipes import parse_recipe_text
    from app.storage.hdf5_writer import Hdf5RunWriter
    from app.ui.run_worker import serialize_settings_snapshot

    settings = simulation_settings()
    settings_path = tmp_path / "settings.yml"
    source = "schema_version: 1\nname: resume\nroot: {id: wait, type: wait, duration: '1 s'}\n"
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    path = tmp_path / "resume.h5"
    writer = Hdf5RunWriter(path, recipe_source=source,
        settings_source=serialize_settings_snapshot(settings, settings_path, simulation=True),
        plan_hash=plan.sha256, device_idn={})
    writer.close("faulted")
    before = path.read_bytes()
    result = resume_preparation.prepare_resume(path, settings, settings_path, True, None)
    assert result.plan.sha256 == plan.sha256
    assert result.checkpoint.next_action_index == 0
    settings.execution["retry_count"] = 5
    with pytest.raises(ConfigurationError, match="settings differ"):
        resume_preparation.prepare_resume(path, settings, settings_path, True, None)
    assert path.read_bytes() == before
