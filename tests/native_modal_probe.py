"""Isolated Windows renderer probe; never connects to laboratory instruments."""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractScrollArea

from app.devices.anritsu_ms2830a.ui.recipe_dialog import (
    AnritsuNodeEditorDialog, AnritsuSignalGeneratorNodeEditorDialog,
)
from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationSnapshot, KeithleyNodeEditorDialog,
)
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
from app.recipes import RecipeNode
from app.ui.design_system import apply_application_theme
from app.ui.design_system.fluent_theme import configure_widget_style
from app.ui.recipes.common_dialogs import (
    AnritsuAcquisitionEditorDialog, CommentEditorDialog, OutputPolicyDialog,
    RepeatCountDialog,
)
from tests.helpers import simulation_settings


def run(destination):
    app = QApplication([])
    configure_widget_style(app)
    settings = simulation_settings()
    factories = {
        "anritsu": lambda: AnritsuNodeEditorDialog(settings),
        "anritsu-sg": AnritsuSignalGeneratorNodeEditorDialog,
        "rigol": lambda: RigolNodeEditorDialog(settings=settings),
        "acquisition": lambda: AnritsuAcquisitionEditorDialog(
            RecipeNode(id="acquire", type="acquire_spectrum", data={})),
        "reference": lambda: AnritsuAcquisitionEditorDialog(
            RecipeNode(id="reference", type="acquire_reference", data={})),
        "output": lambda: OutputPolicyDialog("unchanged"),
        "repeat": RepeatCountDialog,
        "comment": CommentEditorDialog,
    }
    for channel in ("A", "B"):
        for full in (False, True):
            def factory(channel=channel, full=full):
                current = KeithleyConfigurationSnapshot(
                    channel=channel, source_level="0.1 mA", source_range="10 mA",
                )
                dialog = KeithleyNodeEditorDialog(
                    settings, snapshot=current, snapshot_resolver=lambda *args: current,
                    full_configuration=full,
                )
                if not full:
                    dialog.load_plan_actions([
                        {"parameter_id": "source.level", "mode": "set"},
                    ], "unchanged")
                dialog.level.setText("2 mA")
                assert dialog.comparison.counts["changed"] == 1
                return dialog
            factories[f"keithley-{channel}-{full}"] = factory
    destination.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        apply_application_theme(app, theme)
        for name, factory in factories.items():
            dialog = factory()
            try:
                dialog.move(40, 40)
                dialog.show()
                for width, height in ((1120, 780), (760, 640)):
                    dialog.resize(width, height)
                    QTest.qWait(80)
                    for area in dialog.findChildren(QAbstractScrollArea):
                        # Before the fix these were native HWNDs, and scrolling
                        # copied the full modal into its own client area.
                        assert not area.viewport().internalWinId(), (name, "native viewport")
                        bar = area.verticalScrollBar()
                        bar.setValue(bar.maximum())
                    QTest.qWait(80)
                    surface = dialog.modal_shell.surface
                    assert surface.width() > 0 and surface.height() > 0
                    assert dialog.rect().contains(surface.mapTo(dialog, surface.rect().bottomRight()))
                    if hasattr(dialog, "change_summary"):
                        label = dialog.change_summary
                        assert label.isVisible()
                        assert dialog.rect().contains(label.mapTo(dialog, QPoint(0, 0)))
                        assert label.text() == dialog.comparison.summary_text
                    # Exercise both Qt backing-store grabs and the Windows HWND.
                    assert not dialog.grab().isNull()
                    QTest.qWait(80)
                    shot = dialog.screen().grabWindow(int(dialog.winId()))
                    assert not shot.isNull()
                    assert shot.save(str(destination / f"native-{name}-{width}-{theme}.png"))
                print(f"PASS {theme} {name}", flush=True)
            finally:
                dialog.close()
                dialog.deleteLater()
                app.processEvents()


if __name__ == "__main__":
    run(Path(sys.argv[1]))
