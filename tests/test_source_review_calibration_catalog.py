"""Calibration file verification must not occupy the GUI thread."""
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from app.devices.moke_box.ui.field_control import CalibrationCatalogWorker, MokeFieldWorkflow
from app.domain.errors import ConfigurationError
from app.safety.moke_box import control_profile_from_settings
from tests.helpers import simulation_settings


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("operation", ["catalog", "load", "activate"])
def test_catalog_verification_is_async_and_stale_results_are_discarded(app, monkeypatch, stale, operation):
    settings = simulation_settings(approved=True)
    host = QWidget()
    workflow = MokeFieldWorkflow(Mock(), settings, host)
    workflow._profile = control_profile_from_settings(settings, simulation=True)
    entered, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    repository = Mock()

    model = Mock()
    model.context.profile_fingerprint = workflow._profile.fingerprint
    model.context.simulation = workflow._profile.simulation

    def active(*args, **kwargs):
        assert threading.get_ident() != gui_thread
        entered.set()
        assert release.wait(10)
        return None if operation == "catalog" else model

    repository.active.side_effect = active
    repository.load.side_effect = active
    repository.activate.side_effect = active
    repository.list_ids.return_value = ()
    monkeypatch.setattr("app.devices.moke_box.ui.field_control.MokeCalibrationRepository", lambda _: repository)
    apply = Mock()
    monkeypatch.setattr(workflow, "_apply_catalog_result", apply)
    monkeypatch.setattr(workflow, "_apply_review_model", apply)
    monkeypatch.setattr(workflow, "_target_changed", Mock())
    try:
        workflow._start_catalog_job(operation, "selected-model", True)
        deadline = time.monotonic() + 5
        while not entered.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert entered.is_set()
        # GUI remains callable while worker is deliberately blocked on file I/O.
        assert not workflow.prepare_application_shutdown()
        assert not workflow.load_model_button.isEnabled()
        if stale:
            workflow._profile = None
        release.set()
        deadline = time.monotonic() + 5
        while workflow._catalog_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert workflow._catalog_thread is None
        assert apply.call_count == (0 if stale or operation == "activate" else 1)
        if operation == "activate":
            assert workflow._active_model is (None if stale else model)
            repository.activate.assert_called_once()
        assert workflow.prepare_application_shutdown()
    finally:
        release.set()
        deadline = time.monotonic() + 10
        while workflow._catalog_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert workflow._catalog_thread is None
        host.close()
        host.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("operation", ["load", "activate"])
def test_repository_failure_reports_one_terminal_result(app, monkeypatch, operation):
    profile = control_profile_from_settings(simulation_settings(approved=True), simulation=True)
    repository = Mock()
    getattr(repository, operation).side_effect = ConfigurationError("raw HDF5 checksum mismatch")
    monkeypatch.setattr("app.devices.moke_box.ui.field_control.MokeCalibrationRepository", lambda _: repository)
    worker = CalibrationCatalogWorker("unused", profile, operation, "selected", False)
    results, finished = [], []
    worker.completed.connect(results.append)
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    assert results == [(operation, None, "raw HDF5 checksum mismatch")]
    assert finished == [True]
    if operation == "activate":
        repository.activate.assert_called_once_with("selected", profile_fingerprint=profile.fingerprint,
                                                    simulation=profile.simulation, reviewed=False)
