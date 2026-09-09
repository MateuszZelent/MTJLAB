"""Series summary artifacts: all targets, common-window fits and nominal Rigol maps."""

import csv
from dataclasses import asdict, replace
from html import escape
import io
import json
import math
import os

import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak

from app.devices.keithley_2600.characterization.field_analysis import analyze_field_series, FieldFit
from app.devices.keithley_2600.characterization.field_plot_data import field_overlay_curves
from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
from app.devices.keithley_2600.characterization.rigol_equivalence import rigol_equivalent_points
from app.devices.keithley_2600.characterization.report_paths import report_path


def _resistance_axis_scale(axis):
    """Keep floating-point residue from masquerading as a resistance transition."""
    low, high = axis.dataLim.ymin, axis.dataLim.ymax
    if not all(math.isfinite(value) for value in (low, high)):
        return
    center = (low + high) / 2
    minimum_span = abs(center) * 0.02
    current_low, current_high = axis.get_ylim()
    if current_high - current_low < minimum_span:
        axis.set_ylim(center - minimum_span / 2, center + minimum_span / 2)
    axis.ticklabel_format(axis="y", style="plain", useOffset=False)


def _label_fit_points(axis, fits, attribute):
    """Combine coincident labels without combining measured runs or fit values."""
    groups = {}
    for fit in fits:
        value = getattr(fit, attribute)
        if value is not None:
            groups.setdefault((fit.field_current_a, value), []).append(f"#{fit.index + 1}")
    for position, labels in groups.items():
        axis.annotate(", ".join(labels), position, xytext=(4, 4), textcoords="offset points", fontsize=7)


def summary_data(series):
    config = series.manifest["config"]
    window = config.get("analysis_current_window_a")
    analysis = analyze_field_series(series, window, reference_index=config.get("analysis_reference_index")) if window is not None else None
    fits = analysis.fits if analysis else tuple(FieldFit(
        curve.index, curve.current_a, curve.history_segment, curve.status,
        reason="analysis_window_not_configured") for curve in series.curves)
    rows = []
    for curve, fit in zip(series.curves, fits):
        points = curve.dataset.points if curve.dataset else ()
        valid = [point for point in points if point.valid and not point.compliance_active]
        compliance = next((point for point in points if point.compliance_active), None)
        fields = [field for point in points for field in (point.field_before, point.field_after) if field is not None]
        rows.append({
            "field_index": curve.index, "field_current_a": curve.current_a,
            "history_segment": curve.history_segment, "status": curve.status,
            "acquired_points": len(points), "qualified_points": len(valid),
            "field_measured_min_a": min((f.measured_current_a for f in fields), default=None),
            "field_measured_max_a": max((f.measured_current_a for f in fields), default=None),
            "last_qualified_current_a": valid[-1].measured_current_a if valid else None,
            "first_compliance_demanded_si": compliance.demanded_si if compliance else None,
            "source_unit": "A" if config["sweep"]["mode"] == "current" else "V",
            "first_compliance_measured_current_a": compliance.measured_current_a if compliance else None,
            "max_sampled_power_w": max((abs(p.power_w) for p in points), default=None),
            "fit_current_min_a": window[0] if window else None,
            "fit_current_max_a": window[1] if window else None,
            "fit_point_count": len(fit.point_indices), "resistance_fit_ohm": fit.resistance_ohm,
            "voltage_offset_v": fit.voltage_offset_v,
            "bias_coverage": fit.bias_coverage,
            "apparent_ra_ohm_um2": fit.apparent_ra_ohm_um2,
            "resistance_standard_error_ohm": fit.resistance_standard_error_ohm,
            "r_squared": fit.r_squared, "relative_resistance_percent": fit.relative_resistance_percent,
            "reference_field_index": config.get("analysis_reference_index"),
            "fit_unavailable_reason": fit.reason, "detail": curve.detail,
        })
    return analysis, fits, rows


def export_field_summary(series):
    analysis, fits, rows = summary_data(series)
    target = series.directory / "field_series_summary.csv"
    temporary = target.with_suffix(".csv.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["field_index", "status"])
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = series.directory / "field_series_analysis.json"
    temporary = metadata.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "analysis": asdict(analysis) if analysis else None,
                       "fits": [asdict(fit) for fit in fits],
                       "source_csv_checksums": [entry.get("csv_sha256") for entry in series.manifest["entries"]]},
                      stream, sort_keys=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(metadata)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _figure_image(figure):
    output = io.BytesIO()
    try:
        figure.tight_layout()
        figure.savefig(output, format="png", dpi=150)
    finally:
        plt.close(figure)
    output.seek(0)
    return Image(output, width=500, height=365)


def generate_field_summary_pdf(series):
    """Caller holds REPORT_RENDER_LOCK; render only data already read from disk."""
    analysis, fits, rows = summary_data(series)
    KeithleyPdfReportGenerator._ensure_fonts()
    font = KeithleyPdfReportGenerator._FONT_NAME
    body = ParagraphStyle("FieldBody", fontName=font, fontSize=9, leading=12)
    heading = ParagraphStyle("FieldHeading", parent=body, fontSize=15, leading=19, spaceAfter=8)
    def p(text):
        return Paragraph(escape(str(text)), body)
    metadata = series.manifest["config"]["sweep"]["metadata"]
    story = [Paragraph("Field-line characterization — series summary", heading),
             p(f"Sample: {metadata['sample_id']} · {metadata.get('structure_name', '')}"),
             p(f"Acquisition status: {series.manifest['status']}. List order and repeated B currents are preserved."),
             p("B is field-line current, not calibrated magnetic field. Missing fits are not zero resistance. "
               "R_fit is a slope in the selected current window; it is not automatically zero-bias R or TMR."), Spacer(1, 8)]
    if not series.manifest["config"].get("verify_current_stability", True):
        story.append(p("B acquisition uses a fixed waiting time followed by current/voltage and compliance readback. "
                       "No additional current-tolerance or stability criterion was applied."))
    if analysis:
        story.extend((p(f"Common measured-current window: {analysis.current_window_a[0]:.9g} to {analysis.current_window_a[1]:.9g} A. "
                        f"Reference item: {analysis.reference_index + 1 if analysis.reference_index is not None else 'none'}."),
                      p(analysis.model), p(analysis.reference_error or "Fits require at least three distinct currents; no extrapolation or bridging invalid gaps.")))
    else:
        story.append(p("No common fit window was selected. Raw comparisons are available; fitted resistance and relative change are not determined."))

    def table(data, widths):
        item = Table([[p(value) for value in row] for row in data], colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                                 ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#a0adbd")),
                                 ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return item

    def fmt(value):
        return "—" if value is None else f"{value:.6g}"

    def reason_text(reason):
        if reason == "analysis_window_not_configured":
            return "No fit window selected"
        return reason or "fit computed"
    story.extend((Spacer(1, 10), table(
        [["Item", "B [A]", "History", "Status", "Points", "Peak sampled P [W]"]] +
        [[row["field_index"] + 1, fmt(row["field_current_a"]), row["history_segment"], row["status"], row["acquired_points"], fmt(row["max_sampled_power_w"])] for row in rows],
        [35, 65, 45, 170, 40, 145]), Spacer(1, 10)))
    story.append(table([["Item", "R_fit [ohm]", "Offset [V]", "Delta R [%]", "Unavailable reason"]] +
                       [[fit.index + 1, fmt(fit.resistance_ohm), fmt(fit.voltage_offset_v), fmt(fit.relative_resistance_percent), reason_text(fit.reason)] for fit in fits],
                       [35, 85, 80, 70, 230]))
    story.append(p("Delta R compares fitted slopes against the named reference. It does not establish P/AP states, TMR or vortex excitation. "
                   "Thermal drift, bias dependence and magnetic history remain possible confounders. CSV/JSON contain fit quality, slope standard error and source indices. "
                   "The OLS slope standard error is not the full measurement uncertainty."))
    story.append(table([["Item", "Bias coverage", "Apparent RA [ohm um2]"]] +
                       [[fit.index + 1, fit.bias_coverage, fmt(fit.apparent_ra_ohm_um2)] for fit in fits],
                       [35, 205, 260]))
    story.append(p("Positive-only or negative-only fits are one-sided estimates in the selected window, not direct measurements at zero bias. "
                   "Apparent RA uses the supplied junction area and the fitted total resistance; in 2-wire mode it includes leads and contacts. "
                   "No lead subtraction is applied. Missing area leaves RA undetermined. Per-point fit residuals are saved in the analysis JSON."))
    story.append(PageBreak())
    from app.devices.keithley_2600.characterization.observations import load_observations
    annotations = [(curve.index, record) for curve in series.curves for record in load_observations(curve)]
    if annotations:
        story.append(Paragraph("Operator observations — mechanisms unverified", heading))
        for index, record in annotations:
            story.append(p(f"Field item {index + 1}, points {record['point_indices']}; "
                           f"{record['author']}, {record['created_at_utc']}: {record['description']}"))
            story.append(p(f"Hypothesis (unverified): {record['hypothesis'] or 'not provided'}. "
                           f"Contains compliance: {record['contains_compliance']}; invalid points: {record['contains_invalid_points']}."))
            story.append(p(f"Selected endpoints: R_first = {fmt(record.get('first_resistance_ohm'))} ohm; "
                           f"R_last = {fmt(record.get('last_resistance_ohm'))} ohm; "
                           f"delta R = {fmt(record.get('delta_resistance_ohm'))} ohm; "
                           f"change = {fmt(record.get('relative_change_percent'))}% relative to the first selected point. "
                           "An invalid endpoint is not replaced by a neighbouring point. "
                           f"Comparison limitation: {record.get('comparison_unavailable_reason') or 'none recorded'}."))
        story.append(PageBreak())
    # Paginate original list positions, retaining skipped items and numbering.
    for offset in range(0, max(1, len(series.curves)), 12):
        plot_series = replace(series, curves=series.curves[offset:offset + 12])
        plot_fits = fits[offset:offset + 12]
        if offset:
            story.append(PageBreak())
        story.append(p(f"Plot group: field items {offset + 1} to {min(offset + 12, len(series.curves))}"))
        figure, axes = plt.subplots(2, 2, figsize=(9, 6.5))
        mode = series.manifest["config"]["sweep"]["mode"]
        for resistance, axis in ((False, axes[0, 0]), (True, axes[0, 1])):
            for curve in field_overlay_curves(plot_series, resistance=resistance):
                axis.plot(curve.x, curve.y, marker=".", linewidth=1, label=f"#{curve.index + 1}")
            axis.set_xlabel("Demanded current [A]" if mode == "current" else "Demanded voltage [V]")
            axis.set_ylabel("Resistance [ohm]" if resistance else "Measured voltage [V]" if mode == "current" else "Measured current [A]")
            if axis.lines:
                axis.legend(fontsize=7, ncol=3)
        for fit in plot_fits:
            if fit.resistance_ohm is not None:
                axes[1, 0].errorbar(fit.field_current_a, fit.resistance_ohm, yerr=fit.resistance_standard_error_ohm, fmt="o")
            if fit.relative_resistance_percent is not None:
                axes[1, 1].plot(fit.field_current_a, fit.relative_resistance_percent, "o")
        _label_fit_points(axes[1, 0], plot_fits, "resistance_ohm")
        _label_fit_points(axes[1, 1], plot_fits, "relative_resistance_percent")
        for axis, label in ((axes[1, 0], "R_fit [ohm]"), (axes[1, 1], "Relative fitted resistance [%]")):
            axis.set_xlabel("Field-line current B [A]")
            axis.set_ylabel(label)
            if not axis.lines:
                axis.text(.5, .5, "Not determined", ha="center", transform=axis.transAxes)
        for axis in axes.flat:
            axis.grid(alpha=.25)
        _resistance_axis_scale(axes[0, 1])
        _resistance_axis_scale(axes[1, 0])
        story.extend((Paragraph("Measured curves and common-window comparison", heading), _figure_image(figure)))
        story.append(p("Invalid/compliance points and field targets skipped on B compliance are excluded from the comparison. "
                       "Point numbers identify list order; no interpolation between field targets is claimed. "
                       "Resistance axes use a minimum 2% display span to avoid magnifying rounding noise; saved numeric values are unchanged."))
        story.append(PageBreak())
        figure, axes = plt.subplots(1, 2, figsize=(9, 6.5))
        for curve in plot_series.curves:
            if curve.dataset is None or curve.status == "skipped_field_compliance":
                continue
            points = [point for point in rigol_equivalent_points(curve.dataset) if not point.exclusion_reason]
            for axis, attr in ((axes[0], "high_z_voltage_v"), (axes[1], "load_50_voltage_v")):
                axis.scatter([getattr(point, attr) for point in points], [point.current_a for point in points], s=8, label=f"#{curve.index + 1}")
        for axis, title in zip(axes, ("LOAD High-Z", "LOAD 50 ohm")):
            axis.set_title(title)
            axis.set_xlabel("Nominal Rigol DC setting [V]")
            axis.set_ylabel("Measured Keithley current [A]")
            axis.grid(alpha=.25)
            if axis.collections:
                axis.legend(fontsize=7, ncol=3)
        story.extend((Paragraph("Nominal Rigol DC equivalents by field target", heading), _figure_image(figure),
                      p("Assumption: direct nominal 50-ohm generator source and the same sample state/path as the Keithley measurement. "
                        "The two plots use LOAD High-Z and LOAD 50 ohm display conventions respectively. "
                        "These are DC estimates, not measured Rigol currents or qualified AC settings. Changing field, sample resistance, load convention or cabling changes validity. "
                        "Use the detailed operator instructions in each individual report. No limit or output is changed by this report.")))
    target = report_path(series.directory, summary=True)
    temporary = target.with_name(".field_series_report.tmp.pdf")
    try:
        SimpleDocTemplate(str(temporary), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=32, bottomMargin=32).build(story)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
