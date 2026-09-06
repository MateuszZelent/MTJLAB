"""Tests for Keithley characterization PDF report generator and CSV export."""

from __future__ import annotations

from pathlib import Path

from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    SampleMetadata,
)
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from tests.test_keithley_characterization_analyzer import _build_ohmic_clamped_dataset


def test_pdf_report_generation(tmp_path: Path):
    """Verify that the PDF report is rendered without errors and contains expected data."""
    dataset = _build_ohmic_clamped_dataset()
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)

    output_pdf = tmp_path / "test_characterization_report.pdf"
    res_path = KeithleyPdfReportGenerator.generate(dataset, params, output_pdf)

    assert res_path.exists()
    assert res_path.stat().st_size > 20_000  # Report with plots should be > 20 KB


def test_pdf_report_generation_voltage_mode(tmp_path: Path):
    """Verify that PDF report renders properly for voltage-mode sweep."""
    pts = [
        CharacterizationPoint(
            index=i,
            demanded_si=float(0.01 * i),
            measured_voltage_v=float(0.01 * i),
            measured_current_a=float(0.01 * i / 100.0),
            true_resistance_ohm=100.0,
            apparent_resistance_ohm=100.0,
            power_w=float((0.01 * i) ** 2 / 100.0),
            compliance_active=(abs(i) >= 8),
            timestamp_epoch=float(i * 0.05),
        )
        for i in range(-10, 11)
    ]
    cfg = CharacterizationSweepConfig(
        channel="A",
        mode="voltage",
        start_level_si=-0.10,
        stop_level_si=0.10,
        points_count=21,
        compliance_si=0.0008,
        metadata=SampleMetadata(sample_id="Sample-VReport"),
    )
    dataset = CharacterizationDataset(
        config=cfg,
        points=tuple(pts),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="checksum-v",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    output_pdf = tmp_path / "test_voltage_mode_report.pdf"
    res_path = KeithleyPdfReportGenerator.generate(dataset, params, output_pdf)

    assert res_path.exists()
    assert res_path.stat().st_size > 20_000


def test_csv_export(tmp_path: Path):
    """Verify that CSV export contains metadata header and formatted numeric rows."""
    dataset = _build_ohmic_clamped_dataset()
    output_csv = tmp_path / "test_export.csv"
    res_path = KeithleyDataExporter.export_csv(dataset, output_csv)

    assert res_path.exists()
    content = res_path.read_text(encoding="utf-8")
    assert "# MTJLAB - Keithley Sample Characterization Dataset" in content
    assert "# Sample ID: MTJ-Test-01" in content
    assert "# Sense Mode: 4WIRE (Kelvin)" in content
    assert "# Dwell Time [s]: 0.05" in content
    assert "Demanded_SI,Voltage_V,Current_A,True_Resistance_Ohm" in content
    lines = content.strip().splitlines()
    assert len(lines) >= 110


def test_pdf_report_with_resistance_outliers(tmp_path: Path):
    """Verify that PDF generation succeeds when dataset contains extreme zero-crossing outliers."""
    dataset = _build_ohmic_clamped_dataset()
    # Inject an extreme 10 MOhm outlier spike at point 0 (zero-crossing noise)
    pts = list(dataset.points)
    p0 = pts[0]
    pts[0] = CharacterizationPoint(
        index=p0.index,
        demanded_si=p0.demanded_si,
        measured_voltage_v=p0.measured_voltage_v,
        measured_current_a=p0.measured_current_a,
        true_resistance_ohm=1.0e7,  # 10 MOhm outlier
        apparent_resistance_ohm=p0.apparent_resistance_ohm,
        power_w=p0.power_w,
        compliance_active=p0.compliance_active,
        timestamp_epoch=p0.timestamp_epoch,
    )
    noisy_dataset = CharacterizationDataset(
        config=dataset.config,
        points=tuple(pts),
        started_at_iso=dataset.started_at_iso,
        completed_at_iso=dataset.completed_at_iso,
        checksum_sha256="noisy",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(noisy_dataset)
    output_pdf = tmp_path / "test_outlier_report.pdf"
    res_path = KeithleyPdfReportGenerator.generate(noisy_dataset, params, output_pdf)
    assert res_path.exists()
    assert res_path.stat().st_size > 20_000


def test_pdf_report_with_bdr_tunnel_parameters(tmp_path: Path):
    """Verify that PDF generation succeeds when BDR tunneling parameters are active and formatted."""
    from dataclasses import replace
    dataset = _build_ohmic_clamped_dataset()
    base_params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    # Inject valid BDR extraction parameters
    tunnel_params = replace(
        base_params,
        tunnel_barrier_height_ev=2.45,
        tunnel_barrier_asymmetry_ev=0.15,
        tunnel_barrier_thickness_nm=1.2,
        bdr_coefficients=(1.0 / 450.0, 1e-4, 5e-4),
    )
    output_pdf = tmp_path / "test_bdr_report.pdf"
    res_path = KeithleyPdfReportGenerator.generate(dataset, tunnel_params, output_pdf)
    assert res_path.exists()
    assert res_path.stat().st_size > 20_000


def test_pdf_report_unipolar_sweep(tmp_path: Path):
    """Verify that PDF generation formats unipolar sweeps properly without negative compliance line."""
    pts = [
        CharacterizationPoint(
            index=i,
            demanded_si=float(0.001 * i),
            measured_voltage_v=float(0.45 * i * 0.1),
            measured_current_a=float(0.001 * i),
            true_resistance_ohm=450.0,
            apparent_resistance_ohm=450.0,
            power_w=float(0.45 * i * 0.1 * 0.001 * i),
            compliance_active=(i >= 8),
            timestamp_epoch=float(i * 0.05),
        )
        for i in range(0, 11)
    ]
    cfg = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=0.000,
        stop_level_si=0.010,
        points_count=11,
        compliance_si=0.670,
        metadata=SampleMetadata(sample_id="Unipolar-01"),
    )
    dataset = CharacterizationDataset(
        config=cfg,
        points=tuple(pts),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="unipolar",
    )
    params = KeithleyCharacterizationAnalyzer.analyze(dataset)
    commentary = KeithleyPdfReportGenerator._generate_commentary(dataset, params)
    assert "&plusmn;" not in commentary
    assert "+" in commentary

    output_pdf = tmp_path / "test_unipolar_report.pdf"
    res_path = KeithleyPdfReportGenerator.generate(dataset, params, output_pdf)
    assert res_path.exists()
    assert res_path.stat().st_size > 20_000


def test_pdf_report_and_commentary_strictly_english(tmp_path: Path):
    """Verify that all diagnostic narrative commentary and report strings are strictly in English."""
    # 1. Clamped current mode
    dataset_i_clamp = _build_ohmic_clamped_dataset()
    params_i_clamp = KeithleyCharacterizationAnalyzer.analyze(dataset_i_clamp)
    commentary_i = KeithleyPdfReportGenerator._generate_commentary(dataset_i_clamp, params_i_clamp)

    assert "zero-bias resistance" in commentary_i
    assert "compliance limit" in commentary_i
    assert "saturation threshold" in commentary_i
    assert "dielectric breakdown" in commentary_i
    assert "clamped points" in commentary_i

    # 2. Clamped voltage mode
    pts_v = [
        CharacterizationPoint(
            index=i,
            demanded_si=float(0.01 * i),
            measured_voltage_v=float(0.01 * i),
            measured_current_a=float(0.01 * i / 100.0),
            true_resistance_ohm=100.0,
            apparent_resistance_ohm=100.0,
            power_w=float((0.01 * i) ** 2 / 100.0),
            compliance_active=(abs(i) >= 8),
            timestamp_epoch=float(i * 0.05),
        )
        for i in range(-10, 11)
    ]
    cfg_v = CharacterizationSweepConfig(
        channel="B",
        mode="voltage",
        start_level_si=-0.10,
        stop_level_si=0.10,
        points_count=21,
        compliance_si=0.0008,
        metadata=SampleMetadata(sample_id="Sample-VReport"),
    )
    dataset_v_clamp = CharacterizationDataset(
        config=cfg_v,
        points=tuple(pts_v),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="checksum-v",
    )
    params_v_clamp = KeithleyCharacterizationAnalyzer.analyze(dataset_v_clamp)
    commentary_v = KeithleyPdfReportGenerator._generate_commentary(dataset_v_clamp, params_v_clamp)

    assert "thermal degradation" in commentary_v
    assert "electromigration" in commentary_v
    assert "compliance threshold" in commentary_v

    # 3. Unclamped linear ohmic response
    pts_ohmic = [
        CharacterizationPoint(
            index=i,
            demanded_si=float(0.0001 * i),
            measured_voltage_v=float(0.0001 * i * 50.0),
            measured_current_a=float(0.0001 * i),
            true_resistance_ohm=50.0,
            apparent_resistance_ohm=50.0,
            power_w=float((0.0001 * i) ** 2 * 50.0),
            compliance_active=False,
            timestamp_epoch=float(i * 0.05),
        )
        for i in range(-5, 6)
    ]
    cfg_ohmic = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-0.0005,
        stop_level_si=0.0005,
        points_count=11,
        compliance_si=1.0,
        metadata=SampleMetadata(sample_id="Sample-Ohmic"),
    )
    dataset_ohmic = CharacterizationDataset(
        config=cfg_ohmic,
        points=tuple(pts_ohmic),
        started_at_iso="2026-09-06T10:00:00Z",
        completed_at_iso="2026-09-06T10:00:05Z",
        checksum_sha256="checksum-ohmic",
    )
    params_ohmic = KeithleyCharacterizationAnalyzer.analyze(dataset_ohmic)
    commentary_ohmic = KeithleyPdfReportGenerator._generate_commentary(dataset_ohmic, params_ohmic)

    assert "high ohmic linearity" in commentary_ohmic
    assert "zero compliance clamping" in commentary_ohmic

    # Disallow Polish vocabulary across all generated commentary
    polish_keywords = ["prąd", "napięcie", "odrzucenie", "kanał", "bezpieczeństwo", "próbka", "rezystancja", "zabezpieczen"]
    for text in (commentary_i, commentary_v, commentary_ohmic):
        text_lower = text.lower()
        for kw in polish_keywords:
            assert kw not in text_lower, f"Polish keyword '{kw}' found in commentary: {text}"

