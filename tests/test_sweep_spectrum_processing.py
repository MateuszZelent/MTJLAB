"""Compiled sweep DSP, immutable references and durable source evidence."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime

import h5py
import numpy as np
import pytest
import yaml

from app.devices.anritsu_ms2830a import AnritsuAdapter, ReferenceSpectrum
from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.simulators import SimulatedVisaFactory
from app.domain.errors import ConfigurationError
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.engine import RecipeCompiler
from app.recipes import parse_recipe_text
from app.recipes.spectrum_processing import parse_processing, processing_mapping
from app.spectrum.analysis import clean_spectrum_pipeline
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.hdf5_reader import Hdf5RunReader, iter_recipe_spectrum_sweeps
from app.storage.reference_store import ReferenceHdf5Store
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.helpers import simulation_settings
from tests.test_recipe_raw_sweeps import SOURCE, execute


def recipe_with(**fields):
    data = yaml.safe_load(SOURCE)
    data["root"]["children"][-1].update(fields)
    return yaml.safe_dump(data)


@pytest.mark.parametrize(
    "processing",
    [
        {"filters": ["unknown"]},
        {"filters": ["denoise", "denoise"]},
        {"parameters": {"narrow_max_width": 1000}},
        {"parameters": {"denoise_window": 4}},
        {"parameters": {"emi_min_frames": 25}},
        {"parameters": {"emi_threshold_db": float("nan")}},
        {"parameters": {"narrow_threshold_sigma": True}},
    ],
)
def test_processing_policy_rejects_ambiguous_or_unsupported_parameters(processing):
    with pytest.raises(ValueError):
        parse_processing(processing)


def test_processing_units_and_protection_roundtrip():
    modes, parameters = parse_processing(
        {
            "filters": ["denoise", "narrow_reject"],
            "parameters": {
                "narrow_max_width": "125.125 Hz",
                "protected_bands": [["1.000125 MHz", "1.00025 MHz"]],
            },
        }
    )
    assert parse_processing(processing_mapping(modes, parameters)) == (modes, parameters)


@pytest.mark.parametrize(
    "fields",
    [
        {"processing": {"filters": ["emi_reject"]}},
        {
            "average_count": 5,
            "reference_operation": "subtract_power_signed",
            "processing": {"filters": ["emi_reject"]},
        },
        {"store_processed": False, "processing": {"filters": ["denoise"]}},
    ],
)
def test_preflight_rejects_unexecutable_processing(fields):
    with pytest.raises(ConfigurationError):
        RecipeCompiler(simulation_settings()).compile(parse_recipe_text(recipe_with(**fields)))


@pytest.mark.parametrize("changed", [True, False])
def test_reconfiguration_requires_new_reference_only_when_settings_change(changed):
    data = yaml.safe_load(SOURCE)
    config = dict(data["root"]["children"][0], id="reconfigure")
    if changed:
        config["reference_level"] = "-10 dBm"
    data["root"]["children"].insert(2, config)
    compiler = RecipeCompiler(simulation_settings())
    if changed:
        with pytest.raises(ConfigurationError, match="after the last analyzer settings change"):
            compiler.compile(parse_recipe_text(yaml.safe_dump(data)))
    else:
        compiler.compile(parse_recipe_text(yaml.safe_dump(data)))


def test_signed_background_filters_archive_raw_and_match_shared_pipeline(tmp_path):
    processing = {
        "filters": ["narrow_reject", "denoise"],
        "parameters": {"narrow_max_width": "50 kHz", "protected_bands": [["1.3 MHz", "1.5 MHz"]]},
    }
    path = tmp_path / "filtered.h5"
    events = []
    result, acquired = execute(
        path,
        events=events,
        source=recipe_with(reference_operation="subtract_power_signed", processing=processing),
    )
    assert result.error is None
    stored = Hdf5RunReader.spectrum(path, 0)
    signal_w = np.mean([10 ** ((np.asarray(t.powers_dbm) - 30) / 10) for t in acquired[3:]], axis=0)
    ref_w = np.mean([10 ** ((np.asarray(t.powers_dbm) - 30) / 10) for t in acquired[:3]], axis=0)
    modes, parameters = parse_processing(processing)
    expected = clean_spectrum_pipeline(
        signal_w - ref_w,
        unit="W",
        modes=modes,
        parameters=parameters,
        frequencies_hz=stored.frequencies_hz,
    )
    assert stored.processed_unit == "W"
    np.testing.assert_allclose(stored.processed_values, expected.values, atol=1e-20)
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 5
    metadata = Hdf5RunReader.points(path)[0].metadata["spectrum_processing_v1"]
    assert metadata["reference_operation"] == "subtract_power_signed"
    assert metadata["processing"]["filters"] == ["narrow_reject", "denoise"]
    assert metadata["configuration_fingerprint"]
    assert not metadata["uncertainty_qualified"]
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    preview = next(payload for name, payload in events if name == "spectrum_preview")
    assert preview["processed_unit"] == "W"
    np.testing.assert_array_equal(preview["processed_values"], stored.processed_values)


def saved_reference(tmp_path, kind, *, mismatch=False):
    settings = simulation_settings()
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    try:
        plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
        adapter.configure_spectrum(plan.actions[0].payload["config"])
        trace = adapter.acquire_single_sweep("TRAC1")
        full = adapter.read_full_configuration()
        advanced = adapter.read_advanced_spectrum_configuration()
        if mismatch:
            advanced = replace(advanced, attenuation_db=advanced.attenuation_db + 2)
        path = tmp_path / f"{kind}.h5"
        if kind == "reference":
            reference = ReferenceSpectrum(
                trace,
                "single",
                1,
                trace.acquired_at_utc,
                source_device_idn=adapter.identity.idn,
                reference_level_dbm=full.reference_level_dbm,
                advanced_configuration_known=True,
                **{
                    key: value
                    for key, value in asdict(advanced).items()
                    if key != "instrument_mode"
                },
            )
            ReferenceHdf5Store.save(path, reference)
        else:
            context = SpectrumAcquisitionContext(
                np.asarray(trace.frequencies_hz),
                spectrum_configuration_fingerprint(full, advanced, adapter.identity.idn),
                settings_verified=True,
            )
            now = datetime.now(UTC).timestamp()
            profile = BackgroundProfile(
                "sweep-background",
                context.context_id,
                10 ** ((np.asarray(trace.powers_dbm) - 30) / 10),
                np.full(len(trace.powers_dbm), 1e-28),
                None,
                30,
                now - 60,
                now,
                "Low bias chosen by operator",
            )
            BackgroundProfileHdf5Store.save(path, context, profile)
        return path
    finally:
        adapter.disconnect()


def source_with_file(path, kind):
    data = yaml.safe_load(recipe_with(reference_operation="subtract_power_signed"))
    data["root"]["children"][1].update(source_file=str(path), file_kind=kind)
    return yaml.safe_dump(data)


@pytest.mark.parametrize("kind", ["reference", "background"])
@pytest.mark.parametrize("mismatch", [True, False])
def test_imported_reference_verified_against_instrument_and_archived(tmp_path, kind, mismatch):
    reference_path = saved_reference(tmp_path, kind, mismatch=mismatch)
    result_path = tmp_path / "run.h5"
    source = source_with_file(reference_path, kind)
    result, acquired = execute(result_path, source=source)
    if mismatch:
        assert "differ" in result.error
        assert not acquired and result.stored_points == 0
        return
    assert result.error is None and len(acquired) == 2
    assert ThatecCompatibilityValidator().validate(result_path, require_pythat=True).valid
    metadata = Hdf5RunReader.points(result_path)[0].metadata["spectrum_processing_v1"]
    assert metadata["reference_origin"]["file_kind"] == kind
    assert len(metadata["reference_origin"]["source_sha256"]) == 64
    with h5py.File(result_path) as file:
        assert file["references/0"].attrs["kind"] == "loaded"
    from app.engine.estimation import PlanEstimator

    plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
    from app.engine.policy import ExecutionPolicy

    attempts = 1 + ExecutionPolicy.from_settings(simulation_settings()).retry_count
    assert PlanEstimator(simulation_settings()).estimate(plan).spectrum_values == (2 * attempts + 2) * 101


def test_changed_asset_fails_before_recipe_actions(tmp_path, monkeypatch):
    path = saved_reference(tmp_path, "reference")
    from app.engine.runner import RecipeRunner

    original = RecipeRunner.run

    def mutate_then_run(self, plan, **kwargs):
        with h5py.File(path, "r+") as file:
            file.attrs["external_change"] = json.dumps({"changed": True})
        return original(self, plan, **kwargs)

    monkeypatch.setattr(RecipeRunner, "run", mutate_then_run)
    events = []
    result, acquired = execute(
        tmp_path / "changed.h5", source=source_with_file(path, "reference"), events=events
    )
    assert "changed after preflight" in result.error
    assert not acquired
    assert not any(name == "action_started" for name, _ in events)


def test_emi_history_is_local_to_each_point(tmp_path):
    data = yaml.safe_load(
        recipe_with(
            average_count=5,
            reference_operation="none",
            processing={"filters": ["emi_reject", "denoise"]},
        )
    )
    data["root"]["children"].pop(1)
    data["root"]["children"].append(dict(data["root"]["children"][-1], id="next-point"))
    path = tmp_path / "emi-points.h5"
    result, acquired = execute(path, source=yaml.safe_dump(data))
    assert result.error is None and len(acquired) == 10 and result.stored_points == 2
    for index in range(2):
        block = acquired[index * 5 : (index + 1) * 5]
        stored = Hdf5RunReader.spectrum(path, index)
        modes, params = parse_processing(data["root"]["children"][-1]["processing"])
        expected = clean_spectrum_pipeline(
            stored.powers_dbm,
            unit="dBm",
            modes=modes,
            parameters=params,
            frequencies_hz=stored.frequencies_hz,
            history=[trace.powers_dbm for trace in block],
        )
        np.testing.assert_array_equal(stored.processed_values, expected.values)
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_mixed_units_rejected_before_measurement():
    data = yaml.safe_load(SOURCE)
    data["root"]["children"].append(
        dict(data["root"]["children"][-1], id="watts", reference_operation="subtract_power_signed")
    )
    with pytest.raises(ConfigurationError, match="units cannot change"):
        RecipeCompiler(simulation_settings()).compile(parse_recipe_text(yaml.safe_dump(data)))


def test_identical_reconfiguration_can_reuse_reference(tmp_path):
    data = yaml.safe_load(recipe_with(reference_operation="subtract_power_signed"))
    data["root"]["children"].insert(2, dict(data["root"]["children"][0], id="same-config"))
    result, acquired = execute(tmp_path / "same.h5", source=yaml.safe_dump(data))
    assert result.error is None and len(acquired) == 5


def test_unqualified_log_residual_keeps_raw_then_fails_cleanly(tmp_path, monkeypatch):
    original = AnritsuAdapter.acquire_single_sweep

    def constant(self, trace_name, **kwargs):
        trace = original(self, trace_name, **kwargs)
        return replace(trace, powers_dbm=tuple([-60.0] * len(trace.powers_dbm)))

    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", constant)
    path = tmp_path / "log-zero.h5"
    result, acquired = execute(path, source=recipe_with(reference_operation="subtract_power"))
    assert "signed W" in result.error and len(acquired) == 5
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 5
    assert result.stored_points == 0


def test_readback_change_during_block_preserves_raw_but_rejects_mean(tmp_path, monkeypatch):
    original = AnritsuAdapter.read_acquisition_configuration
    calls = 0

    def changed(self):
        nonlocal calls
        calls += 1
        full, snapshot = original(self)
        return full, replace(snapshot, attenuation_db=snapshot.attenuation_db + (2 if calls > 1 else 0))

    monkeypatch.setattr(AnritsuAdapter, "read_acquisition_configuration", changed)
    path = tmp_path / "readback-changed.h5"
    result, acquired = execute(path)
    assert "settings changed during" in result.error and len(acquired) == 3
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 3


def test_resuming_after_reference_fails_before_actions(tmp_path, monkeypatch):
    from app.engine.runner import RecipeRunner

    original = RecipeRunner.run

    def resumed(self, plan):
        return original(self, plan, start_action_index=2)

    monkeypatch.setattr(RecipeRunner, "run", resumed)
    events = []
    result, acquired = execute(tmp_path / "resume.h5", events=events)
    assert "reference step" in result.error and not acquired
    assert not any(name == "action_started" for name, _ in events)


def test_storage_rejects_unit_change_and_rolls_back_public_row(tmp_path):
    from app.devices.anritsu_ms2830a import SpectrumTrace
    from app.domain.errors import ExecutionError
    from app.domain.models import MeasurementPoint
    from app.storage.hdf5_writer import Hdf5RunWriter

    path = tmp_path / "unit-guard.h5"
    writer = Hdf5RunWriter(
        path,
        recipe_source="schema_version: 1\n",
        settings_source="{}",
        plan_hash="units",
        device_idn={"anritsu": "SIMULATED"},
    )
    trace = SpectrumTrace((1e6, 2e6, 3e6), (-60.0, -60.0, -60.0), datetime.now(UTC), "TRAC1")
    try:
        writer.append(
            MeasurementPoint(0, {}, {}),
            trace,
            processed_values=(0.0, 1e-12, -1e-12),
            processed_unit="W",
            processing_operation="subtract_power_signed",
        )
        with pytest.raises(ExecutionError, match="units cannot change"):
            writer.append(
                MeasurementPoint(1, {}, {}),
                trace,
                processed_values=(0.0, 1.0, -1.0),
                processed_unit="dB",
                processing_operation="difference_db",
            )
    finally:
        writer.close("faulted")
    assert len(Hdf5RunReader.points(path)) == 1
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
