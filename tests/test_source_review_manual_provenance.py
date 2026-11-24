"""Manual archives preserve backend, selection and save-time metadata evidence."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import h5py
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import ReferenceSpectrum, SpectrumTrace
from app.devices.anritsu_ms2830a.ui.manual_save import ManualSpectrumSaveOptions
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.domain.errors import ExecutionError
from app.domain.manual_metadata import ManualMetadataValue
from app.storage import Hdf5RunReader, ManualSpectrumArchive, ManualSpectrumSaveMode
from app.ui.results.page import ResultsPage
from tests.helpers import loaded_settings
from tests.test_main_window import wait_for_ui


def trace():
    return SpectrumTrace((1e6, 2e6, 3e6), (-70., -60., -65.), datetime.now(timezone.utc), "TRAC1")


@pytest.mark.parametrize("simulation", [False, True])
def test_archive_persists_backend_and_refuses_mixed_resume(tmp_path, simulation):
    path = tmp_path / "manual.h5"
    archive = ManualSpectrumArchive(simulation=simulation)
    archive.save(trace(), destination=path, mode="append")
    archive.close()
    assert Hdf5RunReader.detail(path).simulation_metadata["enabled"] is simulation
    incompatible = ManualSpectrumArchive(simulation=not simulation)
    with pytest.raises(ExecutionError, match="simulation provenance"):
        incompatible.save(trace(), destination=path, mode="append")
    assert Hdf5RunReader.summary(path).point_count == 1
    matching = ManualSpectrumArchive(simulation=simulation)
    matching.save(trace(), destination=path, mode="append")
    matching.close()
    assert Hdf5RunReader.summary(path).point_count == 2


def test_new_files_use_current_capture_context_and_reject_mixed_backend(tmp_path):
    archive = ManualSpectrumArchive(simulation=True, settings_source="old")
    context = {"schema_version": 1, "settings_source": "revision: 1", "device_idn": {"anritsu": "SIM"},
               "operator_context": {}, "simulation": True}
    first = archive.save(trace(), destination=tmp_path / "first.h5", mode="timestamped", capture_context=context)
    context["settings_source"] = "revision: 2"
    second = archive.save(trace(), destination=tmp_path / "second.h5", mode="timestamped", capture_context=context)
    for result, expected in ((first, "revision: 1"), (second, "revision: 2")):
        with h5py.File(result.path, "r") as handle:
            assert handle["run/settings_yaml"].asstr()[()] == expected
    context["simulation"] = False
    rejected = tmp_path / "rejected.h5"
    with pytest.raises(ExecutionError, match="simulation provenance"):
        archive.save(trace(), destination=rejected, mode="append", capture_context=context)
    assert not rejected.exists()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    widget = AnritsuPage(Mock(), loaded_settings(), single_sweep_available=True)
    yield widget
    assert wait_for_ui(lambda: widget._manual_archive_thread is None, timeout_ms=30_000)
    widget.close_manual_archive_session()
    assert wait_for_ui(lambda: widget._manual_archive_thread is None, timeout_ms=30_000)
    assert widget.shutdown_analysis()
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_selected_metadata_is_refreshed_and_missing_value_blocks_save(page, tmp_path):
    def metadata(value):
        return ManualMetadataValue("keithley.A.current_a", "Keithley", "Current", "current", "A", value)
    current = [metadata(.001)]
    page.set_manual_archive_context(metadata_provider=lambda: tuple(current), simulation=True)
    path = tmp_path / "capture.h5"
    page._apply_manual_save_options(ManualSpectrumSaveOptions(path, ManualSpectrumSaveMode.APPEND, "selected", tuple(current), "raw"))
    page._show_trace(trace())
    current[:] = [metadata(.002)]
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    point, = Hdf5RunReader.points(path)
    assert point.measurements["keithley.A.current_a"] == .002
    assert point.metadata["metadata_snapshot_kind"] == "last_confirmed_at_save"
    assert point.metadata["metadata_sampled_at_utc"]
    assert Hdf5RunReader.detail(path).simulation_metadata["enabled"] is True
    current.clear()
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    assert "no longer available" in page.manual_save_status.text()
    assert Hdf5RunReader.summary(path).point_count == 1


def test_each_capture_records_current_settings_identity_and_operator(page, tmp_path):
    context = {"settings": "revision: 1", "idn": "SIM-ONE", "operator": "first"}
    page.set_manual_archive_context(
        simulation=True,
        settings_source_provider=lambda: context["settings"],
        device_idn_provider=lambda: {"anritsu": context["idn"]},
        operator_context_provider=lambda: {"name": context["operator"]},
    )
    path = tmp_path / "context.h5"
    page._apply_manual_save_options(ManualSpectrumSaveOptions(path, ManualSpectrumSaveMode.APPEND, "none", (), "raw"))
    page._show_trace(trace())
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    context.update(settings="revision: 2", idn="SIM-TWO", operator="second")
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    points = list(Hdf5RunReader.points(path))
    assert len(points) == 2
    first, second = [point.metadata["capture_context_at_save"] for point in points]
    assert first["settings_source"] == "revision: 1"
    assert first["device_idn"]["anritsu"] == "SIM-ONE"
    assert second["settings_source"] == "revision: 2"
    assert second["device_idn"]["anritsu"] == "SIM-TWO"
    assert second["operator_context"] == {"name": "second"}
    assert second["simulation"] is True


@pytest.mark.parametrize("view", ["overlay", "hidden", "absolute"])
def test_timestamped_manual_save_restores_view_and_copies_active_reference(page, app, tmp_path, view, monkeypatch):
    reference_trace = SpectrumTrace((1e6, 2e6, 3e6), (-75., -74., -73.),
                                    datetime.now(timezone.utc), "TRAC1")
    page.set_manual_archive_context(simulation=True)
    page._set_reference(ReferenceSpectrum(reference_trace, "averaged", 32, reference_trace.acquired_at_utc,
                                          source_file="imported-reference.h5"))
    page._show_trace(trace())
    page.show_raw.setChecked(True)
    page.show_reference.setChecked(view == "overlay")
    if view == "absolute":
        monkeypatch.setattr(page, "_validate_display_reference", lambda _frequencies: None)
        page.quick_curves["reference"].setChecked(True)
        page.show_absolute.setChecked(True)
        page._refresh_spectrum_display()
        assert any("absolute_difference" in curve.provenance for curve in page._display_state.traces)
    expected_view = page._display_state.traces
    page._apply_manual_save_options(ManualSpectrumSaveOptions(
        tmp_path / "manual.h5", ManualSpectrumSaveMode.TIMESTAMPED, "none", (), "raw"))
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    path = page._manual_archive_last_path
    stored = Hdf5RunReader.spectrum(path, 0)
    assert stored.reference_index == 0
    reference = Hdf5RunReader.reference(path, stored.reference_index)
    assert reference.powers_dbm == reference_trace.powers_dbm
    assert reference.average_count == 32
    assert [curve.values for curve in stored.saved_display.traces] == [curve.values for curve in expected_view]

    results = ResultsPage(str(tmp_path))
    results.resize(1300, 900)
    results.show()
    try:
        results._on_file_selected(path)
        assert wait_for_ui(lambda: results._result_task is None)
        results.result_tabs.setCurrentIndex(results._spectrum_index)
        results.spectrum_tab.show_stored_spectrum(0)
        tab = results.spectrum_tab
        assert wait_for_ui(lambda: tab.spectrum_variant_combo.currentData() == "saved_view"
                           and tab.spectrum_view.currentWidget() is tab.spectrum_plot)
        assert set(tab.spectrum_plot._traces) == {curve.label for curve in expected_view}
        if view == "hidden":
            tab.spectrum_variant_combo.setCurrentIndex(tab.spectrum_variant_combo.findData("reference"))
            assert wait_for_ui(lambda: set(tab.spectrum_plot._traces) == {"Reference spectrum"})
    finally:
        results.close()
        results.deleteLater()
        app.processEvents()


def test_append_links_each_checkpoint_to_its_reference(tmp_path):
    path = tmp_path / "append.h5"
    archive = ManualSpectrumArchive()
    sample = trace()
    first = ReferenceSpectrum(sample, "averaged", 32, sample.acquired_at_utc)
    other = SpectrumTrace(sample.frequencies_hz, (-71., -65., -67.), sample.acquired_at_utc, "TRAC1")
    second = ReferenceSpectrum(other, "imported", 16, other.acquired_at_utc, source_file="other.h5")
    for reference in (first, first, second, None):
        archive.save(sample, destination=path, mode="append", reference=reference)
    archive.close()
    assert [Hdf5RunReader.spectrum(path, i).reference_index for i in range(4)] == [0, 0, 1, None]
    assert [item.average_count for item in Hdf5RunReader.references(path)] == [32, 16]
    assert Hdf5RunReader.reference(path, 1).powers_dbm == other.powers_dbm


def test_missing_variant_and_execution_preview_cannot_be_saved_as_raw(page, app, tmp_path):
    sample = trace()
    page._show_trace(sample)
    with pytest.raises(ValueError, match="unavailable"):
        page._manual_trace_payload("missing_processed_variant")
    page._apply_manual_save_options(ManualSpectrumSaveOptions(tmp_path / "preview.h5", ManualSpectrumSaveMode.APPEND, "none", (), "raw"))
    page._show_execution_trace({"frequency_hz": sample.frequencies_hz, "power_dbm": sample.powers_dbm, "source_points": 10001, "timestamp_utc": sample.acquired_at_utc.isoformat()})
    page.resize(1366, 768)
    page.show()
    app.processEvents()
    assert not page.save_manual_spectrum.isEnabled()
    with pytest.raises(ValueError, match="Execution previews"):
        page._manual_trace_payload("raw")
    page._save_configured_manual_spectrum()
    assert wait_for_ui(lambda: page._manual_archive_thread is None, timeout_ms=30_000)
    assert not (tmp_path / "preview.h5").exists()
    preview_generation = page._analysis_generation
    page._analysis_raw_snapshot = page._latest_trace
    page._show_trace(sample)
    assert page._analysis_raw_snapshot is None
    assert page._invalidated_before_generation >= preview_generation
    assert page.save_manual_spectrum.isEnabled()
    assert page._manual_trace_payload("raw")[0] is sample
