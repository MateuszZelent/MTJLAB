"""Offline transfer predictions must not masquerade as current measurements."""

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset, CharacterizationPoint, CharacterizationSweepConfig, FieldLineObservation,
)
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from app.devices.keithley_2600.characterization.rigol_equivalence import rigol_equivalent_points
from app.devices.keithley_2600.characterization.rigol_report import export_rigol_equivalence
from app.domain.errors import SafetyViolation
from app.safety.rigol_current import rigol_display_voltage_from_open_circuit


def sample():
    return CharacterizationDataset(
        config=CharacterizationSweepConfig(),
        points=(CharacterizationPoint(
            index=7, demanded_si=210e-6, measured_current_a=200e-6,
            measured_voltage_v=0.6, true_resistance_ohm=999,
            apparent_resistance_ohm=888, power_w=0.00012,
            compliance_active=False, timestamp_epoch=1,
        ),), started_at_iso="2026-09-08T00:00:00Z", completed_at_iso="2026-09-08T00:00:01Z",
    )


def test_measured_point_not_demanded_or_resistance_used():
    point = rigol_equivalent_points(sample())[0]
    assert point.high_z_voltage_v == pytest.approx(0.610)
    assert point.load_50_voltage_v == pytest.approx(0.305)
    assert point.current_a == 200e-6
    assert point.source_index == 7


@pytest.mark.parametrize("defect", ["missing", "compliance", "nonfinite", "none"])
def test_field_series_map_requires_documented_field_conditions(defect):
    dataset = replace(sample(), field_line_current_a=0.0)
    observation = FieldLineObservation(demanded_current_a=0, measured_current_a=0,
        measured_voltage_v=0, power_w=0, timestamp_epoch=1, compliance_active=False)
    after = observation
    if defect == "missing":
        after = None
    elif defect == "compliance":
        after = replace(observation, compliance_active=True)
    elif defect == "nonfinite":
        after = replace(observation, measured_current_a=float("nan"))
    dataset = replace(dataset, points=(replace(dataset.points[0], field_before=observation, field_after=after),))
    row = rigol_equivalent_points(dataset)[0]
    if defect == "none":
        assert not row.exclusion_reason
        assert row.high_z_voltage_v == pytest.approx(.610)
    else:
        assert row.exclusion_reason
        assert row.high_z_voltage_v is None


def test_order_signs_repeats_and_exclusions_retained(tmp_path):
    dataset = sample()
    p = dataset.points[0]
    dataset = replace(dataset, points=(
        p, replace(p, index=8, compliance_active=True),
        replace(p, index=9, measured_current_a=-200e-6, measured_voltage_v=-0.6),
        replace(p, index=10, measured_current_a=float("nan")),
        replace(p, index=11, measured_current_a=0),
        replace(p, index=12, measured_voltage_v=-0.6), p,
    ))
    rows = rigol_equivalent_points(dataset)
    assert [row.source_index for row in rows] == [7, 8, 9, 10, 11, 12, 7]
    assert rows[1].exclusion_reason == "compliance"
    assert rows[1].high_z_voltage_v is None
    assert rows[2].high_z_voltage_v == pytest.approx(-0.610)
    assert rows[3].exclusion_reason == "nonfinite_measurement"
    assert rows[4].exclusion_reason == "zero_measurement"
    assert rows[5].exclusion_reason == "opposite_sign_iv"
    text = export_rigol_equivalence(dataset, tmp_path / "equivalence.csv").read_text()
    assert "0.61,0.305" in text
    assert "compliance" in text
    assert "nan" not in text


@pytest.mark.parametrize("load", ["UNKNOWN", 0, -50, float("nan"), float("inf")])
def test_invalid_load_rejected(load):
    with pytest.raises(SafetyViolation):
        rigol_display_voltage_from_open_circuit(0.610, load)


def test_other_load_consistent():
    assert rigol_display_voltage_from_open_circuit(0.610, 100) == pytest.approx(0.610 / 1.5)


def test_pdf_contains_real_numbers_and_conditions(tmp_path):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtPdf import QPdfDocument

    app = QApplication.instance() or QApplication([])
    dataset = sample()
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    pdf = KeithleyPdfReportGenerator.generate(dataset, params, tmp_path / "report.pdf")
    document = QPdfDocument(app)
    assert document.load(str(pdf)) == QPdfDocument.Error.None_
    content = "\n".join(document.getAllText(i).text() for i in range(document.pageCount()))
    assert "DC-equivalent setup guide" in content
    assert "610" in content and "305" in content
    assert "not measured Rigol currents" in content
    assert "operator instructions" in content
    assert "no interpolation" in content
    # Render the appendix using the already installed Qt PDF renderer.
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage, QPainter
    for page_index in (document.pageCount() - 2, document.pageCount() - 1):
        rendered = document.render(page_index, QSize(1190, 1684))
        assert not rendered.isNull()
        paper = QImage(rendered.size(), QImage.Format.Format_RGB32)
        paper.fill(0xFFFFFFFF)
        painter = QPainter(paper)
        painter.drawImage(0, 0, rendered)
        painter.end()
        assert paper.save(str(tmp_path / f"rigol_page_{page_index}.png"))
    document.close()


def test_empty_map_is_explicit():
    assert rigol_equivalent_points(replace(sample(), points=())) == ()
