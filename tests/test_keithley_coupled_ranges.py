"""2600A Rev. E range coupling, retained preferences and fault regressions."""

import os
from dataclasses import replace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationPanel,
    KeithleyConfigurationSnapshot,
)
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.engine.compiler import RecipeCompiler
from app.safety.keithley import KeithleySourceRequest, validate_keithley_range_pair
from app.settings.models import StationSettings
from tests.helpers import simulation_settings


def settings_for(channel="A", auto=False):
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    for ch in ("A", "B"):
        raw["devices"]["keithley"]["safety"]["channels"][ch]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["channels"][ch]["defaults"]["source_autorange"] = (
            auto if ch == channel else False
        )
    raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]["measured_current_trip"]["min"] = "-2 mA"
    return StationSettings.model_validate(raw)


def request_for(channel="A", mode="current", source_auto=False, measure_auto=False):
    kwargs = (
        {
            "measure_current_autorange": measure_auto,
            "measure_current_range_si": None if measure_auto else 1e-6,
        }
        if mode == "current"
        else {
            "measure_voltage_autorange": measure_auto,
            "measure_voltage_range_si": None if measure_auto else 0.1,
        }
    )
    return KeithleySourceRequest(
        channel,
        mode,
        100e-6 if mode == "current" else 0.05,
        0.05 if mode == "current" else 0.001,
        source_autorange=source_auto,
        source_range_si=None if source_auto else (0.01 if mode == "current" else 1.0),
        **kwargs,
    )


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
@pytest.mark.parametrize("source_auto", [False, True])
@pytest.mark.parametrize("measure_auto", [False, True])
def test_coupled_readback_is_valid_through_configure_enable_measure_and_update(
    channel, mode, source_auto, measure_auto
):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(
        settings_for(channel, source_auto), session_factory=FakeVisaSessionFactory(session)
    )
    adapter.connect()
    request = request_for(channel, mode, source_auto, measure_auto)
    assert adapter.configure_source(request) == request
    retained = session.programmed[
        f"smu{channel.lower()}.measure.range{'i' if mode == 'current' else 'v'}"
    ]
    adapter.set_output(channel, True)
    ranges = adapter.last_range_readback(channel)
    before_measure = len(session.commands)
    measurement = adapter.measure(channel)
    assert measurement.range_readback is None
    assert adapter.last_range_readback(channel) == ranges
    assert not any(".range" in cmd or ".autorange" in cmd for cmd in session.commands[before_measure:])
    key = "measure_current_range_a" if mode == "current" else "measure_voltage_range_v"
    assert ranges[key] == ranges["source_range_si"]
    assert ranges["kind"] == "configuration_readback"
    assert adapter.last_source_request(channel) == request
    if not measure_auto:
        assert (
            session.programmed[
                f"smu{channel.lower()}.measure.range{'i' if mode == 'current' else 'v'}"
            ]
            == retained
        )
    before = len(session.commands)
    adapter.update_source_level(channel, mode=mode, level_si=request.level_si * 0.5)
    assert not any("OUTPUT_OFF" in command for command in session.commands[before:])
    if not source_auto:
        assert not any(".range" in cmd or ".autorange" in cmd for cmd in session.commands[before:])
    assert not any(".nplc" in cmd or ".sense" in cmd for cmd in session.commands[before:])
    adapter.set_output(channel, False)


@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_fake_and_simulator_retain_hidden_preference_after_function_change(mode):
    for session in (FakeVisaSession(), KeithleySimulator()):
        suffix = "i" if mode == "current" else "v"
        function = "OUTPUT_DCAMPS" if suffix == "i" else "OUTPUT_DCVOLTS"
        other = "OUTPUT_DCVOLTS" if suffix == "i" else "OUTPUT_DCAMPS"
        fixed, retained = (0.01, 0.001) if suffix == "i" else (1.0, 0.1)
        session.write(f"smua.source.func = smua.{function}")
        session.write(f"smua.source.range{suffix} = {fixed}")
        session.write(f"smua.measure.range{suffix} = {retained}")
        assert float(session.query(f"print(smua.measure.range{suffix})")) == fixed
        assert session.query(f"print(smua.measure.autorange{suffix})") in (
            "0",
            "smua.AUTORANGE_OFF",
        )
        session.write(f"smua.source.func = smua.{other}")
        assert float(session.query(f"print(smua.measure.range{suffix})")) == retained


@pytest.mark.parametrize("bad", ["0.001", "nan", "0", "-1", "0.002", "garbage"])
def test_invalid_coupled_readback_shuts_down_active_output(bad):
    session = FakeVisaSession(
        responses={
            "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
            "print(errorqueue.count)": "0",
        }
    )
    adapter = KeithleyAdapter(settings_for(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_source(request_for())
    adapter.set_output("A", True)
    session.responses["print(smua.measure.rangei)"] = bad
    with pytest.raises((DeviceError, SafetyViolation)):
        adapter.assert_output_state("A", expected_enabled=True)
    assert session.query("print(smua.source.output)") == "0"
    assert adapter.state in (DeviceState.FAULT, DeviceState.UNKNOWN)


def test_unqualified_highc_and_follow_limit_are_not_treated_as_auto_on():
    session = FakeVisaSession(
        responses={
            "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
            "print(errorqueue.count)": "0",
            "print(smua.source.highc)": "1",
        }
    )
    adapter = KeithleyAdapter(settings_for(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    before = len(session.writes)
    with pytest.raises(SafetyViolation, match="high-C"):
        adapter.configure_source(request_for())
    assert not any(" = " in cmd for cmd in session.writes[before:])
    session.responses["print(smua.source.highc)"] = "0"
    session.responses["print(smua.measure.autorangei)"] = "2"
    with pytest.raises(DeviceError, match="autorangei"):
        adapter.configure_source(request_for(measure_auto=True))


@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_measure_only_uses_existing_hardware_function_without_enabling(mode):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_source(request_for(mode=mode))
    before = len(session.commands)
    request = replace(
        request_for(mode=mode),
        mode="measure_only",
        level_si=0,
        compliance_si=0,
        source_range_si=None,
    )
    adapter.configure_source(request)
    assert adapter.last_range_readback("A")["source_mode"] == mode
    assert not any("OUTPUT_ON" in cmd for cmd in session.commands[before:])


@pytest.mark.parametrize(
    "mode,source,measurement", [("current", 3.0, 40.0), ("voltage", 40.0, 3.0)]
)
def test_incompatible_cross_function_ranges_are_rejected(mode, source, measurement):
    with pytest.raises(SafetyViolation, match="incompatible"):
        validate_keithley_range_pair(mode, source, measurement)


def test_compiler_and_characterization_keep_preferences_while_accepting_source_range(tmp_path):
    settings = settings_for()
    request = RecipeCompiler(settings)._compile_keithley(
        {
            "channel": "A",
            "mode": "current",
            "level": "100 uA",
            "compliance": "50 mV",
            "source_range": "10 mA",
            "measure_current_autorange": False,
            "measure_current_range": "1 uA",
        },
        "coupled",
    )["request"]
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    config = CharacterizationSweepConfig(
        channel="A",
        start_level_si=50e-6,
        stop_level_si=100e-6,
        points_count=2,
        dwell_time_s=0,
        compliance_si=0.05,
        source_range_si=0.01,
        measure_current_autorange=False,
        measure_current_range_si=1e-6,
    )
    KeithleyCharacterizationRunner.validate_preflight(config, settings)
    applied = adapter.configure_source(request)
    KeithleyCharacterizationRunner.assert_applied_configuration_matches_request(request, applied)
    adapter.set_compliance_policy("A", "stop")
    dataset = KeithleyCharacterizationRunner.run_sweep(adapter, config)
    assert len(dataset.points) == 2
    assert dataset.points[0].range_readback is None
    assert dataset.config.measure_current_range_si == 1e-6
    import json
    from dataclasses import asdict

    from app.devices.keithley_2600.characterization.field_reader import load_field_dataset
    from app.devices.keithley_2600.characterization.field_storage import _json_values

    stored = _json_values(asdict(dataset))
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(stored, allow_nan=False), encoding="utf-8")
    restored = load_field_dataset(path)
    assert restored.points[0].range_readback == dataset.points[0].range_readback
    # Historical files with per-point readback remain readable.
    stored["points"][0]["range_readback"] = adapter.last_range_readback("A")
    path.write_text(json.dumps(stored, allow_nan=False), encoding="utf-8")
    assert load_field_dataset(path).points[0].range_readback == adapter.last_range_readback("A")
    for point in stored["points"]:
        point.pop("range_readback")
    path.write_text(json.dumps(stored, allow_nan=False), encoding="utf-8")
    assert load_field_dataset(path).points[0].range_readback is None


def test_ui_displays_coupling_without_overwriting_preferences(tmp_path):
    from pathlib import Path

    from PySide6.QtGui import QFont, QFontDatabase

    app = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        path = Path("C:/Windows/Fonts") / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Segoe UI", 10))
    panel = KeithleyConfigurationPanel(settings_for())
    panel.load_snapshot(
        KeithleyConfigurationSnapshot(
            channel="A",
            source_range="10 mA",
            measure_current_autorange=False,
            measure_current_range="1 uA",
            measure_voltage_autorange=False,
            measure_voltage_range="100 mV",
        )
    )
    panel.resize(1100, 760)
    panel.show()
    panel.advanced_ranges_dialog.show()
    app.processEvents()
    try:
        assert not panel.measure_current_range.isVisible()
        assert panel.measure_voltage_range.isVisible()
        assert "from source" in panel.coupled_range_note.text()
        assert panel.snapshot().measure_current_range == "1 uA"
        panel.mode.setCurrentText("voltage")
        app.processEvents()
        assert panel.measure_current_range.isVisible()
        assert not panel.measure_voltage_range.isVisible()
        assert panel.measure_current_range.text() == "1 uA"
        panel.source_range.setText("1 V")
        for width in (1100, 800):
            panel.resize(width, 760)
            app.processEvents()
            assert panel.coupled_range_note.isVisible()
            assert panel.coupled_range_note.width() > 100
            panel.advanced_ranges_dialog.resize(width, 560)
            app.processEvents()
            assert panel.advanced_ranges_dialog.grab().save(
                str(tmp_path / f"coupled-ranges-modal-{width}.png")
            )
    finally:
        panel.deleteLater()
        app.processEvents()


def test_source_auto_changes_range_at_level_update_without_output_cycle():
    session = KeithleySimulator()
    adapter = KeithleyAdapter(
        settings_for(auto=True), session_factory=FakeVisaSessionFactory(session)
    )
    adapter.connect()
    adapter.configure_source(replace(request_for(source_auto=True), level_si=1e-6))
    adapter.set_output("A", True)
    initial = adapter.last_range_readback("A")["source_range_si"]
    before = len(session.commands)
    adapter.update_source_level("A", mode="current", level_si=10e-6)
    assert adapter.last_range_readback("A")["source_range_si"] > initial
    assert adapter.last_range_readback("A")["measure_current_range_a"] == 1e-5
    assert adapter.measure("A").range_readback is None
    assert not any("OUTPUT_OFF" in cmd for cmd in session.commands[before:])
    adapter.set_output("A", False)


@pytest.mark.parametrize("auto", [False, True])
def test_unchanged_level_checks_level_without_ranges_or_full_configuration(auto):
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(auto=auto), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    request = replace(request_for(source_auto=auto), level_si=1e-6)
    adapter.configure_source(request)
    adapter.set_output("A", True)
    before = len(session.commands)
    assert adapter.update_source_level("A", mode="current", level_si=request.level_si) == request.level_si
    commands = session.commands[before:]
    assert "print(smua.source.leveli)" in commands
    assert not any(" = " in cmd or ".range" in cmd or ".autorange" in cmd or ".nplc" in cmd for cmd in commands)
    # A stale cached setpoint must not conceal a different actual level.
    session.write("smua.source.leveli = 2e-6")
    with pytest.raises(DeviceError, match="source-level readback"):
        adapter.update_source_level("A", mode="current", level_si=request.level_si)
    assert session.query("print(smua.source.output)") == "0"
    assert adapter.state in (DeviceState.FAULT, DeviceState.UNKNOWN)


def test_range_query_timeout_after_auto_level_update_confirms_outputs_off():
    from unittest.mock import patch

    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(auto=True), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_source(request_for(source_auto=True))
    adapter.set_output("A", True)
    query = session.query

    def interrupted(command):
        if command.startswith("print(smua.source.rangei,"):
            raise TimeoutError("range readback timed out")
        return query(command)

    with patch.object(session, "query", side_effect=interrupted), pytest.raises(TimeoutError):
        adapter.update_source_level("A", mode="current", level_si=50e-6)
    assert session.query("print(smua.source.output)") == "0"
    assert adapter.state in (DeviceState.FAULT, DeviceState.UNKNOWN)


def test_highc_rejection_during_reconfiguration_shuts_down_active_output():
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_source(request_for())
    adapter.set_output("A", True)
    session.programmed["smua.source.highc"] = "1"
    with pytest.raises(SafetyViolation, match="high-C"):
        adapter.configure_source(request_for())
    assert session.query("print(smua.source.output)") == "0"
    assert adapter.state in (DeviceState.FAULT, DeviceState.UNKNOWN)


def test_readback_import_retains_coupled_preference_and_checks_source_auto():
    from app.devices.keithley_2600.ui.page import KeithleyPage, _KeithleyReadbackDialog

    app = QApplication.instance() or QApplication([])
    settings = settings_for()
    adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(KeithleySimulator()))
    adapter.connect()
    adapter.configure_source(request_for())
    readback = adapter.read_configuration()
    page = KeithleyPage(Mock(), settings)
    snapshot = KeithleyConfigurationSnapshot(
        channel="A",
        source_range="10 mA",
        measure_current_autorange=False,
        measure_current_range="1 uA",
    )
    page._channel_form_snapshots["A"] = snapshot
    try:
        page._assign_configuration_readback(readback, "A", "ALL")
        assert page._channel_form_snapshots["A"].measure_current_range == "1 uA"
        configured = _KeithleyReadbackDialog._snapshot_values(
            replace(snapshot, source_autorange=True)
        )
        hardware = next(ch for ch in readback.channels if ch.channel == "A")
        actual = _KeithleyReadbackDialog._channel_values(replace(hardware, source_autorange=True))
        assert _KeithleyReadbackDialog._values_match("Active measure I range", actual, configured)
        actual["Active measure I range"] = "1 mA"
        assert not _KeithleyReadbackDialog._values_match(
            "Active measure I range", actual, configured
        )
    finally:
        page.deleteLater()
        app.processEvents()
