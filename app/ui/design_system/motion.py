from __future__ import annotations

import os

from PySide6.QtCore import QSettings


def motion_enabled() -> bool:
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return False
    return not bool(QSettings().value("accessibility/reduceMotion", False, bool))
