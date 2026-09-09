from dataclasses import replace
import json

import pytest

from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.characterization.observations import save_observation, load_observations
from tests.test_keithley_field_worker import make_worker


def test_card_annotation_regenerates_real_summary_without_device_calls(tmp_path, monkeypatch):
    import time
    from unittest.mock import Mock
    from PySide6.QtWidgets import QApplication
    from PySide6.QtPdf import QPdfDocument
    from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
    from app.devices.keithley_2600.ui.observation_dialog import ObservationDialog
    app = QApplication.instance() or QApplication([])
    worker, _ = make_worker(tmp_path)
    worker.run()
    controller = Mock()
    card = KeithleyCharacterizationCard(controller, worker.settings)
    def submit(dialog):
        dialog.author.setText("MZ")
        dialog.description.setPlainText("Operator region integration check")
        dialog._save()
        return dialog.result()
    monkeypatch.setattr(ObservationDialog, "exec", submit)
    try:
        card.open_field_series(worker.directory)
        card.annotate_curve_button.click()
        assert card._field_report_worker is not None
        deadline = time.monotonic() + 45
        while card._field_report_worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert not card._field_report_worker.isRunning()
        app.processEvents()
        assert not card._field_report_worker.result.errors
        document = QPdfDocument(app)
        assert document.load(str(card._field_report_worker.result.summary_pdf)) == QPdfDocument.Error.None_
        text = " ".join(document.getAllText(i).text() for i in range(document.pageCount()))
        assert "Operator region integration check" in text
        document.close()
        controller.call.assert_not_called()
        controller.acquire_run_lease.assert_not_called()
    finally:
        if card._field_report_worker is not None:
            card._field_report_worker.wait(30000)
        app.processEvents()
        card.close()


def test_observation_dialog_validation_render_and_save(tmp_path):
    from PySide6.QtWidgets import QApplication, QDialog
    from app.devices.keithley_2600.ui.observation_dialog import ObservationDialog
    app = QApplication.instance() or QApplication([])
    worker, device = make_worker(tmp_path)
    worker.run()
    curve = load_field_series(worker.directory).curves[0]
    calls = list(device.calls)
    dialog = ObservationDialog(curve, "MZ")
    try:
        dialog.show()
        app.processEvents()
        assert dialog.description.isVisible() and dialog.description.width() > 200
        assert dialog.grab().save(str(tmp_path / "observation_dialog.png"))
        dialog._save()
        assert dialog.saved_path is None and dialog.error.text()
        dialog.description.setPlainText("A change in resistance within selected points")
        dialog.hypothesis.setText("Possible magnetic dynamics")
        dialog._save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.saved_path.is_file()
        assert device.calls == calls
    finally:
        dialog.close()
        app.processEvents()


def test_observation_preserves_raw_data_and_hypothesis_status(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.run()
    curve = load_field_series(worker.directory).curves[0]
    source = (curve.directory / "dataset.json").read_bytes()
    calls = list(device.calls)
    path = save_observation(curve, 0, 2, author="MZ", description="Resistance change", hypothesis="Possible magnetic dynamics")
    record = load_observations(curve)[0]
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert record["mechanism_evidence"] == "unverified"
    assert record["point_indices"] == [0, 1, 2]
    assert record["qualified_resistance_range_ohm"] == pytest.approx([1000, 1000])
    assert record["delta_resistance_ohm"] == pytest.approx(0)
    assert record["relative_change_percent"] == pytest.approx(0)
    assert record["repeatability"] is None
    assert (curve.directory / "dataset.json").read_bytes() == source
    assert device.calls == calls
    changed = replace(curve, dataset=replace(curve.dataset, checksum_sha256="different"))
    with pytest.raises(ValueError, match="does not match"):
        load_observations(changed)


@pytest.mark.parametrize("first,last", [(-1, 1), (2, 1), (0, 100), (False, 1)])
def test_observation_rejects_unmeasured_range(tmp_path, first, last):
    worker, _ = make_worker(tmp_path)
    worker.run()
    curve = load_field_series(worker.directory).curves[0]
    with pytest.raises(ValueError):
        save_observation(curve, first, last, author="MZ", description="Note")
    assert not (curve.directory / "observations").exists()


@pytest.mark.parametrize("invalid_endpoint", [False, True])
def test_resistance_change_uses_selected_endpoints_without_substitution(tmp_path, invalid_endpoint):
    worker, _ = make_worker(tmp_path)
    worker.run()
    curve = load_field_series(worker.directory).curves[0]
    points = list(curve.dataset.points)
    points[-1] = replace(points[-1], measured_voltage_v=points[-1].measured_current_a * 800,
                         valid=not invalid_endpoint)
    curve = replace(curve, dataset=replace(curve.dataset, points=tuple(points), checksum_sha256=""))
    path = save_observation(curve, 0, 2, author="MZ", description="Endpoint change")
    record = json.loads(path.read_text(encoding="utf-8"))
    if invalid_endpoint:
        assert record["delta_resistance_ohm"] is None
        assert record["relative_change_percent"] is None
    else:
        assert record["delta_resistance_ohm"] == pytest.approx(-200)
        assert record["relative_change_percent"] == pytest.approx(-20)


@pytest.mark.parametrize("defect", ["invalid", "compliance", "branch"])
def test_annotation_does_not_compare_across_invalid_region(tmp_path, defect):
    worker, _ = make_worker(tmp_path)
    worker.run()
    curve = load_field_series(worker.directory).curves[0]
    points = list(curve.dataset.points)
    changes = {"valid": False} if defect == "invalid" else (
        {"compliance_active": True} if defect == "compliance" else {"demanded_si": points[-1].demanded_si})
    points[1] = replace(points[1], **changes)
    curve = replace(curve, dataset=replace(curve.dataset, points=tuple(points), checksum_sha256=""))
    path = save_observation(curve, 0, 2, author="MZ", description="Region for review")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["delta_resistance_ohm"] is None
    assert record["comparison_unavailable_reason"]
