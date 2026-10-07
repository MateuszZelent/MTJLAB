"""Native render and scroll audit of the complete dialog class catalogue."""
import os
import sys
import json
import traceback
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QStackedWidget
from PySide6.QtTest import QTest
from shiboken6 import isValid

from app.ui.design_system import apply_application_theme
from tests.modal_catalogue import catalogue, inline_factories


def run(destination):
    app = QApplication.instance()
    app.setQuitOnLastWindowClosed(False)
    destination.mkdir(parents=True, exist_ok=True)
    factories, owners = catalogue(Path.cwd(), destination)
    factories.update(inline_factories(destination, owners))
    errors = []
    def exception_hook(*exc):
        errors.append(str(exc[1]))
        traceback.print_exception(*exc)
    sys.excepthook = exception_hook
    results = []
    for theme in ("light", "dark"):
        apply_application_theme(app, theme)
        for name, factory in factories.items():
            if os.environ.get("PYLAB_MODAL_ONLY") and os.environ["PYLAB_MODAL_ONLY"] not in name:
                continue
            print("OPEN", theme, name, flush=True)
            dialog = factory()
            try:
                dialog.move(30, 30)
                dialog.show()
                for width, height in ((1120, 800), (800, 700)):
                    dialog.resize(width, height)
                    QTest.qWait(45)
                    assert dialog.modal_shell.surface.width() > 0, name
                    # Inspect every route, including advanced settings hidden initially.
                    for stack in dialog.findChildren(QStackedWidget):
                        original = stack.currentIndex()
                        for index in range(stack.count()):
                            stack.setCurrentIndex(index)
                            app.processEvents()
                        stack.setCurrentIndex(original)
                    review = getattr(dialog, "review", None)
                    if review:
                        review.toggle.setChecked(True)
                    app.processEvents()
                    for area in dialog.findChildren(QAbstractScrollArea):
                        assert not area.viewport().internalWinId(), (name, "native child")
                        area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
                    QTest.qWait(45)
                    assert not dialog.grab().isNull(), name
                    QTest.qWait(45)
                    shot = dialog.screen().grabWindow(int(dialog.winId()))
                    assert shot.save(str(destination / f"{name}-{width}-{theme}.png")), name
                    surface = dialog.modal_shell.surface
                    assert dialog.rect().contains(surface.mapTo(dialog, surface.rect().bottomRight())), name
                    results.append(dict(dialog=name, theme=theme, size=[dialog.width(), dialog.height()]))
                assert not errors, (name, errors)
            finally:
                dialog.close()
                # Inline dialogs remain owned/reused by their page; recording
                # owns the CPU workspace and must follow the page's shutdown.
                if isValid(dialog) and not name.startswith("Inline"):
                    dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            print("PASS", theme, name, flush=True)
    for owner in owners:
        if isValid(owner):
            print("CLOSE OWNER", type(owner).__name__, flush=True)
            shutdown = getattr(owner, "shutdown", None)
            if callable(shutdown):
                for _attempt in range(100):
                    if shutdown():
                        break
                    QTest.qWait(20)
                else:
                    raise AssertionError(f"Worker did not shut down: {type(owner).__name__}")
            owner.close()
            owner.deleteLater()
    print("DRAIN OWNERS", flush=True)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print("OWNERS DELETED", flush=True)
    app.processEvents()
    (destination / "catalogue.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    app.quit()


if __name__ == "__main__":
    application = QApplication([])
    try:
        run(Path(sys.argv[1]))
    except BaseException:
        traceback.print_exc()
        raise
