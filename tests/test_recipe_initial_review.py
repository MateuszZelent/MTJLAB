"""Initial configuration has known targets even without an earlier recipe state."""
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationSnapshot,
    KeithleyNodeEditorDialog,
)
from app.recipes import parse_recipe_text
from app.ui.design_system import apply_application_theme
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / filename
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    application.setFont(QFont("Segoe UI", 10))
    return application


SOURCE = """schema_version: 1
name: Saved initial configuration
root:
  id: root
  type: sequence
  children:
    - {id: initial, type: configure_keithley, channel: A, mode: current, level: '0 mA', compliance: '670 mV', source_range: '100 mA', source_autorange: false, sense_mode: 2wire}
"""


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 900)])
def test_saved_initial_targets_are_shown_as_settings_not_unknown(app, tmp_path, theme, width):
    apply_application_theme(app, theme)
    path = tmp_path / "saved.yml"
    path.write_text(SOURCE, encoding="utf-8")
    # A new page reads the corresponding device tab without connecting hardware.
    page = RecipePage(simulation_settings())
    card = KeithleyConfigurationSnapshot(channel="A", source_range="1 A", source_level="3 mA")
    page.set_keithley_snapshot_provider(lambda *_: card)
    page.path.setText(str(path))
    page._apply_builder_source(path.read_text(encoding="utf-8"), "Load after restart")
    node = parse_recipe_text(page.editor.toPlainText()).root.children[0]

    def inspect(dialog):
        dialog.resize(width, 800)
        dialog.show()
        app.processEvents()
        table = dialog.comparison
        rows = {row.key: (i, row) for i, row in enumerate(table.rows)}
        for key, value in {"source_mode": "current", "source_level": "0 mA",
                           "compliance": "670 mV", "source_range": "100 mA"}.items():
            i, row = rows[key]
            assert row.current == getattr(card, key)
            assert row.planned == value
            assert table.item(i, 1).text() == str(getattr(card, key))
            assert ("Same" if row.current == value else "Changes") in table.item(i, 3).text()
        i, row = rows["nplc"]
        assert row.planned == card.nplc
        assert row.action == "Preserve"
        assert "unknown" not in dialog.change_summary.text().lower()
        assert "to set" in dialog.change_summary.text()
        assert table.counts["unknown"] == 0
        for widget in (dialog.comparison_note, table, dialog.apply_button):
            rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
            assert dialog.rect().contains(rect)
            assert widget.isVisibleTo(dialog)
        target = Path("artifacts/sweep-initial-configuration")
        target.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(target / f"initial-{theme}-{width}.png"))
        dialog.close()
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_legacy_keithley_configuration(node)
        edited = parse_recipe_text(page.editor.toPlainText()).root.children[0]
        assert edited.data == node.data
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
