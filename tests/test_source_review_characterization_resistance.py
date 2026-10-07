"""Undefined measured resistance stays missing through acquisition and CSV."""
from dataclasses import replace
import math

import pytest

from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.storage.characterization_csv_reader import CharacterizationCsvReader
from tests.test_keithley_characterization_runner import _MockKeithleyDevice


@pytest.mark.parametrize("current", [0., 1e-13, -1e-13])
def test_unresolved_measured_current_is_not_replaced_by_demanded_current(tmp_path, current):
    device = _MockKeithleyDevice()
    measure = device.measure
    device.measure = lambda channel: replace(measure(channel), current_a=current,
        voltage_v=.001, power_w=abs(.001 * current))
    config = CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
        points_count=3, source_range_si=.01, dwell_time_s=0)
    dataset = KeithleyCharacterizationRunner.run_sweep(device, config)
    assert len(dataset.points) == 3
    assert all(point.measured_current_a == current for point in dataset.points)
    assert all(math.isnan(point.true_resistance_ohm) for point in dataset.points)
    assert [p.apparent_resistance_ohm for p in dataset.points] == pytest.approx([1000, 500, 1000/3])
    path = KeithleyDataExporter.export_csv(dataset, tmp_path / "measured.csv")
    true_r = CharacterizationCsvReader.read_series(path, preferred_y_channel="True_Resistance_Ohm")
    apparent = CharacterizationCsvReader.read_series(path, preferred_y_channel="Apparent_Resistance_Ohm")
    assert true_r.point_count == 3 and all(math.isnan(value) for value in true_r.y_values)
    assert apparent.y_values == pytest.approx([1000, 500, 1000/3])
