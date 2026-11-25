from types import SimpleNamespace

import pyqtgraph as pg
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


def test_other_device_steps_do_not_replay_stale_keithley_levels():
    target = "keithley.B.current"
    plan = (
        PlanAction("bias", "update_keithley_level", {"channel": "B", "mode": "current", "level_si": -.071}, {target: -.071}),
        PlanAction("field", "update_moke_voltage", {"channel": 0, "voltage_v": -.054}, {target: 0.}),
        PlanAction("next", "update_keithley_level", {"channel": "B", "mode": "current", "level_si": -.070}, {target: -.070}),
    )
    row = next(row for row in planned_series(plan) if row.target == target)
    assert row.steps == [1, 3]
    assert row.values == [-.071, -.070]


def test_spectrum_and_reference_have_a_dedicated_clickable_lane():
    app = QApplication.instance() or QApplication([])
    widget = ExecutionPlanTimeline()
    average = {"trace": "TRAC1", "instrument_average_count": 50}
    try:
        widget.set_plan((
            PlanAction("reference", "acquire_reference", average, {}),
            PlanAction("settle", "wait", {"duration_s": .5}, {}),
            PlanAction("spectrum", "acquire_spectrum", average, {}),
            PlanAction("load", "acquire_reference", {"source_file": "ref.h5"}, {}),
            PlanAction("timed", "acquire_spectrum", {"instrument_average_duration_s": 5.0}, {}),
        ))
        widget.resize(1280, 480)
        widget.show()
        app.processEvents()
        assert widget.isVisible() and widget.plot.height() >= 260
        markers = {
            int(point.data()): (item, point)
            for item in widget.plot.getPlotItem().items if isinstance(item, pg.ScatterPlotItem)
            for point in item.points()
        }
        assert markers[2][1].pos().y() == 0
        assert {markers[step][1].pos().y() for step in (1, 3, 4, 5)} == {1}
        assert markers[1][0] is not markers[3][0]
        assert markers[1][0] is not markers[4][0]
        assert "Acquire reference" in widget.legend.text()
        assert "Load reference" in widget.legend.text()
        spectrum_item, spectrum_point = markers[3]
        spectrum_item.sigClicked.emit(spectrum_item, [spectrum_point], None)
        assert widget.step.value() == 3
        assert "Acquire spectrum" in widget.details.text()
        assert "Anritsu Lin Average · 50 sweeps" in widget.details.text()
        assert "Load reference · ref.h5" in widget.action_description(4)
        assert "Anritsu Lin Average · ~5 s" in widget.action_description(5)
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


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
