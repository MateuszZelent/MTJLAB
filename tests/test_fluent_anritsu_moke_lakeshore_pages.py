from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
from qfluentwidgets import CardWidget, CheckBox, PlainTextEdit, PrimaryPushButton, PushButton

from app.ui.shell import MainWindow
from app.ui.design_system import tokens_for
from app.settings import SettingsRepository
from app.devices.discovery import DiscoveredInstrument, identify_device
from app.devices.lakeshore_475.models import FieldUnit, GaussmeterReading, GaussmeterSnapshot, MeasurementMode
from tests.test_main_window import TEST_ENGINEER, write_engineer_settings


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
            self.window.safety_strip.profile,
            self.window.safety_strip.actor,
        )
        for label in required:
            with self.subTest(label=label.text()):
                self.assertIsInstance(label, QLabel)
                foreground = label.palette().color(QPalette.ColorRole.WindowText)
                self.assertLess(foreground.lightness(), 180)
        tokens = tokens_for("light")
        for button in (panel.connect_button, panel.disconnect_button, page.read_now):
            with self.subTest(button=button.text()):
                disabled_qss = button.styleSheet().split(
                    "/* station-disabled-button */", 1
                )[1]
                self.assertIn(f"color: {tokens.text_muted}", disabled_qss)
                self.assertIn(
                    f"background-color: {tokens.surface_raised}", disabled_qss
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
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
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
        self.assertIn(
            f"border: 1px solid {tokens.border}", page.setup_card.styleSheet()
        )
        connection = self.window.connection_panels["anritsu"]
        self.assertEqual(connection.property("stationSurface"), "surface")
        self.assertEqual(
            connection.palette().color(QPalette.ColorRole.WindowText).name(),
            tokens.text_primary,
        )

    def test_moke_workspace_and_live_dialog_use_fluent_cards(self) -> None:
        self.window._navigate_to("moke_box")
        self.application.processEvents()

        page = self.window.moke_box_page
        self.assertIsInstance(page.hero_card, CardWidget)
        self.assertIsInstance(page.vout_card, CardWidget)
        self.assertIsInstance(page.hall_card, CardWidget)
        self.assertIsInstance(page.read_vouts_button, PrimaryPushButton)
        self.assertIsInstance(page.open_live_window_button, PushButton)
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

        with patch("app.ui.dashboard.page.QDialog.exec", new=capture):
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
