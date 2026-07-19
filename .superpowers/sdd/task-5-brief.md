### Task 5: Build the persistent station safety strip

**Files:**
- Create: `app/ui/shell/safety_strip.py`
- Modify: `app/ui/shell/__init__.py`
- Modify: `tests/test_fluent_shell.py`

**Interfaces:**
- Consumes:
  - `StationSafetySnapshot` values supplied by `MainWindow`.
- Produces:
  - `StationSafetySnapshot` frozen dataclass.
  - `StationSafetyStrip.estop_requested` signal.
  - `StationSafetyStrip.update_snapshot(snapshot: StationSafetySnapshot) -> None`.

- [ ] **Step 1: Write failing safety-strip state and E-STOP tests**

Add to `tests/test_fluent_shell.py`:

```python
from app.ui.shell import StationSafetySnapshot, StationSafetyStrip


class StationSafetyStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_snapshot_updates_text_and_semantic_properties(self) -> None:
        strip = StationSafetyStrip()
        strip.update_snapshot(StationSafetySnapshot(
            ready=False,
            active_outputs=2,
            profile_state="LOCKED",
            simulation=True,
            actor="operator",
            roles=("operator",),
        ))
        self.assertIn("2 outputs active", strip.outputs.text())
        self.assertEqual(strip.outputs.property("outputState"), "active")
        self.assertEqual(strip.readiness.property("safetyState"), "danger")
        self.assertIn("SIMULATION", strip.mode.text())

    def test_estop_button_emits_without_animation_or_delay(self) -> None:
        strip = StationSafetyStrip()
        emissions: list[bool] = []
        strip.estop_requested.connect(lambda: emissions.append(True))
        strip.estop.click()
        self.assertEqual(emissions, [True])
```

- [ ] **Step 2: Run tests to verify missing types**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::StationSafetyStripTests -q
```

Expected: collection fails because the strip and snapshot do not exist.

- [ ] **Step 3: Implement snapshot and presentation-only strip**

Create `app/ui/shell/safety_strip.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import PrimaryPushButton


@dataclass(frozen=True, slots=True)
class StationSafetySnapshot:
    ready: bool
    active_outputs: int
    profile_state: str
    simulation: bool
    actor: str
    roles: tuple[str, ...]


class StationSafetyStrip(QWidget):
    estop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationSafetyStrip")
        self.readiness = QLabel()
        self.outputs = QLabel()
        self.profile = QLabel()
        self.mode = QLabel()
        self.actor = QLabel()
        self.estop = PrimaryPushButton("E-STOP — disable all outputs")
        self.estop.setAccessibleName("Emergency stop and disable all outputs")
        self.estop.clicked.connect(self.estop_requested)
        layout = QHBoxLayout(self)
        for widget in (
            self.readiness, self.outputs, self.profile,
            self.mode, self.actor,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.estop)

    def update_snapshot(self, snapshot: StationSafetySnapshot) -> None:
        self.readiness.setText("Station ready" if snapshot.ready else "Station blocked")
        self.readiness.setProperty("safetyState", "ready" if snapshot.ready else "danger")
        self.outputs.setText(
            "Outputs off" if snapshot.active_outputs == 0
            else f"{snapshot.active_outputs} outputs active"
        )
        self.outputs.setProperty(
            "outputState", "off" if snapshot.active_outputs == 0 else "active"
        )
        self.profile.setText(f"Profile {snapshot.profile_state}")
        self.mode.setText("SIMULATION" if snapshot.simulation else "HARDWARE")
        roles = ", ".join(snapshot.roles) or "no role"
        self.actor.setText(f"{snapshot.actor} · {roles}")
        for widget in (self.readiness, self.outputs):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
```

Export both types from `app/ui/shell/__init__.py`.

- [ ] **Step 4: Run the safety-strip tests**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::StationSafetyStripTests -q
```

Expected: pass.

- [ ] **Step 5: Commit the safety strip**

```powershell
git add app/ui/shell/safety_strip.py app/ui/shell/__init__.py tests/test_fluent_shell.py
git commit -m "feat: add persistent station safety strip"
```

