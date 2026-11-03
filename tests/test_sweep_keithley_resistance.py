"""Measured-pair DC resistance survives private/public storage without extra I/O."""

import hashlib

import h5py
import numpy as np
import pytest

from app.devices.keithley_2600.adapter import KeithleyMeasurement
from app.domain.models import MeasurementPoint
from app.engine.compiler import PlanAction
from app.engine.runner import RecipeRunner
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader, ThatecCompatibilityValidator
from app.ui.results.scalar_data import read_scalar_columns
from tests.test_adapters_and_runner import MemoryWriter


class ReadOnlyMeasurementProbe:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def measure(self, channel):
        self.calls.append(channel)
        return self.result


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("voltage,current,connected,expected", [
    (.15, .0015, True, 100.), (-.15, -.0015, True, 100.),
    (-.15, .0015, True, 100.), (0., .0015, True, 0.),
    (.15, 0., True, None), (.15, 1e-15, True, None), (.15, -1e-16, True, None),
    (.15, .0015, False, None), (1e308, 1e-14, True, None),
])
def test_derived_resistance_is_read_only_and_replaces_stale_sample(channel, voltage, current, connected, expected):
    probe = ReadOnlyMeasurementProbe(KeithleyMeasurement(channel, voltage, current, 0., True,
                                                        measurement_path_connected=connected))
    runner = RecipeRunner(rigol=None, keithley=probe, anritsu=None, writer=MemoryWriter())
    prefix = f"keithley.{channel}"
    measurements = {f"{prefix}.resistance_ohm": 999., f"{prefix}.resistance_valid": 1.,
                    f"keithley.{'B' if channel == 'A' else 'A'}.resistance_ohm": 123.}
    runner._execute_impl(PlanAction("read", "measure_keithley", {"channel": channel}, {}), measurements)
    assert probe.calls == [channel]
    assert measurements[f"{prefix}.resistance_valid"] == float(expected is not None)
    if expected is None:
        assert f"{prefix}.resistance_ohm" not in measurements
    else:
        assert measurements[f"{prefix}.resistance_ohm"] == pytest.approx(expected)
    assert measurements[f"keithley.{'B' if channel == 'A' else 'A'}.resistance_ohm"] == 123.


@pytest.mark.parametrize("channel", ["A", "B"])
def test_measure_block_persists_resistance_units_validity_and_gaps(tmp_path, channel):
    path = tmp_path / "resistance.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: resistance\nroot: {id: read, type: measure_keithley, channel: " + channel + "}\n",
                          settings_source="profile: {id: test, name: Test}\n", plan_hash="a" * 64,
                          device_idn={"keithley": "KEITHLEY,2602A,1,1"}, csv_summary_path=path.with_suffix(".csv"))
    probe = ReadOnlyMeasurementProbe(None)
    runner = RecipeRunner(rigol=None, keithley=probe, anritsu=None, writer=writer)
    measurements = {}
    # Begin with undefined R, then valid, then undefined again: late public
    # row creation must pad earlier samples, and stale R must not survive.
    for index, (voltage, current) in enumerate(((.1, 0.), (-.15, -.0015), (.1, 0.), (.3, .0015))):
        probe.result = KeithleyMeasurement(channel, voltage, current, voltage * current, True)
        runner._execute_impl(PlanAction("read", "measure_keithley", {"channel": channel}, {}), measurements)
        writer.append(MeasurementPoint(index=index, setpoints={f"keithley.{channel}.current": .0015},
            measurements=dict(measurements)), device_states=runner._device_states)
    writer.close("completed")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    points = Hdf5RunReader.points(path)
    key = f"keithley.{channel}.resistance_ohm"
    assert [point.measurements.get(key) for point in points] == [None, 100., None, 200.]
    assert [point.measurements[f"keithley.{channel}.resistance_valid"] for point in points] == [0., 1., 0., 1.]
    actual = points[1].device_states["keithley"][f"measurement_{channel}"]["actual"]
    assert actual["resistance_definition"] == "abs(measured_voltage_v / measured_current_a)"
    assert actual["resistance_min_abs_current_a"] == 1e-15 and actual["resistance_valid"]
    assert points[2].device_states["keithley"][f"measurement_{channel}"]["actual"]["resistance_ohm"] is None
    run = ThatecRunReader.describe(path)
    for private_points in ((), points):
        column = next(column for column in read_scalar_columns(path, run, private_points)
                      if column.id == f"measurement:{key}")
        assert column.unit == "Ω"
        np.testing.assert_allclose(column.values, [np.nan, 100., np.nan, 200.], equal_nan=True)
    assert key in path.with_suffix(".csv").read_text(encoding="utf-8")
    with h5py.File(path, "r") as file:
        assert file["run"].attrs["status"] == "completed" and len(file["_pending"]) == 0
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_compiled_recipe_measure_blocks_save_both_channels_at_checkpoint(tmp_path, monkeypatch):
    from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute

    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: a-config, type: configure_keithley, channel: A, mode: current, level: '0.2 mA', compliance: '20 mV', source_range: '10 mA'}
    - {id: b-config, type: configure_keithley, channel: B, mode: current, level: '-4 mA', compliance: '60 mV', source_range: '100 mA'}
    - {id: a-on, type: set_keithley_output, channel: A, enabled: true}
    - {id: b-on, type: set_keithley_output, channel: B, enabled: true}
    - {id: a-read, type: measure_keithley, channel: A}
    - {id: b-read, type: measure_keithley, channel: B}
    - {id: save, type: checkpoint}
""")
    path = tmp_path / "compiled-resistance.h5"
    result, _waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    point = Hdf5RunReader.point(path, 0)
    for channel in ("A", "B"):
        prefix = f"keithley.{channel}"
        assert point.measurements[f"{prefix}.resistance_valid"] == 1.
        assert point.measurements[f"{prefix}.resistance_ohm"] == pytest.approx(
            abs(point.measurements[f"{prefix}.voltage_v"] / point.measurements[f"{prefix}.current_a"]))
