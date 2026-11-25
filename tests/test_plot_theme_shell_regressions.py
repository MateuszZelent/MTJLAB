"""Focused regression tests for plot, theme, workers, and Fluent shell fixes."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QEvent, QRectF
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import isValid

from app.devices.moke_box.ui.voltage_history import MokeVoltageHistory
from app.domain.models import DeviceState
from app.settings import SettingsRepository
from app.ui.dashboard.device_card import DeviceConnectionPanel
from app.ui.design_system import (
    apply_application_theme,
    plot_theme,
    set_dynamic_property,
    tokens_for,
)
from app.ui.settings_page import SettingsPage
from app.ui.shell import MainWindow
from app.ui.widgets.limit_field import SafetyRangePill
from app.ui.widgets.plot_style import (
    _PaperGridItem,
    apply_scientific_style,
    unit_allows_auto_si_prefix,
)
from app.ui.widgets.spectrum_plot import SpectrumPlotWidget
from app.ui.workers import InstrumentWorker
from tests.shell_test_isolation import (
    isolated_shell_persistence as isolated_shell_persistence,  # noqa: PLC0414
)
from tests.shell_test_isolation import (
    shell_qt_application as shell_qt_application,  # noqa: PLC0414
)


def _ensure_app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# BUG-C1: _PaperGridItem zero-size / non-finite guard + weakref ownership
# ---------------------------------------------------------------------------


def test_paper_grid_handles_zero_view_and_non_finite_bounds_with_weakref() -> None:
    app = _ensure_app()
    plot_widget = pg.PlotWidget()
    try:
        plot_item = plot_widget.plotItem
        apply_scientific_style(plot_item, "dark")
        grid = plot_item._paper_grid
        assert isinstance(grid, _PaperGridItem)
        # No dual C++ QObject ownership: scene owns the QGraphicsItem, weakref tracks PlotItem.
        assert grid.parent() is None
        assert grid._plot_item_ref() is plot_item

        # Simulate degenerate 0-pixel view and non-finite bounding rectangles.
        plot_widget.resize(0, 0)
        app.processEvents()
        grid.generatePicture()  # Must not raise ZeroDivisionError.

        original_bounds = grid.boundingRect
        try:
            grid.boundingRect = lambda: QRectF(float("nan"), 0.0, 10.0, 10.0)
            grid.generatePicture()
            grid.boundingRect = lambda: QRectF(0.0, 0.0, float("inf"), 10.0)
            grid.generatePicture()
        finally:
            grid.boundingRect = original_bounds
    finally:
        plot_widget.close()
        plot_widget.deleteLater()
        app.processEvents()


def test_spectrum_plot_cleanup_is_safe_for_subsequent_theme_calls() -> None:
    app = _ensure_app()
    widget = SpectrumPlotWidget()
    native_plot = widget.plot
    try:
        widget.set_trace("Live", np.array([1e9, 2e9]), np.array([-70.0, -65.0]), primary=True)
        widget.show()
        app.processEvents()
        widget.cleanup_plot()
        # Theme refresh on a cleaned-up plot must be a safe no-op.
        widget.apply_theme("dark")
        apply_scientific_style(None, "dark")
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(native_plot), "PlotWidget.close() detaches the native view from its owner"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# BUG-M1: unit_allows_auto_si_prefix blocks log, ratio, squared & compound units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        "dB",
        "dBm",
        "dBc",
        "dB/Hz",
        "dBm/Hz",
        "ratio",
        "linear ratio",
        "W²",
        "mW²",
        "W^2",
        "W*Hz",
        "W·Hz",
        "V/Hz",
        "MHz",
        "kHz",
        "mV",
        "uA",
        "%",
        "ppm",
        "",
    ],
)
def test_auto_si_prefix_disabled_for_log_ratio_squared_compound_and_prefixed_units(
    unit: str,
) -> None:
    assert not unit_allows_auto_si_prefix(unit)


@pytest.mark.parametrize("unit", ["Hz", "W", "V", "A", "s", "T", "Ω", "Ohm"])
def test_auto_si_prefix_allowed_for_base_si_units(unit: str) -> None:
    assert unit_allows_auto_si_prefix(unit)


# ---------------------------------------------------------------------------
# UI-H1, BUG-L2, PERF-C4: Dark mode marker pills, finite peak indices, batching
# ---------------------------------------------------------------------------


def test_spectrum_plot_dark_marker_pills_and_batch_update_coalescing(tmp_path: Path) -> None:
    app = _ensure_app()
    widget = SpectrumPlotWidget()
    try:
        widget.apply_theme("dark")
        dark_tokens = tokens_for("dark")
        dark_palette = plot_theme(dark_tokens)
        assert widget.plot.backgroundBrush().color().name() == dark_palette.background.lower()
        assert widget.marker.label.color.name() == dark_tokens.text_primary.lower()
        assert widget.delta_marker.label.color.name() == dark_tokens.text_primary.lower()

        widget.apply_theme("light")
        light_tokens = tokens_for("light")
        light_palette = plot_theme(light_tokens)
        assert widget.plot.backgroundBrush().color().name() == light_palette.background.lower()
        assert widget.marker.label.color.name() == light_tokens.text_primary.lower()

        # PERF-C4: set_trace triggers PlotDataItem.updateItems at most once per frame.
        x = np.linspace(1e9, 2e9, 200)
        y = -80.0 + np.sin(np.linspace(0, 4 * math.pi, 200))
        widget.set_trace("Averaged", x, y, primary=True)
        curve = widget._curves["Averaged"]
        update_calls = 0
        original_update = curve.updateItems

        def _counting_update(*args, **kwargs):
            nonlocal update_calls
            update_calls += 1
            return original_update(*args, **kwargs)

        curve.updateItems = _counting_update  # type: ignore[method-assign]
        widget.set_trace("Averaged", x, y + 1.0, primary=True)
        assert update_calls <= 1

        # PERF-C4: batch_updates coalesces traces_updated signal to a single emission.
        emissions = 0
        widget.traces_updated.connect(lambda: _inc())

        def _inc() -> None:
            nonlocal emissions
            emissions += 1

        with widget.batch_updates():
            widget.set_trace("Averaged", x, y + 2.0, primary=True)
            widget.set_trace("Live frame", x, y + 3.0)
            assert emissions == 0
        assert emissions == 1

        # Publication export keeps white paper style even when screen is in dark mode,
        # while toggling back to dark mode re-renders legend item HTML with dark text_primary.
        widget.apply_theme("dark")
        assert widget.plot.plotItem.legend is not None
        for _sample, legend_label in widget.plot.plotItem.legend.items:
            assert dark_tokens.text_primary.lower() in legend_label.item.toHtml().lower()
        export_path = tmp_path / "pub.png"
        info = widget._export_figure(export_path)
        assert export_path.is_file()
        assert info["traces"][0]["color"] == "#1f77b4"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_peak_markers_preserve_original_indices_across_nans() -> None:
    app = _ensure_app()
    widget = SpectrumPlotWidget()
    try:
        # Original index 0 is NaN, index 1 is valid (-60), index 2 is NaN, index 3 is valid (-50).
        freqs = [1.0e9, 1.2e9, 1.4e9, 1.6e9]
        vals = [float("nan"), -60.0, float("inf"), -50.0]
        widget.set_peak_markers(freqs, vals)
        spots = widget.peak_markers.points()
        assert [spot.data() for spot in spots] == [1, 3]

        widget.select_peak_marker(3)
        palette = plot_theme(tokens_for(widget._theme_name))
        spots = widget.peak_markers.points()
        assert len(spots) == 2
        # Original index 3 (second finite spot) must be highlighted with reference color and size 14.
        assert spots[1].size() == 14
        assert spots[1].brush().color().name() == palette.reference.lower()
        assert spots[0].size() == 10
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# UI-H2, PERF-H1, UI-H4, UI-M3: Semantic QSS, repolish guard, workers, cards, pill
# ---------------------------------------------------------------------------


def test_semantic_dynamic_property_filter_and_repolish_dedup() -> None:
    app = _ensure_app()
    apply_application_theme(app, "dark")
    dark = tokens_for("dark")
    label = QLabel("LIVE ON")
    label.setObjectName("anritsuLiveIndicator")
    try:
        style = label.style()
        polish_calls = 0
        orig_polish = style.polish

        def _counting_polish(w):
            nonlocal polish_calls
            if w is label:
                polish_calls += 1
            return orig_polish(w)

        style.polish = _counting_polish  # type: ignore[method-assign]
        try:
            assert set_dynamic_property(label, "liveState", "on") is True
            app.processEvents()
            assert polish_calls == 1
            assert label.palette().color(label.foregroundRole()).name() == dark.success.lower()

            # Repeated identical value must not trigger another polish storm.
            assert set_dynamic_property(label, "liveState", "on") is False
            assert polish_calls == 1

            # Unknown outputState uses amber caution token on a Fluent BodyLabel too.
            from qfluentwidgets import BodyLabel

            output_label = BodyLabel("Outputs unknown")
            try:
                assert set_dynamic_property(output_label, "outputState", "unknown") is True
                app.processEvents()
                assert (
                    output_label.palette().color(output_label.foregroundRole()).name()
                    == dark.caution.lower()
                )
                assert output_label.darkColor.name() == dark.caution.lower()
                assert output_label.lightColor.name() == tokens_for("light").caution.lower()
            finally:
                output_label.deleteLater()
        finally:
            style.polish = orig_polish  # type: ignore[method-assign]
    finally:
        label.deleteLater()
        app.processEvents()


def test_instrument_worker_skips_redundant_state_signal_on_read_only_polls() -> None:
    adapter = MagicMock()
    adapter.state = DeviceState.VERIFIED
    adapter.fetch_current_trace.return_value = "trace-ok"
    adapter.fetch_current_trace_fast.return_value = "trace-fast-ok"
    adapter.connect.return_value = "connected"

    worker = InstrumentWorker(
        adapter,
        dispatcher=lambda dev, op, _payload: getattr(dev, op)(),
    )
    emitted_states: list[str] = []
    worker.state_changed.connect(emitted_states.append)

    # First read-only poll emits state once (from None -> "verified").
    worker.execute("fetch_current_trace", None)
    assert emitted_states == ["verified"]

    # Repeated read-only polls with unchanged state must NOT re-emit state_changed.
    worker.execute("fetch_current_trace", None)
    worker.execute("fetch_current_trace_fast", None)
    assert emitted_states == ["verified"]

    # State change during read-only poll DOES emit.
    adapter.state = DeviceState.UNKNOWN
    worker.execute("fetch_current_trace", None)
    assert emitted_states == ["verified", "unknown"]


def test_device_connection_panel_demotes_connect_when_connected() -> None:
    app = _ensure_app()
    panel = DeviceConnectionPanel("Anritsu MS2830A", "GPIB0::18::INSTR")
    try:
        panel.update_state("verified")
        assert panel.connect_button.text() == "Connected"
        assert panel.connect_button.property("controlState") == "confirmed"
        # Clearing connecting spinner after connect must preserve "Connected" label.
        panel.set_connecting(False)
        assert panel.connect_button.text() == "Connected"

        panel.update_state("disconnected")
        assert panel.connect_button.text() == "Connect"
        assert panel.connect_button.property("controlState") == "available"
    finally:
        panel.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_safety_range_pill_paints_cleanly_in_both_themes(theme: str) -> None:
    from PySide6.QtWidgets import QLineEdit

    from app.ui.widgets.limit_field import LimitField

    app = _ensure_app()
    apply_application_theme(app, theme)
    field = LimitField(QLineEdit("1 V"), "-5 V", "5 V")
    pill = field.range_pill
    try:
        assert isinstance(pill, SafetyRangePill)
        pill.resize(160, 26)
        image = QImage(160, 26, QImage.Format.Format_ARGB32)
        image.fill(0)
        pill.render(image)
        assert not image.isNull()
    finally:
        field.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# PERF-M1: MokeVoltageHistory idle 50ms timer stops when hidden or empty
# ---------------------------------------------------------------------------


def test_moke_voltage_history_timer_idle_when_hidden_or_empty() -> None:
    app = _ensure_app()
    history = MokeVoltageHistory()
    try:
        assert not history._window_timer.isActive()
        history.append(0.25)
        # Hidden widget must not run 50ms timer in background.
        assert not history._window_timer.isActive()

        history.show()
        app.processEvents()
        assert history._window_timer.isActive()

        history.clear()
        assert not history._window_timer.isActive()

        history.append(0.30)
        assert history._window_timer.isActive()

        history.hide()
        app.processEvents()
        assert not history._window_timer.isActive()
    finally:
        history.close()
        history.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# PERF-C2, UI-C1, B-01, BUG-H7: SettingsPage lazy + MainWindow shell fixes
# ---------------------------------------------------------------------------


def test_settings_page_defers_hidden_trees_and_limits_table(tmp_path: Path) -> None:
    app = _ensure_app()
    target = tmp_path / "settings.yml"
    target.write_text(Path(".config/settings.yml").read_text(encoding="utf-8"), encoding="utf-8")
    page = SettingsPage(SettingsRepository(target), read_only=True)
    try:
        assert page._trees_cache is None
        assert page._limits_table_widget is None
        # Only active "general" form is built initially.
        assert "general" in page._built_forms
        assert "anritsu" not in page._built_forms

        # Transparent property access materializes lazily.
        assert len(page.trees) == 6
        assert page.limits_table.rowCount() > 0
        assert page.limits_table.horizontalHeader().sectionResizeMode(0) != (
            page.limits_table.horizontalHeader().ResizeMode.ResizeToContents
        )
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_main_window_narrow_1024_collapse_lazy_pages_batched_log_and_two_phase_close(
    shell_qt_application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        # PERF-C2: heavy pages are NOT eagerly built during MainWindow.__init__.
        assert window._realized_device_pages == {}
        assert window._settings_page is None
        assert window._results_page is None
        assert window._recipe_page is None
        assert window._navigation_expand_threshold >= 1_200

        # UI-C1: 1024x700 auto-collapses navigation rail cleanly.
        window.resize(1024, 700)
        window.show()
        shell_qt_application.processEvents()
        assert window.navigationInterface.panel.isCollapsed()
        assert window.navigationInterface.width() <= 64
        assert window.fluent_content.width() > 900

        # B-01: ordinary simulation logs queue via the 50ms batch timer rather than
        # appending synchronously per line, while toPlainText() flushes transparently.
        window._flush_pending_log_ui()
        window._log("Ordinary simulation telemetry line")
        assert "Ordinary simulation telemetry line" in window._pending_log_ui_entries
        assert window._log_ui_flush_timer.isActive()
        assert "Ordinary simulation telemetry line" in window.log.toPlainText()
        assert window._pending_log_ui_entries == []

        # Transparent lazy materialization on property access.
        anritsu = window.anritsu_page
        assert "anritsu" in window._realized_device_pages
        assert isValid(anritsu)

        # BUG-H7: an ignored Phase-1 close must NOT kill _keithley_defaults_thread!
        with monkeypatch.context() as patch:
            patch.setattr(anritsu, "prepare_manual_archive_shutdown", lambda: False)
            assert not window.close()
            assert window._keithley_defaults_thread.isRunning()
            assert not window._closed

        assert window.close()
        assert window._closed
        assert not window._keithley_defaults_thread.isRunning()
    finally:
        window.close()
        shell_qt_application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        shell_qt_application.processEvents()
