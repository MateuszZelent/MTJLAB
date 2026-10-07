"""Single-run reports leave the event loop alive and keep their owner alive."""
import threading

import pytest
from PySide6.QtCore import QThread, QTimer

from app.devices.keithley_2600.characterization import report_pdf, single_report
from tests.test_source_review_characterization_gaps import card as card, qt_application as qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("failure", [False, True])
def test_report_worker_snapshot_lifecycle_and_gui_responsiveness(card, qt_application, monkeypatch, tmp_path, failure):
    entered, release = threading.Event(), threading.Event()
    ticks = []
    dataset, parameters = {"points": [1]}, {"values": [2]}
    path = tmp_path / "report.pdf"

    def render(data, params, destination):
        assert QThread.currentThread() != qt_application.thread()
        entered.set()
        assert release.wait(5)
        assert data == {"points": [1]} and params == {"values": [2]}
        if failure:
            raise OSError("report write failed")
        destination.write_bytes(b"fake report")
        return destination

    monkeypatch.setattr(report_pdf.KeithleyPdfReportGenerator, "generate", render)
    card._pending_single_report = (dataset, parameters, path, None)
    card._temporary_policy_phase = "restoring"
    card._finish_single_report_after_restore()
    assert card._single_report_worker is None
    card._temporary_policy_phase = "idle"
    card.resize(1366, 768)
    card.show()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    worker = None
    try:
        timer.start(5)
        card._finish_single_report_after_restore()
        worker = card._single_report_worker
        dataset["points"].append(3)
        parameters["values"].append(4)
        wait_until(qt_application, lambda: entered.is_set() and len(ticks) >= 3)
        assert not card.start_button.isEnabled()
        assert not card.close() and card.isVisible()
        assert card.width() > 0 and card.height() > 0
        card._on_start_clicked()
        assert card._single_report_worker is worker
        release.set()
        wait_until(qt_application, lambda: card._single_report_worker is None)
        assert card.start_button.isEnabled()
        assert (card._current_pdf_path == path) is (not failure)
        assert path.exists() is (not failure)
        assert card.prepare_application_shutdown()
    finally:
        release.set()
        timer.stop()
        if card._single_report_worker is not None:
            assert card._single_report_worker.wait(5000)
            qt_application.processEvents()


def test_report_snapshot_failure_does_not_leave_shutdown_locked(card, monkeypatch):
    def fail(*args):
        raise ValueError("cannot snapshot")

    monkeypatch.setattr(single_report, "SingleReportWorker", fail)
    card._pending_single_report = ({}, {}, "report.pdf", None)
    card._finish_single_report_after_restore()
    assert card._single_report_worker is None
    assert card._single_report_record is None
    assert card.prepare_application_shutdown()
