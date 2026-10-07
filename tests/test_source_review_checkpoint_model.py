"""Large checkpoint lists retain every row without eager Qt items/tooltips."""
from unittest.mock import Mock

from PySide6.QtCore import Qt

from app.storage import StoredPoint
from app.ui.results.spectrum_tab import SpectrumResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def test_large_list_formats_visible_cells_and_tooltips_only_on_demand(shell_qt_application, tmp_path, monkeypatch):
    app = shell_qt_application
    page = SpectrumResultsTab()
    metadata = {str(i): "large metadata field" for i in range(100)}
    points = tuple(StoredPoint(i, "2026-10-06T12:34:56.123+00:00", "ok",
                              {"current_a": i * 1e-6}, {}, metadata, {}, False) for i in range(10000))
    tooltip = Mock(return_value="checkpoint detail")
    page.points_model._tooltip = tooltip
    calls = []
    original = page.points_model.data
    def data(index, role=Qt.ItemDataRole.DisplayRole):
        calls.append(index.row())
        return original(index, role)
    monkeypatch.setattr(page.points_model, "data", data)
    try:
        page._populate_points(points)
        assert page.points_model.records is points
        assert page.points_model.rowCount() == 10000
        tooltip.assert_not_called()
        page.resize(1440, 900)
        page.show()
        app.processEvents()
        assert len(set(calls)) < 200
        assert page.points.isVisible() and page.points.viewport().height() > 0
        tooltip.assert_not_called()
        last = page.points_model.index(9999, 0)
        assert last.data(Qt.ItemDataRole.UserRole) is points[-1]
        page.points.setCurrentIndex(last)
        assert page.prev_button.isEnabled() and not page.next_button.isEnabled()
        page._go_previous()
        assert page.points.currentIndex().row() == 9998
        assert page.points_model.index(9999, 3).data(Qt.ItemDataRole.ToolTipRole) == "checkpoint detail"
        tooltip.assert_called_once_with(points[-1])
        page._populate_points(points[-3:])
        assert page.points_model.rowCount() == 3
        assert page.points_model.index(0, 0).data() == "9997"
        app.processEvents()
        assert page.grab().save(str(tmp_path / "checkpoint-model.png"))
        page.clear()
        assert page.points_model.rowCount() == 0
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
