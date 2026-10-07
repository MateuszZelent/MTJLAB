"""A partial carrier mutation cannot derive its companion from a UI draft."""
import pytest

from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("parameter", ["amplitude", "offset"])
@pytest.mark.parametrize("action", ["set", "sweep"])
def test_partial_level_review_marks_recipe_dependent_endpoints(shell_qt_application, parameter, action, tmp_path):
    app = shell_qt_application
    snapshot = RigolConfigurationSnapshot(high_level="10 mV", low_level="-10 mV")
    dialog = RigolNodeEditorDialog(snapshot=snapshot, current_snapshot_resolver=lambda _: snapshot)
    try:
        dialog.level_mode.setCurrentText("Amplitude / Offset")
        selector = dialog.parameter_selectors[f"carrier.{parameter}"]
        selector.setCurrentIndex(selector.findData(action))
        dialog.resize(1200, 900)
        dialog.review.toggle.setChecked(True)
        dialog.show()
        app.processEvents()
        rows = {row.key: row for row in dialog.review.table.rows}
        assert rows[parameter].action == ("Sweep" if action == "sweep" else "Set")
        for name in ("high_level", "low_level"):
            assert rows[name].action == "Derived"
            assert "recipe" in rows[name].planned.lower()
            assert ("offset" if parameter == "amplitude" else "amplitude") in rows[name].planned
            index = next(i for i, row in enumerate(dialog.review.table.rows) if row.key == name)
            assert "Depends on preceding recipe" in dialog.review.table.item(index, 3).text()
            assert "Same" not in dialog.review.table.item(index, 3).text()
        assert dialog.review.table.isVisible()
        assert dialog.review.table.width() > 0
        assert dialog.review.table.counts["unknown"] >= 2
        assert dialog.grab().save(str(tmp_path / "rigol-coupled-review.png"))
        # Authoring both components removes the unknown companion dependency.
        other = dialog.parameter_selectors[f"carrier.{'offset' if parameter == 'amplitude' else 'amplitude'}"]
        other.setCurrentIndex(other.findData("set"))
        assert all(row.action != "Derived" for row in dialog._comparison_rows())
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
