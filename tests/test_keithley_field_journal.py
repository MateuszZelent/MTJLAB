"""Raw journal recovery is diagnostic and never completes or resumes acquisition."""

import csv
from dataclasses import replace
import json

import pytest

from app.devices.keithley_2600.characterization.field_journal import export_field_journal
from app.devices.keithley_2600.characterization.field_reader import load_field_series
from tests.test_keithley_field_worker import make_worker


def test_complete_journal_matches_committed_curves(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.run()
    calls = list(device.calls)
    series = load_field_series(worker.directory)
    manifest = (worker.directory / "series.json").read_bytes()
    result = export_field_journal(series)
    with (worker.directory / result["csv"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows and all(row["record_state"] == "committed_dataset" for row in rows)
    assert not result["incomplete_final_record"]
    assert device.calls == calls
    assert (worker.directory / "series.json").read_bytes() == manifest


def test_interrupted_after_raw_reading_keeps_it_unqualified(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    journal = worker.directory / "events.jsonl"
    records = journal.read_bytes().splitlines(keepends=True)
    end = next(i for i, line in enumerate(records) if json.loads(line)["kind"] == "sample_raw")
    journal.write_bytes(b"".join(records[:end + 1]) + b'{"kind":')
    curves = (replace(series.curves[0], dataset=None), *series.curves[1:])
    series = replace(series, curves=curves)
    result = export_field_journal(series)
    assert result["incomplete_final_record"]
    assert result["raw_readings"] == 1
    with (worker.directory / result["csv"]).open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["record_state"] == "raw_only_field_after_unconfirmed"
    assert float(row["measured_current_a"]) == pytest.approx(1e-6)


def test_malformed_complete_record_does_not_replace_previous_export(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    target = worker.directory / "journal_readings.csv"
    target.write_bytes(b"previous")
    (worker.directory / "events.jsonl").write_bytes(b'{broken}\n')
    with pytest.raises(ValueError):
        export_field_journal(series)
    assert target.read_bytes() == b"previous"
