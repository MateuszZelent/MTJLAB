"""Initialization failures must terminate runs and not prevent other E-STOPs."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.ui import run_worker
from tests.helpers import simulation_settings
from tests.test_run_controller import _RunLeaseAdapter


def plan(settings):
    return RecipeCompiler(settings).compile(parse_recipe_text(
        "schema_version: 1\nname: init fault\nroot: {id: analyzer, type: connect, device: anritsu}\n"
    ))


def test_worker_reports_adapter_creation_error(monkeypatch, tmp_path):
    settings = simulation_settings()
    monkeypatch.setattr(run_worker, "AnritsuAdapter", Mock(side_effect=RuntimeError("factory failed")))
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
    failures, finished = [], []
    worker.failed.connect(failures.append)
    worker.finished.connect(finished.append)
    worker.run()
    assert len(failures) == 1 and "factory failed" in failures[0]
    assert not finished
    assert not list(tmp_path.glob("*.h5"))


def test_controller_thread_finishes_after_adapter_creation_error(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    monkeypatch.setattr(run_worker, "AnritsuAdapter", Mock(side_effect=RuntimeError("factory failed")))
    controller = run_worker.RunController()
    failures = []
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    controller.failed.connect(failures.append)
    try:
        controller.start(settings, tmp_path / "settings.yml", plan(settings), simulation=True)
        controller._thread.finished.connect(loop.quit)
        timer.start(5000)
        loop.exec()
        app.processEvents()
        assert not controller.running
        assert len(failures) == 1 and "factory failed" in failures[0]
    finally:
        timer.stop()
        controller.close()


def test_unrelated_controller_is_not_asked_for_a_run_lease():
    settings = simulation_settings()
    controller = SimpleNamespace(adapter_for_run=Mock(side_effect=RuntimeError("No lease")))
    worker = run_worker.RunWorker(settings, Path("settings.yml"), plan(settings),
                                  device_controllers={"rigol": controller})
    passive_adapter = object()
    assert worker._adapter_for_run("rigol", lambda: passive_adapter) is passive_adapter
    controller.adapter_for_run.assert_not_called()


def test_estop_attempts_remaining_instruments_after_factory_failure(monkeypatch):
    settings = simulation_settings()
    rigol, analyzer = _RunLeaseAdapter(), _RunLeaseAdapter()
    monkeypatch.setattr(run_worker, "KeithleyAdapter", Mock(side_effect=RuntimeError("constructor failed")))
    monkeypatch.setattr(run_worker, "RigolAdapter", lambda *_args, **_kwargs: rigol)
    monkeypatch.setattr(run_worker, "AnritsuAdapter", lambda *_args, **_kwargs: analyzer)
    worker = run_worker.EmergencyStopWorker(settings, simulation=True)
    results = []
    worker.finished.connect(results.append)
    worker.run()
    assert len(results) == 1
    assert any("keithley emergency session: constructor failed" in error for error in results[0])
    assert rigol.emergency_off_count == analyzer.emergency_off_count == 1
    assert rigol.disconnect_count == analyzer.disconnect_count == 1
