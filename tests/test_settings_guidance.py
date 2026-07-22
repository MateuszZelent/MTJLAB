from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from app.ui.shell import MainWindow
from app.ui.dialogs import StationMessageBox
from app.ui.settings_guidance import recipe_dut_issue_for_error, settings_issue_for_error


def test_anritsu_acquisition_lock_points_to_each_required_setting() -> None:
    issue = settings_issue_for_error(
        "Anritsu acquisition is locked by the safety profile. Define the RF input "
        "and frequency limits in Settings > Anritsu, then enable acquisition."
    )

    assert issue is not None
    assert issue.paths == (
        ("devices", "anritsu", "safety", "acquisition_allowed"),
        (
            "devices",
            "anritsu",
            "safety",
            "rf_input",
            "max_expected_power_at_connector",
        ),
        ("devices", "anritsu", "safety", "frequency", "min"),
        ("devices", "anritsu", "safety", "frequency", "max"),
    )


def test_runtime_or_unknown_errors_are_not_offered_a_settings_fix() -> None:
    assert settings_issue_for_error("VISA transport lost while acquiring") is None


def test_missing_keithley_dut_limits_route_to_recipe_channel() -> None:
    issue = recipe_dut_issue_for_error(
        "OUTPUT for keithley channel B requires complete recipe.dut_limits for "
        "current, voltage/power or impedance/current/power."
    )

    assert issue is not None
    assert issue.device == "keithley"
    assert issue.channel == "B"


def test_keithley_station_limit_identifies_exact_channel_and_range() -> None:
    issue = settings_issue_for_error(
        "node-1: Keithley B current level 0.02 SI is outside the station range [-0.01, 0.01]."
    )

    assert issue is not None
    assert issue.paths == (
        (
            "devices",
            "keithley",
            "safety",
            "channels",
            "B",
            "lab_limits",
            "source_current",
        ),
    )


def test_shared_message_box_routes_limit_failure_to_settings() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.resize(1440, 900)
        window.show()
        application.processEvents()
        with patch(
            "app.ui.dialogs.StationSettingsGuidanceDialog"
        ) as guidance_dialog:
            guidance_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            StationMessageBox.warning(
                window.keithley_page,
                "Keithley",
                "node-1: Keithley B current level 0.02 SI is outside the station "
                "range [-0.01, 0.01].",
            )
        application.processEvents()

        path = (
            "devices",
            "keithley",
            "safety",
            "channels",
            "B",
            "lab_limits",
            "source_current",
            "min",
        )
        assert window._current_route() == "settings"
        assert window.settings_page._form_editors[path].property("validationState") == "error"
    finally:
        window.close()
        application.processEvents()


def test_settings_link_opens_anritsu_and_renders_highlighted_fields() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.resize(1440, 900)
        window.show()
        application.processEvents()
        issue = settings_issue_for_error(
            "Anritsu acquisition is locked by the safety profile."
        )
        assert issue is not None

        window._open_settings_issue(issue)
        application.processEvents()

        page = window.settings_page
        field = page._form_editors[issue.paths[0]]
        assert window._current_route() == "settings"
        assert page.tabs.currentWidget() is page.forms["anritsu"]
        assert field.property("validationState") == "error"
        assert field.isVisible()
        assert field.geometry().width() > 0 and field.geometry().height() > 0
    finally:
        window.close()
        application.processEvents()
