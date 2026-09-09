"""PDF appendix and full CSV for nominal Rigol equivalents of a measured curve."""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

from reportlab.lib import colors
from reportlab.platypus import Image, PageBreak, Paragraph, Spacer, Table, TableStyle

from app.devices.keithley_2600.characterization.models import CharacterizationDataset
from app.devices.keithley_2600.characterization.rigol_equivalence import rigol_equivalent_points, rigol_equivalence_context


def export_rigol_equivalence(dataset: CharacterizationDataset, path: Path) -> Path:
    """Publish an atomic derivative; never mutate the source characterization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write("# Rigol DC equivalence model v1: nominal direct connection, source 50 ohm\n")
        stream.write("# Not a current measurement, safety approval, or AC calibration\n")
        stream.write(f"# Source checksum: {dataset.checksum_sha256}\n")
        stream.write("# Context_JSON: " + json.dumps(rigol_equivalence_context(dataset), sort_keys=True, allow_nan=False) + "\n")
        writer = csv.writer(stream)
        writer.writerow(["Source_Index", "Measured_Current_A", "Measured_Voltage_V",
                         "Rigol_HighZ_DC_V", "Rigol_Load50_DC_V", "Exclusion_Reason"])
        for row in rigol_equivalent_points(dataset):
            def numeric(value):
                return format(value, ".12g") if value is not None and math.isfinite(value) else ""
            writer.writerow([row.source_index, numeric(row.current_a), numeric(row.measured_voltage_v),
                             numeric(row.high_z_voltage_v), numeric(row.load_50_voltage_v),
                             row.exclusion_reason])
        stream.flush()
    temporary.replace(path)
    return path


def rigol_report_story(dataset, heading_style, body_style):
    """Build a conditional operator guide entirely from acquired data."""
    # Import after the caller establishes the Agg backend.
    import matplotlib.pyplot as plt

    rows = rigol_equivalent_points(dataset)
    context = rigol_equivalence_context(dataset)
    eligible = [row for row in rows if not row.exclusion_reason]
    story = [PageBreak(), Paragraph("Rigol DG1032Z: DC-equivalent setup guide", heading_style)]
    texts = [
        f"<b>Model context:</b> {context['model_version']}; sense {context['sense_mode']}; "
        f"voltage plane: {context['voltage_reference_plane']}; status: {context['equivalence_status']}. "
        "Calibration, frequency validity and measurement uncertainty are not specified. "
        "An unknown external series drop is not assumed to be a measured zero correction.",
        "<b>Scope:</b> calculated equivalents for this dataset, not measured Rigol currents or "
        "approved output settings. Rigol LOAD, channel limits, frequency, field-line state and "
        "external circuit are not established by this table. Do not infer zero magnetic field.",
        "<b>Model:</b> the same external circuit measured by Keithley, driven through Rigol's "
        "nominal 50-ohm series output. No unmodelled termination, bias-tee, attenuator or parallel "
        "input. High-Z does not remove that 50 ohm. " + (
            "For these 2-wire data, the measured voltage includes the lead/contact drop; do not count it twice."
            if dataset.config.sense_mode == "2wire" else
            "This historical dataset uses 4-wire sensing; excluded series drops require a circuit correction."
        ),
        "<b>Formula:</b> V_oc = V_measured + 50 ohm * I_measured. Rigol DC = V_oc for LOAD High-Z; "
        "Rigol DC = V_oc/2 for LOAD 50 ohm. Numbers below are DC levels, not Vpp. Values use measured "
        "current, never demanded current or nominal resistance. LOAD changes the voltage convention.",
        f"<b>Data:</b> {len(eligible)} eligible of {len(rows)} acquired points. Excluded points include "
        "compliance, invalid/zero measurements and opposite-sign V/I. Near-zero uncertainty, state "
        "transitions and field stability still require review. Source order is preserved; no interpolation "
        "or extrapolation is performed. Use the accompanying CSV for all rows and exclusion reasons.",
    ]
    for text in texts:
        story.extend([Paragraph(text, body_style), Spacer(1, 7)])
    if eligible:
        # Deterministic selection of original points, not new target currents.
        indices = sorted({round(i * (len(eligible) - 1) / 11) for i in range(12)})
        table_rows = [[Paragraph(text, body_style) for text in (
            "Source index", "Measured I [uA]", "Measured V [mV]", "Rigol DC [mV]<br/>High-Z",
            "Rigol DC [mV]<br/>LOAD 50 ohm",
        )]]
        for index in indices:
            row = eligible[index]
            table_rows.append([str(row.source_index), f"{row.current_a * 1e6:.6g}",
                               f"{row.measured_voltage_v * 1e3:.6g}",
                               f"{row.high_z_voltage_v * 1e3:.6g}",
                               f"{row.load_50_voltage_v * 1e3:.6g}"])
        table = Table(table_rows, colWidths=[60, 105, 105, 125, 125], repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), body_style.fontName),
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
        ]))
        story.extend([table, Spacer(1, 7), Paragraph(
            f"Selected {len(indices)} of {len(eligible)} eligible points. Rounded model values "
            "do not represent instrument accuracy or command resolution.", body_style,
        )])
        fig, ax = plt.subplots(figsize=(8, 3))
        try:
            current = [row.current_a * 1e6 for row in eligible]
            ax.scatter([row.high_z_voltage_v * 1e3 for row in eligible], current, s=10, label="High-Z")
            ax.scatter([row.load_50_voltage_v * 1e3 for row in eligible], current, s=10,
                       marker="x", label="LOAD 50 ohm")
            ax.set_xlabel("Equivalent Rigol DC setting [mV]")
            ax.set_ylabel("Measured Keithley current [uA]")
            ax.grid(alpha=0.25)
            ax.legend()
            fig.tight_layout()
            buffer = io.BytesIO()
            fig.savefig(buffer, format="png", dpi=150)
            buffer.seek(0)
        finally:
            plt.close(fig)
        story.append(Image(buffer, width=520, height=195))
    else:
        story.append(Paragraph("<b>No numerical Rigol settings available.</b> No eligible points; "
                               "do not substitute a fitted resistance.", body_style))
    story.extend([PageBreak(), Paragraph("Rigol: operator instructions and AC limitations", heading_style)])
    for text in (
        "<b>1. Verify sample and state.</b> Use the recorded cell and magnetic history. An attained "
        "Keithley point is not proof that repeating it with a voltage source is safe.",
        "<b>2. With outputs OFF, verify connections.</b> Isolate the unused source according to "
        "the approved circuit. Keithley OUTPUT OFF alone need not isolate its terminals. Do not "
        "assume two sources may remain connected in parallel.",
        "<b>3. Select and verify LOAD before entering voltage.</b> Use the matching table column. "
        "For DC use the DC level/offset control, not the Vpp amplitude control. Entering the same "
        "number under a different LOAD convention can produce a different physical signal.",
        "<b>4. Apply the normal Rigol validation.</b> Check High/Low and all sample current, voltage "
        "and power limits before enabling. Do not widen limits based on this report. Validate the "
        "circuit at an independently approved low level. Predicted current is not hardware compliance.",
        "<b>5. AC is a separate calculation.</b> For sine voltage, High=offset+Vpp/2 and "
        "Low=offset-Vpp/2. Only for constant R: I_DC=alpha*offset/(R+50 ohm), "
        "I_AC_peak=alpha*Vpp/(2*(R+50 ohm)); alpha=1 for High-Z, 2 for LOAD 50 ohm. "
        "I_AC_RMS=I_AC_peak/sqrt(2). Check both extremes and the full modulation envelope. "
        "A DC table value must not be copied as an AC amplitude.",
        "<b>6. Dynamic validity.</b> No frequency, pulse duration or vortex threshold is qualified "
        "by this DC model. Nonlinear current can be distorted. Positive-only data do not establish "
        "negative-bias response. RF impedance, reflections and circuit loading require validation. "
        "A resistance drop may increase Rigol current; equal static points do not guarantee equal "
        "magnetic trajectories or stable operating states.",
        "<b>Example only:</b> measured 200 uA at 600 mV gives 610 mV High-Z or 305 mV with LOAD 50 ohm. "
        "For constant 3000 ohm, 600 mVpp High-Z with zero offset gives about 98.36 uA AC peak, "
        "not 200 uA DC. These example values are not recommended settings for this sample.",
        "Reference: Rigol DG1000Z User's Guide, Output Impedance, section 2-88. "
        "https://int.rigol.com/ind/Images/DG1000Z_UserGuide_EN_tcm13-2800.pdf. "
        "Model v1, nominal direct 50-ohm source; report generation issues no instrument commands.",
    ):
        story.extend([Paragraph(text, body_style), Spacer(1, 8)])
    return story
