"""Authored incoming state, without device defaults or whole-recipe compilation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from app.recipes.models import Recipe, RecipeNode, parse_recipe_text


def node(id, type="sequence", children=(), otherwise=(), **data):
    return RecipeNode(id, type, data, tuple(children), tuple(otherwise))


def recipe(*children, finally_nodes=()):
    return Recipe(1, "context", node("root", children=children), tuple(finally_nodes), "")


def baseline(id="baseline", channel="A", source_autorange=False, **data):
    return node(
        id,
        "configure_keithley",
        channel=channel,
        mode="current",
        level="1 mA",
        compliance="67 mV",
        source_range="10 mA",
        source_autorange=source_autorange,
        **data,
    )


def module(id="module", device="keithley", channel="A", actions=(), children=(), **data):
    return node(
        id,
        device_module=device,
        channel=channel,
        operation="configure_selected_parameters",
        parameter_actions=list(actions),
        children=children,
        **data,
    )


def action(parameter, value=None, mode="set", **data):
    return dict(parameter_id=parameter, mode=mode, value=value, **data)


@pytest.fixture
def resolve():
    from app.recipes import configuration_context

    return configuration_context.configuration_before


def test_interface_is_frozen_and_has_no_invented_defaults(resolve):
    from app.recipes.configuration_context import ConfigurationValue

    value = ConfigurationValue("10 mA", "baseline")
    assert value.varying is False
    with pytest.raises(FrozenInstanceError):
        value.value = "1 A"
    assert resolve(recipe(node("target")), "target", "keithley", "A") == {}
    assert resolve(recipe(node("target")), "root", "keithley", "A") == {}


def test_prefix_latest_write_and_channel_isolation(resolve):
    r = recipe(
        baseline(settle_time="250 ms"),
        baseline("b", "B"),
        node("level", "update_keithley_level", channel="A", mode="current", level="2 mA"),
        node("target"),
        baseline("later"),
    )
    a = resolve(r, "target", "keithley", "A")
    b = resolve(r, "target", "keithley", "B")
    assert (a["source_level"].value, a["source_level"].source) == ("2 mA", "level")
    assert a["source_mode"].value == "current"
    assert a["source_range"].source == "baseline"
    assert a["settling_time"].value == "250 ms"
    assert b["source_level"].value == "1 mA"
    assert b["source_range"].source == "b"
    assert "nplc" not in a


def test_stale_persisted_snapshot_1a_does_not_replace_10ma_without_range_action(resolve):
    r = parse_recipe_text("""schema_version: 1
name: stale draft
root:
  id: root
  type: sequence
  children:
    - {id: baseline, type: configure_keithley, channel: A, mode: current,
       level: 1 mA, compliance: 67 mV, source_range: 10 mA, source_autorange: false}
    - id: selected
      type: sequence
      device_module: keithley
      channel: A
      operation: configure_selected_parameters
      configuration: {channel: A, source_mode: current, source_range: 1 A, nplc: 99}
      parameter_actions: [{parameter_id: source.level, mode: set, value: 2 mA}]
    - {id: target, type: comment, text: edit here}
""")
    state = resolve(r, "target", "keithley", "A")
    assert (state["source_range"].value, state["source_range"].source) == ("10 mA", "baseline")
    assert state["source_level"].value == "2 mA"
    assert "nplc" not in state


@pytest.mark.parametrize(
    "parameter,key,flag",
    [
        ("source.range", "source_range", "source_autorange"),
        ("measurement.voltage_range", "measure_voltage_range", "measure_voltage_autorange"),
        ("measurement.current_range", "measure_current_range", "measure_current_autorange"),
    ],
)
@pytest.mark.parametrize("value,enabled", [("AUTO", True), ("10 mA", False)])
def test_selected_ranges_update_autorange_flags(resolve, parameter, key, flag, value, enabled):
    r = recipe(baseline(), module(actions=[action(parameter, value)]), node("target"))
    state = resolve(r, "target", "keithley", "A")
    assert state[key].value == value
    assert state[flag].value is enabled
    assert state[flag].source == "module"
    assert not state[key].varying and not state[flag].varying


def test_only_set_or_sweep_rows_mutate_and_omitted_fields_survive(resolve):
    r = recipe(
        baseline(settle_time="250 ms", nplc=2),
        module(
            actions=[
                action("source.level", "9 mA", "same"),
                action("measurement.nplc", 4),
                action("measurement.settling_time", "50 ms"),
            ]
        ),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_level"].value == "1 mA"
    assert state["compliance"].value == "67 mV"
    assert state["nplc"].value == 4
    assert state["settling_time"].value == "50 ms"


def test_ancestor_selected_actions_apply_before_children_but_not_before_self(resolve):
    r = recipe(
        baseline(),
        module(actions=[action("source.compliance", "80 mV")], children=[node("target")]),
    )
    assert resolve(r, "module", "keithley", "A")["compliance"].value == "67 mV"
    assert resolve(r, "target", "keithley", "A")["compliance"].value == "80 mV"


def test_disabled_subtree_and_disabled_literal_do_not_write(resolve):
    r = recipe(
        baseline(),
        node(
            "disabled",
            disabled=True,
            children=[module(actions=[action("source.range", "1 A")], children=[node("inside")])],
        ),
        baseline("also-disabled", disabled=True),
        node("target"),
    )
    assert resolve(r, "target", "keithley", "A")["source_range"].source == "baseline"
    assert resolve(r, "inside", "keithley", "A")["source_range"].source == "baseline"


def test_repeat_incoming_includes_writes_after_target_in_prior_iteration(resolve):
    r = recipe(
        baseline(nplc=2),
        node(
            "loop",
            "repeat",
            count=3,
            children=[
                node("target"),
                module(actions=[action("source.range", "100 mA")]),
            ],
        ),
        node("after"),
    )
    for target in ("target", "after"):
        state = resolve(r, target, "keithley", "A")
        assert state["source_range"].varying and state["source_range"].value is None
        assert state["nplc"].value == 2 and not state["nplc"].varying


def test_repeat_single_iteration_and_guaranteed_prefix_reset_remain_known(resolve):
    single = recipe(baseline(), node("loop", "repeat", count=1, children=[node("target")]))
    assert resolve(single, "target", "keithley", "A")["source_range"].value == "10 mA"
    r = recipe(
        baseline(),
        node(
            "loop",
            "repeat",
            count=100000,
            children=[
                module("reset", actions=[action("source.range", "10 mA")]),
                node("target"),
                module("change", actions=[action("source.range", "100 mA")]),
            ],
        ),
    )
    assert resolve(r, "target", "keithley", "A")["source_range"].value == "10 mA"


def test_nested_sweep_axis_and_native_sweep_are_variable(resolve):
    r = recipe(
        baseline(nplc=2),
        node(
            "outer",
            "repeat",
            count=2,
            children=[
                node(
                    "axis",
                    "sweep",
                    target="keithley.A.current",
                    start="1 mA",
                    stop="2 mA",
                    points=3,
                    children=[
                        module(
                            "inner",
                            actions=[
                                action(
                                    "source.compliance",
                                    "67 mV",
                                    "sweep",
                                    segments=[{"start": "50 mV", "stop": "67 mV", "points": 2}],
                                )
                            ],
                            children=[node("target")],
                        )
                    ],
                )
            ],
        ),
    )
    state = resolve(r, "target", "keithley", "A")
    for key in ("source_level", "compliance"):
        assert state[key].varying and state[key].value is None
    assert state["source_range"].value == "10 mA"
    assert state["nplc"].value == 2


def test_native_sweep_does_not_reapply_fixed_rows_on_later_iterations(resolve):
    r = recipe(
        baseline(),
        module(
            actions=[
                action("source.range", "10 mA"),
                action("source.level", "1 mA", "sweep"),
            ],
            children=[node("target"), module("change", actions=[action("source.range", "100 mA")])],
        ),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_range"].varying
    assert state["source_level"].varying


@pytest.mark.parametrize("other,variable", [("10 mA", False), ("100 mA", True)])
def test_unknown_if_merges_equal_values_and_marks_differences(resolve, other, variable):
    r = recipe(
        baseline(),
        node(
            "branch",
            "if",
            left="${repeat.r.index}",
            operator="==",
            right=0,
            children=[module("yes", actions=[action("source.range", "10 mA")])],
            otherwise=[module("no", actions=[action("source.range", other)])],
        ),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_range"].varying is variable
    assert state["source_range"].value == (None if variable else "10 mA")
    assert state["compliance"].value == "67 mV"


def test_one_sided_branch_and_sibling_branches_do_not_create_false_certainty(resolve):
    r = recipe(
        node(
            "branch",
            "if",
            left="${x}",
            operator="==",
            right=0,
            children=[module("yes", actions=[action("measurement.nplc", 3)])],
            otherwise=[node("else-target")],
        ),
        node("target"),
    )
    assert resolve(r, "target", "keithley", "A")["nplc"].varying
    assert "nplc" not in resolve(r, "else-target", "keithley", "A")


@pytest.mark.parametrize("condition,expected", [(True, "100 mA"), (False, "10 mA")])
def test_literal_condition_uses_executed_branch(resolve, condition, expected):
    r = recipe(
        baseline(),
        node(
            "branch",
            "if",
            condition=condition,
            children=[module(actions=[action("source.range", "100 mA")])],
        ),
        node("target"),
    )
    assert resolve(r, "target", "keithley", "A")["source_range"].value == expected


def test_dynamic_and_unsupported_mutations_invalidate_previous_certainty(resolve):
    r = recipe(
        baseline(),
        node(
            "dynamic",
            "update_keithley_level",
            channel="A",
            mode="current",
            level="${keithley.A.current}",
        ),
        node("target"),
    )
    value = resolve(r, "target", "keithley", "A")["source_level"]
    assert value.value is None and value.varying and value.source == "dynamic"
    unsupported = recipe(
        baseline(), module(actions=[action("future.parameter", "anything")]), node("target")
    )
    assert resolve(unsupported, "target", "keithley", "A")["source_range"].value is None


def test_finally_is_after_root_and_ordered(resolve):
    r = recipe(
        baseline(),
        node("root-target"),
        finally_nodes=[node("zero", "ramp_keithley_to_zero", channel="A"), node("final-target")],
    )
    assert resolve(r, "root-target", "keithley", "A")["source_level"].value == "1 mA"
    state = resolve(r, "final-target", "keithley", "A")
    assert state["source_level"].value == "0 A"
    assert state["source_level"].source == "zero"
    assert state["output_enabled"].value is False


def test_rigol_literal_selected_patch_and_channel_isolation(resolve):
    r = recipe(
        node(
            "carrier",
            "configure_rigol",
            channel=1,
            waveform="SIN",
            frequency="1 kHz",
            high_level="1 V",
            low_level="-1 V",
        ),
        node("output", "configure_rigol_output", channel=1, polarity="INV", mode="NORM"),
        module(
            device="rigol",
            channel=1,
            configuration={"frequency": "99 MHz", "high_level": "9 V"},
            actions=[action("carrier.frequency", "2 kHz")],
        ),
        node("target"),
    )
    state = resolve(r, "target", "rigol", "1")
    assert state["frequency"].value == "2 kHz"
    assert state["high_level"].value == "1 V"
    assert state["output_polarity"].value == "INV"
    assert state["output_mode"].value == "NORM"
    assert resolve(r, "target", "rigol", 2) == {}
    assert "sync_delay" not in state


def test_rigol_coupled_amplitude_invalidates_high_low_instead_of_preserving_stale_pair(resolve):
    r = recipe(
        node("carrier", "configure_rigol", channel=1, high_level="1 V", low_level="-1 V"),
        module(device="rigol", channel=1, actions=[action("carrier.amplitude", "4 V")]),
        node("target"),
    )
    state = resolve(r, "target", "rigol", 1)
    assert state["high_level"].value is None and state["low_level"].value is None


def test_anritsu_selected_and_literal_fields_and_sg_isolation(resolve):
    r = recipe(
        node(
            "spectrum",
            "configure_anritsu",
            start_frequency="1 MHz",
            stop_frequency="2 MHz",
            reference_level="-20 dBm",
            points=501,
        ),
        node("advanced", "configure_anritsu_advanced", detector="RMS"),
        node("sg", "configure_anritsu_sg", frequency="10 MHz", power="-30 dBm"),
        module(
            device="anritsu",
            channel=None,
            configuration={"start_frequency": "999 MHz", "points": 999},
            actions=[
                action("spectrum.start_frequency", "1.5 MHz"),
                action("advanced.rbw_mode", "auto"),
            ],
        ),
        module(
            "sg-update", device="anritsu_sg", channel=None, actions=[action("sg.power", "-25 dBm")]
        ),
        node("target"),
    )
    state = resolve(r, "target", "anritsu")
    assert state["start_frequency"].value == "1.5 MHz"
    assert state["points"].value == 501
    assert state["detector"].value == "RMS"
    assert state["rbw_mode"].value == "auto"
    assert "rbw" not in state
    sg = resolve(r, "target", "anritsu_sg")
    assert sg["frequency"].value == "10 MHz" and sg["power"].value == "-25 dBm"


def test_invalid_target_and_later_values_do_not_prevent_prefix_editing(resolve):
    r = recipe(
        baseline(),
        node("target", "configure_keithley", channel="A", level="not a quantity"),
        node("later", "repeat", count="bad", children=[node("broken", "sweep", target="invalid")]),
    )
    before = deepcopy(r)
    state = resolve(r, "target", "keithley", "A")
    assert state["source_range"].value == "10 mA"
    assert r == before
    state.clear()
    assert resolve(r, "target", "keithley", "A")["source_range"].value == "10 mA"
    with pytest.raises(KeyError, match="missing"):
        resolve(r, "missing", "keithley", "A")


def test_source_range_preserves_measurement_preferences_without_inventing_them(resolve):
    r = recipe(
        baseline(measure_current_range="1 A", measure_voltage_range="100 mV"),
        module(actions=[action("source.range", "100 mA")]),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["measure_current_range"].value == "1 A"
    assert state["measure_current_range"].source == "baseline"
    assert state["measure_voltage_range"].value == "100 mV"
    unknown_mode = recipe(module(actions=[action("source.range", "100 mA")]), node("target"))
    state = resolve(unknown_mode, "target", "keithley", "A")
    assert "source_mode" not in state
    assert "measure_current_range" not in state and "measure_voltage_range" not in state


def test_source_mode_transition_invalidates_omitted_dimension_dependent_values(resolve):
    r = recipe(
        baseline(measure_voltage_range="100 mV"),
        node("voltage", "configure_keithley", channel="A", mode="voltage"),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_mode"].value == "voltage"
    for key in (
        "source_level",
        "compliance",
        "source_range",
    ):
        assert state[key].value is None and state[key].varying
    assert state["measure_voltage_range"].value == "100 mV"
    assert state["measure_voltage_range"].source == "baseline"
    assert not state["measure_voltage_range"].varying
    assert "measure_current_range" not in state


@pytest.mark.parametrize("measurement_auto", [False, True])
@pytest.mark.parametrize("change_kind", ["range", "mode", "autorange"])
def test_measurement_preferences_and_autorange_survive_source_changes(
    resolve, measurement_auto, change_kind
):
    change = {
        "range": module("change", actions=[action("source.range", "100 mA")]),
        "mode": node("change", "configure_keithley", channel="A", mode="voltage"),
        "autorange": module("change", actions=[action("source.range", "AUTO")]),
    }[change_kind]
    r = recipe(
        baseline(
            measure_current_range="1 A",
            measure_current_autorange=measurement_auto,
            measure_voltage_range="100 mV",
            measure_voltage_autorange=not measurement_auto,
        ),
        change,
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    for key, expected in (
        ("measure_current_range", "1 A"),
        ("measure_current_autorange", measurement_auto),
        ("measure_voltage_range", "100 mV"),
        ("measure_voltage_autorange", not measurement_auto),
    ):
        assert state[key].value == expected
        assert not state[key].varying
        assert state[key].source == "baseline"


def test_unknown_branch_measurement_preference_and_policy_remain_variable(resolve):
    r = recipe(
        baseline(measure_current_range="1 A", measure_current_autorange=False),
        node(
            "branch",
            "if",
            left="${condition}",
            operator="==",
            right=0,
            children=[module(actions=[action("measurement.current_range", "AUTO")])],
        ),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    for key in ("measure_current_range", "measure_current_autorange"):
        assert state[key].value is None and state[key].varying
        assert state[key].source == "branch"


def test_level_update_with_auto_range_does_not_preserve_fixed_range_certainty(resolve):
    r = recipe(
        baseline(source_autorange=True),
        node("update", "update_keithley_level", channel="A", mode="current", level="2 mA"),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_level"].value == "2 mA"
    assert state["source_range"].varying and state["source_range"].value is None
    assert "measure_current_range" not in state


@pytest.mark.parametrize(
    "policy,inside,after", [("on", True, False), ("on_keep", True, True), ("off", False, False)]
)
def test_native_output_transitions_surround_children(resolve, policy, inside, after):
    r = recipe(baseline(), module(output_policy=policy, children=[node("target")]), node("after"))
    assert resolve(r, "target", "keithley", "A")["output_enabled"].value is inside
    assert resolve(r, "after", "keithley", "A")["output_enabled"].value is after


def test_malformed_native_actions_cannot_silently_preserve_same(resolve):
    broken = node(
        "broken",
        device_module="keithley",
        channel="A",
        operation="configure_selected_parameters",
        parameter_actions="invalid",
    )
    r = recipe(baseline(), broken, node("target"))
    assert resolve(r, "target", "keithley", "A")["source_range"].varying


def test_autorange_mode_invalidates_previous_anritsu_manual_value(resolve):
    r = recipe(
        node("manual", "configure_anritsu_advanced", rbw_mode="manual", rbw="1 kHz"),
        module(device="anritsu", channel=None, actions=[action("advanced.rbw_mode", "auto")]),
        node("target"),
    )
    state = resolve(r, "target", "anritsu")
    assert state["rbw_mode"].value == "auto"
    assert state["rbw"].value is None and state["rbw"].varying


@pytest.mark.parametrize("policy,child_enabled", [("on_keep", False), ("off", True)])
def test_native_policies_do_not_invent_a_second_output_transition_after_children(
    resolve, policy, child_enabled
):
    r = recipe(
        baseline(),
        module(
            output_policy=policy,
            children=[node("output", "set_keithley_output", channel="A", enabled=child_enabled)],
        ),
        node("target"),
    )
    assert resolve(r, "target", "keithley", "A")["output_enabled"].value is child_enabled


def test_native_sweep_output_on_is_not_repeated_before_every_iteration(resolve):
    r = recipe(
        baseline(),
        module(
            output_policy="on_keep",
            actions=[action("source.level", "1 mA", "sweep")],
            children=[
                node("target"),
                node("off", "set_keithley_output", channel="A", enabled=False),
            ],
        ),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["output_enabled"].value is None and state["output_enabled"].varying


def test_unknown_mutation_and_missing_native_endpoint_do_not_leave_false_same(resolve):
    for mutation in (
        node("future", "future_hardware_action"),
        node(
            "missing-channel",
            device_module="keithley",
            operation="configure_selected_parameters",
            parameter_actions=[action("source.range", "1 A")],
        ),
    ):
        r = recipe(baseline(), mutation, node("target"))
        assert resolve(r, "target", "keithley", "A")["source_range"].varying


def test_incomplete_set_output_does_not_invent_enabled_default(resolve):
    r = recipe(baseline(), node("incomplete", "set_keithley_output", channel="A"), node("target"))
    assert resolve(r, "target", "keithley", "A")["output_enabled"].varying


@pytest.mark.parametrize("kind", ["update", "sweep", "ramp"])
def test_autorange_source_changes_invalidate_old_fixed_range(resolve, kind):
    change = {
        "update": node(
            "change", "configure_keithley", channel="A", mode="current", source_autorange=True
        ),
        "sweep": node(
            "change", "sweep", target="keithley.A.current", start="1 mA", stop="2 mA", points=2
        ),
        "ramp": node("change", "ramp_keithley_to_zero", channel="A"),
    }[kind]
    r = recipe(baseline(source_autorange=kind != "update"), change, node("target"))
    assert resolve(r, "target", "keithley", "A")["source_range"].varying


def test_literal_rigol_configuration_forces_output_off(resolve):
    r = recipe(
        node("on", "set_rigol_output", channel=1, enabled=True),
        node("configure", "configure_rigol", channel=1, frequency="1 kHz"),
        node("target"),
    )
    output = resolve(r, "target", "rigol", 1)["output_enabled"]
    assert output.value is False and output.source == "configure"


@pytest.mark.parametrize("policy,after", [("on", False), ("on_keep", True)])
def test_rigol_native_output_during_children_and_after(resolve, policy, after):
    r = recipe(
        node("configure", "configure_rigol", channel=1, frequency="1 kHz"),
        module(
            device="rigol",
            channel=1,
            output_policy=policy,
            actions=[action("carrier.frequency", "2 kHz")],
            children=[node("target")],
        ),
        node("after"),
    )
    assert resolve(r, "target", "rigol", 1)["output_enabled"].value is True
    assert resolve(r, "after", "rigol", 1)["output_enabled"].value is after


@pytest.mark.parametrize("prior_auto", [None, False, True])
def test_literal_range_without_authored_policy_cannot_infer_station_autorange(resolve, prior_auto):
    preceding = () if prior_auto is None else (baseline(source_autorange=prior_auto),)
    r = recipe(
        *preceding,
        node(
            "range-only-policy",
            "configure_keithley",
            channel="A",
            mode="current",
            level="1 mA",
            compliance="67 mV",
            source_range="1 mA",
        ),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    # source_range adds source_autorange to compiler changed_fields. Its value
    # comes from settings, so even an explicit preceding policy cannot supply it.
    for key in ("source_autorange", "source_range"):
        assert state[key].value is None and state[key].varying
        assert state[key].source == "range-only-policy"
    assert "measure_current_range" not in state


def test_literal_omitting_both_range_fields_preserves_prior_explicit_fixed_policy(resolve):
    r = recipe(
        baseline(source_autorange=False),
        node(
            "level-only",
            "configure_keithley",
            channel="A",
            mode="current",
            level="2 mA",
            compliance="67 mV",
        ),
        node("target"),
    )
    state = resolve(r, "target", "keithley", "A")
    assert state["source_autorange"].value is False
    assert state["source_range"].value == "10 mA"
    assert state["source_autorange"].source == state["source_range"].source == "baseline"


def test_literal_explicit_autorange_does_not_claim_an_effective_manual_range(resolve):
    r = recipe(baseline(source_autorange=True), node("target"))
    state = resolve(r, "target", "keithley", "A")
    assert state["source_autorange"].value is True
    assert state["source_range"].value is None and state["source_range"].varying
