"""Actual Anritsu and Results controls must display the same reference curve."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.spectrum.comparison_view import compare_power
from app.spectrum.display_model import build_display_state
from app.ui.results.processing import ResultProcessing, ResultSpectrumProcessor
from app.ui.results.spectrum_views import SpectrumView, SpectrumViewControls, _build_view
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def spectra():
    frequencies = tuple(np.linspace(1e6, 2e6, 101))
    signal = np.linspace(-80., -30., 101)
    reference = signal + np.sin(np.linspace(0., 12., 101)) * 2.
    timestamp = datetime(2026, 11, 3, tzinfo=UTC)
    return (SpectrumTrace(frequencies, tuple(signal), timestamp, "TRAC1"),
            SpectrumTrace(frequencies, tuple(reference), timestamp, "TRAC1"))


def test_raw_auto_is_the_unchanged_analyzer_trace_in_dbm():
    signal, _reference = spectra()
    session = SimpleNamespace(path="unused.h5")
    raw = _build_view(session, 0, signal.frequencies_hz, signal.powers_dbm, "dBm", (),
                      ResultProcessing(modes=("denoise",)),
                      SpectrumView(show_analysis=False, show_raw=True), None)
    assert len(raw.traces) == 1
    assert raw.traces[0].unit == "dBm"
    np.testing.assert_array_equal(raw.traces[0].values, signal.powers_dbm)
    np.testing.assert_array_equal(raw.traces[0].frequencies_hz, signal.frequencies_hz)


def test_raw_and_unprocessed_analysis_are_identical_in_auto_units():
    signal, _reference = spectra()
    session = SimpleNamespace(path="unused.h5")
    analysis = _build_view(session, 0, signal.frequencies_hz, signal.powers_dbm, "dBm", (),
                           ResultProcessing(), SpectrumView(), None)
    raw = _build_view(session, 0, signal.frequencies_hz, signal.powers_dbm, "dBm", (),
                      ResultProcessing(), SpectrumView(show_analysis=False, show_raw=True), None)
    assert raw.traces[0].unit == analysis.traces[0].unit == "dBm"
    np.testing.assert_array_equal(raw.traces[0].values, analysis.traces[0].values)


def test_results_raw_reference_is_bitwise_equal_to_anritsu_comparison():
    signal, reference = spectra()
    baseline = SimpleNamespace(index=1, purpose="reference", powers_dbm=reference.powers_dbm,
        frequencies_hz=reference.frequencies_hz, configuration_fingerprint=None,
        acquired_at_utc=reference.acquired_at_utc.isoformat(), average_count=1,
        selected_sweep=None, source_sweep_indices=(), collection_average_count=1)
    reader = SimpleNamespace(reference=lambda *_: baseline)
    result = ResultSpectrumProcessor("unused.h5", ResultProcessing("subtract_reference_signed", 1),
                                     reader=reader).process(0, signal.frequencies_hz, signal.powers_dbm)
    live = compare_power(signal, frame_id=0, reference=reference).by_key["reference_difference"]
    assert result.unit == live.unit == "W"
    np.testing.assert_array_equal(result.values, live.values)


@pytest.mark.parametrize("comparison", ["show_raw", "show_background", "show_reference"])
def test_quick_comparison_does_not_leave_implicit_analysis_raw_overlay(shell_qt_application, comparison):
    controls = SpectrumViewControls()
    controls.resize(1250, 250)
    controls.show()
    try:
        assert controls.boxes["show_analysis"].isChecked()
        controls.boxes[comparison].setChecked(True)
        shell_qt_application.processEvents()
        assert not controls.state.show_analysis
        assert not controls.boxes["show_analysis"].isEnabled()
        assert getattr(controls.state, comparison)
        controls.boxes[comparison].setChecked(False)
        assert controls.state.show_analysis
        assert controls.boxes["show_analysis"].isEnabled()
    finally:
        controls.close()
        controls.deleteLater()
        shell_qt_application.processEvents()


@pytest.mark.parametrize("operation", ["difference_db", "ratio_linear", "add_power", "subtract_power", "multiply_linear"])
def test_normal_reference_math_is_identical_to_main_anritsu(operation):
    signal, reference = spectra()
    baseline = SimpleNamespace(index=1, purpose="reference", powers_dbm=reference.powers_dbm,
        frequencies_hz=reference.frequencies_hz, configuration_fingerprint=None,
        acquired_at_utc=reference.acquired_at_utc.isoformat(), average_count=1,
        selected_sweep=None, source_sweep_indices=(), collection_average_count=1)
    result = ResultSpectrumProcessor("unused.h5", ResultProcessing(operation, 1),
        reader=SimpleNamespace(reference=lambda *_: baseline)).process(0, signal.frequencies_hz, signal.powers_dbm)
    live = build_display_state(raw=signal, averaged=None, reference=reference,
        reference_operation=operation, visible={"raw": False, "reference": False, "processed": True}, frame_id=0)
    assert result.unit == live.by_key["processed"].unit
    np.testing.assert_array_equal(result.values, live.by_key["processed"].values)


def test_results_reference_checkbox_defaults_to_anritsu_subtract_db(shell_qt_application):
    controls = SpectrumViewControls()
    try:
        controls.boxes["show_reference"].setChecked(True)
        assert controls.state.reference_operation == "difference_db"
        assert controls.reference_operation.currentText() == "Subtract [dB]"
        assert not controls.state.show_raw and not controls.state.show_analysis
        assert not controls.boxes["show_raw"].isEnabled()
        assert not controls.unit.isEnabled()
    finally:
        controls.close()
        controls.deleteLater()
        shell_qt_application.processEvents()


@pytest.mark.parametrize("operation", ["difference_db", "ratio_linear", "add_power", "subtract_power", "multiply_linear"])
def test_actual_results_compare_controls_select_same_math_as_main(shell_qt_application, operation):
    signal, reference = spectra()
    baseline = SimpleNamespace(index=1, purpose="reference", powers_dbm=reference.powers_dbm,
        frequencies_hz=reference.frequencies_hz, configuration_fingerprint=None,
        acquired_at_utc=reference.acquired_at_utc.isoformat(), average_count=1,
        selected_sweep=None, source_sweep_indices=(), collection_average_count=1)
    session = SimpleNamespace(path="unused.h5", reference=lambda *_: baseline,
                              references=lambda *_args, **_kwargs: (baseline,))
    controls = SpectrumViewControls()
    try:
        controls.boxes["show_reference"].setChecked(True)
        index = controls.reference_operation.findData(operation)
        assert index >= 0
        controls.reference_operation.setCurrentIndex(index)
        payload = _build_view(session, 0, signal.frequencies_hz, signal.powers_dbm, "dBm", (),
                              ResultProcessing(), controls.state, None)
        live = build_display_state(raw=signal, averaged=None, reference=reference,
            reference_operation=operation, visible={"raw": False, "reference": False, "processed": True}, frame_id=0)
        assert len(payload.traces) == len(live.traces) == 1
        assert payload.traces[0].unit == live.by_key["processed"].unit
        np.testing.assert_array_equal(payload.traces[0].values, live.by_key["processed"].values)
    finally:
        controls.close()
        controls.deleteLater()
        shell_qt_application.processEvents()


def test_reference_subtract_db_keeps_selected_filters_like_anritsu(shell_qt_application):
    from app.spectrum.display_processing import clean_display_spectrum
    from app.spectrum.preview_processing import SpectrumPreviewProcessor

    signal, reference = spectra()
    baseline = SimpleNamespace(index=1, purpose="reference", powers_dbm=reference.powers_dbm,
        frequencies_hz=reference.frequencies_hz, configuration_fingerprint=None,
        acquired_at_utc=reference.acquired_at_utc.isoformat(), average_count=1,
        selected_sweep=None, source_sweep_indices=(), collection_average_count=1)
    session = SimpleNamespace(path="unused.h5", reference=lambda *_: baseline,
                              references=lambda *_args, **_kwargs: (baseline,))
    controls = SpectrumViewControls()
    processing = ResultProcessing(modes=("denoise",))
    try:
        controls.boxes["show_reference"].setChecked(True)
        payload = _build_view(session, 0, signal.frequencies_hz, signal.powers_dbm, "dBm", (),
                              processing, controls.state, None)
        expected, _stats = SpectrumPreviewProcessor().process(signal.powers_dbm,
            frequencies_hz=signal.frequencies_hz, modes=processing.modes,
            parameters=processing.parameters, reference_values_dbm=reference.powers_dbm,
            reference_operation="difference_db", cleaner=clean_display_spectrum)
        assert payload.traces[0].unit == expected.unit == "dB"
        assert not np.array_equal(expected.values, np.asarray(signal.powers_dbm) - reference.powers_dbm)
        np.testing.assert_array_equal(payload.traces[0].values, expected.values)
    finally:
        controls.close()
        controls.deleteLater()
        shell_qt_application.processEvents()
