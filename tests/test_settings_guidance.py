from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.shell import MainWindow
from app.ui.settings_guidance import settings_issue_for_error


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
