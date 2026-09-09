"""Offline report regeneration preserves raw scientific records on failure."""

import json

import pytest

from app.devices.keithley_2600.characterization.field_reports import regenerate_field_reports
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from app.devices.keithley_2600.characterization.report_paths import report_path
from tests.test_keithley_field_worker import make_worker


def test_reports_are_generated_offline_for_every_acquired_curve(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.run()
    commands = list(device.calls)
    raw = {path: path.read_bytes() for path in worker.directory.glob('*/characterization.csv')}
    result = regenerate_field_reports(worker.directory)
    assert not result.errors
    assert len(result.report_paths) == 3
    assert all(path.read_bytes().startswith(b"%PDF-") for path in result.report_paths)
    assert len({path.name for path in result.report_paths}) == 3
    from types import SimpleNamespace
    from app.devices.keithley_2600.characterization.field_catalogue import summary_artifacts_for_run
    links = summary_artifacts_for_run(SimpleNamespace(csv_path=str(next(iter(raw))), run_path=""))
    assert links[0][1] == str(result.summary_pdf)
    assert device.calls == commands
    assert all(path.read_bytes() == data for path, data in raw.items())
    manifest = json.loads((worker.directory / "series.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed_with_skips"
    assert [entry["report_status"] for entry in manifest["entries"]] == ["ready", "no_curve", "ready", "ready"]
    from PySide6.QtWidgets import QApplication
    from PySide6.QtPdf import QPdfDocument
    app = QApplication.instance() or QApplication([])
    document = QPdfDocument(app)
    assert document.load(str(result.report_paths[-1])) == QPdfDocument.Error.None_
    text = " ".join(document.getAllText(page).text() for page in range(document.pageCount()))
    assert "Field-line channel B" in text
    assert "5e-06 A" in text
    assert "history segment 1" in text
    assert "Rigol" in text
    assert "0.00 mA to 0.00 mA" not in text
    assert "1 uA to 3 uA" in text
    document.close()


def test_render_failure_preserves_measurements_and_previous_report(tmp_path, monkeypatch):
    worker, _ = make_worker(tmp_path)
    worker.run()
    paths = list(worker.directory.glob('*/characterization.csv'))
    old_pdf = report_path(paths[0].parent, field_curve=True)
    old_pdf.write_bytes(b"previous valid artifact")
    def fail(*args):
        raise OSError("injected renderer failure")
    monkeypatch.setattr(KeithleyPdfReportGenerator, "generate", fail)
    result = regenerate_field_reports(worker.directory)
    assert len(result.errors) == 3
    assert old_pdf.read_bytes() == b"previous valid artifact"
    assert all(path.is_file() for path in paths)
    manifest = json.loads((worker.directory / "series.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed_with_skips"
    assert manifest["entries"][0]["report_status"] == "error"


def test_running_store_is_not_modified_by_report_regeneration(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    path = worker.directory / "series.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "running"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Close/recover"):
        regenerate_field_reports(worker.directory)
    assert path.read_bytes() == before
    assert not list(worker.directory.glob('*/characterization_report*.pdf'))
