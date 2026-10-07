from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from app.engine.compiler import PlanAction
from app.ui.execution.plan_timeline import ExecutionPlanTimeline, planned_series


def actions():
    return (
        PlanAction("initial", "configure_keithley", {"request": SimpleNamespace(
            channel="A", mode="current", level_si=0.0, changed_fields=None)}, {}),
        PlanAction("on", "set_keithley_output", {"channel": "A", "enabled": True}, {}),
        PlanAction("set", "update_keithley_level", {"channel": "A", "mode": "current", "level_si": .0016}, {"keithley.A.current": .0016}),
        PlanAction("wait", "wait", {"duration_s": 3.0}, {"keithley.A.current": .0016}),
        PlanAction("zero", "ramp_keithley_to_zero", {"channel": "A"}, {"keithley.A.current": .0016}, is_finally=True),
        PlanAction("off", "set_keithley_output", {"channel": "A", "enabled": False}, {"keithley.A.current": .0016}, is_finally=True),
    )


def test_compiled_changes_hold_output_and_final_zero_is_not_reverted_by_context():
    rows = {row.target: row for row in planned_series(actions())}
    assert rows["keithley.A.current"].steps == [1, 3, 5]
    assert rows["keithley.A.current"].values == [0., .0016, 0.]
    assert rows["keithley.A.output"].steps == [1, 2, 6]
    assert rows["keithley.A.output"].values == [0., 1., 0.]


def test_live_cursor_does_not_rebuild_curves_and_inspection_does_not_move_it(tmp_path):
    app = QApplication.instance() or QApplication([])
    widget = ExecutionPlanTimeline()
    try:
        widget.set_plan(actions(), shutdown_actions=("keithley.outputs_off",))
        widget.resize(1280, 600)
        widget.show()
        app.processEvents()
        curves = tuple(widget.plot.listDataItems())
        widget.set_current_step(3)
        assert widget.cursor.value() == 3
        assert "1.6 mA" in widget.details.text()
        widget.follow.setChecked(False)
        widget.step.setValue(6)
        assert widget.cursor.value() == 3
        assert "FINALLY" in widget.details.text()
        widget.set_current_step(4)
        assert widget.step.value() == 6
        widget.follow.setChecked(True)
        assert widget.step.value() == 4
        assert widget.cursor.value() == 4
        assert tuple(widget.plot.listDataItems()) == curves
        assert "keithley.outputs_off" in widget.shutdown.text()
        assert widget.grab().save(str(tmp_path / "timeline.png"))
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
