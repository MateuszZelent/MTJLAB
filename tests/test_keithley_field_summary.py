"""Summary artifact content, explicit analysis window and atomic PDF publication."""

import csv
from dataclasses import replace
import json

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtPdf import QPdfDocument
from PySide6.QtCore import QSize

from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.characterization.field_summary import export_field_summary, generate_field_summary_pdf
from tests.test_keithley_field_worker import make_worker


def test_large_series_pdf_paginates_plots_without_dropping_positions(tmp_path, monkeypatch):
    from app.devices.keithley_2600.characterization import field_summary
    app = QApplication.instance() or QApplication([])
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    original = series.curves[0]
    curves = tuple(replace(original, index=index, current_a=index * 1e-6,
        dataset=replace(original.dataset, field_sequence_index=index, field_line_current_a=index * 1e-6)) for index in range(13))
    series = replace(series, curves=curves)
    captured = []
    render = field_summary._figure_image
    def capture(figure):
        captured.append([line.get_label() for line in figure.axes[0].lines])
        return render(figure)
    monkeypatch.setattr(field_summary, "_figure_image", capture)
    pdf = generate_field_summary_pdf(series)
    assert captured[0] == [f"#{i}" for i in range(1, 13)]
    assert captured[2] == ["#13"]
    document = QPdfDocument(app)
    assert document.load(str(pdf)) == QPdfDocument.Error.None_
    text = " ".join(document.getAllText(i).text() for i in range(document.pageCount()))
    assert "field items 1 to 12" in text and "field items 13 to 13" in text
    document.close()


def test_repeated_field_fit_labels_preserve_all_run_numbers():
    import matplotlib.pyplot as plt
    from app.devices.keithley_2600.characterization.field_analysis import FieldFit
    from app.devices.keithley_2600.characterization.field_summary import _label_fit_points
    fits = (FieldFit(0, 0, 0, "completed", resistance_ohm=1000),
            FieldFit(2, 0, 1, "completed", resistance_ohm=1000),
            FieldFit(3, .001, 1, "completed", resistance_ohm=900))
    figure, axis = plt.subplots()
    try:
        _label_fit_points(axis, fits, "resistance_ohm")
        assert [text.get_text() for text in axis.texts] == ["#1, #3", "#4"]
        assert fits[0].history_segment != fits[1].history_segment
    finally:
        plt.close(figure)


def test_resistance_plot_does_not_magnify_roundoff_or_clip_real_transition():
    import matplotlib.pyplot as plt
    from app.devices.keithley_2600.characterization.field_summary import _resistance_axis_scale
    figure, axes = plt.subplots(1, 2)
    try:
        axes[0].plot([0, 1, 2], [1000, 1000 + 1e-10, 1000 - 1e-10])
        axes[1].plot([0, 1, 2], [1000, 800, 700])
        for axis in axes:
            _resistance_axis_scale(axis)
        low, high = axes[0].get_ylim()
        assert high - low >= 20 - 1e-9
        low, high = axes[1].get_ylim()
        assert low <= 700 and high >= 1000
        assert list(axes[1].lines[0].get_ydata()) == [1000, 800, 700]
    finally:
        plt.close(figure)


def test_common_window_summary_has_every_target_and_real_pdf_numbers(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, device = make_worker(tmp_path)
    worker.config = replace(worker.config, analysis_current_window_a=(1e-6, 3e-6), analysis_reference_index=0)
    worker.run()
    series = load_field_series(worker.directory)
    commands = list(device.calls)
    csv_path = export_field_summary(series)
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert float(rows[0]["resistance_fit_ohm"]) == pytest.approx(1000)
    assert float(rows[2]["relative_resistance_percent"]) == pytest.approx(0, abs=1e-9)
    assert rows[1]["status"] == "skipped_field_compliance"
    assert rows[1]["resistance_fit_ohm"] == ""
    assert rows[0]["reference_field_index"] == "0"
    metadata = json.loads((worker.directory / "field_series_analysis.json").read_text(encoding="utf-8"))
    assert metadata["analysis"]["current_window_a"] == [1e-6, 3e-6]
    assert metadata["fits"][0]["point_indices"] == [0, 1, 2]
    from app.devices.keithley_2600.characterization.observations import save_observation
    save_observation(series.curves[0], 0, 2, author="MZ", description="Review this region", hypothesis="Possible dynamics")
    pdf = generate_field_summary_pdf(series)
    document = QPdfDocument(app)
    assert document.load(str(pdf)) == QPdfDocument.Error.None_
    text = " ".join(document.getAllText(page).text() for page in range(document.pageCount()))
    assert "series summary" in text
    assert "Reference item: 1" in text
    assert "Review this region" in text and "Possible dynamics" in text
    assert "1000" in text and "skipped_field_compliance" in text
    assert "LOAD High-Z" in text and "LOAD 50 ohm" in text
    assert document.pageCount() >= 3
    for page in range(document.pageCount()):
        assert document.render(page, QSize(900, 1273)).save(str(tmp_path / f"summary_page_{page + 1}.png"))
    document.close()
    assert device.calls == commands


def test_summary_pdf_failure_preserves_previous_artifact(tmp_path, monkeypatch):
    from app.devices.keithley_2600.characterization.report_paths import report_path
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    path = report_path(worker.directory, summary=True)
    path.write_bytes(b"previous PDF")
    def fail(*args, **kwargs):
        raise OSError("injected PDF write failure")
    monkeypatch.setattr("app.devices.keithley_2600.characterization.field_summary.SimpleDocTemplate.build", fail)
    with pytest.raises(OSError):
        generate_field_summary_pdf(series)
    assert path.read_bytes() == b"previous PDF"
    assert not (worker.directory / ".field_series_report.tmp.pdf").exists()


def test_optional_fit_window_is_explicitly_absent(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    path = export_field_summary(load_field_series(worker.directory))
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert all(row["resistance_fit_ohm"] == "" for row in rows)
    assert all(row["fit_unavailable_reason"] == "analysis_window_not_configured" for row in rows)

    app = QApplication.instance() or QApplication([])
    pdf = generate_field_summary_pdf(load_field_series(worker.directory))
    document = QPdfDocument(app)
    try:
        assert document.load(str(pdf)) == QPdfDocument.Error.None_
        text = " ".join(document.getAllText(i).text() for i in range(document.pageCount()))
        assert "No fit window selected" in " ".join(text.split())
        assert "analysis_window_not_configured" not in text
    finally:
        document.close()


def test_simple_wait_is_recorded_in_manifest_and_summary_pdf(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _ = make_worker(tmp_path)
    worker.config = replace(worker.config, verify_current_stability=False,
                            current_tolerance_a=0, current_tolerance_relative=0, stable_readings=1)
    worker.run()
    series = load_field_series(worker.directory)
    assert series.manifest['config']['verify_current_stability'] is False
    pdf = generate_field_summary_pdf(series)
    document = QPdfDocument(app)
    try:
        assert document.load(str(pdf)) == QPdfDocument.Error.None_
        text = ' '.join(document.getAllText(i).text() for i in range(document.pageCount()))
        assert 'No additional current-tolerance or stability criterion was applied.' in ' '.join(text.split())
    finally:
        document.close()
