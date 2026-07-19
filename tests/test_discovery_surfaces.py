from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication
from qfluentwidgets import BodyLabel

from app.ui.dashboard.discovery_surfaces import SavedInstrumentsView, TcpDiscoveryResultsView
from app.ui.shell import MainWindow


class DiscoverySurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_tcp_rows_are_card_based_selectable_and_update_in_place(self) -> None:
        view = TcpDiscoveryResultsView()
        view.resize(940, 520)
        view.upsert_endpoint(
            host="192.168.1.42",
            endpoint="192.168.1.42:10001",
            state="open",
            verification="MOKE Box verified",
        )
        view.upsert_endpoint(
            host="192.168.1.42",
            endpoint="192.168.1.42:10001",
            state="open",
            verification="MOKE Box verified",
        )
        view.show()
        self.application.processEvents()

        self.assertEqual(view.row_count, 1)
        row = view.row_for_host("192.168.1.42")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertGreater(row.height(), 0)
        self.assertEqual(row.property("stationSurface"), "card")
        row.click()
        self.application.processEvents()
        self.assertEqual(view.selected_host, "192.168.1.42")
        self.assertEqual(view.selected_endpoint, "192.168.1.42:10001")
        self.assertEqual(view.selected_state, "open")
        self.assertEqual(view.selected_verification, "MOKE Box verified")
        view.close()

    def test_saved_instruments_are_semantic_cards_without_horizontal_overflow(self) -> None:
        view = SavedInstrumentsView()
        view.resize(420, 520)
        view.set_instruments((
            ("Rigol DG1032Z", "TCPIP0::192.168.123.123::hislip0::INSTR", "system", "Disconnected"),
            ("MOKE Box", "192.168.1.33:10001", "TCP/IP", "Verified"),
        ))
        view.show()
        self.application.processEvents()

        self.assertEqual(view.count, 2)
        self.assertTrue(all(card.property("stationSurface") == "card" for card in view.cards))
        self.assertEqual(view.scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertTrue(all(label.wordWrap() for card in view.cards for label in card.findChildren(BodyLabel)))
        view.close()

    def test_dashboard_hosts_tcp_and_saved_routes_as_visible_fluent_card_views(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 720)
            window.show()
            window._navigate_to("discovery")
            window.dashboard._show_discovery_page("tcp")
            self.application.processEvents()
            self.assertIsInstance(window.dashboard.tcp_results, TcpDiscoveryResultsView)
            self.assertTrue(window.dashboard.tcp_results.isVisibleTo(window))
            self.assertGreater(window.dashboard.tcp_results.width(), 500)
            window.dashboard._show_discovery_page("saved")
            self.application.processEvents()
            self.assertIsInstance(window.dashboard.saved_instruments, SavedInstrumentsView)
            self.assertTrue(window.dashboard.saved_instruments.isVisibleTo(window))
            self.assertEqual(window.dashboard.saved_instruments.count, 5)
        finally:
            window.close()
            self.application.processEvents()
