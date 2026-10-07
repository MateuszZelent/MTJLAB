"""Large checkpoint trees materialize only the expanded page of details."""
from types import SimpleNamespace

from PySide6.QtCore import Qt

from app.storage.hdf5_reader import StoredPoint
from app.ui.results.sweep_tree_panel import SweepTreePanel
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def test_large_shown_tree_defers_details_and_keeps_last_checkpoint_reachable(shell_qt_application, tmp_path):
    panel = SweepTreePanel()
    panel._run = SimpleNamespace(rows={}, devices=(), labbook=(), post_process=())
    points = tuple(StoredPoint(i, None, "ok", {f"set_{j}": j for j in range(20)},
                               {f"value_{j}": j for j in range(20)}, {}, {}, True)
                   for i in range(1001))
    try:
        panel.resize(1100, 750)
        panel.view_switch.setCurrentItem("data")
        panel.show()
        panel._populate_tree((), points=points)
        shell_qt_application.processEvents()
        root = next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount())
                    if panel.tree.topLevelItem(i).text(0) == "Results")
        checkpoints = root.child(0)
        assert checkpoints.childCount() == 11
        assert all(checkpoints.child(i).childCount() == 0 for i in range(11))
        last = checkpoints.child(10)
        last.setExpanded(True)
        shell_qt_application.processEvents()
        assert last.childCount() == 1
        point_item = last.child(0)
        assert point_item.data(0, Qt.ItemDataRole.UserRole) == points[-1]
        assert point_item.child(0).childCount() == 20
        assert point_item.child(1).childCount() == 20
        assert point_item.child(2).data(0, Qt.ItemDataRole.UserRole) == (points[-1], "raw")
        last.setExpanded(False)
        last.setExpanded(True)
        assert last.childCount() == 1
        assert panel.tree.isVisible() and panel.tree.width() > 0
        panel.tree.scrollToItem(last)
        shell_qt_application.processEvents()
        assert panel.grab().save(str(tmp_path / "lazy-checkpoints.png"))
        panel.clear()
        assert not panel._checkpoint_pages
    finally:
        panel.close()
        panel.deleteLater()
