"""Analyzer-average provenance across checkpoint, archive, and Results boundaries."""

import csv
from dataclasses import replace
import hashlib
import json

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import ReferenceSpectrum
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.spectrum_averaging import InstrumentAverageProvenance
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.engine.recovery import RunRecoveryManager
from app.spectrum.analysis import SpectrumAnalysisParameters
from app.storage import Hdf5RunReader, Hdf5RunWriter, ReferenceHdf5Store, ThatecRunReader
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.pythat_bridge import open_measurement_tree
from app.storage.recipe_reference import recipe_reference_uses_instrument_average
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.heatmap_coordinates import HeatmapRequest, build_heatmap_coordinates
from app.ui.results.heatmap_tab import _read_heatmap_payload
from app.ui.results.processing import ResultProcessing, read_processed_private, read_processed_public
from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tests.test_results_series_navigation import close_tab, load_tab
from tests.test_source_review_partial_append import trace
from tests.test_spectrum_correction_controller import wait_until


NATIVE = {
    "trace_storage_mode": "LAV",
    "sweep_count": 40,
    "sweep_time_s": .006,
    "nominal_duration_s": .24,
    "target_duration_s": .245,
    "elapsed_s": .41,
}
IDENTITY = dict(recipe_source="", settings_source="", plan_hash="averaging-provenance")


def average_point(index):
    return MeasurementPoint(index, {"keithley.A.current": index * .001}, {}, metadata={
        "spectrum_average_count": NATIVE["sweep_count"],
        "spectrum_processing_v1": {
            "instrument_average": NATIVE, "source_spectra_stored": False,
        },
        "raw_recipe_sweep_indices": (),
    })


def signal():
    return replace(trace(), powers_dbm=(-59., -61., -63.))


@pytest.fixture
def native_archive(tmp_path):
    path = tmp_path / "native-average.h5"
    writer = Hdf5RunWriter(path, device_idn={}, **IDENTITY)
    writer.store_reference(trace(), kind="averaged", average_count=NATIVE["sweep_count"],
                           acquisition_metadata={"instrument_average": NATIVE})
    writer.append(average_point(0), signal())
    writer.append(average_point(1), signal())
    writer.close("completed")
    return path


@pytest.mark.parametrize("retained_count", [0, 1])
def test_resume_keeps_active_native_reference_and_rebuilds_csv(tmp_path, retained_count):
    path = tmp_path / "interrupted.h5"
    index_path = tmp_path / "interrupted.csv"
    writer = Hdf5RunWriter(path, csv_summary_path=index_path, device_idn={}, **IDENTITY)
    active_reference = 0
    if retained_count:
        earlier = writer.store_reference(trace())
        point = average_point(0)
        point.metadata["reference_index"] = earlier
        writer.append(point, trace())
        active_reference = 1
    writer.store_reference(trace(), kind="averaged", average_count=NATIVE["sweep_count"],
                           acquisition_metadata={"instrument_average": NATIVE})
    writer.store_reference(trace())
    writer.append(average_point(retained_count), trace())
    writer.close("aborted")

    with h5py.File(path, "r") as file:
        recovered = RunRecoveryManager._read_reference(file, {"index": active_reference, "fingerprint": "confirmed"})
        assert recovered.instrument_average.metadata() == NATIVE
    writer = Hdf5RunWriter.resume(path, checkpoint_count=retained_count, reference_index=active_reference,
                                  csv_summary_path=index_path, **IDENTITY)
    point = average_point(retained_count)
    point.metadata["reference_index"] = active_reference
    writer.append(point, trace())
    writer.close("completed")

    with index_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == retained_count + 1
    assert all(json.loads(row["instrument_average_json"]) == NATIVE for row in rows)
    assert all(int(row["spectrum_average_count"]) == NATIVE["sweep_count"] for row in rows)
    assert len(Hdf5RunReader.references(path)) == active_reference + 1
    assert Hdf5RunReader.reference(path, active_reference).instrument_average.metadata() == NATIVE
    assert Hdf5RunReader.spectrum(path, retained_count).reference_index == active_reference
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


@pytest.mark.parametrize("modes,frames", [(("emi_reject",), 1), ((), 2)])
def test_native_average_history_filters_fail_across_readers(native_archive, modes, frames):
    path = native_archive
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    points = Hdf5RunReader.points(path, include_details=False)
    run = ThatecRunReader.describe(path)
    row = find_heatmap_rows(run)[0]
    spectrum = ThatecRunReader.spectrum_slice(path, row.id, 0)
    coords = build_heatmap_coordinates(path, run, row, points)
    request = HeatmapRequest("frequency", coords.dimensions[1].id, {})
    state = ResultProcessing(modes=modes,
                             parameters=SpectrumAnalysisParameters(temporal_average_frames=frames))
    with pytest.raises(ValueError, match="Anritsu average.*individual"):
        read_processed_private(path, points[0], state)
    with pytest.raises(ValueError, match="Anritsu average.*individual"):
        read_processed_public(path, spectrum, 0, state, points)
    with pytest.raises(ValueError, match="Anritsu average.*individual"):
        _read_heatmap_payload(path, row, coords, request, processing=state, points=points)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_native_average_private_and_public_default_labels(native_archive, shell_qt_application, tmp_path):
    app = shell_qt_application
    tab = load_tab(app, native_archive)
    try:
        tab.resize(1440, 900)
        tab.show()
        app.processEvents()
        wait_until(app, lambda: not tab._read_tasks)
        label = "Anritsu average (40 sweeps)"
        assert label in tab.spectrum_plot._traces
        assert tab.spectrum_plot.geometry().height() >= 150
        assert tab.spectrum_plot.isVisibleTo(tab)
        assert tab.spectrum_plot.export_metadata["instrument_average"] == NATIVE
        assert not tab.processing_controls.filters["emi_reject"].isEnabled()
        assert tab.grab().save(str(tmp_path / "results-native-average.png"))

        tab._load_selected_thatec_spectrum()
        wait_until(app, lambda: not tab._read_tasks)
        assert tab._public_spectrum is not None
        assert label in tab.spectrum_plot._traces
        np.testing.assert_allclose(tab.spectrum_plot._traces[label][1], signal().powers_dbm)
        assert tab.spectrum_plot.export_metadata["instrument_average"] == NATIVE
    finally:
        close_tab(app, tab)


def test_writer_rejects_native_average_labeled_with_individual_sweeps(tmp_path):
    writer = Hdf5RunWriter(tmp_path / "invalid.h5", device_idn={}, **IDENTITY)
    point = average_point(0)
    point.metadata["raw_recipe_sweep_indices"] = (0,)
    try:
        with pytest.raises(ExecutionError, match="individual"):
            writer.append(point, trace())
        assert writer.point_count == 0
    finally:
        writer.close("faulted")


@pytest.mark.parametrize("operation,unit", [
    ("difference_db", "dB"), ("subtract_reference_signed", "W"), ("add_power", "dBm"),
])
def test_native_means_keep_absolute_power_and_signed_contrast(native_archive, operation, unit):
    points = Hdf5RunReader.points(native_archive, include_details=False)
    run = ThatecRunReader.describe(native_archive)
    row = find_heatmap_rows(run)[0]
    spectrum = ThatecRunReader.spectrum_slice(native_archive, row.id, 0)
    coords = build_heatmap_coordinates(native_archive, run, row, points)
    request = HeatmapRequest("frequency", coords.dimensions[1].id, {})
    state = ResultProcessing(operation, reference_index=0)
    private = read_processed_private(native_archive, points[0], state)
    public = read_processed_public(native_archive, spectrum, 0, state, points)
    heatmap = _read_heatmap_payload(native_archive, row, coords, request, processing=state, points=points)
    signal_w = 10 ** ((np.asarray(signal().powers_dbm) - 30) / 10)
    baseline_w = 10 ** ((np.asarray(trace().powers_dbm) - 30) / 10)
    if unit == "dB":
        expected = (1., 0., -1.)
    elif unit == "W":
        expected = signal_w - baseline_w
    else:
        expected = 10 * np.log10(signal_w + baseline_w) + 30
    assert private.unit == public.unit == heatmap.z_unit == unit
    assert private.baselines[0].instrument_average == NATIVE
    assert heatmap.instrument_averages == {0: NATIVE, 1: NATIVE}
    for values in (private.values, public.values, heatmap.matrix[0]):
        np.testing.assert_allclose(values, expected, rtol=1e-13)
    public_tree = open_measurement_tree(native_archive)
    np.testing.assert_array_equal(public_tree.dataset["Spectrum"].values, [signal().powers_dbm] * 2)
    assert not tuple(Hdf5RunReader.references(native_archive)[0].source_sweep_indices)


def test_public_derived_power_row_cannot_reapply_reference(tmp_path):
    path = tmp_path / "derived-power.h5"
    writer = Hdf5RunWriter(path, device_idn={}, **IDENTITY)
    writer.append(average_point(0), signal(), processed_values=(-57., -58., -59.),
                  processed_unit="dBm", processing_operation="add_power")
    writer.close("completed")
    points = Hdf5RunReader.points(path, include_details=False)
    run = ThatecRunReader.describe(path)
    row = next(row for row in find_heatmap_rows(run)
               if dict(row.definition).get("lab control role") == "spectrum_processed")
    spectrum = ThatecRunReader.spectrum_slice(path, row.id, 0)
    coords = build_heatmap_coordinates(path, run, row, points)
    request = HeatmapRequest("frequency", coords.dimensions[1].id, {})
    state = ResultProcessing("add_power", reference_index=0)
    with pytest.raises(ValueError, match="already contains derived values"):
        read_processed_public(path, spectrum, 0, state, points)
    with pytest.raises(ValueError, match="already contains derived values"):
        _read_heatmap_payload(path, row, coords, request, processing=state, points=points)


@pytest.mark.parametrize("kind", ["typed", "legacy_label", "unknown"])
def test_portable_reference_preserves_only_available_provenance(tmp_path, kind):
    original = trace()
    if kind == "typed":
        evidence = InstrumentAverageProvenance.from_metadata(NATIVE)
        original = replace(original, instrument_average=evidence)
    elif kind == "legacy_label":
        original = replace(original, trace_name="TRAC1_ANRITSU_LAV40")
    reference = ReferenceSpectrum(original, "averaged", 40, original.acquired_at_utc)
    path = tmp_path / "reference.h5"
    ReferenceHdf5Store.save(path, reference)
    loaded = ReferenceHdf5Store.load(path)
    assert loaded.average_count == 40
    assert loaded.instrument_average == reference.instrument_average
    assert recipe_reference_uses_instrument_average(loaded) is (kind != "unknown")
    assert Hdf5RunReader.spectrum(path, 0).powers_dbm == original.powers_dbm


@pytest.mark.parametrize("legacy", [False, True])
def test_instrument_background_round_trip_preserves_mean_and_missing_variance(tmp_path, legacy):
    context = SpectrumAcquisitionContext((1., 2., 3.), "analyzer-settings", settings_verified=True)
    evidence = None if legacy else InstrumentAverageProvenance.from_metadata(NATIVE)
    profile = BackgroundProfile(
        "instrument_mean", context.context_id, (1e-8, 2e-8, 3e-8),
        None, None, 40, 1.0, 1.41, "background", averaging_source="instrument",
        nominal_sweep_time_s=.006, instrument_average=evidence,
    )
    path = tmp_path / "background.h5"
    BackgroundProfileHdf5Store.save(path, context, profile)
    if legacy:
        with h5py.File(path, "r+") as file:
            group = file["spectrum_processing_v1/profiles/instrument_mean"]
            metadata = json.loads(group.attrs["metadata_json"])
            del metadata["instrument_average"]
            group.attrs["metadata_json"] = json.dumps(metadata)
    loaded_context, loaded = BackgroundProfileHdf5Store.load(path)
    assert loaded_context.context_id == context.context_id
    assert loaded.content_hash == profile.content_hash
    assert loaded.instrument_average == profile.instrument_average
    assert loaded.instrument_average.nominal_duration_s == .24
    np.testing.assert_array_equal(loaded.mean_w, profile.mean_w)
    assert loaded.sample_variance_w2 is None and loaded.mean_variance_w2 is None
    assert not loaded.signal_free_qualified
