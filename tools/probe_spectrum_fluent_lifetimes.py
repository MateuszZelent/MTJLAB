"""Process-scoped Fluent lifecycle hypothesis; never a production UI patch."""

import argparse
from contextlib import ExitStack
import json
import weakref
from unittest.mock import patch

from qfluentwidgets import FluentStyleSheet, qconfig
from qfluentwidgets.common.style_sheet import (
    CustomStyleSheet, CustomStyleSheetWatcher, DirtyStyleSheetWatcher,
    StyleSheetCompose, StyleSheetFile, StyleSheetManager,
)
from qfluentwidgets.components.widgets.label import FluentLabelBase
from shiboken6 import isValid

from tools.diagnose_spectrum_qt_lifetimes import diagnose_shell_lifetimes


def weak_style_register(self, source, widget, reset=True):
    """Installed register semantics, changing only destroyed callback capture."""
    if isinstance(source, str):
        source = StyleSheetFile(source)
    if widget not in self.widgets:
        reference = weakref.ref(widget)

        def deregister():
            owned = reference()
            if owned is not None:
                self.deregister(owned)

        widget.destroyed.connect(deregister)
        widget.installEventFilter(CustomStyleSheetWatcher(widget))
        widget.installEventFilter(DirtyStyleSheetWatcher(widget))
        self.widgets[widget] = StyleSheetCompose([source, CustomStyleSheet(widget)])
    if not reset:
        self.source(widget).add(source)
    else:
        self.widgets[widget] = StyleSheetCompose([source, CustomStyleSheet(widget)])


def weak_label_init(self):
    FluentStyleSheet.LABEL.apply(self)
    self.setFont(self.getFont())
    self.setTextColor()
    reference = weakref.ref(self)

    def refresh():
        owned = reference()
        if owned is not None and isValid(owned):
            owned.setTextColor(owned.lightColor, owned.darkColor)

    qconfig.themeChanged.connect(refresh)
    self.destroyed.connect(lambda: qconfig.themeChanged.disconnect(refresh))
    self.customContextMenuRequested.connect(self._onContextMenuRequested)
    return self


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--weak-labels", action="store_true")
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args()
    with ExitStack() as scope:
        scope.enter_context(patch.object(StyleSheetManager, "register", weak_style_register))
        if args.weak_labels:
            scope.enter_context(patch.object(FluentLabelBase, "_init", weak_label_init))
        overrides = ["StyleSheetManager.register: weak destroyed callback"]
        if args.weak_labels:
            overrides.append("FluentLabelBase._init: weak theme callback")
        report = diagnose_shell_lifetimes(args.output, cycles=args.cycles, points=101,
                                         frames=5, experimental_overrides=overrides)
    print(json.dumps({"experimental_only": True,
        "weak_labels": args.weak_labels,
        "invalid": [sum(row["census"]["qobject_invalid_counts"].values()) for row in report["cycles"]],
        "rss": [row["memory"]["rss_bytes"] for row in report["cycles"]]}))


if __name__ == "__main__":
    main()
