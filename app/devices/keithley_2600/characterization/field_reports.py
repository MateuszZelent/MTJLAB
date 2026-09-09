"""Regenerate field-curve reports from committed files, never from a device."""

from dataclasses import dataclass
import json
import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.devices.keithley_2600.characterization.analyzer import KeithleyCharacterizationAnalyzer
from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.characterization.report_pdf import (
    KeithleyPdfReportGenerator, REPORT_RENDER_LOCK,
)
from app.devices.keithley_2600.characterization.rigol_report import export_rigol_equivalence
from app.devices.keithley_2600.characterization.report_paths import report_path


@dataclass(frozen=True, slots=True)
class FieldReportResult:
    directory: Path
    report_paths: tuple[Path, ...]
    errors: tuple[str, ...]
    summary_csv: Path | None = None
    summary_pdf: Path | None = None


def regenerate_field_reports(directory: Path) -> FieldReportResult:
    """Keep acquisition status immutable; publish independent report outcomes."""
    with REPORT_RENDER_LOCK:
        series = load_field_series(directory)
        if series.manifest["status"] == "running":
            raise ValueError("Close/recover the series data before regenerating reports.")
        errors, reports = [], []
        from app.devices.keithley_2600.characterization.field_journal import export_field_journal
        try:
            series.manifest["journal_diagnostics"] = export_field_journal(series)
        except Exception as exc:
            errors.append(f"Journal diagnostic export: {exc}")
        for curve in series.curves:
            if curve.dataset is None:
                continue
            entry = series.manifest["entries"][curve.index]
            curve_errors = []
            try:
                export_rigol_equivalence(curve.dataset, curve.directory / "rigol_equivalence.csv")
            except Exception as exc:
                curve_errors.append(f"Rigol CSV: {exc}")
            try:
                parameters = KeithleyCharacterizationAnalyzer.analyze(curve.dataset)
                report = KeithleyPdfReportGenerator.generate(
                    curve.dataset, parameters, report_path(curve.directory, field_curve=True))
                reports.append(report)
            except Exception as exc:
                curve_errors.append(f"PDF: {exc}")
            entry["report_status"] = "error" if curve_errors else "ready"
            entry["report_errors"] = curve_errors
            errors.extend(f"Field item {curve.index + 1}: {error}" for error in curve_errors)
        from app.devices.keithley_2600.characterization.field_summary import export_field_summary, generate_field_summary_pdf
        summary_csv = summary_pdf = None
        summary_errors = []
        try:
            summary_csv = export_field_summary(series)
        except Exception as exc:
            summary_errors.append(f"Summary CSV/JSON: {exc}")
        try:
            summary_pdf = generate_field_summary_pdf(series)
        except Exception as exc:
            summary_errors.append(f"Summary PDF: {exc}")
        errors.extend(summary_errors)
        series.manifest["summary_report"] = {
            "status": "error" if summary_errors else "ready", "errors": summary_errors,
            "csv": summary_csv.name if summary_csv else None, "pdf": summary_pdf.name if summary_pdf else None,
        }
        # Acquisition records, raw CSV and snapshots are never rewritten here.
        # Atomically publish the report status after all renderer file handles close.
        temporary = series.directory / ".series.reports.json.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(series.manifest, stream, sort_keys=True, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(series.directory / "series.json")
        finally:
            temporary.unlink(missing_ok=True)
        return FieldReportResult(series.directory, tuple(reports), tuple(errors), summary_csv, summary_pdf)


class FieldReportWorker(QThread):
    def __init__(self, directory: Path, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.result = None
        self.error = None

    completed_reports = Signal(object)

    def run(self):
        try:
            self.result = regenerate_field_reports(self.directory)
        except Exception as exc:
            self.error = str(exc)
        self.completed_reports.emit(self.result)
