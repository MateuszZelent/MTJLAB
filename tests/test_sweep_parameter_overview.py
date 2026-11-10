"""Offline aggregate review of authored settings against the current device tabs."""

import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from app.recipes.block_registry import ACTION_FIELDS
from app.recipes.models import Recipe, RecipeNode, load_recipe
from app.recipes.parameter_overview import (
    SweepParameterEntry,
    build_sweep_parameter_entries,
    entry_comparison,
)


def node(id, kind="sequence", children=(), otherwise=(), **data):
    return RecipeNode(id, kind, data, tuple(children), tuple(otherwise))


def recipe(*nodes, final=()):
    return Recipe(1, "Overview", node("root", children=nodes), tuple(final), "unchanged YAML")


def build(r, cards=None):
    cards = cards or {}
    return build_sweep_parameter_entries(r, lambda d, c: cards.get((d, c), {}))


def row(rows, source, key):
    return next(e for e in rows if e.source_id == source and e.parameter == key)


def test_actual_nested_recipe_completeness_and_ab_mapping():
    path = (
        Path(__file__).resolve().parents[1] / "recipes/example_nested_keithley_ab_fixed_moke2.yml"
    )
    original_source = path.read_bytes()
    r = load_recipe(path)
    cards = {
        ("keithley", "A"): {"source_level": "0 A", "source_range": "0.1 A", "nplc": "2"},
        ("keithley", "B"): {"source_level": "0 A", "source_range": "0.01 A", "nplc": "4"},
        ("anritsu", None): {"spectrum.start_frequency": "1 GHz", "advanced.rbw": "10 kHz"},
        ("moke_box", 0): {"voltage": "0 V", "calibration_id": "actual-card"},
    }
    rows = build(r, cards)
    a = row(rows, "keithley-a-initial", "source_range")
    b = row(rows, "keithley-b-initial", "source_range")
    assert (a.channel, a.card_value, a.sweep_value, entry_comparison(a)) == (
        "A",
        "0.1 A",
        "100 mA",
        "same",
    )
    assert (b.channel, b.card_value, b.sweep_value, entry_comparison(b)) == (
        "B",
        "0.01 A",
        "1 A",
        "different",
    )
    assert row(rows, "keithley-a-initial", "nplc").action == "preserve"
    assert row(rows, "keithley-b-initial", "nplc").sweep_value == "4"
    assert row(rows, "initial-moke-voltage", "voltage").sweep_value == "-14 mV"
    assert row(rows, "background", "minimum_duration").sweep_value == "30 s"
    assert row(rows, "background", "average_count").sweep_value == 4
    for key in ("reference_operation", "store_raw", "store_processed", "average_count", "trace"):
        assert row(rows, "acquire-spectrum", key).device == "anritsu"
    assert row(rows, "acquire-spectrum", "start_frequency").action == "preserve"
    assert row(rows, "measure-moke-hall", "checkpoint").sweep_value is False
    assert row(rows, "baseline-keithley-a-off", "output_enabled").sweep_value is False
    for source, count, endpoint in (
        ("keithley-a-current-sweep", 61, "14 mA"),
        ("keithley-b-current-sweep", 5, "6 mA"),
    ):
        entry = row(rows, source, "source_level")
        assert entry.action == "sweep" and entry_comparison(entry) == "different"
        assert f"{count} points" in entry.detail and endpoint in str(entry.sweep_value)
    assert row(rows, "keithley-a-ramp-zero-finally", "source_level").sweep_value == "0 A"
    assert row(rows, "keithley-b-off-finally", "output_enabled").sweep_value is False
    assert row(rows, "final-moke-hold", "voltage").sweep_value == "-14 mV"
    assert path.read_bytes() == original_source


@pytest.mark.parametrize(
    "left,right,result",
    [
        ("1000 uA", "1 mA", "same"),
        ("1e-3 A", "1 mA", "same"),
        ("1,5 mA", "0.0015 A", "same"),
        ("1 MHz", "1000 kHz", "same"),
        ("0 mA", "1e-12 A", "different"),
        ("1 mA", "1 mV", "different"),
        ("0 dBm", "1 mW", "different"),
        (True, 1, "different"),
        (None, "1 mA", "unavailable"),
        ("bad mA", "1 mA", "unavailable"),
        (float("nan"), float("nan"), "unavailable"),
        ("2", 2, "same"),
    ],
)
def test_comparison_preserves_dimensions_and_prefixes(left, right, result):
    e = SweepParameterEntry("test", None, "value", "Value", left, right, "set", "n", "n")
    assert entry_comparison(e) == result
    with pytest.raises(FrozenInstanceError):
        e.card_value = 0


def test_selected_actions_preserve_inactive_drafts_and_secondary_context():
    r = recipe(
        node(
            "before",
            "configure_keithley",
            channel="B",
            mode="current",
            level="1 mA",
            compliance="70 mV",
            source_range="10 mA",
            source_autorange=False,
        ),
        node(
            "selected",
            device_module="keithley",
            channel="B",
            source_mode="current",
            operation="configure_selected_parameters",
            output_policy="continue",
            configuration={
                "channel": "B",
                "source_mode": "current",
                "source_range": "1 A",
                "nplc": 99,
            },
            parameter_actions=[
                {"parameter_id": "source.level", "mode": "set", "value": "2 mA"},
                {"parameter_id": "measurement.nplc", "mode": "same", "value": 99},
            ],
        ),
    )
    original = deepcopy(r)
    rows = build(
        r, {("keithley", "B"): {"source_level": "2 mA", "source_range": "100 mA", "nplc": 4}}
    )
    level = row(rows, "selected", "source_level")
    assert level.card_value == "2 mA" and entry_comparison(level) == "same"
    assert "before" in level.detail and "1 mA" in level.detail
    preserved = row(rows, "selected", "source_range")
    assert preserved.action == "preserve" and preserved.sweep_value == "10 mA"
    assert preserved.card_value == "100 mA" and "before" in preserved.detail
    assert row(rows, "selected", "nplc").sweep_value == 4
    assert not any(e.sweep_value == 99 and e.action == "set" for e in rows)
    assert r == original


def test_nested_huge_sweeps_multiinterval_hysteresis_log_never_generate_points():
    inner = node(
        "inner",
        "sweep",
        target="anritsu.sg.frequency",
        segments=[
            {"start": "1 Hz", "stop": "1 GHz", "points": 10**12, "spacing": "log"},
            {"start": "1 GHz", "stop": "1 Hz", "points": 10**12, "spacing": "log"},
            {"value": "2 Hz"},
        ],
    )
    outer = node(
        "outer",
        "sweep",
        target="keithley.B.current",
        start="0 A",
        stop="1 A",
        points=10**12,
        children=[inner],
    )
    with (
        patch(
            "app.recipes.sweep_points.generate_sweep_points",
            side_effect=AssertionError("allocation"),
        ),
        patch(
            "app.recipes.sweep_points.generate_sweep_stage_points",
            side_effect=AssertionError("allocation"),
        ),
    ):
        rows = build(recipe(outer))
    assert "1000000000000 points" in row(rows, "outer", "source_level").detail
    e = row(rows, "inner", "frequency")
    assert e.device == "anritsu_sg" and e.channel is None
    assert "2000000000000 points" in e.detail and "log" in e.detail
    assert "1 GHz" in str(e.sweep_value)


def test_aliases_unknown_fields_and_repeated_configuration_origins():
    r = recipe(
        node(
            "sa", "configure_anritsu", start_frequency="1 GHz", stop_frequency="2 GHz", points=501
        ),
        node("advanced", "configure_anritsu_advanced", rbw="1 MHz", detector="RMS"),
        node("sg", "configure_anritsu_sg", frequency="3 GHz", power="-10 dBm"),
        node(
            "rigol",
            "configure_rigol",
            channel=2,
            frequency="1 MHz",
            waveform="SIN",
            high_level="1 V",
        ),
        node(
            "repeat-rigol",
            "configure_rigol_output",
            channel=2,
            polarity="normal",
            sync_enabled=True,
        ),
        node("hall", "measure_lakeshore_field", checkpoint=True),
    )
    cards = {
        ("anritsu", None): {
            "start_frequency": "1000 MHz",
            "spectrum.start_frequency": "1 GHz",
            "advanced.rbw": "1000 kHz",
            "future.setting": "show me",
        },
        ("anritsu_sg", None): {"sg.frequency": "3000 MHz", "power": "-10 dBm"},
        ("rigol", 2): {"frequency": "1000 kHz", "output_polarity": "normal", "future": 7},
        ("lakeshore", None): {"range": "AUTO", "filter": True},
    }
    rows = build(r, cards)
    assert len([e for e in rows if e.source_id == "sa" and e.parameter == "start_frequency"]) == 1
    assert entry_comparison(row(rows, "advanced", "rbw")) == "same"
    assert entry_comparison(row(rows, "sg", "frequency")) == "same"
    assert entry_comparison(row(rows, "repeat-rigol", "output_polarity")) == "same"
    assert row(rows, "rigol", "future").sweep_value == 7
    assert row(rows, "repeat-rigol", "future").sweep_value == 7
    assert row(rows, "sa", "future.setting").sweep_value == "show me"
    assert row(rows, "hall", "filter").sweep_value is True


def test_disabled_nodes_branches_and_dynamic_branches_are_visible():
    r = recipe(
        node(
            "disabled",
            disabled=True,
            children=[node("off", "set_keithley_output", channel="A", enabled=False)],
        ),
        node(
            "if",
            "if",
            condition=False,
            children=[node("skipped", "set_keithley_output", channel="B", enabled=True)],
            otherwise=[node("taken", "set_keithley_output", channel="B", enabled=False)],
        ),
        node(
            "dynamic",
            "if",
            left="${measurement.x}",
            operator=">",
            right=0,
            children=[node("maybe", "set_rigol_output", channel=1, enabled=True)],
        ),
    )
    rows = build(r)
    for source in ("off", "skipped"):
        e = row(rows, source, "output_enabled")
        assert e.enabled is False and entry_comparison(e) == "unavailable"
        assert "disabled" in e.detail.lower()
    assert row(rows, "taken", "output_enabled").enabled is True
    assert "conditional" in row(rows, "maybe", "output_enabled").detail.lower()


def test_invalid_inputs_and_unavailable_callback_do_not_require_compilation():
    r = recipe(
        node("bad", "configure_anritsu_advanced", rbw="bananas", sweep_time="NaN s"),
        node(
            "axis",
            "sweep",
            target="keithley.A.current",
            segments=[{"start": "1 mA", "stop": "2 V", "step": "0 A"}],
        ),
        node(
            "unknown",
            "sweep",
            target="new_device.endpoint.new_quantity",
            start="1 qux",
            stop="2 qux",
            points=2,
        ),
    )

    def unavailable(*_):
        raise RuntimeError("device tab not available")

    rows = build_sweep_parameter_entries(r, unavailable)
    for source, key in (
        ("bad", "rbw"),
        ("bad", "sweep_time"),
        ("axis", "source_level"),
        ("unknown", "new_quantity"),
    ):
        e = row(rows, source, key)
        assert "invalid" in e.detail.lower() or "unknown" in e.detail.lower()
        assert entry_comparison(e) == "unavailable" or e.action == "sweep"


def test_final_states_output_policies_and_no_field_loss():
    r = recipe(
        node(
            "device",
            device_module="rigol",
            channel=1,
            operation="configure_selected_parameters",
            output_policy="on",
            parameter_actions=[
                {"parameter_id": "carrier.frequency", "mode": "set", "value": "1 kHz"}
            ],
            configuration={"channel": 1, "waveform": "SIN"},
        ),
        node(
            "measure",
            "acquire_spectrum",
            average_count=3,
            inter_sweep_delay="1 s",
            processing={"operation": "difference_db", "future_option": True},
            store_raw=True,
        ),
        node("wait", "wait", duration="250 ms"),
        final=[
            node(
                "final",
                "final_state",
                device="rigol",
                channel=1,
                output="off",
                frequency="2 kHz",
                high_level="2 V",
                low_level="0 V",
            )
        ],
    )
    rows = build(r)
    assert row(rows, "device", "output_enabled").sweep_value is True
    assert any(
        e.source_id == "device" and e.parameter == "output_enabled" and e.sweep_value is False
        for e in rows
    )
    assert row(rows, "final", "frequency").action == "final"
    assert row(rows, "final", "output_enabled").sweep_value is False
    assert row(rows, "measure", "processing.future_option").sweep_value is True
    assert row(rows, "wait", "duration").device == "sequence"


@pytest.mark.parametrize(
    "segments",
    [
        [{"start": "0 A", "stop": "1 A", "points": 1}],
        [{"start": "0 A", "stop": "1 A", "step": "0 A"}],
        [{"start": "0 A", "stop": "1 A", "points": 2, "spacing": "log"}],
        [{"start": "0 A", "stop": "1 A"}],
        [],
    ],
)
def test_invalid_estimator_inputs_are_reported_instead_of_raising(segments):
    e = row(
        build(recipe(node("bad", "sweep", target="keithley.A.current", segments=segments))),
        "bad",
        "source_level",
    )
    assert "Invalid sweep" in e.detail


@pytest.mark.parametrize(
    "key,value",
    [
        ("nplc", "garbage"),
        ("nplc", -1),
        ("average_count", "garbage"),
        ("average_count", 0),
        ("points", "501"),
        ("store_raw", "yes"),
    ],
)
def test_invalid_numeric_or_boolean_fields_remain_reviewable(key, value):
    kind = (
        "configure_keithley"
        if key == "nplc"
        else "configure_anritsu"
        if key == "points"
        else "acquire_spectrum"
    )
    e = row(build(recipe(node("invalid", kind, **{key: value}))), "invalid", key)
    assert "Invalid" in e.detail
    assert e.sweep_value == value


def test_unknown_authored_fields_and_device_extension_stay_visible():
    rows = build(
        recipe(
            node(
                "new",
                "configure_future_device",
                device="future_device",
                future_value="7 qux",
                points=3,
                source_mode="future",
            )
        )
    )
    for key in ("future_value", "points", "source_mode"):
        assert row(rows, "new", key).device == "future_device"


def test_callback_once_per_endpoint_and_nested_snapshot_input_unchanged():
    values = {"spectrum": {"start_frequency": "1 GHz"}, "advanced": {"rbw": "1 kHz"}}
    original = deepcopy(values)
    requests = []

    def provider(device, channel):
        requests.append((device, channel))
        return values

    r = recipe(node("one", "acquire_spectrum"), node("two", "acquire_reference"))
    rows = build_sweep_parameter_entries(r, provider)
    assert requests == [("anritsu", None)]
    assert values == original
    assert row(rows, "one", "rbw").sweep_value == "1 kHz"


def test_unchanged_rigol_output_is_not_claimed_off_and_all_drafts_are_preserved():
    r = recipe(
        node(
            "selected",
            device_module="rigol",
            channel=2,
            operation="configure_selected_parameters",
            output_policy="unchanged",
            configuration={"channel": 2, "waveform": "DC", "frequency": "99 kHz"},
            parameter_actions=[],
        )
    )
    e = row(
        build(r, {("rigol", 2): {"output_enabled": True, "frequency": "1 kHz"}}),
        "selected",
        "output_enabled",
    )
    assert e.action == "preserve" and e.sweep_value is True


@pytest.mark.parametrize("kind", [kind for kind in ACTION_FIELDS if kind.startswith("configure_")])
def test_every_literal_configure_field_is_authored_even_without_a_device_snapshot(kind):
    data = {field: f"authored-{field}" for field in sorted(ACTION_FIELDS[kind])}
    if "channel" in data:
        data["channel"] = 2 if "rigol" in kind else 0 if "moke" in kind else "B"
    # An incomplete/invalid field is still an authored setting to review.
    rows = build(recipe(node("literal", kind, **data)))
    aliases = {"level": "source_level", "settle_time": "settling_time"}
    if kind == "configure_keithley":
        aliases["mode"] = "source_mode"
    if kind == "configure_rigol_output":
        aliases.update(mode="output_mode", polarity="output_polarity")
    for field, value in data.items():
        if field == "channel":
            assert all(e.channel == value for e in rows if e.source_id == "literal")
            continue
        candidates = [
            e for e in rows if e.source_id == "literal" and e.parameter == aliases.get(field, field)
        ]
        assert any(e.sweep_value == value and e.action == "set" for e in candidates), (kind, field)


def test_literal_analyzer_points_override_card_and_snapshot_ui_policy_is_omitted():
    rows = build(
        recipe(
            node("sa", "configure_anritsu", points=1001), node("k", "measure_keithley", channel="A")
        ),
        {
            ("anritsu", None): {"spectrum.points": 501},
            ("keithley", "A"): {"channel": "A", "output_policy": "enable_for_run", "nplc": "2"},
        },
    )
    points = row(rows, "sa", "points")
    assert (points.card_value, points.sweep_value, points.action) == (501, 1001, "set")
    assert entry_comparison(points) == "different"
    assert not any(e.parameter in {"channel", "output_policy"} for e in rows)


@pytest.mark.parametrize(
    "reference_count,reference_status,spectrum_status",
    [(4, "same", "different"), (8, "different", "same"), (None, "unavailable", "different")],
)
def test_acquisition_averaging_uses_matching_device_tab_role(
    reference_count, reference_status, spectrum_status
):
    cards = {
        "average_count": 8,
        "reference_operation": "difference_db",
    }
    if reference_count is not None:
        cards["reference_average_count"] = reference_count
    # The second case makes the reference and spectrum targets intentionally
    # differ, ensuring neither operation borrows the other's current value.
    spectrum_count = 8 if spectrum_status == "same" else 4
    r = recipe(
        node("reference", "acquire_reference", average_count=4),
        node(
            "spectrum",
            "acquire_spectrum",
            average_count=spectrum_count,
            reference_operation="difference_db",
        ),
    )
    original = deepcopy(cards)
    rows = build(r, {("anritsu", None): cards})
    reference = row(rows, "reference", "average_count")
    spectrum = row(rows, "spectrum", "average_count")
    assert reference.card_value == reference_count
    assert entry_comparison(reference) == reference_status
    assert "Current device tab: reference_average_count" in reference.detail
    assert spectrum.card_value == 8
    assert entry_comparison(spectrum) == spectrum_status
    assert "Current device tab: average_count" in spectrum.detail
    assert entry_comparison(row(rows, "spectrum", "reference_operation")) == "same"
    if reference_count is not None:
        assert row(rows, "spectrum", "reference_average_count").action == "preserve"
    assert cards == original


def test_preserved_matching_axis_retains_readable_range_without_duplicate_sweep_action():
    r = recipe(
        node(
            "axis",
            "sweep",
            target="keithley.A.current",
            segments=[
                {"start": "0 mA", "stop": "10 mA", "step": "1 mA"},
            ],
            children=[node("measure", "measure_keithley", channel="A")],
        )
    )
    rows = build(r, {("keithley", "A"): {"source_level": "0 A"}})
    inherited = row(rows, "measure", "source_level")
    assert inherited.action == "preserve"
    assert "Sweep" in inherited.sweep_value and "0 mA" in inherited.sweep_value
    assert "10 mA" in inherited.sweep_value
    assert "11 points" in inherited.detail and "axis" in inherited.detail
    assert entry_comparison(inherited) == "different"
    assert len([e for e in rows if e.action == "sweep"]) == 1


def test_actual_nested_recipe_preserved_axes_remain_readable():
    r = load_recipe(
        Path(__file__).resolve().parents[1] / "recipes/example_nested_keithley_ab_fixed_moke2.yml"
    )
    rows = build(r)
    for channel, stop in (("A", "14 mA"), ("B", "6 mA")):
        inherited = row(rows, f"measure-keithley-{channel.lower()}", "source_level")
        assert inherited.action == "preserve" and stop in inherited.sweep_value
        assert entry_comparison(inherited) == "different"


def test_native_preserved_sweep_uses_matching_parameter_and_endpoint_only():
    r = recipe(
        node(
            "native",
            device_module="rigol",
            channel=2,
            operation="configure_selected_parameters",
            output_policy="continue",
            parameter_actions=[
                {
                    "parameter_id": "carrier.frequency",
                    "mode": "sweep",
                    "segments": [{"start": "1 kHz", "stop": "10 kHz", "points": 10}],
                }
            ],
            children=[
                node("inside", "update_rigol_levels", channel=2, high_level="1 V", low_level="0 V")
            ],
        )
    )
    e = row(build(r), "inside", "frequency")
    assert e.action == "preserve" and "10 kHz" in e.sweep_value
    assert entry_comparison(e) == "different"


def test_branch_and_nonmatching_loop_variability_do_not_invent_sweep_ranges():
    r = recipe(
        node(
            "branch",
            "if",
            left="${value}",
            operator=">",
            right=0,
            children=[
                node("one", "update_keithley_level", channel="A", mode="current", level="1 mA")
            ],
            otherwise=[
                node("two", "update_keithley_level", channel="A", mode="current", level="2 mA")
            ],
        ),
        node("measure", "measure_keithley", channel="A"),
        node(
            "frequency-axis",
            "sweep",
            target="rigol.1.frequency",
            start="1 kHz",
            stop="2 kHz",
            points=2,
            children=[
                node("read", "measure_keithley", channel="B"),
                node("write", "update_keithley_level", channel="B", mode="current", level="9 mA"),
            ],
        ),
    )
    rows = build(r)
    for source in ("measure", "read"):
        e = row(rows, source, "source_level")
        assert e.sweep_value is None and entry_comparison(e) == "unavailable"
        assert "execution-dependent" in e.detail.lower()


def test_omitted_spectrum_options_show_execution_defaults_instead_of_card_preservation():
    r = recipe(node("spectrum", "acquire_spectrum"))
    original = deepcopy(r)
    rows = build(
        r,
        {
            ("anritsu", None): {
                "average_count": 8,
                "reference_operation": "difference_db",
                "trace": "TRAC2",
                "inter_sweep_delay": "2 s",
                "store_raw": False,
                "store_processed": True,
                "processing.filters": ["denoise"],
            }
        },
    )
    defaults = {
        "average_count": 1,
        "reference_operation": "none",
        "trace": "TRAC1",
        "inter_sweep_delay": "0 s",
        "store_raw": True,
        "store_processed": False,
        "processing.filters": [],
    }
    for key, value in defaults.items():
        entry = row(rows, "spectrum", key)
        assert entry.sweep_value == value and entry.action == "acquire"
        assert "Execution default" in entry.detail
        assert entry_comparison(entry) == "different"
        assert len([e for e in rows if e.source_id == "spectrum" and e.parameter == key]) == 1
    assert r == original


def test_omitted_reference_options_use_execution_defaults_and_reference_card_role():
    rows = build(
        recipe(node("reference", "acquire_reference")),
        {
            ("anritsu", None): {
                "average_count": 8,
                "reference_average_count": 4,
                "minimum_duration": "30 s",
                "purpose": "background",
            }
        },
    )
    defaults = {
        "trace": "TRAC1",
        "average_count": 1,
        "inter_sweep_delay": "0 s",
        "minimum_duration": "0 s",
        "purpose": "reference",
    }
    for key, value in defaults.items():
        entry = row(rows, "reference", key)
        assert entry.sweep_value == value and entry.action == "acquire"
        assert "Execution default" in entry.detail
    average = row(rows, "reference", "average_count")
    assert average.card_value == 4 and entry_comparison(average) == "different"


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, False),
        ({"reference_operation": "difference_db"}, True),
        ({"processing": {"filters": ["denoise"]}}, True),
        ({"processing": {"filters": []}}, False),
        ({"reference_operation": "difference_db", "store_processed": False}, False),
    ],
)
def test_processed_storage_default_follows_compiler_operation_and_filter_policy(options, expected):
    rows = build(recipe(node("spectrum", "acquire_spectrum", **options)))
    entry = row(rows, "spectrum", "store_processed")
    assert entry.sweep_value is expected and entry.action == "acquire"
    if "store_processed" not in options:
        assert "Execution default" in entry.detail


def test_explicit_acquisition_options_remain_authored_and_are_not_duplicated():
    fields = {
        "trace": "TRAC3",
        "average_count": 7,
        "inter_sweep_delay": "250 ms",
        "reference_operation": "ratio_linear",
        "store_raw": True,
        "store_processed": True,
    }
    rows = build(recipe(node("explicit", "acquire_spectrum", **fields)))
    for key, value in fields.items():
        entry = row(rows, "explicit", key)
        assert entry.sweep_value == value and "Execution default" not in entry.detail
        assert len([e for e in rows if e.source_id == "explicit" and e.parameter == key]) == 1


def test_imported_reference_count_default_is_unresolved_without_reading_reference_file():
    rows = build(
        recipe(node("import", "acquire_reference", source_file="missing-reference.h5")),
        {("anritsu", None): {"reference_average_count": 8}},
    )
    entry = row(rows, "import", "average_count")
    assert entry.action == "acquire" and entry.sweep_value is None
    assert "imported file" in entry.detail.lower()
    assert entry_comparison(entry) == "unavailable"
    assert row(rows, "import", "file_kind").sweep_value == "reference"


def test_invalid_processing_does_not_break_default_overview():
    rows = build(recipe(node("invalid", "acquire_spectrum", processing={"filters": "bad"})))
    entry = row(rows, "invalid", "store_processed")
    assert entry.sweep_value is None and "Invalid processing" in entry.detail


def test_acquisition_default_review_imports_no_qt_hardware_or_engine_modules():
    script = """
import sys
from app.recipes.models import Recipe, RecipeNode
from app.recipes.parameter_overview import build_sweep_parameter_entries
recipe = Recipe(1, 'pure', RecipeNode('spectrum', 'acquire_spectrum',
    {'processing': {'filters': ['denoise']}}), (), '')
build_sweep_parameter_entries(recipe, lambda device, channel: {})
blocked = [name for name in sys.modules if name.startswith(
    ('PySide6', 'qfluentwidgets', 'app.devices.', 'app.engine.'))]
assert not blocked, blocked
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr


def test_actual_recipe_effective_measurement_ranges_follow_fixed_source_through_level_sweeps():
    r = load_recipe(
        Path(__file__).resolve().parents[1] / "recipes/example_nested_keithley_ab_fixed_moke2.yml"
    )
    cards = {
        ("keithley", channel): {
            "source_mode": "current",
            "source_autorange": False,
            "source_range": card_range,
            "measure_current_range": "10 mA",
            "measure_current_autorange": False,
        }
        for channel, card_range in (("A", "0.1 A"), ("B", "100 mA"))
    }
    rows = build(r, cards)
    for channel, planned, comparison in (("A", "100 mA", "same"), ("B", "1 A", "different")):
        for source in (
            f"keithley-{channel.lower()}-initial",
            f"keithley-{channel.lower()}-current-sweep",
            f"measure-keithley-{channel.lower()}",
        ):
            effective = row(rows, source, "effective_measure_current_range")
            assert effective.action == "derived" and effective.sweep_value == planned
            assert entry_comparison(effective) == comparison
            assert "source_range" in effective.detail and "source_autorange" in effective.detail
        preference = row(rows, f"keithley-{channel.lower()}-initial", "measure_current_range")
        assert preference.card_value == "10 mA"


def test_effective_voltage_range_uses_resolved_actions_and_keeps_measurement_preference():
    r = recipe(
        node(
            "initial",
            "configure_keithley",
            channel="A",
            mode="voltage",
            source_range="2 V",
            source_autorange=False,
            measure_voltage_range="200 mV",
        ),
        node(
            "selected",
            device_module="keithley",
            channel="A",
            source_mode="voltage",
            operation="configure_selected_parameters",
            configuration={"source_range": "20 V"},
            parameter_actions=[{"parameter_id": "source.range", "mode": "set", "value": "4 V"}],
        ),
    )
    rows = build(
        r,
        {
            ("keithley", "A"): {
                "source_mode": "voltage",
                "source_autorange": False,
                "source_range": "4000 mV",
                "measure_voltage_range": "100 mV",
            }
        },
    )
    assert row(rows, "initial", "measure_voltage_range").sweep_value == "200 mV"
    effective = row(rows, "selected", "effective_measure_voltage_range")
    assert effective.sweep_value == "4 V" and effective.card_value == "4000 mV"
    assert entry_comparison(effective) == "same"


@pytest.mark.parametrize(
    "planned,card,expected_plan,expected_card",
    [
        (
            {"mode": "current", "source_range": "100 mA", "source_autorange": True},
            {"source_mode": "current", "source_range": "1 A", "source_autorange": False},
            None,
            "1 A",
        ),
        (
            {"mode": "current", "source_range": "100 mA"},
            {"source_mode": "current", "source_range": "1 A", "source_autorange": False},
            None,
            "1 A",
        ),
        (
            {"mode": "current", "source_range": "100 mA", "source_autorange": False},
            {"source_mode": "current", "source_range": "1 A"},
            "100 mA",
            None,
        ),
        (
            {"mode": "current", "source_range": "100 mA", "source_autorange": False},
            {"source_mode": "voltage", "source_range": "1 V", "source_autorange": False},
            "100 mA",
            None,
        ),
        (
            {"mode": "current", "source_range": "100 mA", "source_autorange": False},
            {"source_range": "1 A", "source_autorange": False},
            "100 mA",
            None,
        ),
        ({"source_range": "100 mA", "source_autorange": False}, {}, None, None),
    ],
)
def test_effective_ranges_leave_auto_or_missing_mode_policy_execution_dependent(
    planned, card, expected_plan, expected_card
):
    rows = build(
        recipe(node("initial", "configure_keithley", channel="A", **planned)),
        {("keithley", "A"): card},
    )
    effective = row(rows, "initial", "effective_measure_current_range")
    assert effective.sweep_value == expected_plan and effective.card_value == expected_card
    assert entry_comparison(effective) == "unavailable"
    assert "execution-dependent" in effective.detail.lower()


def test_acquisition_keeps_stored_measurement_preference_separate_from_effective_source_range():
    r = recipe(
        node(
            "preference",
            "configure_keithley",
            channel="A",
            mode="current",
            source_range="100 mA",
            source_autorange=False,
            measure_current_range="1 A",
            measure_current_autorange=False,
        ),
        node(
            "source-range",
            "configure_keithley",
            channel="A",
            mode="current",
            source_range="10 mA",
            source_autorange=False,
        ),
        node("acquisition", "measure_keithley", channel="A"),
    )
    rows = build(
        r,
        {
            ("keithley", "A"): {
                "source_mode": "current",
                "source_range": "10 mA",
                "source_autorange": False,
                "measure_current_range": "1 A",
                "measure_current_autorange": False,
            }
        },
    )
    preference = row(rows, "acquisition", "measure_current_range")
    autorange = row(rows, "acquisition", "measure_current_autorange")
    effective = row(rows, "acquisition", "effective_measure_current_range")
    assert preference.action == "preserve" and preference.sweep_value == "1 A"
    assert autorange.action == "preserve" and autorange.sweep_value is False
    assert effective.action == "derived" and effective.sweep_value == "10 mA"
    statuses = [entry_comparison(entry) for entry in (preference, autorange, effective)]
    assert statuses.count("same") == 3 and statuses.count("unavailable") == 0
