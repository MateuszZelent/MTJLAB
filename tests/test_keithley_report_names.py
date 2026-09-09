"""Repeated acquisitions preserve previous artifacts and use distinct PDF basenames."""
from unittest.mock import Mock
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.characterization.report_paths import report_path, create_run_directory
from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.ui import characterization_card as module
from tests.test_keithley_field_worker import make_worker


def test_two_measurements_at_same_proposed_path_keep_first_pdf_and_csv(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'ui.ini'), QSettings.Format.IniFormat)
    monkeypatch.setattr(module, 'QSettings', lambda *args: settings)
    worker, _ = make_worker(tmp_path)
    worker.run()
    dataset = load_field_series(worker.directory).curves[0].dataset
    parameters = KeithleyCharacterizationAnalyzer.analyze(dataset)
    card = module.KeithleyCharacterizationCard(Mock(), worker.settings)
    monkeypatch.setattr(card, '_automatic_run_directory', lambda _: tmp_path / '20260908_150000_000000')
    try:
        card._save_completed_measurement(dataset, parameters)
        card._finish_single_report_after_restore()
        first_pdf, first_csv = card._current_pdf_path, card._current_csv_path
        pdf_bytes, csv_bytes = first_pdf.read_bytes(), first_csv.read_bytes()
        assert pdf_bytes.startswith(b'%PDF-')
        card._save_completed_measurement(dataset, parameters)
        card._finish_single_report_after_restore()
        assert card._current_pdf_path != first_pdf
        assert card._current_pdf_path.name != first_pdf.name
        assert card._current_csv_path.parent.name.endswith('_002')
        assert card._current_pdf_path.read_bytes().startswith(b'%PDF-')
        assert first_pdf.read_bytes() == pdf_bytes
        assert first_csv.read_bytes() == csv_bytes
    finally:
        card.close()
        app.processEvents()


def test_series_reports_include_series_and_position_and_keep_legacy_readable(tmp_path):
    first = tmp_path / '20260908_150000' / '0001_Ib_0A'
    second = tmp_path / '20260908_150001' / '0001_Ib_0A'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert report_path(first, field_curve=True).name != report_path(second, field_curve=True).name
    legacy = first / 'characterization_report.pdf'
    legacy.write_bytes(b'old report')
    assert report_path(first, field_curve=True, existing=True) == legacy
    new = report_path(first, field_curve=True)
    new.write_bytes(b'new report')
    assert report_path(first, field_curve=True, existing=True) == new
    assert legacy.read_bytes() == b'old report'
    assert report_path(first.parent, summary=True).name != report_path(second.parent, summary=True).name


def test_directory_collision_increments_without_reusing_existing_folder(tmp_path):
    proposed = tmp_path / 'run'
    paths = [create_run_directory(proposed) for _ in range(3)]
    assert [path.name for path in paths] == ['run', 'run_002', 'run_003']
    assert all(path.is_dir() for path in paths)
