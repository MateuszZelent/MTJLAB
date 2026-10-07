"""Execution owns immutable data, independently of mutable compiler drafts."""
from copy import deepcopy
from dataclasses import asdict
import json
import pickle

import pytest

from app.engine.compiler import ExecutionPlan, PlanAction, RecipeCompiler


def test_plan_detaches_nested_payload_limits_and_setpoints():
    payload = {"processing": {"stages": [{"gain": 2}]}}
    points = {"moke_box.vout0.voltage": .05}
    limits = {"device": {"maximum": .1}}
    upload = {"tags": ["sweep"]}
    draft = PlanAction("node", "checkpoint", payload, points)
    plan = ExecutionPlan("test", [draft], 1, "hash", "", recipe_dut_limits=limits, elab_upload_config=upload)
    before = json.dumps(RecipeCompiler._canonicalize(plan.actions))
    payload["processing"]["stages"][0]["gain"] = 900
    points["moke_box.vout0.voltage"] = 5
    limits["device"]["maximum"] = 10
    upload["tags"].append("changed")
    assert json.dumps(RecipeCompiler._canonicalize(plan.actions)) == before
    assert plan.recipe_dut_limits["device"]["maximum"] == .1
    assert plan.elab_upload_config["tags"] == ("sweep",)
    assert plan.actions[0] is not draft
    for mapping in (plan.actions[0].payload, plan.actions[0].payload["processing"]["stages"][0], plan.actions[0].setpoints_si, plan.recipe_dut_limits["device"]):
        for mutate in (lambda: mapping.update({"new": 1}), lambda: mapping.clear(), lambda: mapping.popitem(), lambda: mapping.setdefault("new", 1), lambda: mapping.__setitem__("new", 1), lambda: mapping.__delitem__(next(iter(mapping))), lambda: mapping.__ior__({"new": 1})):
            with pytest.raises(TypeError, match="immutable"):
                mutate()
        snapshot = dict(mapping)
        mapping.__init__({"new": 1})
        assert mapping == snapshot
    assert asdict(deepcopy(plan)) == asdict(plan)
    assert asdict(pickle.loads(pickle.dumps(plan))) == asdict(plan)
