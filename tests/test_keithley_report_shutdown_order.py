"""A pending report must not delay or bypass compliance-policy restoration."""

from unittest.mock import Mock
from types import SimpleNamespace

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui import characterization_card as module
from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from tests.test_keithley_field_worker import make_worker


def test_shown_fluent_window_rejects_close_during_characterization(tmp_path):
    from app.ui.shell.main_window import MainWindow
    from tests.helpers import SETTINGS_TEMPLATE
    from PySide6.QtTest import QTest
    app = QApplication.instance() or QApplication([])
    settings_path = tmp_path / "station.yml"
    settings_path.write_text(SETTINGS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    window = MainWindow(str(settings_path), simulation=True)
    card = window.keithley_page.characterization_card
    worker = Mock()
    worker.isRunning.return_value = True
    card._field_worker = worker
    card._field_lease = object()
    try:
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        QTest.qWait(50)
        controller = window._controllers["keithley"]
        assert not window.close()
        app.processEvents()
        assert window.isVisible()
        assert card.isVisible()
        assert window._controllers["keithley"] is controller
        worker.request_stop.assert_called_once()
        assert window.grab().save(str(tmp_path / "shutdown_waiting.png"))
    finally:
        card._field_worker = None
        card._field_lease = None
        window.close()
        app.processEvents()


def test_application_close_keeps_controllers_alive_until_characterization_finishes(tmp_path, monkeypatch):
    from app.ui.shell.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "shutdown.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(module, "QSettings", lambda *args: settings)
    template, _ = make_worker(tmp_path)
    card = module.KeithleyCharacterizationCard(Mock(), template.settings)
    acquisition = Mock()
    acquisition.isRunning.return_value = True
    card._field_worker = acquisition
    card._field_lease = object()
    shell = SimpleNamespace(recipe_page=Mock(), keithley_page=SimpleNamespace(characterization_card=card))
    shell._navigate_to = Mock()
    shell.recipe_page.confirm_close.return_value = True
    event = Mock()
    try:
        MainWindow.closeEvent(shell, event)
        event.ignore.assert_called_once()
        acquisition.request_stop.assert_called_once()
        acquisition.isRunning.return_value = False
        assert not card.prepare_application_shutdown()  # Lease/recovery still pending.
        card._field_lease = None
        card._temporary_policy_phase = "restoring"
        assert not card.prepare_application_shutdown()
        card._temporary_policy_phase = "idle"
        reporting = Mock()
        reporting.isRunning.return_value = True
        card._field_report_worker = reporting
        assert not card.prepare_application_shutdown()
        reporting.isRunning.return_value = False
        assert card.prepare_application_shutdown()
    finally:
        card._field_worker = None
        card._field_lease = None
        card._temporary_policy_phase = "idle"
        card.close()
        app.processEvents()


def test_csv_survives_failed_restore_and_pdf_waits_for_confirmed_policy(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(module, "QSettings", lambda *args: settings)
    monkeypatch.setattr(module.StationMessageBox, "critical", lambda *args: None)
    worker, _ = make_worker(tmp_path)
    worker.run()
    dataset = load_field_series(worker.directory).curves[0].dataset
    card = module.KeithleyCharacterizationCard(Mock(), worker.settings)
    card._automatic_run_directory = lambda _: tmp_path / "single"
    callbacks = []
    def transition(channel, policy, starting, success, failure):
        assert (channel, policy, starting) == ("A", "warn_clamp", False)
        callbacks.append((success, failure))
        return True
    card._compliance_policy_transition_provider = transition
    card._temporary_policy_channel = "A"
    card._temporary_policy_original = "warn_clamp"
    card._temporary_policy_phase = "running"
    generated = []
    def generate(dataset, params, path):
        assert card._temporary_policy_phase == "idle"
        path.write_bytes(b"test report")
        generated.append(path)
        return path
    monkeypatch.setattr(KeithleyPdfReportGenerator, "generate", generate)
    try:
        card._on_sweep_finished(dataset)
        assert card._current_csv_path.is_file()
        assert not generated
        assert not card.start_button.isEnabled()
        callbacks[0][1]("injected restore failure")
        assert card._current_csv_path.is_file()
        assert not generated
        assert card._pending_single_report is not None
        card._begin_policy_restore()
        callbacks[1][0]("warn_clamp")
        assert len(generated) == 1
        assert card.pdf_button.isEnabled()
        assert card.start_button.isEnabled()
    finally:
        card.close()
        app.processEvents()


def test_close_during_real_simulated_field_acquisition(tmp_path):
    import threading
    import time
    from dataclasses import replace
    import yaml
    from PySide6.QtCore import Qt
    from app.ui.shell.main_window import MainWindow
    from app.devices.keithley_2600.characterization.field_worker import FieldSeriesWorker
    from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
    app = QApplication.instance() or QApplication([])
    template, _ = make_worker(tmp_path)
    raw = template.settings.model_dump(mode="json")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    path = tmp_path / "simulation.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    window = MainWindow(str(path), simulation=True)
    card = window.keithley_page.characterization_card
    controller = window._controllers["keithley"]
    lease = controller.acquire_run_lease()
    worker = None
    try:
        lease.connect()
        config = replace(template.config, sweep=replace(template.config.sweep,
            points_count=101, dwell_time_s=.05))
        lease.configure_source(KeithleyCharacterizationRunner.source_request_for_level(config.sweep, 0))
        lease.configure_source(config.field_source)
        policies = {ch: lease.compliance_policy(ch) for ch in ("A", "B")}
        worker = FieldSeriesWorker(lease, window._settings, config,
            tmp_path / "active_series", policies, policies)
        card._field_worker = worker
        card._field_lease = lease
        worker.event.connect(card._on_field_event)
        worker.finished.connect(card._on_field_finished)
        first_point = threading.Event()
        worker.event.connect(lambda kind, data: first_point.set() if kind == "sample_point" else None,
                             Qt.ConnectionType.DirectConnection)
        window.resize(1400, 900)
        window.show()
        worker.start()
        deadline = time.monotonic() + 15
        while not first_point.is_set() and worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert first_point.is_set(), worker.outcome
        assert worker.isRunning()
        assert not window.close()
        assert window.isVisible()
        assert window._controllers["keithley"] is controller
        deadline = time.monotonic() + 30
        while (card._field_lease is not None or (card._field_report_worker is not None
               and card._field_report_worker.isRunning())) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert worker.outcome.status == "cancelled", worker.outcome.errors
        assert worker.outcome.outputs_off and worker.outcome.policies_restored
        assert card._field_lease is None
        assert window.isVisible()
        assert card.prepare_application_shutdown()
        assert window.close()
    finally:
        if worker is not None:
            worker.request_stop()
            worker.wait(5000)
        app.processEvents()
        if card._field_report_worker is not None:
            card._field_report_worker.wait(30000)
        if card._field_lease is not None:
            card._field_lease.release()
            card._field_lease = None
        elif worker is None:
            lease.release()
        window.close()
        app.processEvents()
