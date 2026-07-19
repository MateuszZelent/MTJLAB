### Task 2: Implement semantic theme tokens and station QSS

**Files:**
- Create: `app/ui/design_system/tokens.py`
- Create: `app/ui/design_system/station_qss.py`
- Modify: `app/ui/design_system/__init__.py`
- Create: `tests/test_design_system.py`

**Interfaces:**
- Consumes: theme names `"light"` and `"dark"`.
- Produces:
  - `ThemeTokens` frozen dataclass.
  - `tokens_for(theme: str) -> ThemeTokens`.
  - `station_qss(tokens: ThemeTokens) -> str`.
  - `SPACING`, `RADII`, `TYPOGRAPHY`, and `MOTION` immutable mappings.

- [ ] **Step 1: Write failing token and QSS tests**

Create `tests/test_design_system.py`:

```python
from __future__ import annotations

import unittest

from app.ui.design_system import SPACING, ThemeTokens, station_qss, tokens_for


class DesignSystemTests(unittest.TestCase):
    def test_light_and_dark_tokens_expose_all_station_semantics(self) -> None:
        for mode in ("light", "dark"):
            tokens = tokens_for(mode)
            self.assertIsInstance(tokens, ThemeTokens)
            for name in (
                "background", "surface", "surface_raised", "border",
                "text_primary", "text_muted", "accent", "focus",
                "success", "caution", "danger", "neutral",
                "output_active", "compliance", "interlock", "emergency",
                "plot_background", "plot_axes", "plot_grid",
                "plot_reference", "plot_measurement",
            ):
                self.assertRegex(getattr(tokens, name), r"^#[0-9a-fA-F]{6}$")

    def test_spacing_scale_is_monotonic_and_shared(self) -> None:
        self.assertEqual(tuple(SPACING), ("xs", "sm", "md", "lg", "xl"))
        self.assertEqual(sorted(SPACING.values()), list(SPACING.values()))

    def test_station_qss_contains_only_semantic_property_selectors(self) -> None:
        qss = station_qss(tokens_for("dark"))
        for selector in (
            '[safetyState="danger"]',
            '[outputState="active"]',
            '[validationState="error"]',
            '[deviceState="verified"]',
        ):
            self.assertIn(selector, qss)
        self.assertNotIn("QPushButton {", qss)
        self.assertNotIn("QLineEdit {", qss)

    def test_invalid_theme_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "light or dark"):
            tokens_for("sepia")
```

- [ ] **Step 2: Run tests to verify imports fail**

Run:

```powershell
python -m pytest tests/test_design_system.py -q
```

Expected: collection fails because `ThemeTokens`, `tokens_for`, `SPACING`, and `station_qss` do not exist.

- [ ] **Step 3: Implement immutable tokens**

Create `app/ui/design_system/tokens.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


SPACING = MappingProxyType({"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24})
RADII = MappingProxyType({"sm": 4, "md": 8, "lg": 12})
TYPOGRAPHY = MappingProxyType({
    "caption": 9,
    "body": 10,
    "subtitle": 12,
    "title": 20,
})
MOTION = MappingProxyType({"fast_ms": 120, "normal_ms": 180})


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    background: str
    surface: str
    surface_raised: str
    border: str
    text_primary: str
    text_muted: str
    accent: str
    focus: str
    success: str
    caution: str
    danger: str
    neutral: str
    output_active: str
    compliance: str
    interlock: str
    emergency: str
    plot_background: str
    plot_axes: str
    plot_grid: str
    plot_reference: str
    plot_measurement: str


_LIGHT = ThemeTokens(
    background="#f5f7fa", surface="#ffffff", surface_raised="#f9fafb",
    border="#d6dce5", text_primary="#18202a", text_muted="#5f6b7a",
    accent="#0067c0", focus="#005fb8", success="#0f7b4f",
    caution="#8a5d00", danger="#b4233a", neutral="#667085",
    output_active="#b4233a", compliance="#9a6700", interlock="#9a6700",
    emergency="#a4262c", plot_background="#ffffff", plot_axes="#344054",
    plot_grid="#d0d5dd", plot_reference="#7a5af8",
    plot_measurement="#0067c0",
)

_DARK = ThemeTokens(
    background="#111418", surface="#1b1f24", surface_raised="#22272e",
    border="#343a43", text_primary="#f2f4f7", text_muted="#a6adbb",
    accent="#60a5fa", focus="#7dd3fc", success="#43c58a",
    caution="#f4c152", danger="#ff6b7d", neutral="#98a2b3",
    output_active="#ff6b7d", compliance="#f4c152", interlock="#f4c152",
    emergency="#e5484d", plot_background="#111418", plot_axes="#d0d5dd",
    plot_grid="#343a43", plot_reference="#a78bfa",
    plot_measurement="#60a5fa",
)


def tokens_for(theme: str) -> ThemeTokens:
    normalized = theme.strip().lower()
    if normalized == "light":
        return _LIGHT
    if normalized == "dark":
        return _DARK
    raise ValueError("Theme must be light or dark.")
```

- [ ] **Step 4: Implement semantic-only station QSS and facade exports**

Create `app/ui/design_system/station_qss.py`:

```python
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
```

Update `app/ui/design_system/__init__.py`:

```python
from .station_qss import station_qss
from .theme import effective_theme
from .tokens import MOTION, RADII, SPACING, TYPOGRAPHY, ThemeTokens, tokens_for

__all__ = [
    "MOTION", "RADII", "SPACING", "TYPOGRAPHY", "ThemeTokens",
    "effective_theme", "station_qss", "tokens_for",
]
```

- [ ] **Step 5: Run the design-system tests**

Run:

```powershell
python -m pytest tests/test_design_system.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit semantic tokens**

```powershell
git add app/ui/design_system tests/test_design_system.py
git commit -m "feat: add semantic station design tokens"
```

