import h5py
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.recipes import parse_recipe_text
from app.storage.hdf5_reader import Hdf5RunReader
from app.ui.execution.page import RunMonitorPage
from app.ui.recipes.page import RecipePage
from app.ui.series_dialog import MeasurementSeriesDialog
from tests.test_sweep_audit_contracts import audit_settings, execute


def series_source(completed="[]"):
    source = f"""schema_version: 1
name: Series test
completed_series: {completed}
root:
  id: root
  type: sequence
  children:
  - {{id: initial-a, type: set_keithley_output, channel: A, enabled: false}}
  - {{id: initial-b, type: set_keithley_output, channel: B, enabled: false}}
  - {{id: prepare-a, type: configure_keithley, channel: A, mode: current, level: '0 uA', compliance: '20 mV', source_range: '10 mA'}}
  - {{id: prepare-b, type: configure_keithley, channel: B, mode: current, level: '0 uA', compliance: '20 mV', source_range: '100 mA'}}
  - {{id: moke-config, type: configure_moke_box, channel: 0, minimum_voltage: '-80 mV', maximum_voltage: '35 mV'}}
  - {{id: moke-arm, type: arm_moke_voltage}}
  - {{id: moke-zero, type: update_moke_voltage, channel: 0, voltage: '0 mV'}}
  - {{id: reference, type: acquire_reference, instrument_average_count: 50, store_source_spectra: false}}
  - {{id: on-a, type: set_keithley_output, channel: A, enabled: true}}
  - {{id: on-b, type: set_keithley_output, channel: B, enabled: true}}
"""
    for group, voltage, series_numbers in (("low", -75, (1, 2)), ("high", 32, (3, 4))):
        source += f"""  - id: {group}
    type: sweep
    target: moke_box.vout0.voltage
    segments: [{{value: '{voltage} mV'}}]
    children:
"""
        for number in series_numbers:
            source += f"""    - id: series-{number}
      type: sequence
      children:
      - {{id: bias-{number}, type: ramp_keithley_current, channel: B, level: '{number * 100} uA', step: '100 uA', settle_time: '2 ms'}}
      - {{id: approach-{number}, type: ramp_keithley_current, channel: A, level: '-100 uA', step: '100 uA', settle_time: '2 ms'}}
      - id: scan-{number}
        type: sweep
        target: keithley.A.current
        respect_ramp_step: true
        segments: [{{start: '-100 uA', stop: '100 uA', points: 3}}]
        children:
        - {{id: trace-{number}, type: acquire_spectrum, instrument_average_count: 50, store_source_spectra: false}}
      - {{id: retract-{number}, type: ramp_keithley_current, channel: A, level: '0 uA', step: '100 uA', settle_time: '2 ms'}}
"""
    return source + """finally:
- {id: zero-a, type: ramp_keithley_to_zero, channel: A}
- {id: zero-b, type: ramp_keithley_to_zero, channel: B}
- {id: zero-moke, type: stop_moke_voltage, channel: 0}
"""


def test_start_at_four_recompiles_setup_trajectory_and_cleanup(tmp_path):
    settings = audit_settings(tmp_path)
    original = RecipeCompiler(settings).compile(parse_recipe_text(series_source()))
    plan = RecipeCompiler(settings).compile(parse_recipe_text(series_source("[series-1, series-2, series-3]")))
    assert original.total_spectra == 12
    assert plan.total_spectra == 3
    assert plan.sha256 != original.sha256
    assert [(item.number, item.already_measured) for item in plan.series] == [
        (1, True), (2, True), (3, True), (4, False),
    ]
    assert [action.node_id for action in plan.actions if action.kind == "acquire_reference"] == ["reference"]
    start, end = plan.series_action_ranges["series-4"]
    assert plan.actions[start].node_id == "bias-4"
    assert plan.actions[end - 1].node_id == "retract-4"
    assert plan.actions[start].payload["planned_step_count"] == 4
    assert [(action.node_id, action.payload["voltage_v"]) for action in plan.actions
            if action.kind == "update_moke_voltage"] == [
        ("moke-zero", 0), ("high.point.0", pytest.approx(0.032)),
    ]
    assert [action.node_id for action in plan.actions if action.is_finally] == ["zero-a", "zero-b", "zero-moke"]
    assert RecipeCompiler(settings).compile(parse_recipe_text(plan.recipe_source)).sha256 == plan.sha256


@pytest.mark.parametrize("completed", ["[missing]", "[series-1, series-2, series-3, series-4]"])
def test_unusable_series_selection_cannot_compile(tmp_path, completed):
    with pytest.raises(ConfigurationError, match="series"):
        RecipeCompiler(audit_settings(tmp_path)).compile(parse_recipe_text(series_source(completed)))


def test_selected_series_records_only_its_points_with_original_series_number(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(series_source("[series-1, series-2, series-3]")))
    path = tmp_path / "series-4.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    points = Hdf5RunReader.points(path)
    assert len(points) == 3
    assert [point.metadata["series_number"] for point in points] == [4, 4, 4]
    assert [point.metadata["series_id"] for point in points] == ["series-4"] * 3
    assert [point.setpoints["keithley.B.current"] for point in points] == pytest.approx([0.0004] * 3)
    with h5py.File(path) as file:
        assert parse_recipe_text(file["run/recipe_yaml"].asstr()[()]).completed_series == (
            "series-1", "series-2", "series-3",
        )
        events = file["events/name"].asstr()[:].tolist()
        assert events.count("series_started") == events.count("series_finished") == 1


@pytest.mark.parametrize("width", [920, 1280])
def test_series_selection_is_visible_in_sweeps_and_execution(tmp_path, monkeypatch, width):
    app = QApplication.instance() or QApplication([])
    settings = audit_settings(tmp_path)
    sweeps = RecipePage(settings)
    execution = RunMonitorPage()
    try:
        sweeps.resize(width, 820)
        sweeps.show()
        sweeps.path.setText(str(tmp_path / "series.yml"))
        sweeps.editor.setPlainText(series_source())
        assert sweeps.apply_yaml_to_tree(show_error=False)
        app.processEvents()
        assert sweeps.series_button.isVisibleTo(sweeps)
        assert sweeps.series_button.width() > 0

        def select_four(dialog):
            dialog.show()
            app.processEvents()
            assert dialog.table.isVisible() and dialog.table.height() > 0
            dialog.start_combo.setCurrentIndex(3)
            assert dialog.completed_series_ids == ("series-1", "series-2", "series-3")
            return dialog.DialogCode.Accepted

        monkeypatch.setattr(MeasurementSeriesDialog, "exec", select_four)
        sweeps.series_button.click()
        source = sweeps.editor.toPlainText()
        assert parse_recipe_text(source).completed_series == ("series-1", "series-2", "series-3")
        assert "1/4 planned" in sweeps.series_button.text()
        plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
        execution.resize(width, 820)
        execution.show()
        execution.preview_plan(
            plan, PlanEstimator(settings).estimate(plan),
            sweeps.semantic_tree_snapshot(source, plan, update_editor=False),
            execution_mode="measurement", output_preview="data.h5",
        )
        app.processEvents()
        assert "Start at series 4" in execution.plan_preview_summary.text()
        assert execution.series_button.isVisibleTo(execution) and execution.series_button.width() > 0
        execution.append_event("series_started", {
            "series_number": 4, "series_total": 4, "moke_channel": 0,
            "moke_voltage_v": 0.032, "keithley_b_a": 0.0004,
        })
        assert "Series 4 of 4" in execution.series_status.text()
    finally:
        sweeps._close_discard_confirmed = True
        for page in (execution, sweeps):
            page.close()
            page.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_series_can_be_excluded_and_reincluded_from_the_measurement_tree(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = audit_settings(tmp_path)
    sweeps = RecipePage(settings)
    execution = RunMonitorPage()
    try:
        sweeps.resize(1300, 820)
        sweeps.show()
        sweeps.path.setText(str(tmp_path / "series.yml"))
        sweeps.editor.setPlainText(series_source())
        assert sweeps.apply_yaml_to_tree(show_error=False)
        sweeps.compile_recipe()
        assert sweeps.run_button.isEnabled()
        tree = sweeps.measurement_tree
        model = sweeps.tree_model
        for series_id in ("series-1", "series-2", "series-3"):
            row = model.index_for_semantic_id(series_id)
            cell = row.siblingAtColumn(3)
            assert "Series " in model.data(row)
            assert cell.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
            tree.scrollTo(cell)
            app.processEvents()
            bounds = tree.visualRect(cell)
            assert bounds.width() >= 120 and bounds.height() > 0
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                             pos=QPoint(bounds.left() + 32, bounds.center().y()))
            app.processEvents()
            state = model.index_for_semantic_id(series_id).siblingAtColumn(3)
            assert state.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
            assert state.data() == "Measured"
        assert not sweeps.run_button.isEnabled()
        assert parse_recipe_text(sweeps.editor.toPlainText()).completed_series == (
            "series-1", "series-2", "series-3",
        )
        sweeps.set_execution_controlled(True)
        locked = model.index_for_semantic_id("series-1").siblingAtColumn(3)
        assert not model.setData(locked, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        sweeps.set_execution_controlled(False)
        sweeps.compile_recipe()
        plan = sweeps.execution_request()[0]
        assert plan.total_spectra == 3
        execution.preview_plan(
            plan, PlanEstimator(settings).estimate(plan),
            sweeps.semantic_tree_snapshot(plan.recipe_source, plan, update_editor=False),
            execution_mode="measurement", output_preview="data.h5",
        )
        state = execution.tree_model.index_for_semantic_id("series-1").siblingAtColumn(3)
        assert state.data() == "Measured"
        assert not state.flags() & Qt.ItemFlag.ItemIsUserCheckable
        cell = model.index_for_semantic_id("series-2").siblingAtColumn(3)
        tree.setCurrentIndex(cell)
        QTest.keyClick(tree, Qt.Key.Key_Space)
        assert parse_recipe_text(sweeps.editor.toPlainText()).completed_series == ("series-1", "series-3")
        sweeps.compile_recipe()
        assert sweeps.execution_request()[0].total_spectra == 6
    finally:
        sweeps._close_discard_confirmed = True
        for page in (execution, sweeps):
            page.close()
            page.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
