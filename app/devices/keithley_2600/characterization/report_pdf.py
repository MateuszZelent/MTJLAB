"""Laboratory PDF report generator for Keithley sample characterization."""

from __future__ import annotations

import io
import math
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    ExtractedScientificParameters,
)


class KeithleyPdfReportGenerator:
    """Produces publication-grade scientific characterization PDF reports."""

    _FONT_NAME = "AppSegoe"
    _FONT_BOLD = "AppSegoeBold"
    _FONTS_REGISTERED = False

    @classmethod
    def _ensure_fonts(cls) -> None:
        if cls._FONTS_REGISTERED:
            return

        regular_path = "C:/Windows/Fonts/segoeui.ttf"
        bold_path = "C:/Windows/Fonts/segoeuib.ttf"

        if not os.path.exists(regular_path):
            regular_path = "C:/Windows/Fonts/arial.ttf"
            bold_path = "C:/Windows/Fonts/arialbd.ttf"

        if os.path.exists(regular_path):
            try:
                pdfmetrics.registerFont(TTFont(cls._FONT_NAME, regular_path))
                if os.path.exists(bold_path):
                    pdfmetrics.registerFont(TTFont(cls._FONT_BOLD, bold_path))
                else:
                    pdfmetrics.registerFont(TTFont(cls._FONT_BOLD, regular_path))
                cls._FONTS_REGISTERED = True
                return
            except Exception:
                pass

        # Fallback to standard PDF fonts (diacritics limited)
        cls._FONT_NAME = "Helvetica"
        cls._FONT_BOLD = "Helvetica-Bold"
        cls._FONTS_REGISTERED = True

    @classmethod
    def generate(
        cls,
        dataset: CharacterizationDataset,
        params: ExtractedScientificParameters,
        output_path: str | Path,
    ) -> Path:
        """Generate complete PDF characterization report."""
        cls._ensure_fonts()
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        doc = SimpleDocTemplate(
            str(target),
            pagesize=A4,
            leftMargin=36,
            rightMargin=36,
            topMargin=32,
            bottomMargin=32,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "ReportTitle",
            parent=styles["Heading1"],
            fontName=cls._FONT_BOLD,
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#0f2d59"),
        )
        subtitle_style = ParagraphStyle(
            "ReportSubtitle",
            parent=styles["Normal"],
            fontName=cls._FONT_NAME,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#555555"),
        )
        h2_style = ParagraphStyle(
            "ReportH2",
            parent=styles["Heading2"],
            fontName=cls._FONT_BOLD,
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#1b497e"),
            spaceBefore=6,
            spaceAfter=3,
        )
        body_style = ParagraphStyle(
            "ReportBody",
            parent=styles["Normal"],
            fontName=cls._FONT_NAME,
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor("#222222"),
        )
        comment_style = ParagraphStyle(
            "ReportComment",
            parent=styles["Normal"],
            fontName=cls._FONT_NAME,
            fontSize=8.5,
            leading=12.5,
            textColor=colors.HexColor("#1e293b"),
        )

        story = []

        # 1. Header
        story.append(Paragraph("MTJLAB &mdash; Raport Charakterystyki Próbki (I-V & R)", title_style))
        story.append(
            Paragraph(
                "Laboratorium Spintroniki i Nanostruktur &bull; Miernik źródłowy Keithley 2600 &bull; Moduł analityczny",
                subtitle_style,
            )
        )
        story.append(Spacer(1, 4))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0f2d59"), spaceAfter=8))

        # 2. Metadata & Test Configuration Table
        meta = dataset.config.metadata
        cfg = dataset.config

        meta_data = [
            [
                Paragraph("<b>Identyfikator próbki:</b>", body_style),
                Paragraph(meta.sample_id, body_style),
                Paragraph("<b>Kanał Keithley:</b>", body_style),
                Paragraph(f"Kanał {cfg.channel}", body_style),
            ],
            [
                Paragraph("<b>Struktura / Chip:</b>", body_style),
                Paragraph(meta.structure_name or "&mdash;", body_style),
                Paragraph("<b>Tryb przemiatania:</b>", body_style),
                Paragraph(f"{'Prądowy (I &rarr; V)' if cfg.mode == 'current' else 'Napięciowy (V &rarr; I)'}", body_style),
            ],
            [
                Paragraph("<b>Powierzchnia złącza:</b>", body_style),
                Paragraph(f"{meta.junction_area_um2:.2f} &mu;m&sup2;" if meta.junction_area_um2 else "Nie podano", body_style),
                Paragraph("<b>Zakres przemiatania:</b>", body_style),
                Paragraph(
                    f"{cfg.start_level_si * 1e3:.2f} mA do {cfg.stop_level_si * 1e3:.2f} mA ({cfg.points_count} pkt)"
                    if cfg.mode == "current"
                    else f"{cfg.start_level_si * 1e3:.1f} mV do {cfg.stop_level_si * 1e3:.1f} mV ({cfg.points_count} pkt)",
                    body_style,
                ),
            ],
            [
                Paragraph("<b>Operator:</b>", body_style),
                Paragraph(meta.operator or "&mdash;", body_style),
                Paragraph("<b>Limit compliance:</b>", body_style),
                Paragraph(f"{cfg.compliance_si * 1e3:.1f} mV" if cfg.mode == "current" else f"{cfg.compliance_si * 1e3:.2f} mA", body_style),
            ],
            [
                Paragraph("<b>Czas pomiaru:</b>", body_style),
                Paragraph(f"{dataset.started_at_iso[:19]} UTC", body_style),
                Paragraph("<b>Tryb pomiaru sondy:</b>", body_style),
                Paragraph(f"{cfg.sense_mode.upper()} (Kelvin)", body_style),
            ],
        ]

        meta_table = Table(meta_data, colWidths=[110, 150, 110, 150])
        meta_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(meta_table)
        story.append(Spacer(1, 8))

        # 3. Scientific Parameters Table
        story.append(Paragraph("Parametry naukowe i fizyczne złącza", h2_style))

        r0_str = f"{params.zero_bias_resistance_ohm:.2f} &Omega;" if math.isfinite(params.zero_bias_resistance_ohm) else "&mdash;"
        g0_str = f"{params.zero_bias_conductance_s * 1e3:.3f} mS" if math.isfinite(params.zero_bias_conductance_s) else "&mdash;"
        ra_str = f"{params.ra_product_ohm_um2:.1f} &Omega;&bull;&mu;m&sup2;" if params.ra_product_ohm_um2 is not None else "Brak danych pow."
        pmax_str = f"{params.max_power_dissipated_w * 1e3:.2f} mW"
        r2_str = f"{params.linearity_r2:.4f}"

        if params.compliance_detected and params.compliance_onset_point:
            ci, cv = params.compliance_onset_point
            comp_status_str = f"Aktywny przy |I| &ge; {abs(ci) * 1e3:.2f} mA (V = {abs(cv) * 1e3:.1f} mV)"
        else:
            comp_status_str = "Nie wystąpił (w pełnym zakresie liniowym)"

        clamped_pct_str = f"{params.clamped_points_fraction * 100:.1f}% punktów"
        rect_str = f"{params.rectification_ratio:.3f}" if params.rectification_ratio is not None else "&mdash;"

        if params.tunnel_barrier_height_ev is not None:
            bdr_str = f"&Phi; = {params.tunnel_barrier_height_ev:.2f} eV, &Delta;&Phi; = {params.tunnel_barrier_asymmetry_ev:+.2f} eV (s = {params.tunnel_barrier_thickness_nm:.1f} nm)"
        else:
            bdr_str = "Dopasowanie BDR niedostępne (złącze omowe/symetryczne)"

        param_rows = [
            [
                Paragraph("<b>Rezystancja zeroprądowa (R<sub>0</sub>):</b>", body_style),
                Paragraph(r0_str, body_style),
                Paragraph("<b>Przewodność (G<sub>0</sub>):</b>", body_style),
                Paragraph(g0_str, body_style),
            ],
            [
                Paragraph("<b>Iloczyn R&bull;A (Junction RA):</b>", body_style),
                Paragraph(ra_str, body_style),
                Paragraph("<b>Maks. moc wydzielana:</b>", body_style),
                Paragraph(pmax_str, body_style),
            ],
            [
                Paragraph("<b>Stan compliance (nasycenie):</b>", body_style),
                Paragraph(comp_status_str, body_style),
                Paragraph("<b>Ułamek w compliance:</b>", body_style),
                Paragraph(clamped_pct_str, body_style),
            ],
            [
                Paragraph("<b>Współczynnik prostowania (RR):</b>", body_style),
                Paragraph(rect_str, body_style),
                Paragraph("<b>Liniowość (R&sup2; do Ohma):</b>", body_style),
                Paragraph(r2_str, body_style),
            ],
            [
                Paragraph("<b>Model bariery tunelowej (BDR):</b>", body_style),
                Paragraph(bdr_str, body_style),
                Paragraph("<b>Integralność danych:</b>", body_style),
                Paragraph(f"SHA-256: {dataset.checksum_sha256[:12]}...", body_style),
            ],
        ]

        param_table = Table(param_rows, colWidths=[130, 130, 120, 140])
        param_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(param_table)
        story.append(Spacer(1, 8))

        # 4. Interpretive Scientific Commentary
        story.append(Paragraph("Komentarz fizyczno-diagnostyczny", h2_style))
        comment_text = cls._generate_commentary(dataset, params)
        comment_table = Table([[Paragraph(comment_text, comment_style)]], colWidths=[520])
        comment_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#3b82f6")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ])
        )
        story.append(comment_table)
        story.append(Spacer(1, 8))

        # 5. Matplotlib 4-panel figure
        fig_buf = cls._render_figure(dataset, params)
        story.append(Image(fig_buf, width=520, height=330))
        story.append(Spacer(1, 6))

        # 6. Footer note
        footer_text = (
            f"Raport wygenerowano automatycznie w środowisku MTJLAB. Suma kontrolna próbki SHA-256: "
            f"{dataset.checksum_sha256}. Wyniki zapisano zgodnie ze standardem bezpieczeństwa aparatury."
        )
        story.append(Paragraph(footer_text, subtitle_style))

        doc.build(story)
        return target

    @classmethod
    def _generate_commentary(
        cls,
        dataset: CharacterizationDataset,
        params: ExtractedScientificParameters,
    ) -> str:
        """Generate detailed physical diagnosis in Polish."""
        cfg = dataset.config
        r0 = params.zero_bias_resistance_ohm

        if not math.isfinite(r0):
            return "Błąd interpretacji: nie udało się wyznaczyć rezystancji spoczynkowej próbki."

        if cfg.mode == "current":
            v_comp_mv = cfg.compliance_si * 1e3
            if params.compliance_detected and params.compliance_onset_point:
                i_clamp_ma = abs(params.compliance_onset_point[0]) * 1e3
                v_clamp_mv = abs(params.compliance_onset_point[1]) * 1e3
                max_demanded_ma = max(abs(cfg.start_level_si), abs(cfg.stop_level_si)) * 1e3
                hypothetical_v = (max_demanded_ma * 1e-3) * abs(r0) * 1e3

                return (
                    f"Dla próbki o rezystancji zeroprądowej R<sub>0</sub> = {r0:.1f} &Omega;, wymuszenie zadanego "
                    f"prądu maksymalnego I<sub>max</sub> = &plusmn;{max_demanded_ma:.1f} mA wymagałoby przyłożenia do złącza "
                    f"napięcia &plusmn;{hypothetical_v:.1f} mV. Zgodnie z nastawami stacji laboratoryjnej, kanał {cfg.channel} "
                    f"posiadał aktywny limit bezpieczeństwa compliance V<sub>comp</sub> = {v_comp_mv:.1f} mV. "
                    f"W punkcie |I| &approx; {i_clamp_ma:.2f} mA napięcie osiągnęło poziom nasycenia ({v_clamp_mv:.1f} mV), "
                    f"wskutek czego Keithley ograniczył dalszy wzrost prądu. Pomiędzy prądem zadanym a zmierzonym "
                    f"powstało nasycenie aparaturowe ({params.clamped_points_fraction * 100:.0f}% punktów w compliance). "
                    f"Bariera próbki została skutecznie ochroniona przed przebiciem dielektrycznym."
                )
            else:
                max_v_mv = params.max_voltage_v * 1e3
                return (
                    f"Próbka o rezystancji zeroprądowej R<sub>0</sub> = {r0:.1f} &Omega; w całym zadanym zakresie "
                    f"prądowym pracowała poniżej limitu compliance V<sub>comp</sub> = {v_comp_mv:.1f} mV. Maksymalne napięcie "
                    f"przyłożone do próbki wyniosło {max_v_mv:.1f} mV. "
                    f"Charakterystyka wykazuje wysoką liniowość (R&sup2; = {params.linearity_r2:.4f}) bez wystąpienia clamping-u."
                )
        else:
            i_comp_ma = cfg.compliance_si * 1e3
            if params.compliance_detected and params.compliance_onset_point:
                i_clamp_ma = abs(params.compliance_onset_point[0]) * 1e3
                v_clamp_mv = abs(params.compliance_onset_point[1]) * 1e3
                max_demanded_mv = max(abs(cfg.start_level_si), abs(cfg.stop_level_si)) * 1e3
                hypothetical_i_ma = (max_demanded_mv * 1e-3) / abs(r0) * 1e3

                return (
                    f"Dla próbki o rezystancji zeroprądowej R<sub>0</sub> = {r0:.1f} &Omega;, przyłożenie zadanego "
                    f"napięcia maksymalnego V<sub>max</sub> = &plusmn;{max_demanded_mv:.1f} mV wywołałoby prąd rzędu "
                    f"&plusmn;{hypothetical_i_ma:.2f} mA. Zgodnie z nastawami stacji laboratoryjnej, kanał {cfg.channel} "
                    f"posiadał aktywny limit prądowy compliance I<sub>comp</sub> = {i_comp_ma:.2f} mA. "
                    f"W punkcie |V| &approx; {v_clamp_mv:.1f} mV prąd osiągnął limit ({i_clamp_ma:.2f} mA), "
                    f"wskutek czego Keithley ograniczył dalszy wzrost prądu ({params.clamped_points_fraction * 100:.0f}% punktów w compliance). "
                    f"Złącze zostało skutecznie ochronione przed uszkodzeniem termicznym i elektromigracją."
                )
            else:
                max_i_ma = params.max_current_a * 1e3
                return (
                    f"Próbka o rezystancji zeroprądowej R<sub>0</sub> = {r0:.1f} &Omega; w całym zadanym zakresie "
                    f"napięciowym pracowała poniżej limitu compliance I<sub>comp</sub> = {i_comp_ma:.2f} mA. Maksymalny prąd "
                    f"płynący przez próbkę wyniósł {max_i_ma:.2f} mA. "
                    f"Charakterystyka wykazuje wysoką liniowość (R&sup2; = {params.linearity_r2:.4f}) bez wystąpienia clamping-u."
                )

    @classmethod
    def _render_figure(
        cls,
        dataset: CharacterizationDataset,
        params: ExtractedScientificParameters,
    ) -> io.BytesIO:
        """Render high-resolution 4-panel analytical figure."""
        points = dataset.points
        is_current = dataset.config.mode == "current"

        v_meas = np.array([p.measured_voltage_v * 1e3 for p in points])  # mV
        i_meas = np.array([p.measured_current_a * 1e3 for p in points])  # mA
        r_true = np.array([p.true_resistance_ohm for p in points])
        r_app = np.array([p.apparent_resistance_ohm for p in points])
        p_mw = np.array([p.power_w * 1e3 for p in points])
        comp = np.array([p.compliance_active for p in points])

        # Independent sweep axis (x):
        # Current mode -> I_demanded [mA]; Voltage mode -> V_demanded [mV]
        x_dem = np.array([p.demanded_si * 1e3 for p in points])
        x_label = "Zadany prąd $I_{dem}$ [mA]" if is_current else "Zadane napięcie $V_{dem}$ [mV]"

        fig, axs = plt.subplots(2, 2, figsize=(7.6, 4.8), dpi=250)
        plt.subplots_adjust(hspace=0.35, wspace=0.32, top=0.94, bottom=0.10, left=0.09, right=0.96)

        # Plot 1: Response vs Demanded Level
        ax1 = axs[0, 0]
        if is_current:
            v_comp_mv = dataset.config.compliance_si * 1e3
            ax1.plot(x_dem, v_meas, color="#0284c7", lw=1.8, label=r"$V(I_{dem})$")
            if np.any(comp):
                ax1.scatter(x_dem[comp], v_meas[comp], color="#ef4444", s=18, zorder=5, label="Compliance clamp")
            ax1.axhline(v_comp_mv, color="#dc2626", ls="--", lw=1.0, alpha=0.8, label=rf"+$V_{{comp}}$ ({v_comp_mv:.0f} mV)")
            ax1.axhline(-v_comp_mv, color="#dc2626", ls="--", lw=1.0, alpha=0.8, label=r"-$V_{comp}$")
            ax1.set_ylabel("Napięcie próbki $V$ [mV]", fontsize=8.5)
            ax1.set_title(r"1. Krzywa napięciowa $V(I)$ i limit clamping", fontsize=9, fontweight="bold", color="#0f2d59")
        else:
            i_comp_ma = dataset.config.compliance_si * 1e3
            ax1.plot(x_dem, i_meas, color="#0284c7", lw=1.8, label=r"$I(V_{dem})$")
            if np.any(comp):
                ax1.scatter(x_dem[comp], i_meas[comp], color="#ef4444", s=18, zorder=5, label="Compliance clamp")
            ax1.axhline(i_comp_ma, color="#dc2626", ls="--", lw=1.0, alpha=0.8, label=rf"+$I_{{comp}}$ ({i_comp_ma:.2f} mA)")
            ax1.axhline(-i_comp_ma, color="#dc2626", ls="--", lw=1.0, alpha=0.8, label=r"-$I_{comp}$")
            ax1.set_ylabel("Prąd próbki $I$ [mA]", fontsize=8.5)
            ax1.set_title(r"1. Krzywa prądowa $I(V)$ i limit clamping", fontsize=9, fontweight="bold", color="#0f2d59")

        ax1.set_xlabel(x_label, fontsize=8.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=7, loc="best")

        # Plot 2: Resistance vs Sweep Level (True vs Apparent)
        ax2 = axs[0, 1]
        ax2.plot(x_dem, r_true, color="#059669", lw=1.8, label=r"$R_{true} = V_{meas} / I_{meas}$")
        if np.any(comp):
            app_label = r"$R_{app} = V / I_{dem}$" if is_current else r"$R_{app} = V_{dem} / I$"
            ax2.plot(x_dem, r_app, color="#f59e0b", lw=1.4, ls="--", label=app_label)
        r_all = np.concatenate([r_true[np.isfinite(r_true)], r_app[np.isfinite(r_app)]])
        if len(r_all) > 0:
            r_median = float(np.median(r_all))
            if r_median > 0:
                p95 = float(np.percentile(r_all, 95))
                y_max = min(float(np.max(r_all)), max(r_median * 3.5, p95 * 1.5))
                y_min = max(0.0, float(np.min(r_all)))
                if y_max > y_min:
                    ax2.set_ylim(y_min * 0.9, y_max * 1.1)
        ax2.set_xlabel(x_label, fontsize=8.5)
        ax2.set_ylabel(r"Rezystancja $R$ [$\Omega$]", fontsize=8.5)
        ax2.set_title(r"2. Rezystancja rzeczywista vs pozorna", fontsize=9, fontweight="bold", color="#0f2d59")
        ax2.grid(True, linestyle=":", alpha=0.6)
        ax2.legend(fontsize=7, loc="best")

        # Plot 3: Differential conductance dI/dV
        ax3 = axs[1, 0]
        if params.differential_conductance_curve:
            v_diff = [pt[0] for pt in params.differential_conductance_curve]
            g_diff = [pt[1] * 1e3 for pt in params.differential_conductance_curve]  # mS
            ax3.plot(v_diff, g_diff, color="#7c3aed", lw=1.5, label="dI/dV numeryczne")
            if params.bdr_coefficients:
                c0, c1, c2 = params.bdr_coefficients
                v_fit = np.linspace(min(v_diff), max(v_diff), 100)
                g_fit = (c0 + c1 * v_fit + c2 * (v_fit ** 2)) * 1e3
                ax3.plot(v_fit, g_fit, color="#db2777", lw=1.2, ls="--", label="Model BDR")
            ax3.legend(fontsize=7, loc="lower center")
        ax3.set_xlabel("Napięcie próbki $V$ [V]", fontsize=8.5)
        ax3.set_ylabel("Przewodność $dI/dV$ [mS]", fontsize=8.5)
        ax3.set_title("3. Przewodność różniczkowa $dI/dV(V)$", fontsize=9, fontweight="bold", color="#0f2d59")
        ax3.grid(True, linestyle=":", alpha=0.6)

        # Plot 4: Power dissipation
        ax4 = axs[1, 1]
        ax4.plot(x_dem, p_mw, color="#d97706", lw=1.8, label="Moc próbki $P$")
        ax4.set_xlabel(x_label, fontsize=8.5)
        ax4.set_ylabel("Moc tracona $P$ [mW]", fontsize=8.5)
        ax4.set_title("4. Moc wydzielana na próbce $P$", fontsize=9, fontweight="bold", color="#0f2d59")
        ax4.grid(True, linestyle=":", alpha=0.6)
        ax4.legend(fontsize=7, loc="upper center")

        for ax in (ax1, ax2, ax3, ax4):
            ax.tick_params(labelsize=7.5)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=250, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf
