"""Results EMI replays the same checkpoint history as live Anritsu."""

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.domain.recipe_spectrum import RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.spectrum.analysis import SpectrumAnalysisParameters
from app.spectrum.display_processing import clean_display_spectrum
from app.spectrum.preview_processing import SpectrumPreviewProcessor
from app.spectrum.processing import average_dbm_traces
from app.storage import Hdf5RunReader, Hdf5RunWriter
from app.ui.results.processing import ResultProcessing, read_processed_private


def _archive(tmp_path, rows):
    frequencies = tuple(np.linspace(1e6, 2e6, 101))
    path = tmp_path / "reference-history.h5"

    def trace(values):
        return SpectrumTrace(frequencies, tuple(values), datetime.now(UTC), "TRAC1")

    writer = Hdf5RunWriter(
        path, recipe_source="name: history\n", settings_source="schema_version: 1\n",
        plan_hash="history", device_idn={}, expected_points=3,
    )
    try:
        writer.store_reference(
            trace(np.full(101, -90.)),
            acquisition_metadata={"purpose": "reference", "configuration_fingerprint": "same"},
        )
        # Neighbors deliberately share analyzer settings, but are different setpoints.
        neighbor = (tuple(rows[-1]),) * 24
        for checkpoint, block in enumerate((neighbor, rows, neighbor)):
            setpoints = {"keithley.A.current": checkpoint * .001}
            indices = []
            for ordinal, values in enumerate(block):
                indices.append(writer.store_recipe_spectrum_sweep(RecipeSpectrumSweep(
                    frequencies_hz=frequencies, powers_dbm=values,
                    recipe_node_id="signal", execution_id="execution",
                    role=SpectrumFrameRole.SIGNAL, average_index=ordinal,
                    average_count=len(block), acquired_at_s=1000. + checkpoint * 100 + ordinal,
                    configuration_generation=1, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
                    setpoints_si=tuple(setpoints.items()),
                )))
            writer.append(MeasurementPoint(
                index=checkpoint, setpoints=setpoints, measurements={},
                metadata={"raw_recipe_sweep_indices": indices,
                          "spectrum_processing_v1": {"configuration_fingerprint": "same"}},
            ), trace(average_dbm_traces(block)))
    finally:
        writer.close("completed")
    return path


def _rows(count, variable_count):
    rows = []
    for index in range(count):
        values = np.full(101, -90.)
        if index < variable_count:
            values[50] = -30. if index % 2 == 0 else -60.
        else:
            values[50] = -40.
        rows.append(tuple(values))
    return tuple(rows)


def _live(raw, rows, parameters):
    # Anritsu supplies 24 individually corrected rows to EMI, independently
    # of the longer raw-power history needed for temporal averaging.
    return SpectrumPreviewProcessor().process(
        raw.powers_dbm, frequencies_hz=raw.frequencies_hz,
        modes=("emi_reject",), parameters=parameters,
        history=tuple(tuple(value + 90. for value in row) for row in rows[-24:]),
        power_rows=rows, timestamps_s=tuple(1100. + i for i in range(len(rows))),
        reference_values_dbm=(-90.,) * 101, reference_operation="difference_db",
        cleaner=clean_display_spectrum,
    )[0]


def test_emi_matches_live_with_24_same_checkpoint_sweeps(tmp_path):
    rows = _rows(24, 19)
    path = _archive(tmp_path, rows)
    point = Hdf5RunReader.point(path, 1)
    raw = Hdf5RunReader.spectrum(path, 1)
    state = ResultProcessing("difference_db", 0, ("emi_reject",))
    expected = _live(raw, rows, state.parameters)
    assert expected.values[50] > 40.  # The full history proves the peak is variable.

    result = read_processed_private(path, point, state)

    np.testing.assert_allclose(result.values, expected.values, atol=1e-12)
    assert result.unit == expected.unit == "dB"


def test_emi_uses_last_24_while_averaging_retains_32_same_checkpoint_sweeps(tmp_path):
    rows = _rows(32, 8)
    path = _archive(tmp_path, rows)
    point = Hdf5RunReader.point(path, 1)
    raw = Hdf5RunReader.spectrum(path, 1)
    parameters = replace(SpectrumAnalysisParameters(), temporal_average_frames=32)
    expected = _live(raw, rows, parameters)
    np.testing.assert_allclose(expected.values, 0., atol=1e-12)

    result = read_processed_private(
        path, point, ResultProcessing("difference_db", 0, ("emi_reject",), parameters),
    )

    np.testing.assert_allclose(result.values, expected.values, atol=1e-12)
    assert "32/32" in result.method


def test_short_checkpoint_does_not_borrow_neighboring_emi_history(tmp_path):
    rows = _rows(4, 0)
    path = _archive(tmp_path, rows)
    point = Hdf5RunReader.point(path, 1)
    state = ResultProcessing("difference_db", 0, ("emi_reject",))

    result = read_processed_private(path, point, state)

    assert result.values[50] == 50.
    assert any("4/5" in note for note in result.notes)
