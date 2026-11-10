"""Native, read-only walkthrough of a loaded sweep's actual application editors.

No dialog exec is replaced. Timed Qt input opens the normal editors, captures
their rendered windows and dismisses them with Cancel. Hardware requests and
run starts are blocked at the controller boundary for this inspection process.
"""
from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", default="recipes/example_nested_keithley_ab_fixed_moke2.yml")
    parser.add_argument("--settings", default=".config/settings.yml")
    parser.add_argument("--destination", default="artifacts/native-sweep-inspection")
    parser.add_argument("--leave-open", action="store_true")
    parser.add_argument("--overview-only", action="store_true")
    parser.add_argument("--from-node")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    faulthandler.dump_traceback_later(45, repeat=True)
    os.environ["QT_QPA_PLATFORM"] = "windows"

    from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QAbstractScrollArea,
        QApplication,
        QDialog,
        QLineEdit,
        QStackedWidget,
        QTableWidget,
    )

    from app.recipes import parse_recipe_text
    from app.settings import SettingsRepository
    from app.ui.design_system.fluent_theme import configure_widget_style
    from app.ui.run_worker import RunController
    from app.ui.workers import DeviceController

    destination = Path(args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    original_recipe = Path(args.recipe).resolve()
    original_settings = Path(args.settings).resolve()
    recipe_bytes = original_recipe.read_bytes()
    settings_bytes = original_settings.read_bytes()
    report = {
        "recipe": str(original_recipe), "recipe_sha256": hashlib.sha256(recipe_bytes).hexdigest(),
        "platform": "windows", "simulation": False,
        "method": "Actual MainWindow, native dialogs, Qt input, original production profile values",
        "windows": [], "issues": [], "hardware_requests": [], "run_starts": 0,
        "status": "starting",
        "process_id": os.getpid(),
    }

    def save_report():
        (destination / "inspection.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def block_io(_self, operation, payload=None):
        report["hardware_requests"].append(str(operation))
        save_report()
        raise RuntimeError("Instrument access is disabled in the UI inspection process")

    def block_run(*_args, **_kwargs):
        report["run_starts"] += 1
        save_report()
        raise RuntimeError("Sweep execution is disabled in the UI inspection process")

    DeviceController.call = block_io
    RunController.start = block_run

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    configure_widget_style(app)
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI", 10))

    # Only persistence destinations are isolated. Device settings, limits,
    # assignments and the actual recipe's numbers are left as authored.
    from ruamel.yaml import YAML
    raw = YAML(typ="safe").load(settings_bytes.decode("utf-8"))
    raw["storage"].update(output_directory=str(destination / "measurements"),
                          catalogue_directory=str(destination / "catalogue"))
    raw["application"].update(audit_log_directory=str(destination / "logs"), restore_last_recipe=False)
    profile = destination / "settings.yml"
    SettingsRepository(profile).save_raw(raw)
    import app.ui.shell.main_window as shell_module
    shell_module.QSettings = lambda *_args: QSettings(str(destination / "ui-state.ini"), QSettings.Format.IniFormat)
    window = shell_module.MainWindow(profile, simulation=False)
    window.setWindowTitle("MTJLAB — Sweep UI inspection (execution disabled)")
    window._set_theme_mode(args.theme, persist=False)

    def fit(widget, width, height):
        available = widget.screen().availableGeometry()
        widget.resize(min(width, available.width() - 40), min(height, available.height() - 60))
        widget.move(available.x() + 15, available.y() + 15)

    fit(window, 1360, 900)
    window.show()
    window._navigate_to("sweeps")
    page = window.recipe_page
    page.path.setText(str(original_recipe))
    page.load_editor()
    page._autosave_timer.stop()
    page.run_button.setEnabled(False)
    document = parse_recipe_text(recipe_bytes.decode("utf-8"))
    nodes = []

    def visit(node, in_finally=False):
        if node.type != "sequence":
            nodes.append((node, in_finally))
        for child in (*node.children, *node.else_children):
            visit(child, in_finally)

    visit(document.root)
    for node in document.finally_nodes:
        visit(node, True)
    report["node_count"] = len(nodes)
    from types import SimpleNamespace
    if args.from_node:
        first = next(index for index, (node, _final) in enumerate(nodes) if node.id == args.from_node)
        nodes = nodes[first:]
    if args.overview_only:
        nodes = []
    nodes.append((SimpleNamespace(id="all-device-settings", type="overview", data={}), False))
    screen = window.screen()
    report["screen"] = {"available": screen.availableGeometry().getRect(),
                        "geometry": screen.geometry().getRect(), "dpr": screen.devicePixelRatio()}
    report["theme"] = args.theme
    save_report()

    class Walkthrough(QObject):
        def __init__(self):
            super().__init__(app)
            self.index = 0
            self.current = None
            self.dialog = None
            self.seen = set()
            self.timeout = QTimer(self)
            self.timeout.setSingleShot(True)
            self.timeout.timeout.connect(self.missing_dialog)

        def eventFilter(self, obj, event):
            if (event.type() == QEvent.Type.Show and isinstance(obj, QDialog)
                    and self.current is not None and self.dialog is None):
                self.dialog = obj
                self.timeout.stop()
                QTimer.singleShot(350, self.inspect)
            return False

        def advance(self):
            try:
                if self.index >= len(nodes):
                    self.finish()
                    return
                node, in_finally = nodes[self.index]
                self.current = (node, in_finally)
                self.dialog = None
                if node.type != "overview":
                    page._select_source_node(node.id)
                page.measurement_tree.setFocus()
                window.raise_()
                window.activateWindow()
                print("OPEN", self.index + 1, node.id, node.type, flush=True)
                self.timeout.start(12000)
                # Use the actual editor button and its production signal path.
                if node.type == "overview":
                    page.all_device_settings_action.trigger()
                else:
                    page.open_editor_button.click()
            except BaseException:  # noqa: BLE001 - persist failures from nested Qt event loops
                self.fail(traceback.format_exc())

        def missing_dialog(self):
            self.fail("Editor did not open for " + self.current[0].id)

        def capture(self, suffix):
            dialog = self.dialog
            node = self.current[0]
            stem = f"{self.index + 1:02d}-{node.id}-{suffix}"
            QTest.qWait(180)
            assert dialog.grab().save(str(destination / f"{stem}.png"))
            native = dialog.screen().grabWindow(int(dialog.winId()))
            native.save(str(destination / f"{stem}-native.png"))

        def inspect(self):
            try:
                dialog = self.dialog
                node, in_finally = self.current
                dialog.raise_()
                dialog.activateWindow()
                fit(dialog, 1120, 800)
                app.processEvents()
                assert dialog.isVisible(), node.id
                assert dialog.width() > 400 and dialog.height() > 250, node.id
                review = getattr(dialog, "review", None)
                if review is not None:
                    review.toggle.setChecked(True)
                for stack in dialog.findChildren(QStackedWidget):
                    original = stack.currentIndex()
                    for index in range(stack.count()):
                        stack.setCurrentIndex(index)
                        app.processEvents()
                    stack.setCurrentIndex(original)
                self.capture("desktop")
                tables = []
                for table in dialog.findChildren(QTableWidget):
                    tables.append({"name": table.objectName(), "rows": [
                        [table.item(row, col).text() if table.item(row, col) else ""
                         for col in range(table.columnCount())] for row in range(table.rowCount())]})
                fields = [{"type": type(widget).__name__, "name": widget.objectName(),
                           "text": widget.text(), "visible": widget.isVisibleTo(dialog),
                           "enabled": widget.isEnabled()} for widget in dialog.findChildren(QLineEdit)]
                record = {"node": node.id, "type": node.type, "finally": in_finally,
                          "dialog": type(dialog).__name__, "title": dialog.windowTitle(),
                          "authored": node.data, "tables": tables, "fields": fields,
                          "frame": dialog.frameGeometry().getRect(),
                          "inside_screen": dialog.screen().availableGeometry().contains(dialog.frameGeometry())}
                if node.type == "configure_keithley":
                    snapshot = dialog.configuration_snapshot()
                    assert snapshot.channel == node.data["channel"]
                    assert snapshot.source_range == node.data["source_range"]
                    assert snapshot.source_level == node.data["level"]
                    assert snapshot.compliance == node.data["compliance"]
                    assert "unknown" not in dialog.change_summary.text().lower()
                    record["summary"] = dialog.change_summary.text()
                    card = page._device_tab_comparison_values("keithley", snapshot.channel)
                    for row in dialog.comparison.rows:
                        if row.key in card:
                            assert row.current == card[row.key], (node.id, row.key, row.current, card[row.key])
                    record["device_tab"] = card
                if node.type == "overview":
                    assert not dialog.only_differences.isChecked()
                    assert len(dialog._rows) == len(dialog.entries) > 0
                    expected_sources = {entry.id for entry, _final in nodes if entry.type != "overview"}
                    actual_sources = {entry.source_id for entry in dialog.entries}
                    assert expected_sources <= actual_sources, expected_sources - actual_sources
                    assert all(not item.isHidden() and not item.parent().isHidden()
                               for item, _entry, _search in dialog._rows)
                    record["summary"] = dialog.summary_label.text()
                    record["entries"] = [
                        {"device": entry.device, "channel": entry.channel, "parameter": entry.parameter,
                         "source": entry.source_id, "card": entry.card_value, "sweep": entry.sweep_value,
                         "action": entry.action, "comparison": item.text(3)}
                        for item, entry, _search in dialog._rows
                    ]
                    dialog.search.setText("Keithley B")
                    assert any(not item.isHidden() for item, _entry, _search in dialog._rows)
                    dialog.search.clear()
                    dialog.only_differences.setChecked(True)
                    dialog.only_differences.setChecked(False)
                    assert all(not item.isHidden() for item, _entry, _search in dialog._rows)
                report["windows"].append(record)
                self.seen.add(type(dialog).__name__)
                report["status"] = f"inspected {len(report['windows'])}/{len(nodes)}"
                save_report()
                QTimer.singleShot(250, self.inspect_bottom)
            except BaseException:  # noqa: BLE001 - persist failures from nested Qt event loops
                self.fail(traceback.format_exc())

        def inspect_bottom(self):
            try:
                for area in self.dialog.findChildren(QAbstractScrollArea):
                    area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
                app.processEvents()
                self.capture("scrolled")
                fit(self.dialog, 900, 700)
                QTimer.singleShot(250, self.inspect_narrow)
            except BaseException:  # noqa: BLE001 - persist failures from nested Qt event loops
                self.fail(traceback.format_exc())

        def inspect_narrow(self):
            try:
                self.capture("narrow")
                self.dialog.reject()
                self.index += 1
                self.current = None
                self.dialog = None
                assert page.editor.toPlainText() == recipe_bytes.decode("utf-8").replace("\r\n", "\n")
                QTimer.singleShot(200, self.advance)
            except BaseException:  # noqa: BLE001 - persist failures from nested Qt event loops
                self.fail(traceback.format_exc())

        def finish(self):
            faulthandler.cancel_dump_traceback_later()
            self.timeout.stop()
            assert original_recipe.read_bytes() == recipe_bytes, "Recipe changed"
            assert original_settings.read_bytes() == settings_bytes, "Station settings changed"
            assert not report["hardware_requests"] and report["run_starts"] == 0
            fit(window, 1360, 900)
            window._navigate_to("sweeps")
            window.grab().save(str(destination / "sweeps-loaded.png"))
            report["dialog_types"] = sorted(self.seen)
            report["status"] = "complete"
            report["recipe_unchanged"] = True
            report["settings_unchanged"] = True
            save_report()
            print("COMPLETE", len(report["windows"]), "windows; no sweep or instrument commands", flush=True)
            app.removeEventFilter(self)
            if not args.leave_open:
                page._close_discard_confirmed = True
                window.close()
                app.quit()

        def fail(self, message):
            faulthandler.cancel_dump_traceback_later()
            self.timeout.stop()
            report["issues"].append(message)
            report["status"] = "failed"
            save_report()
            print("ISSUE", message, flush=True)
            if self.dialog is not None:
                self.dialog.reject()
            self.current = None
            self.dialog = None
            app.removeEventFilter(self)
            if not args.leave_open:
                page._close_discard_confirmed = True
                window.close()
                app.quit()

    walker = Walkthrough()
    app.installEventFilter(walker)

    def exception_hook(*exc):
        walker.fail("".join(traceback.format_exception(*exc)))

    sys.excepthook = exception_hook
    QTimer.singleShot(1000, walker.advance)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
