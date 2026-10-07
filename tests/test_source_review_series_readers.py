"""Malformed/missing scalar values must not invent or shift measurements."""

import json
import math

import h5py
import pytest

from app.storage.characterization_csv_reader import CharacterizationCsvReader
from app.storage.hdf5_series_reader import Hdf5SeriesReader
from app.domain.errors import ExecutionError


def write_point(file, index, measurements, *, setpoints=None, complete=True):
    group = file.create_group(f"points/{index}")
    group.attrs["complete"] = complete
    group.create_dataset("measurements_json", data=json.dumps(measurements))
    group.create_dataset("setpoints_json", data=json.dumps(
        {"keithley.A.current": index * .001} if setpoints is None else setpoints
    ))


def test_hdf5_missing_invalid_y_preserves_gaps_and_pairing(tmp_path):
    path = tmp_path / "gaps.h5"
    with h5py.File(path, "w") as file:
        write_point(file, 0, {"voltage_v": 1})
        write_point(file, 1, {"voltage_v": "invalid"})
        write_point(file, 2, {"other": 999})
        write_point(file, 3, {"voltage_v": 4}, setpoints={})
        write_point(file, 4, {"voltage_v": 5})
        write_point(file, 5, {"voltage_v": 6}, complete=False)
        write_point(file, 6, {"uncommitted": 7})
    series = Hdf5SeriesReader.read_series(path, preferred_y_channel="voltage_v")
    assert series.point_count == len(series.x_values) == len(series.y_values) == 5
    assert series.y_values[0] == 1 and series.y_values[4] == 5
    assert all(math.isnan(series.y_values[i]) for i in (1, 2))
    assert math.isnan(series.x_values[3])
    assert series.x_values[4] == .004
    assert series.available_y_channels == ("voltage_v", "other")
    assert (series.x_unit, series.y_unit) == ("A", "V")


def test_late_channel_is_available_and_missing_preference_is_not_replaced(tmp_path):
    path = tmp_path / "late.h5"
    with h5py.File(path, "w") as file:
        write_point(file, 0, {})
        write_point(file, 1, {"power_w": .002})
    result = Hdf5SeriesReader.read_series(path, preferred_y_channel="power_w")
    assert result.y_unit == "W"
    assert math.isnan(result.y_values[0]) and result.y_values[1] == .002
    absent = Hdf5SeriesReader.read_series(path, preferred_y_channel="voltage_v")
    assert all(math.isnan(v) for v in absent.y_values)
    assert absent.y_unit == "V"


@pytest.mark.parametrize("channel,unit", [
    ("keithley.A.power_w", "W"), ("spectrum.power_dbm", "dBm"),
    ("field_t", "T"), ("magnet_field", ""), ("power", ""),
    ("resistance_ohm", "Ω"), ("rigol.1.frequency", "Hz"),
])
def test_units_come_from_registry_or_explicit_suffix(channel, unit):
    assert Hdf5SeriesReader._format_channel_label(channel)[1] == unit


def test_y_preference_does_not_match_letter_inside_unrelated_channel():
    assert Hdf5SeriesReader._select_y_channel(("temperature", "voltage_v")) == "voltage_v"


def test_csv_invalid_values_keep_pairs_and_discontinuities(tmp_path):
    path = tmp_path / "characterization.csv"
    path.write_text("# Mode: current\nDemanded_SI,Voltage_V\n.001,1\n.002,invalid\n.003,3\ninvalid,4\n.005,inf\n", encoding="utf-8")
    series = CharacterizationCsvReader.read_series(path)
    assert series.point_count == len(series.x_values) == len(series.y_values) == 5
    assert math.isnan(series.y_values[1]) and math.isnan(series.y_values[4])
    assert series.x_values[2] == .003 and series.y_values[2] == 3
    assert math.isnan(series.x_values[3])
    unavailable = CharacterizationCsvReader.read_series(path, preferred_y_channel="Absent")
    assert unavailable.point_count == 5
    assert all(math.isnan(v) for v in unavailable.y_values)
    assert unavailable.y_label == "Absent"


@pytest.mark.parametrize("kind", ["missing", "mismatch", "uncommitted"])
def test_invalid_spectrum_never_forms_a_curve(tmp_path, kind):
    path = tmp_path / "spectrum.h5"
    with h5py.File(path, "w") as file:
        trace = file.create_group("spectra/0")
        trace.create_dataset("frequency_hz", data=[1., 2.])
        if kind != "missing":
            trace.create_dataset("power_dbm", data=[-80.] if kind == "mismatch" else [-80., -79.])
        if kind != "uncommitted":
            write_point(file, 0, {})
        if kind == "uncommitted":
            assert Hdf5SeriesReader.read_series(path).is_empty
        else:
            # Direct helper checks the spectrum branch independently of scalar preference.
            if kind == "mismatch":
                with pytest.raises(ExecutionError, match="matching frequency and power"):
                    Hdf5SeriesReader._extract_spectrum_series("test", file["spectra"])
            else:
                assert Hdf5SeriesReader._extract_spectrum_series("test", file["spectra"]).is_empty
