"""Resume must evaluate current station readiness before starting its worker."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.readiness import ReadinessItem, ReadinessLevel, StationReadiness
from app.engine.runner import ExecutionMode
from app.ui.shell import main_window


@pytest.mark.parametrize("blocker", ["audit", "storage", "device.anritsu", "estop", None])
def test_resume_checks_current_readiness_and_estop_before_start(monkeypatch, blocker):
    estimate = SimpleNamespace(nominal_duration_s=10.)
    estimator = Mock(return_value=SimpleNamespace(estimate=Mock(return_value=estimate)))
    monkeypatch.setattr(main_window, "PlanEstimator", estimator)
    warning, critical = Mock(), Mock()
    monkeypatch.setattr(main_window.QMessageBox, "warning", warning)
    monkeypatch.setattr(main_window.QMessageBox, "critical", critical)
    items = () if blocker in {None, "estop"} else (
        ReadinessItem(blocker, blocker, "current readiness fault", ReadinessLevel.FAIL),
    )
    window = SimpleNamespace(
        _emergency_inhibit=blocker == "estop", _settings=object(),
        dashboard=SimpleNamespace(evaluate_readiness=Mock(return_value=StationReadiness(items))),
        recipe_page=SimpleNamespace(semantic_tree_snapshot=Mock(return_value="tree")),
        _active_device_controllers=Mock(return_value={}),
        _run_controller=SimpleNamespace(start=Mock()),
        _repository=SimpleNamespace(path="settings.yml"), _simulation=True,
        _access=SimpleNamespace(identity=SimpleNamespace(as_context=Mock(return_value={}))),
        run_monitor=SimpleNamespace(run_started=Mock()),
        _set_run_ui_locked=Mock(), _navigate_to=Mock(), _log=Mock(),
    )
    plan = SimpleNamespace(recipe_source="name: test", actions=("a", "b"), safe_shutdown_actions=())
    checkpoint = SimpleNamespace(next_action_index=1, prelude_actions=(), stored_points=1)
    dialog = SimpleNamespace(missing_devices=(), accept=Mock())
    main_window.MainWindow._start_resume_from_ready_dialog(window, dialog, plan, checkpoint,
        estimate_execution_mode=ExecutionMode.MEASUREMENT, outputs_forced_off=False, discarded=0)
    if blocker == "estop":
        warning.assert_called_once()
        window.dashboard.evaluate_readiness.assert_not_called()
        estimator.assert_not_called()
    else:
        window.dashboard.evaluate_readiness.assert_called_once_with(plan, estimate)
    if blocker is not None:
        window._run_controller.start.assert_not_called()
        window.recipe_page.semantic_tree_snapshot.assert_not_called()
        window._set_run_ui_locked.assert_not_called()
        dialog.accept.assert_not_called()
        if blocker != "estop":
            critical.assert_called_once()
            assert "current readiness fault" in critical.call_args.args[2]
    else:
        window._run_controller.start.assert_called_once()
        assert window._run_controller.start.call_args.kwargs["recovery"] is checkpoint
        dialog.accept.assert_called_once()
        window._set_run_ui_locked.assert_called_once_with(True)
