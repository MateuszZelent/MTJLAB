from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from app.devices.discovery import DiscoveredInstrument
from app.ui.dashboard import VisaResultState, VisaResultsView


class VisaResultsViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_states_distinguish_recognized_unknown_unavailable_and_assigned(self) -> None:
        recognized = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        )
        unknown = VisaResultState.from_result(
            DiscoveredInstrument("TCPIP0::1::INSTR", "system", "VENDOR,MODEL,1,1", None),
            configured_device=None,
        )
        unavailable = VisaResultState.from_result(
            DiscoveredInstrument("ASRL1::INSTR", "system", None, None, "timeout"),
            configured_device=None,
        )
        assigned = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device="keithley",
        )
        self.assertEqual(
            (recognized.status, unknown.status, unavailable.status, assigned.status),
            ("recognized", "unknown", "unavailable", "assigned"),
        )

    def test_assign_emits_existing_payload_shape(self) -> None:
        view = VisaResultsView()
        state = VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        )
        emitted: list[object] = []
        view.assignment_requested.connect(emitted.append)
        view.set_results((state,))
        row = view.rows[0]
        row.assignment.setCurrentIndex(row.assignment.findData("keithley"))
        row.assign_button.click()
        self.assertEqual(emitted, [{
            "keithley": ("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1")
        }])

    def test_recognized_lakeshore_remains_assignable_if_combo_data_is_transiently_empty(self) -> None:
        view = VisaResultsView()
        state = VisaResultState.from_result(
            DiscoveredInstrument(
                "GPIB0::12::INSTR",
                "system",
                "LSCI,MODEL475,LSA1823,11272013",
                "lakeshore_gaussmeter",
            ),
            configured_device=None,
        )
        emitted: list[object] = []
        view.assignment_requested.connect(emitted.append)
        view.set_results((state,))
        row = view.rows[0]
        row.assignment.setCurrentIndex(0)

        self.assertTrue(row.assign_button.isEnabled())
        row.assign_button.click()
        self.assertEqual(emitted, [{
            "lakeshore_gaussmeter": (
                "GPIB0::12::INSTR",
                "system",
                "LSCI,MODEL475,LSA1823,11272013",
            )
        }])

    def test_unavailable_and_permission_denied_rows_cannot_assign(self) -> None:
        view = VisaResultsView()
        unavailable = VisaResultState.from_result(
            DiscoveredInstrument("ASRL1::INSTR", "system", None, None, "timeout"),
            configured_device=None,
        )
        view.set_results((unavailable,))
        self.assertFalse(view.rows[0].assignment.isEnabled())
        self.assertFalse(view.rows[0].assign_button.isEnabled())
        view.set_assignment_allowed(False)
        self.assertFalse(view.rows[0].assign_button.isEnabled())

    def test_non_empty_view_has_visible_row_geometry_after_show(self) -> None:
        view = VisaResultsView()
        view.resize(960, 520)
        view.set_results((VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        ),))
        view.show()
        self.application.processEvents()
        self.assertGreater(view.rows[0].geometry().width(), 0)
        self.assertGreater(view.rows[0].geometry().height(), 0)
        view.close()

    def test_compact_view_does_not_create_a_horizontal_scrollbar(self) -> None:
        view = VisaResultsView()
        view.resize(360, 520)
        view.set_results((VisaResultState.from_result(
            DiscoveredInstrument("GPIB0::22::INSTR", "system", "KEITHLEY,2602A,1,1", "keithley"),
            configured_device=None,
        ),))
        view.show()
        self.application.processEvents()
        self.assertEqual(view.scroll_area.horizontalScrollBar().maximum(), 0)
        view.close()

    def test_compact_empty_view_does_not_create_a_horizontal_scrollbar(self) -> None:
        view = VisaResultsView()
        view.resize(360, 520)
        view.show()
        self.application.processEvents()
        self.assertEqual(view.scroll_area.horizontalScrollBar().maximum(), 0)
        view.close()

    def test_compact_view_wraps_long_visa_resource_and_identity_without_scrolling(self) -> None:
        view = VisaResultsView()
        view.resize(360, 520)
        view.set_results((VisaResultState.from_result(
            DiscoveredInstrument(
                "TCPIP0::192.168.123.123::hislip0::INSTR",
                "system",
                "Rohde&Schwarz,SMA100B,1419.8888K02/110186,5.00.122.24 SP1",
                None,
            ),
            configured_device=None,
        ),))
        view.show()
        self.application.processEvents()
        self.assertEqual(view.scroll_area.horizontalScrollBar().maximum(), 0)
        view.close()
