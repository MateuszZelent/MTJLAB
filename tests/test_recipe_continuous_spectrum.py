import json
from itertools import pairwise

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.domain.errors import ConfigurationError, DeviceError
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.storage.hdf5_reader import Hdf5RunReader, iter_recipe_spectrum_sweeps
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute


def test_recipe_live_reference_averages_frames_without_triggering(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: baseline, type: acquire_reference, average_count: 32, acquisition_mode: continuous_live}
""")
    fetch = AnritsuAdapter.fetch_current_trace_fast
    frames = []

    def capture(adapter, trace):
        frame = fetch(adapter, trace)
        frames.append(frame.powers_dbm)
        return frame

    def forbid_trigger(*_args, **_kwargs):
        raise AssertionError("Continuous reference issued an individual trigger.")

    monkeypatch.setattr(AnritsuAdapter, "fetch_current_trace_fast", capture)
    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", forbid_trigger)
    path = tmp_path / "live-reference.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert len(frames) == 33  # The first read establishes the initial buffer contents.
    reference = Hdf5RunReader.reference(path, 0)
    assert reference.average_count == 32
    assert reference.source_sweep_indices == tuple(range(32))
    recorded = [record for _, _, record in iter_recipe_spectrum_sweeps(path)]
    assert len(recorded) == 32
    assert all(record.role == SpectrumFrameRole.REFERENCE and record.evidence == SweepEvidence.UNKNOWN
               and record.acquisition_mode == "continuous_live" for record in recorded)
    expected_w = np.mean(10 ** ((np.asarray(frames[1:]) - 30) / 10), axis=0)
    np.testing.assert_allclose(10 ** ((np.asarray(reference.powers_dbm) - 30) / 10), expected_w)
    with h5py.File(path) as archive:
        metadata = json.loads(archive["references/0"].attrs["acquisition_metadata_json"])
        assert metadata["acquisition_mode"] == "continuous_live"


def test_recipe_rejects_correction_between_different_acquisition_modes(tmp_path):
    with pytest.raises(ConfigurationError, match="matching acquisition modes"):
        compile_source(audit_settings(tmp_path), """
    - {id: baseline, type: acquire_reference, acquisition_mode: continuous_live}
    - {id: signal, type: acquire_spectrum, reference_operation: difference_db}
""")


def test_recipe_continuous_point_archives_32_distinct_frames_and_linear_mean(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: acquire-live, type: acquire_spectrum, average_count: 32, acquisition_mode: continuous_live}
""")
    fetch = AnritsuAdapter.fetch_current_trace_fast
    last = None
    calls = 0

    def fetch_with_duplicates(adapter, trace):
        nonlocal last, calls
        calls += 1
        if calls % 2:
            last = fetch(adapter, trace)
        return last

    def forbid_trigger(*_args, **_kwargs):
        raise AssertionError("Continuous acquisition issued an individual trigger.")

    monkeypatch.setattr(AnritsuAdapter, "fetch_current_trace_fast", fetch_with_duplicates)
    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", forbid_trigger)
    path = tmp_path / "live.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert result.error is None
    assert result.stored_points == 1 and calls == 65

    recorded = [record for _, _, record in iter_recipe_spectrum_sweeps(path)]
    assert len(recorded) == 32
    assert all(record.evidence == SweepEvidence.UNKNOWN and record.acquisition_mode == "continuous_live"
               for record in recorded)
    assert all(not np.array_equal(left.powers_dbm, right.powers_dbm)
               for left, right in pairwise(recorded))
    with h5py.File(path) as archive:
        metadata = json.loads(archive["points/0/metadata_json"].asstr()[()])
        assert metadata["raw_recipe_sweep_indices"] == list(range(32))
        expected_w = np.mean([10 ** ((record.powers_dbm - 30) / 10) for record in recorded], axis=0)
        np.testing.assert_allclose(10 ** ((archive["spectra/0/power_dbm"][:] - 30) / 10), expected_w)
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors


def test_failed_continuous_block_retains_raw_frames_without_a_partial_mean(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """
    - {id: acquire-live, type: acquire_spectrum, average_count: 32, acquisition_mode: continuous_live}
""")
    fetch = AnritsuAdapter.fetch_current_trace_fast
    calls = 0
    cleaned_up = []
    stop_live = AnritsuAdapter.stop_live

    def fail_mid_capture(adapter, trace):
        nonlocal calls
        calls += 1
        if calls > 6:
            raise DeviceError("injected Live read failure")
        return fetch(adapter, trace)

    def observe_cleanup(adapter):
        was_live = adapter.live
        stop_live(adapter)
        if was_live:
            cleaned_up.append(not adapter.live)

    monkeypatch.setattr(AnritsuAdapter, "fetch_current_trace_fast", fail_mid_capture)
    monkeypatch.setattr(AnritsuAdapter, "stop_live", observe_cleanup)
    path = tmp_path / "failed-live.h5"
    result, _ = execute(settings, plan, path, monkeypatch)
    assert "injected Live read failure" in result.error
    assert result.stored_points == 0 and cleaned_up and all(cleaned_up)
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 5
    with h5py.File(path) as archive:
        assert archive["run"].attrs["status"] == "faulted"
        assert len(archive["spectra"]) == len(archive["_pending"]) == 0
