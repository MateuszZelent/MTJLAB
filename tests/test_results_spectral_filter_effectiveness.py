"""Controlled Results DSP effects, before heatmap assembly or frequency cropping.

These are display-domain transfer checks, not instrument qualification. Gaussian
widths below describe dB ordinates, not physical half-power bandwidths. Archives
are written directly; no recipe runner, sweep, or hardware is used.
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.domain.recipe_spectrum import RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.heatmap_coordinates import HeatmapRequest, build_heatmap_coordinates
from app.ui.results.heatmap_tab import _read_heatmap_payload
from app.ui.results.processing import ResultProcessing, ResultSpectrumProcessor

MODES = (
    (),
    ("narrow_reject",),
    ("emi_reject",),
    ("denoise",),
    ("narrow_reject", "emi_reject", "denoise"),
)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _gaussian(frequencies_hz, center_hz, width_hz, height_db):
    return height_db * np.exp(-4 * np.log(2) * ((frequencies_hz - center_hz) / width_hz) ** 2)


def _write_archive(path, frequencies_hz, spectra_dbm, histories_dbm=()):
    """Commit real reader-compatible records, with distinct setpoints per block."""
    writer = Hdf5RunWriter(
        path,
        recipe_source="name: controlled filter evidence\n",
        settings_source="schema_version: 1\n",
        plan_hash="controlled-filter-test",
        device_idn={},
        expected_points=len(spectra_dbm),
    )
    writer.store_reference(
        SpectrumTrace(tuple(frequencies_hz), (-82.0,) * len(frequencies_hz), NOW, "TRAC1"),
        acquisition_metadata={"purpose": "reference", "configuration_fingerprint": "same"},
    )
    for checkpoint, values_dbm in enumerate(spectra_dbm):
        setpoints = {"keithley.A.current": (checkpoint + 1) * 1e-3}
        history = histories_dbm[checkpoint] if len(histories_dbm) else ()
        indices = []
        for repeat, powers_dbm in enumerate(history):
            indices.append(
                writer.store_recipe_spectrum_sweep(
                    RecipeSpectrumSweep(
                        frequencies_hz=frequencies_hz,
                        powers_dbm=powers_dbm,
                        recipe_node_id="recorded-signal",
                        execution_id=f"checkpoint-{checkpoint}",
                        role=SpectrumFrameRole.SIGNAL,
                        average_index=repeat,
                        average_count=len(history),
                        acquired_at_s=NOW.timestamp() + checkpoint * 100 + repeat,
                        configuration_generation=1,
                        evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
                        setpoints_si=tuple(setpoints.items()),
                    )
                )
            )
        writer.append(
            MeasurementPoint(
                index=checkpoint,
                setpoints=setpoints,
                measurements={},
                metadata={
                    "raw_recipe_sweep_indices": indices,
                    "spectrum_processing_v1": {"configuration_fingerprint": "same"},
                },
            ),
            SpectrumTrace(tuple(frequencies_hz), tuple(values_dbm), NOW, "TRAC1"),
        )
    writer.close("completed")
    return path


@pytest.fixture
def make_archive(tmp_path):
    """Every test-created source must remain byte-for-byte unchanged after reads."""
    archives = []

    def make(frequencies_hz, spectra_dbm, histories_dbm=()):
        path = _write_archive(
            tmp_path / f"controlled-{len(archives)}.h5", frequencies_hz, spectra_dbm, histories_dbm
        )
        archives.append((path, hashlib.sha256(path.read_bytes()).digest()))
        return path

    yield make
    for path, digest in archives:
        assert hashlib.sha256(path.read_bytes()).digest() == digest, "Source archive was modified"


def _process(path, state, checkpoint, *, points=None):
    points = Hdf5RunReader.points(path) if points is None else points
    trace = Hdf5RunReader.spectrum(path, checkpoint)
    return ResultSpectrumProcessor(path, state, points).process(
        checkpoint, trace.frequencies_hz, trace.powers_dbm
    )


def _heatmap(path, state, *, transpose=False, crop=None, points=None):
    run = ThatecRunReader.describe(path)
    row = find_heatmap_rows(run)[0]
    points = Hdf5RunReader.points(path) if points is None else points
    coordinates = build_heatmap_coordinates(path, run, row, points)
    sweep_id = coordinates.dimensions[1].id
    request = HeatmapRequest(
        sweep_id if transpose else "frequency",
        "frequency" if transpose else sweep_id,
        {} if crop is None else {"frequency": crop},
    )
    # Deliberately no _process_map_payload: all map-level filters are off.
    return _read_heatmap_payload(path, row, coordinates, request, processing=state, points=points)


@pytest.mark.parametrize("spike_sign", [-1, 1])
def test_narrow_peaks_suppress_spikes_on_moving_broad_resonance(make_archive, spike_sign):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    centers_hz = (600e6, 700e6, 800e6)
    clean, raw = [], []
    for checkpoint, center_hz in enumerate(centers_hz):
        noise_db = np.random.default_rng(710 + checkpoint).normal(0, 0.05, frequencies_hz.size)
        signal_dbm = -85 + _gaussian(frequencies_hz, center_hz, 90e6, 6) + noise_db
        clean.append(signal_dbm)
        raw.append(signal_dbm + _gaussian(frequencies_hz, center_hz, 3e6, spike_sign * 8))
    path = make_archive(frequencies_hz, raw)
    for checkpoint, center_hz in enumerate(centers_hz):
        result = _process(path, ResultProcessing(modes=("narrow_reject",)), checkpoint)
        filtered = np.asarray(result.values)
        center = np.argmin(abs(frequencies_hz - center_hz))
        assert abs(raw[checkpoint][center] - clean[checkpoint][center]) == pytest.approx(8)
        assert abs(filtered[center] - clean[checkpoint][center]) < 0.4
        # Erasing or flattening the broad resonance fails these independent gates.
        assert np.max(abs(filtered - clean[checkpoint])) < 0.4
        assert np.sqrt(np.mean((filtered - clean[checkpoint]) ** 2)) < 0.05
        assert filtered[center] - (-85) == pytest.approx(6, abs=0.4)
        assert result.unit == "dBm"
        np.testing.assert_array_equal(result.frequencies_hz, frequencies_hz)


def test_denoise_reduces_seeded_random_noise_and_preserves_moving_resonance(make_archive):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    centers_hz = (600e6, 700e6, 800e6)
    clean, raw = [], []
    for checkpoint, center_hz in enumerate(centers_hz):
        signal_dbm = -85 + _gaussian(frequencies_hz, center_hz, 90e6, 12)
        clean.append(signal_dbm)
        raw.append(
            signal_dbm + np.random.default_rng(910 + checkpoint).normal(0, 0.8, frequencies_hz.size)
        )
    path = make_archive(frequencies_hz, raw)
    for checkpoint, center_hz in enumerate(centers_hz):
        result = _process(path, ResultProcessing(modes=("denoise",)), checkpoint)
        filtered = np.asarray(result.values)
        quiet = abs(frequencies_hz - center_hz) > 150e6
        input_error = raw[checkpoint] - clean[checkpoint]
        error = filtered - clean[checkpoint]
        assert np.std(error[quiet]) < 0.55 * np.std(input_error[quiet])
        assert np.linalg.norm(error) < 0.55 * np.linalg.norm(input_error)
        assert abs(np.mean(error[quiet])) < 0.1
        center = np.argmin(abs(frequencies_hz - center_hz))
        assert filtered[center] == pytest.approx(-73, abs=1)
        assert result.unit == "dBm"
        np.testing.assert_array_equal(result.frequencies_hz, frequencies_hz)


@dataclass(frozen=True)
class _RecordedArchive:
    path: Path
    frequencies_hz: np.ndarray
    clean_dbm: np.ndarray
    raw_dbm: np.ndarray
    line_bins: tuple[int, ...]


@pytest.fixture(scope="module")
def recorded_archive(tmp_path_factory):
    """Six setpoints, each with its own eight recorded repeats and EMI frequency."""
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    histories, clean, line_bins = [], [], []
    for checkpoint in range(6):
        baseline_dbm = -85 + _gaussian(frequencies_hz, 650e6 + checkpoint * 35e6, 90e6, 6)
        line_bin = int(np.argmin(abs(frequencies_hz - (1000e6 + checkpoint * 20e6))))
        clean.append(baseline_dbm)
        line_bins.append(line_bin)
        rng = np.random.default_rng(7100 + checkpoint)
        frames = []
        for repeat in range(8):
            frame = baseline_dbm + rng.normal(0, 0.12, frequencies_hz.size)
            # A desired narrow resonance moves during these same-checkpoint repeats.
            frame += _gaussian(frequencies_hz, 400e6 + repeat * 5e6, 3e6, 14)
            frame[line_bin] += 24
            frames.append(frame)
        histories.append(np.asarray(frames))
    # Independent logarithmic conversion: the stored spectrum is a power mean.
    raw = 10 * np.log10(np.mean(10 ** ((np.asarray(histories) - 30) / 10), axis=1)) + 30
    path = _write_archive(
        tmp_path_factory.mktemp("spectral-filter-effects") / "repeats.h5",
        frequencies_hz,
        raw,
        histories,
    )
    digest = hashlib.sha256(path.read_bytes()).digest()
    yield _RecordedArchive(path, frequencies_hz, np.asarray(clean), raw, tuple(line_bins))
    assert hashlib.sha256(path.read_bytes()).digest() == digest, "Source archive was modified"


@pytest.mark.parametrize(
    "operation,unit,offset_db", [("none", "dBm", 0), ("difference_db", "dB", 82)]
)
def test_emi_removes_only_lines_supported_by_own_checkpoint_repeats(
    recorded_archive, operation, unit, offset_db
):
    archive = recorded_archive
    points = Hdf5RunReader.points(archive.path)
    # Check the evidence itself, rather than trusting the fixture's metadata link.
    for point in points:
        sources = tuple(
            iter_recipe_spectrum_sweeps(
                archive.path, selected_indices=set(point.metadata["raw_recipe_sweep_indices"])
            )
        )
        assert len(sources) == 8
        assert {record.setpoints_si for _, _, record in sources} == {tuple(point.setpoints.items())}
        assert {record.execution_id for _, _, record in sources} == {f"checkpoint-{point.index}"}
    # Reverse order also exposes an accidental cross-checkpoint history accumulator.
    processor = ResultSpectrumProcessor(
        archive.path,
        ResultProcessing(operation=operation, reference_index=0, modes=("emi_reject",)),
        points,
    )
    for checkpoint in reversed(range(len(points))):
        result = processor.process(checkpoint, archive.frequencies_hz, archive.raw_dbm[checkpoint])
        filtered = np.asarray(result.values)
        input_values = archive.raw_dbm[checkpoint] + offset_db
        line_bin = archive.line_bins[checkpoint]
        assert input_values[line_bin] - filtered[line_bin] > 20
        assert filtered[line_bin] == pytest.approx(
            archive.clean_dbm[checkpoint, line_bin] + offset_db, abs=0.2
        )
        untouched = np.ones(filtered.size, dtype=bool)
        untouched[line_bin - 1 : line_bin + 2] = False
        # Includes the broad setpoint-dependent resonance, moving narrow signal,
        # and frequencies where OTHER checkpoints have their stationary line.
        np.testing.assert_array_equal(filtered[untouched], input_values[untouched])
        assert result.unit == unit
        assert not any("needs history" in note for note in result.notes)


@pytest.mark.parametrize("repeat_count", [0, 4])
def test_emi_does_not_substitute_neighbor_setpoints_for_missing_repeats(make_archive, repeat_count):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    raw, histories = [], []
    for checkpoint in range(6):
        row = -85 + _gaussian(frequencies_hz, 650e6 + checkpoint * 35e6, 90e6, 6)
        row[1600] += 24  # Six neighboring setpoints would look like convincing EMI history.
        raw.append(row)
        histories.append([row.copy() for _ in range(repeat_count)])
    path = make_archive(frequencies_hz, raw, histories)
    state = ResultProcessing(modes=("emi_reject",))
    for checkpoint in range(6):
        result = _process(path, state, checkpoint)
        np.testing.assert_array_equal(result.values, raw[checkpoint])
        assert any(f"{repeat_count}/5" in note and "needs history" in note for note in result.notes)
    payload = _heatmap(path, state)
    np.testing.assert_array_equal(payload.matrix, raw)
    assert any(f"{repeat_count}/5" in note for note in payload.processing_notes)


def test_emi_works_with_exactly_five_recorded_repeats_and_one_checkpoint(make_archive):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    clean = -85 + _gaussian(frequencies_hz, 700e6, 90e6, 6)
    raw = clean.copy()
    raw[1600] += 24
    path = make_archive(frequencies_hz, [raw], [[raw.copy() for _ in range(5)]])
    state = ResultProcessing(modes=("emi_reject",))
    result = _process(path, state, 0)
    assert raw[1600] - result.values[1600] > 23.9
    assert result.values[1600] == pytest.approx(clean[1600], abs=0.01)
    assert not any("needs history" in note for note in result.notes)
    payload = _heatmap(path, state)
    np.testing.assert_array_equal(payload.matrix[0], result.values)


@pytest.mark.parametrize(
    "operation,unit", [("subtract_reference_signed", "W"), ("ratio_linear", "ratio")]
)
def test_emi_is_intentionally_unavailable_for_linear_units(recorded_archive, operation, unit):
    archive = recorded_archive
    state = ResultProcessing(operation=operation, reference_index=0, modes=("emi_reject",))
    payload = _heatmap(archive.path, state)
    raw = archive.raw_dbm
    # The recorded reference is -82 dBm = 10**(-112/10) W.
    reference_w = 10 ** (-112 / 10)
    expected = 10 ** ((raw - 30) / 10) - reference_w if unit == "W" else 10 ** ((raw + 82) / 10)
    assert payload.z_unit == unit
    # Direct subtraction loses relative precision near zero; bound its roundoff
    # by the input power scale. The EMI/no-EMI equality below remains exact.
    absolute_tolerance = 64 * np.finfo(float).eps * reference_w if unit == "W" else 0
    np.testing.assert_allclose(payload.matrix, expected, rtol=1e-12, atol=absolute_tolerance)
    if unit == "W":
        assert np.any(payload.matrix < 0) and np.any(payload.matrix > 0)
    for checkpoint in range(6):
        result = _process(archive.path, state, checkpoint)
        without_emi = _process(
            archive.path, ResultProcessing(operation=operation, reference_index=0), checkpoint
        )
        np.testing.assert_array_equal(result.values, without_emi.values)
        np.testing.assert_array_equal(payload.matrix[checkpoint], result.values)
        assert result.unit == unit
        assert any("requires dB or dBm" in note and unit in note for note in result.notes)
    assert any("requires dB or dBm" in note for note in payload.processing_notes)


@pytest.mark.parametrize("modes", MODES, ids=("raw", "narrow", "emi", "denoise", "combined"))
@pytest.mark.parametrize("transpose", [False, True], ids=("frequency-x", "frequency-y"))
@pytest.mark.parametrize("crop", [None, (395e6, 1050e6)], ids=("full", "cropped"))
def test_every_heatmap_row_equals_independently_processed_full_spectrum(
    recorded_archive, modes, transpose, crop
):
    archive = recorded_archive
    state = ResultProcessing(modes=modes)
    points = Hdf5RunReader.points(archive.path)
    payload = _heatmap(archive.path, state, transpose=transpose, crop=crop, points=points)
    mask = (
        np.ones(archive.frequencies_hz.size, dtype=bool)
        if crop is None
        else (archive.frequencies_hz >= crop[0]) & (archive.frequencies_hz <= crop[1])
    )
    np.testing.assert_array_equal(
        payload.y_values if transpose else payload.x_values, archive.frequencies_hz[mask]
    )
    assert payload.z_unit == "dBm"
    assert payload.missing_checkpoints == 0
    assert payload.matrix.shape == ((mask.sum(), 6) if transpose else (6, mask.sum()))
    for checkpoint in reversed(range(6)):
        # A NEW processor and only this checkpoint: no adjacent point can be history.
        independent = _process(archive.path, state, checkpoint, points=(points[checkpoint],))
        assembled = payload.matrix[:, checkpoint] if transpose else payload.matrix[checkpoint]
        cells = (
            payload.cell_checkpoints[:, checkpoint]
            if transpose
            else payload.cell_checkpoints[checkpoint]
        )
        np.testing.assert_array_equal(independent.frequencies_hz, archive.frequencies_hz)
        np.testing.assert_array_equal(assembled, np.asarray(independent.values)[mask])
        np.testing.assert_array_equal(cells, np.full(mask.sum(), checkpoint))


@pytest.mark.parametrize("mode", ["narrow_reject", "denoise"])
def test_crop_boundary_uses_outside_bins_before_filtering(make_archive, mode):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    raw = -85 + 0.3 * np.sin(np.arange(frequencies_hz.size) * 0.8)
    raw += _gaussian(frequencies_hz, 500e6, 3e6, 10)
    path = make_archive(frequencies_hz, [raw])
    state = ResultProcessing(modes=(mode,))
    crop = (499e6, 530e6)
    mask = (frequencies_hz >= crop[0]) & (frequencies_hz <= crop[1])
    full = _process(path, state, 0)
    payload = _heatmap(path, state, crop=crop)
    np.testing.assert_array_equal(payload.matrix[0], np.asarray(full.values)[mask])
    # Establish that this fixture CAN detect moving the crop ahead of filtering.
    cropped_first = ResultSpectrumProcessor(path, state).process(0, frequencies_hz[mask], raw[mask])
    assert np.max(abs(payload.matrix[0] - np.asarray(cropped_first.values))) > 0.01


def test_combined_filters_reduce_noise_and_suppress_spike_without_flattening_resonance(
    make_archive,
):
    frequencies_hz = np.linspace(200e6, 1200e6, 2001)
    clean = -85 + _gaussian(frequencies_hz, 700e6, 90e6, 6)
    noise = np.random.default_rng(408).normal(0, 0.12, frequencies_hz.size)
    raw = clean + noise + _gaussian(frequencies_hz, 400e6, 3e6, 8)
    path = make_archive(frequencies_hz, [raw])
    # EMI's missing-history note must not prevent either other filter from working.
    result = _process(path, ResultProcessing(modes=MODES[-1]), 0)
    filtered = np.asarray(result.values)
    spike = np.argmin(abs(frequencies_hz - 400e6))
    resonance = np.argmin(abs(frequencies_hz - 700e6))
    quiet = (abs(frequencies_hz - 400e6) > 20e6) & (abs(frequencies_hz - 700e6) > 150e6)
    assert abs(filtered[spike] - clean[spike]) < 0.3
    assert np.std((filtered - clean)[quiet]) < 0.6 * np.std(noise[quiet])
    assert filtered[resonance] == pytest.approx(-79, abs=0.3)
    assert any("0/5" in note for note in result.notes)
