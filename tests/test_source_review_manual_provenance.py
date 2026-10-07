"""Manual archives preserve backend, selection and save-time metadata evidence."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import h5py
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.manual_save import ManualSpectrumSaveOptions
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.domain.errors import ExecutionError
from app.domain.manual_metadata import ManualMetadataValue
from app.storage import Hdf5RunReader, ManualSpectrumArchive, ManualSpectrumSaveMode
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
