"""Physical DUT coordinates own their files and independent settings."""
import json

import pytest

from app.inventory import ActiveSampleTarget, InventoryStore, Sample


def test_measurements_and_notes_are_separate_per_dut(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        sample = store.save_sample(Sample(sample_id="wafer", name="wafer", rows=("20",), cols=("4", "5")))
        root = store.sample_directory(sample.sample_id)
        a = store.measurement_directory_for("wafer", "sweeps", row="20", col="4")
        b = store.measurement_directory_for("wafer", "sweeps", row="20", col="5")
        assert a == root / "devices" / "R20C4" / "measurements" / "sweeps"
        assert b == root / "devices" / "R20C5" / "measurements" / "sweeps"
        assert not (root / "measurements" / "sweeps").exists()
        store.save_device_settings("wafer", "20", "4", {"nominal_resistance_ohm": 1200})
        sample = sample.with_cell_update("20", "4", notes="Only R20C4", state="good")
        store.save_sample(sample)
        assert store.device_settings("wafer", "20", "4") == {"nominal_resistance_ohm": 1200}
        assert store.device_settings("wafer", "20", "5") == {}
        assert json.loads((a.parent.parent / "device.json").read_text())["notes"] == "Only R20C4"
        assert json.loads((b.parent.parent / "device.json").read_text())["notes"] == ""
        single = store.measurement_directory_for("wafer", "single_measurements", "Anritsu_MS2830A", row="20", col="4")
        assert single == a.parent / "single_measurements" / "Anritsu_MS2830A"
        with pytest.raises(ValueError, match="Unknown"):
            store.measurement_directory_for("wafer", "sweeps", row="21", col="4")
    finally:
        store.close()


def test_missing_coordinate_is_explicitly_unassigned(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        store.save_sample(Sample(sample_id="wafer", name="wafer"))
        directory = store.measurement_directory_for("wafer", "sweeps")
        assert directory.parent.parent.name == "unassigned"
        with pytest.raises(ValueError, match="both"):
            store.device_directory_for("wafer", "20", None)
    finally:
        store.close()


def test_active_dut_settings_survive_restart_and_do_not_leak(tmp_path):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    store.save_sample(Sample(sample_id="wafer", name="wafer", rows=("20",), cols=("4", "5")))
    store.save_device_settings("wafer", "20", "4", {"note": "A"})
    store.set_active_target(ActiveSampleTarget(sample_id="wafer", row="20", col="4"))
    assert store.get_active_target().device_settings == {"note": "A"}
    store.close()
    store = InventoryStore(database)
    try:
        assert store.get_active_target().device_settings == {"note": "A"}
        store.set_active_target(ActiveSampleTarget(sample_id="wafer", row="20", col="5"))
        assert store.get_active_target().device_settings == {}
        store.save_device_settings("wafer", "20", "5", {"note": "B"})
        assert store.get_active_target().device_settings == {"note": "B"}
        assert store.device_settings("wafer", "20", "4") == {"note": "A"}
    finally:
        store.close()
