"""Artifact preparation must not delay restoration or discard raw data on analysis failure."""
import hashlib
import threading

import pytest
from PySide6.QtCore import QThread, QTimer

from app.devices.keithley_2600.characterization import single_artifacts, report_pdf
from tests.test_keithley_characterization_analyzer import _build_ohmic_clamped_dataset
from tests.test_source_review_characterization_gaps import card as card, qt_application as qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("restore_first", [True, False])
def test_artifacts_and_restore_join_without_blocking_gui(card, qt_application, tmp_path, monkeypatch, restore_first):
    entered, release = threading.Event(), threading.Event()
    ticks, restorations = [], []
    analyze = single_artifacts.KeithleyCharacterizationAnalyzer.analyze
    export = single_artifacts.KeithleyDataExporter.export_csv

    def analyze_off_gui(dataset):
        assert QThread.currentThread() != qt_application.thread()
        assert (tmp_path / "run" / "characterization.csv").is_file()
        entered.set()
        assert release.wait(5)
        return analyze(dataset)

    def export_off_gui(*args):
        assert QThread.currentThread() != qt_application.thread()
        return export(*args)

    def restore(channel, policy, enabled, success, failure):
        restorations.append((channel, policy, enabled, success))
        return True

    def pdf(data, parameters, path):
        assert QThread.currentThread() != qt_application.thread()
        path.write_bytes(b"test report")
        return path

    monkeypatch.setattr(single_artifacts.KeithleyCharacterizationAnalyzer, "analyze", analyze_off_gui)
    monkeypatch.setattr(single_artifacts.KeithleyDataExporter, "export_csv", export_off_gui)
    monkeypatch.setattr(report_pdf.KeithleyPdfReportGenerator, "generate", pdf)
    monkeypatch.setattr(card, "_automatic_run_directory", lambda _: tmp_path / "run")
    card._temporary_policy_channel = "A"
    card._temporary_policy_original = "warn_clamp"
    card._temporary_policy_phase = "running"
    card._compliance_policy_transition_provider = restore
    card._run_inventory_target = ("original-sample", "1", "2", "cell")
    card.show()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        timer.start(5)
        card._on_sweep_finished(_build_ohmic_clamped_dataset())
        assert len(restorations) == 1
        assert restorations[0][:3] == ("A", "warn_clamp", False)
        wait_until(qt_application, lambda: entered.is_set() and len(ticks) >= 3)
        assert not card.close()
        assert card._single_artifacts_worker.inventory_target == ("original-sample", "1", "2", "cell")
        if restore_first:
            restorations[0][3]("warn_clamp")
            assert not card.start_button.isEnabled()
        release.set()
        wait_until(qt_application, lambda: card._single_artifacts_worker is None)
        if not restore_first:
            assert card._single_report_worker is None
            assert not card.start_button.isEnabled()
            assert card._pending_single_report is not None
            restorations[0][3]("warn_clamp")
        wait_until(qt_application, lambda: card._single_report_worker is None)
        assert card.start_button.isEnabled()
        assert card.csv_button.isEnabled() and card.pdf_button.isEnabled()
        assert card._current_csv_path.is_file() and card._current_pdf_path.is_file()
        assert card.prepare_application_shutdown()
    finally:
        release.set()
        timer.stop()
        if card._temporary_policy_phase == "restoring":
            restorations[0][3]("warn_clamp")
        wait_until(qt_application, lambda: card._single_artifacts_worker is None and card._single_report_worker is None)


@pytest.mark.parametrize("failure", ["analysis", "csv"])
def test_artifact_failure_is_explicit_and_independent_results_survive(qt_application, monkeypatch, tmp_path, failure):
    def fail(*args):
        raise OSError("injected failure")

    if failure == "analysis":
        monkeypatch.setattr(single_artifacts.KeithleyCharacterizationAnalyzer, "analyze", fail)
    else:
        monkeypatch.setattr(single_artifacts.KeithleyDataExporter, "export_csv", fail)
    worker = single_artifacts.SingleArtifactsWorker(_build_ohmic_clamped_dataset(), tmp_path / "run", None)
    worker.start()
    try:
        wait_until(qt_application, lambda: not worker.isRunning())
        assert worker.error is None
        result = worker.result
        assert len(result.errors) == 1 and "injected failure" in result.errors[0]
        if failure == "analysis":
            assert result.parameters is None
            assert result.csv_path.is_file()
            assert result.csv_sha256 == hashlib.sha256(result.csv_path.read_bytes()).hexdigest()
        else:
            assert result.csv_path is None and result.csv_sha256 is None
            assert result.parameters is not None
        assert (tmp_path / "run" / "rigol_equivalence.csv").is_file()
    finally:
        assert worker.wait(5000)
        worker.deleteLater()


def test_preparation_failure_still_requests_policy_restore(card, monkeypatch):
    called = []
    def fail(_):
        raise OSError("directory unavailable")
    monkeypatch.setattr(card, "_automatic_run_directory", fail)
    monkeypatch.setattr(card, "_begin_policy_restore", lambda: called.append(True))
    card._on_sweep_finished(_build_ohmic_clamped_dataset())
    assert called == [True]
    assert card._single_artifacts_worker is None
    assert "directory unavailable" in card.banner.last_message


def test_analysis_failure_publishes_raw_without_stale_metrics(card, qt_application, monkeypatch, tmp_path):
    def fail(_):
        raise ValueError("analysis unavailable")
    monkeypatch.setattr(single_artifacts.KeithleyCharacterizationAnalyzer, "analyze", fail)
    monkeypatch.setattr(card, "_automatic_run_directory", lambda _: tmp_path / "run")
    card.metric_r0.setText("R0: previous result")
    card._on_sweep_finished(_build_ohmic_clamped_dataset())
    wait_until(qt_application, lambda: card._single_artifacts_worker is None)
    assert "previous result" not in card.metric_r0.text()
    assert card._current_parameters is None
    assert card._current_csv_path.is_file() and card.csv_button.isEnabled()
    assert card._single_report_worker is None and not card.pdf_button.isEnabled()
    assert "analysis unavailable" in card.banner.last_message
    assert card.prepare_application_shutdown()
