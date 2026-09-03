from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
from qfluentwidgets import CardWidget, CheckBox, ComboBox, PlainTextEdit, PrimaryPushButton, PushButton, TransparentPushButton

from app.ui.shell import MainWindow
from app.ui.design_system import tokens_for
from app.settings import SettingsRepository
from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.manual_save import (
    ManualSpectrumSaveDialog,
    ManualSpectrumSaveOptions,
)
from app.devices.discovery import DiscoveredInstrument, identify_device
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import DIMENSION_CURRENT
from app.storage import Hdf5RunReader, ManualSpectrumSaveMode
from app.devices.lakeshore_475.models import FieldUnit, GaussmeterReading, GaussmeterSnapshot, MeasurementMode
from app.devices.moke_box.models import MokeHallVoltageReading
from tests.test_main_window import (
    TEST_ENGINEER,
    synthetic_anritsu_peaks,
    wait_for_ui,
    write_engineer_settings,
)


class FluentLakeShorePageTests(unittest.TestCase):
    """Rendered regression coverage for the Fluent migration of device surfaces."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow(".config/settings.yml", simulation=True)
        self.window.resize(1360, 880)
        self.window.show()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.application.processEvents()

    def test_lakeshore_card_surface_rethemes_while_visible(self) -> None:
        self.window._navigate_to("lakeshore_gaussmeter")
        self.application.processEvents()

        page = self.window.lakeshore_gaussmeter_page
        self.assertIsInstance(page.hero_card, CardWidget)
        self.assertIsInstance(page.values_card, CardWidget)
        self.assertIsInstance(page.live_card, CardWidget)
        self.assertIsInstance(page.plot_card, CardWidget)
        self.assertIsInstance(page.read_now, PrimaryPushButton)
        self.assertIsInstance(page.live, CheckBox)
        self.assertTrue(page.read_now.isVisibleTo(self.window))
        self.assertTrue(page.history_plot.isVisibleTo(self.window))
        self.assertGreater(page.history_plot.geometry().height(), 200)

        point = page.values_card.mapTo(self.window, QPoint(8, 8))
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()
        light = self.window.grab().toImage().pixelColor(point).name()
        light_color = self.window.grab().toImage().pixelColor(point)
        self.window._set_theme_mode("dark", persist=False)
        self.application.processEvents()
        dark = self.window.grab().toImage().pixelColor(point).name()
        self.assertNotEqual(light, dark)
        self.assertGreater(light_color.lightness(), 220)

        page_point = page.mapTo(self.window, QPoint(2, 2))
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()
        page_color = self.window.grab().toImage().pixelColor(page_point)
        self.assertGreater(page_color.lightness(), 220)

    def test_lakeshore_live_controls_separate_sampling_refresh_and_history(self) -> None:
        page = self.window.lakeshore_gaussmeter_page
        page.sample_interval.setCurrentIndex(0)
        page.refresh_interval.setCurrentIndex(1)
        page.history_window.setCurrentIndex(3)
        page._timer.start()
        page._plot_timer.start()
        page._sampling_changed(0)
        page._refresh_changed(1)

        self.assertEqual(page._timer.interval(), 500)
        self.assertEqual(page._plot_timer.interval(), 250)
        self.assertEqual(page._selected_value(page.history_window), 600)
        self.assertIn("10 min", page.plot_span.text())
        page._timer.stop()
        page._plot_timer.stop()

    def test_lakeshore_read_error_remains_visible_on_the_page(self) -> None:
        page = self.window.lakeshore_gaussmeter_page

        page._error("read_measurement", "timeout while reading field")

        self.assertIn("Read failed: timeout while reading field", page.banner.text())

    def test_lakeshore_plots_expose_fluent_reset_pan_and_zoom_controls(self) -> None:
        page = self.window.lakeshore_gaussmeter_page
        navigation = page.plot_navigation
        self.assertEqual(
            [button.text() for button in navigation.toolbar_buttons],
            ["Reset view", "Pan", "Box zoom", "Zoom out"],
        )
        self.assertTrue(
            all(isinstance(button, TransparentPushButton) for button in navigation.toolbar_buttons)
        )

        navigation.box_zoom.click()
        self.assertEqual(page.history_plot.getViewBox().state["mouseMode"], pg.ViewBox.RectMode)
        navigation.pan.click()
        self.assertEqual(page.history_plot.getViewBox().state["mouseMode"], pg.ViewBox.PanMode)

        page._field_curve.setData([0.0, 10.0], [0.0, 1.0])
        page.history_plot.setXRange(50.0, 60.0, padding=0)
        navigation.reset_view.click()
        self.application.processEvents()
        x_range = page.history_plot.viewRange()[0]
        self.assertLessEqual(x_range[0], 0.0)
        self.assertGreaterEqual(x_range[1], 10.0)

        page._open_live_window()
        self.application.processEvents()
        assert page._live_window is not None
        self.assertEqual(len(page._live_window.plot_navigation.toolbar_buttons), 4)

    def test_lakeshore_floating_window_shares_live_state_reading_and_history(self) -> None:
        page = self.window.lakeshore_gaussmeter_page
        page._open_live_window()
        self.application.processEvents()
        floating = page._live_window
        self.assertIsNotNone(floating)
        assert floating is not None
        self.assertTrue(floating.isVisible())
        self.assertTrue(
            bool(floating.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )

        with patch.object(page, "_read"):
            floating.live.setChecked(True)
            self.assertTrue(page.live.isChecked())
            floating.live.setChecked(False)
            self.assertFalse(page.live.isChecked())

        now = datetime.now(timezone.utc)
        snapshot = GaussmeterSnapshot(
            mode_code="1",
            mode=MeasurementMode.DC,
            unit_code="2",
            unit=FieldUnit.TESLA,
            range_code="3",
            autorange_enabled=True,
            probe_type_code="40",
            timestamp_utc=now,
            dc_resolution_code="3",
            rms_filter_mode_code="1",
            peak_mode_code="1",
            peak_display_code="1",
        )
        result = GaussmeterReading.now(
            mode=MeasurementMode.DC,
            unit=FieldUnit.TESLA,
            snapshot=snapshot,
            field_t=0.0125,
        )
        page._result("read_measurement", result)
        page._refresh_plot_if_needed()

        self.assertIn("+0.0125 T", floating.field.text())
        self.assertEqual(floating.mode.text(), "DC")
        x_values, y_values = floating.field_curve.getData()
        self.assertEqual(len(x_values), 1)
        self.assertEqual(list(y_values), [0.0125])

    def test_lakeshore_reflows_without_horizontal_overflow_at_minimum_window_size(self) -> None:
        self.window.resize(820, 560)
        self.window._navigate_to("lakeshore_gaussmeter")
        self.application.processEvents()

        page = self.window.lakeshore_gaussmeter_page
        host = self.window.navigation_routes["lakeshore_gaussmeter"]
        self.assertTrue(page._compact_layout)
        self.assertGreater(page.values_card.geometry().top(), page.hero_card.geometry().top())
        self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertGreater(
            page.sample_interval.geometry().top(),
            page.read_now.geometry().top(),
        )
        self.assertTrue(page.history_plot.isVisibleTo(self.window))

    def test_lakeshore_light_theme_keeps_every_visible_label_readable(self) -> None:
        self.window._navigate_to("lakeshore_gaussmeter")
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()

        page = self.window.lakeshore_gaussmeter_page
        panel = self.window.connection_panels["lakeshore_gaussmeter"]
        form = page.values_card.layout()
        required = (
            panel.heading,
            panel.summary,
            panel.state,
            form.labelForField(page.field),
            form.labelForField(page.frequency),
            form.labelForField(page.configuration),
            self.window.safety_strip.readiness,
            self.window.safety_strip.outputs,
            self.window.safety_strip.mode,
            self.window.safety_strip.actor,
        )
        for label in required:
            with self.subTest(label=label.text()):
                self.assertIsInstance(label, QLabel)
                foreground = label.palette().color(QPalette.ColorRole.WindowText)
                self.assertLess(foreground.lightness(), 180)
        for button in (panel.connect_button, panel.disconnect_button, page.read_now):
            with self.subTest(button=button.text()):
                disabled_qss = button.styleSheet().split(
                    "/* station-disabled-button */", 1
                )[1]
                self.assertIn("color: palette(placeholder-text)", disabled_qss)
                self.assertIn(
                    "background-color: palette(alternate-base)", disabled_qss
                )

    def test_lakeshore_plot_uses_elapsed_time_and_prunes_to_selected_window(self) -> None:
        page = self.window.lakeshore_gaussmeter_page
        page.history_window.setCurrentIndex(0)
        now = datetime.now(timezone.utc)

        def reading(timestamp: datetime, value: float) -> GaussmeterReading:
            snapshot = GaussmeterSnapshot(
                "1", MeasurementMode.DC, "2", FieldUnit.TESLA, "0", True, "40", timestamp
            )
            return GaussmeterReading(
                mode=MeasurementMode.DC,
                unit=FieldUnit.TESLA,
                snapshot=snapshot,
                timestamp_utc=timestamp,
                field_t=value,
            )

        page._history.extend((reading(now - timedelta(seconds=61), 1.0), reading(now, 2.0)))
        page._prune_history(now)
        page._plot_dirty = True
        page._refresh_plot_if_needed()

        self.assertEqual(len(page._history), 1)
        x_values, y_values = page._field_curve.getData()
        self.assertEqual(list(x_values), [0.0])
        self.assertEqual(list(y_values), [2.0])
        x_range = page.history_plot.viewRange()[0]
        self.assertAlmostEqual(x_range[0], -60.0, delta=0.5)
        self.assertAlmostEqual(x_range[1], 0.0, delta=0.5)

    def test_lakeshore_discovery_assignment_persists_and_refreshes_page(self) -> None:
        self.window.close()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.yml"
            write_engineer_settings(path)
            self.window = MainWindow(
                path, simulation=False, authenticated_username=TEST_ENGINEER
            )
            resource = "ASRL3::INSTR"
            identity = "LSCI,MODEL475,11272013,1.0"
            discovered = DiscoveredInstrument(
                resource,
                "system",
                identity,
                identify_device(identity),
                serial_baud=9_600,
            )
            self.window.dashboard._scan_completed((discovered,))
            row = self.window.dashboard.visa_results.rows[0]
            self.assertEqual(discovered.device, "lakeshore_gaussmeter")
            assignment_index = row.assignment.findData("lakeshore_gaussmeter")
            self.assertGreaterEqual(assignment_index, 0)
            row.assignment.setCurrentIndex(assignment_index)
            with patch(
                "app.ui.shell.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                row.assign_button.click()
            self.application.processEvents()

            saved = SettingsRepository(path).load().settings.lakeshore_gaussmeter
            self.assertTrue(saved.enabled)
            self.assertEqual(saved.resource, resource)
            self.assertEqual(saved.visa_backend, "system")
            self.assertEqual(saved.baud_rate, 9_600)
            self.assertIn(resource, self.window.lakeshore_gaussmeter_page.resource.text())
            self.assertTrue(self.window.lakeshore_gaussmeter_page.read_now.isEnabled())


class FluentAnritsuAndMokePageTests(unittest.TestCase):
    """Visual regression coverage for the Fluent-native Anritsu and MOKE surfaces."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow(".config/settings.yml", simulation=True)
        self.window.resize(1360, 880)
        self.window.show()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.application.processEvents()

    def test_anritsu_control_workspace_uses_fluent_cards_and_actions(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()

        page = self.window.anritsu_page
        self.assertIsInstance(page.hero_card, CardWidget)
        self.assertIsInstance(page.setup_card, CardWidget)
        self.assertIsInstance(page.processing_card, CardWidget)
        self.assertIsInstance(page.configure_button, PrimaryPushButton)
        self.assertIsInstance(page.live, PrimaryPushButton)
        self.assertTrue(page.hero_card.isVisibleTo(self.window))
        self.assertGreater(page.hero_card.geometry().width(), 300)

    def test_anritsu_manual_archive_panel_and_modal_are_rendered_and_operable(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()

        page = self.window.anritsu_page
        self.assertIsInstance(page.manual_save_card, CardWidget)
        self.assertTrue(page.manual_save_card.isVisibleTo(self.window))
        self.assertGreater(page.manual_save_card.height(), 120)
        self.assertIsInstance(page.configure_manual_spectrum, PushButton)
        self.assertTrue(page.configure_manual_spectrum.isVisibleTo(self.window))
        self.assertFalse(page.save_manual_spectrum.isEnabled())

        page._show_trace(
            SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
        )
        self.application.processEvents()
        self.assertFalse(page.save_manual_spectrum.isEnabled())
        self.assertIn("Configure the archive", page.manual_save_status.text())

        metadata = ManualMetadataValue(
            key="keithley.B.current_a",
            device="Keithley 2600",
            label="Keithley B · current",
            dimension=DIMENSION_CURRENT,
            unit="A",
            value_si=0.001,
        )
        dialog = ManualSpectrumSaveDialog(
            page,
            trace_choices=page._manual_trace_choices(),
            metadata_values=(metadata,),
            default_destination=Path("measurements/manual_spectrum.h5"),
            default_mode=ManualSpectrumSaveMode.APPEND,
        )
        dialog.show()
        self.application.processEvents()
        try:
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.mode.currentData(), "append")
            self.assertEqual(dialog.trace.currentData(), "raw")
            self.assertEqual(dialog.options().metadata_values, (metadata,))

            dialog.metadata_scope.setCurrentIndex(1)
            dialog._metadata_scope_changed()
            self.assertEqual(dialog.options().metadata_values, (metadata,))
            dialog.metadata_scope.setCurrentIndex(2)
            dialog._metadata_scope_changed()
            self.assertEqual(dialog.options().metadata_values, ())
        finally:
            dialog.close()

        self.window.resize(820, 560)
        self.application.processEvents()
        host = self.window.navigation_routes["anritsu"]
        self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)

    def test_anritsu_manual_archive_reuses_one_accepted_configuration(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        page = self.window.anritsu_page
        first = SpectrumTrace(
            frequencies_hz=(1e6, 2e6, 3e6),
            powers_dbm=(-60.0, -50.0, -55.0),
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name="TRAC1",
        )
        second = SpectrumTrace(
            frequencies_hz=first.frequencies_hz,
            powers_dbm=(-58.0, -48.0, -53.0),
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name="TRAC1",
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manual.h5"
            options = ManualSpectrumSaveOptions(
                destination=path,
                mode=ManualSpectrumSaveMode.APPEND,
                metadata_scope="none",
                metadata_values=(),
                trace_variant="raw",
            )
            page._show_trace(first)
            self.assertFalse(page.save_manual_spectrum.isEnabled())
            page._apply_manual_save_options(options)
            self.application.processEvents()
            self.assertTrue(page.save_manual_spectrum.isEnabled())
            self.assertFalse(path.exists())

            page.save_manual_spectrum.click()
            page._show_trace(second)
            page.save_manual_spectrum.click()

            self.assertEqual(Hdf5RunReader.summary(path).point_count, 2)
            self.assertEqual(page._manual_save_options, options)
            page.close_manual_archive_session()

    def test_anritsu_manual_timestamped_save_queues_optional_elab_upload(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        page = self.window.anritsu_page
        uploaded: list[Path] = []
        page.set_manual_elab_context(
            configuration_provider=lambda: (
                True,
                False,
                "Uses the configured research template.",
            ),
            upload_callback=uploaded.append,
        )
        trace = SpectrumTrace(
            frequencies_hz=(1e6, 2e6, 3e6),
            powers_dbm=(-60.0, -50.0, -55.0),
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name="TRAC1",
        )

        with tempfile.TemporaryDirectory() as temporary:
            options = ManualSpectrumSaveOptions(
                destination=Path(temporary) / "manual.h5",
                mode=ManualSpectrumSaveMode.TIMESTAMPED,
                metadata_scope="none",
                metadata_values=(),
                trace_variant="raw",
                upload_to_elab=True,
            )
            page._show_trace(trace)
            page._apply_manual_save_options(options)
            page.save_manual_spectrum.click()

            self.assertEqual(len(uploaded), 1)
            self.assertTrue(uploaded[0].is_file())
            self.assertEqual(Hdf5RunReader.summary(uploaded[0]).status, "completed")

    def test_anritsu_manual_archive_reconfiguration_and_failure_keep_state(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        page = self.window.anritsu_page
        trace = SpectrumTrace(
            frequencies_hz=(1e6, 2e6, 3e6),
            powers_dbm=(-60.0, -50.0, -55.0),
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name="TRAC1",
        )

        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first.h5"
            second_path = Path(temporary) / "second.h5"
            first_options = ManualSpectrumSaveOptions(
                destination=first_path,
                mode=ManualSpectrumSaveMode.APPEND,
                metadata_scope="none",
                metadata_values=(),
                trace_variant="raw",
            )
            second_options = ManualSpectrumSaveOptions(
                destination=second_path,
                mode=ManualSpectrumSaveMode.APPEND,
                metadata_scope="none",
                metadata_values=(),
                trace_variant="raw",
            )
            page._show_trace(trace)
            page._apply_manual_save_options(first_options)
            page.save_manual_spectrum.click()
            page._apply_manual_save_options(second_options)
            page.save_manual_spectrum.click()

            self.assertEqual(Hdf5RunReader.summary(first_path).point_count, 1)
            self.assertEqual(Hdf5RunReader.summary(second_path).point_count, 1)

            incompatible = SpectrumTrace(
                frequencies_hz=(1e6, 2.1e6, 3e6),
                powers_dbm=(-58.0, -48.0, -53.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            page._show_trace(incompatible)
            page.save_manual_spectrum.click()
            self.assertEqual(page._manual_save_options, second_options)
            self.assertIn("failed", page.manual_save_status.text().lower())
            page.close_manual_archive_session()

    def test_anritsu_workspace_stacks_without_clipping_at_minimum_window_size(self) -> None:
        self.window.resize(820, 560)
        self.window._navigate_to("anritsu")
        self.application.processEvents()

        page = self.window.anritsu_page
        host = self.window.navigation_routes["anritsu"]
        self.assertEqual(page.workspace_splitter.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertEqual(page.width(), host.scroll_area.viewport().width())

    def test_anritsu_spectrogram_controls_reflow_without_horizontal_overflow(self) -> None:
        self.window.resize(820, 560)
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        page = self.window.anritsu_page
        page.analysis_tabs.setCurrentIndex(1)
        self.application.processEvents()
        host = self.window.navigation_routes["anritsu"]

        self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertTrue(page.spectrogram_source.isVisibleTo(self.window))
        self.assertTrue(page.spectrogram_plot.isVisibleTo(self.window))
        viewport = host.scroll_area.viewport()
        for control in (
            page.spectrogram_source,
            page.spectrogram_window_span,
            page.spectrogram_reset_view,
            page.open_spectrogram_window,
        ):
            right = control.mapTo(
                viewport, control.rect().bottomRight()
            ).x()
            self.assertLessEqual(right, viewport.rect().right())

    def test_anritsu_peak_analysis_and_tracking_surfaces_fit_normal_and_narrow_windows(self) -> None:
        self.window.resize(820, 560)
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        page = self.window.anritsu_page
        page.analysis_tabs.setCurrentIndex(0)
        page._show_trace(synthetic_anritsu_peaks())
        self.assertTrue(wait_for_ui(lambda: len(page._detected_peaks) >= 2))
        host = self.window.navigation_routes["anritsu"]

        self.assertIsInstance(page.signal_analysis_card, CardWidget)
        self.assertIsInstance(page.cleanup_mode, ComboBox)
        self.assertIsInstance(page.open_peak_table, PrimaryPushButton)
        self.assertTrue(page.signal_analysis_card.isVisibleTo(self.window))
        self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)

        page._open_peak_table()
        self.application.processEvents()
        assert page._peak_table_dialog is not None
        table = page._peak_table_dialog
        table.resize(720, 380)
        self.application.processEvents()
        self.assertTrue(table.isVisible())
        self.assertGreaterEqual(table.table.rowCount(), 2)
        self.assertLessEqual(
            table.track_selected.mapTo(
                table, table.track_selected.rect().bottomRight()
            ).x(),
            table.rect().right(),
        )

        page._start_peak_tracking(0)
        self.application.processEvents()
        assert page._peak_tracking_window is not None
        tracking = page._peak_tracking_window
        tracking.resize(500, 360)
        self.application.processEvents()
        self.assertTrue(tracking.plot.isVisibleTo(tracking))
        self.assertGreater(tracking.plot.height(), 100)
        self.assertLessEqual(
            tracking.clear_history.mapTo(
                tracking, tracking.clear_history.rect().bottomRight()
            ).x(),
            tracking.rect().right(),
        )

    def test_anritsu_light_theme_uses_tokenized_surfaces_and_readable_connection_text(self) -> None:
        self.window._navigate_to("anritsu")
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()

        page = self.window.anritsu_page
        tokens = tokens_for("light")
        self.assertEqual(page.hero_card.property("stationSurface"), "card")
        self.assertEqual(page.setup_card.property("stationSurface"), "card")
        self.assertEqual(page.processing_card.property("stationSurface"), "card")
        self.assertEqual(page.spectrum_plot.property("stationSurface"), "raised")
        self.assertIn("border: 1px solid palette(mid)", page.setup_card.styleSheet())
        self.assertEqual(
            page.setup_card.palette().color(QPalette.ColorRole.Mid).name(),
            tokens.border,
        )
        connection = self.window.connection_panels["anritsu"]
        self.assertEqual(connection.property("stationSurface"), "surface")
        self.assertEqual(
            connection.palette().color(QPalette.ColorRole.WindowText).name(),
            tokens.text_primary,
        )

    def test_moke_workspace_and_live_dialog_use_fluent_cards(self) -> None:
        self.window._navigate_to("moke_box")
        self.window.moke_box_page.views.setCurrentIndex(1)
        self.application.processEvents()

        page = self.window.moke_box_page
        self.assertIsInstance(page.hero_card, CardWidget)
        self.assertIsInstance(page.vout_card, CardWidget)
        self.assertIsInstance(page.hall_card, CardWidget)
        self.assertIsInstance(page.read_vouts_button, PrimaryPushButton)
        self.assertIsInstance(page.open_live_window_button, PushButton)
        self.assertIsInstance(page.sample_interval, ComboBox)
        self.assertIsInstance(page.refresh_interval, ComboBox)
        self.assertIsInstance(page.history_window, ComboBox)
        self.assertEqual(page._selected_value(page.sample_interval), 1_000)
        self.assertEqual(page._selected_value(page.refresh_interval), 500)
        self.assertEqual(page._selected_value(page.history_window), 60)
        self.assertTrue(page.history_plot.isVisibleTo(self.window))
        now = datetime.now(timezone.utc)
        for timestamp, voltage in ((now - timedelta(seconds=61), 0.01), (now, 0.02)):
            page._show_hall_reading(
                MokeHallVoltageReading(voltage, 0.0, 1, (0x800000,), timestamp)
            )
        self.assertEqual(len(page._history), 1)
        self.assertEqual(len(page._voltage_curve.xData), 1)
        x_range = page.history_plot.viewRange()[0]
        self.assertAlmostEqual(x_range[0], -60.0, places=3)
        self.assertAlmostEqual(x_range[1], 0.0, places=3)
        self.assertTrue(page.hero_card.isVisibleTo(self.window))
        page._open_hall_live_window()
        self.application.processEvents()
        assert page._hall_live_window is not None
        self.assertIsInstance(page._hall_live_window.readout_card, CardWidget)

    def test_anritsu_and_moke_cards_retheme_while_their_routes_are_visible(self) -> None:
        self.window._navigate_to("anritsu")
        self.application.processEvents()
        anritsu = self.window.anritsu_page
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()
        anritsu_light = anritsu.grab().toImage().pixelColor(QPoint(2, 2)).name()
        self.window._set_theme_mode("dark", persist=False)
        self.application.processEvents()
        anritsu_dark = anritsu.grab().toImage().pixelColor(QPoint(2, 2)).name()
        self.assertNotEqual(anritsu_light, anritsu_dark)

        self.window._navigate_to("moke_box")
        self.application.processEvents()
        moke = self.window.moke_box_page
        self.window._set_theme_mode("light", persist=False)
        self.application.processEvents()
        moke_light = moke.grab().toImage().pixelColor(QPoint(2, 2)).name()
        self.window._set_theme_mode("dark", persist=False)
        self.application.processEvents()
        moke_dark = moke.grab().toImage().pixelColor(QPoint(2, 2)).name()
        self.assertNotEqual(moke_light, moke_dark)

    def test_moke_protocol_trace_dialog_uses_a_fluent_surface_without_local_palette(self) -> None:
        dialogs = []

        def capture(dialog):
            dialogs.append(dialog)
            return 0

        with patch("app.ui.dashboard.page.StationDialog.exec", new=capture):
            self.window.dashboard._show_moke_test_trace(
                "131.246.221.33:10001", True, "Verified", b"\x18\x00\x00\x18", b"\x00"
            )

        self.assertEqual(len(dialogs), 1)
        trace = dialogs[0].findChild(PlainTextEdit, "mokeProtocolTrace")
        self.assertIsNotNone(trace)
        assert trace is not None
        self.assertNotIn("#101820", trace.styleSheet())
        self.assertNotIn("#e7f1f8", trace.styleSheet())
        self.assertIsNotNone(dialogs[0].findChild(CardWidget, "mokeProtocolTraceCard"))
        dialogs[0].close()
