from __future__ import annotations

from .tokens import ThemeTokens


def station_qss(tokens: ThemeTokens) -> str:
    return f"""
QLabel[deviceState="verified"], QLabel[outputState="off"] {{
    color: {tokens.success};
}}
QLabel[deviceState="fault"], QLabel[safetyState="danger"],
QLabel[outputState="active"] {{
    color: {tokens.danger};
}}
QLabel[safetyState="caution"], QLabel[deviceState="compliance"] {{
    color: {tokens.caution};
}}
QLineEdit[validationState="error"], QComboBox[validationState="error"],
QSpinBox[validationState="error"] {{
    border: 2px solid {tokens.danger};
}}
"""
