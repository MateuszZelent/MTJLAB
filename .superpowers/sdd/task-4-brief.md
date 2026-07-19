### Task 4: Add a compatibility host for legacy pages

**Files:**
- Create: `app/ui/shell/page_host.py`
- Modify: `app/ui/shell/__init__.py`
- Modify: `tests/test_fluent_shell.py`

**Interfaces:**
- Consumes: an existing top-level `QWidget`.
- Produces:
  - `FluentPageHost(QWidget)`.
  - `FluentPageHost.content: QWidget`.
  - `FluentPageHost.scroll_area: QScrollArea`.

- [ ] **Step 1: Write the failing page-host test**

Add to `tests/test_fluent_shell.py`:

```python
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from app.ui.shell import FluentPageHost


class FluentPageHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_host_preserves_legacy_widget_and_exposes_scroll_area(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel("Legacy page"))
        host = FluentPageHost(content)
        self.assertIs(host.content, content)
        self.assertIs(host.scroll_area.widget(), content)
        self.assertTrue(host.scroll_area.widgetResizable())
        self.assertEqual(host.objectName(), "fluentPageHost")
```

- [ ] **Step 2: Run the test to verify import failure**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::FluentPageHostTests -q
```

Expected: collection fails because `FluentPageHost` is absent.

- [ ] **Step 3: Implement the compatibility host**

Create `app/ui/shell/page_host.py`:

```python
from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget


class FluentPageHost(QWidget):
    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fluentPageHost")
        self.content = content
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)
```

Export it from `app/ui/shell/__init__.py`.

- [ ] **Step 4: Run the focused test**

Run:

```powershell
python -m pytest tests/test_fluent_shell.py::FluentPageHostTests -q
```

Expected: pass.

- [ ] **Step 5: Commit the compatibility host**

```powershell
git add app/ui/shell/page_host.py app/ui/shell/__init__.py tests/test_fluent_shell.py
git commit -m "feat: add Fluent legacy page host"
```

