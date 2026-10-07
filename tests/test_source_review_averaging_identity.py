from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from tests.helpers import loaded_settings


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    widget = AnritsuPage(Mock(), loaded_settings(), single_sweep_available=True)
    yield widget
    widget.cancel_averaging()
    assert widget.shutdown_analysis()
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("destination", ["spectrum", "reference"])
@pytest.mark.parametrize("change", ["grid", "generation", "trace", "invalid_grid"])
def test_averaging_rejects_changed_identity_without_replacing_result(page, destination, change):
    original = SpectrumTrace((1e6, 2e6, 3e6), (-70., -60., -50.), datetime.now(timezone.utc), "TRAC1", configuration_generation=4)
    page._averaged_trace = original
    old_reference = page._reference_spectrum
    page.average_count.setValue(2)
    page.reference_average_count.setValue(2)
    page._start_temporal_averaging(destination)
    page._result("single_sweep", original)
    changed = {
        "grid": replace(original, frequencies_hz=(1e6, 2.1e6, 3e6)),
        "generation": replace(original, configuration_generation=5),
        "trace": replace(original, trace_name="TRAC2"),
        "invalid_grid": replace(original, frequencies_hz=(1e6, float("nan"), 3e6)),
    }[change]
    page._result("single_sweep", changed)
    assert not page._averaging_active
    assert page._averager.count == 0
    assert page._averaging_source_trace is None
    assert page._averaged_trace is original
    assert page._reference_spectrum is old_reference
    assert "Averaging stopped" in page.info.text()


def test_new_average_can_use_new_grid_and_generation_after_failed_series(page):
    first = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(timezone.utc), "TRAC1", configuration_generation=1)
    second = replace(first, frequencies_hz=(2e6, 3e6), configuration_generation=2, powers_dbm=(-50., -50.))
    page.average_count.setValue(2)
    page.start_averaging()
    page._result("single_sweep", first)
    page._result("single_sweep", second)
    page.start_averaging()
    page._result("single_sweep", second)
    page._result("single_sweep", second)
    assert not page._averaging_active
    assert page._averaged_trace.frequencies_hz == second.frequencies_hz
    assert page._averaged_trace.configuration_generation == 2
    assert page._averaged_trace.powers_dbm == pytest.approx((-50., -50.))


def test_live_average_ignores_inflight_poll_and_requests_qualified_sweep(page):
    sample = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(timezone.utc), "TRAC1")
    page._timer.start(10000)
    page._fetch_pending = True
    page.start_averaging()
    assert page._resume_live_after_averaging
    page._controller.call.reset_mock()
    page._result("fetch_current_trace_fast", sample)
    assert page._averager.count == 0
    page._controller.call.assert_called_once_with("single_sweep", "TRAC1")
    page._result("single_sweep", sample)
    assert page._averager.count == 1


@pytest.mark.parametrize("destination", ["spectrum", "reference"])
def test_cancel_restart_discards_old_inflight_sweep(page, destination):
    old = SpectrumTrace((1e6, 2e6), (-10., -10.), datetime.now(timezone.utc), "TRAC1")
    fresh = replace(old, powers_dbm=(-60., -60.))
    page.average_count.setValue(1)
    page.reference_average_count.setValue(1)
    page._start_temporal_averaging(destination)
    page.cancel_averaging()
    page._controller.call.reset_mock()
    page._start_temporal_averaging(destination)
    page._controller.call.assert_not_called()
    page._result("single_sweep", old)
    assert page._averager.count == 0
    assert page._averaging_active
    assert page._latest_trace is not old
    page._controller.call.assert_called_once_with("single_sweep", "TRAC1")
    page._result("single_sweep", fresh)
    assert not page._averaging_active
    assert not page._discard_cancelled_average_frame
    if destination == "spectrum":
        assert page._averaged_trace.powers_dbm == pytest.approx(fresh.powers_dbm)
    else:
        assert page._reference_spectrum is not None


def test_cancel_does_not_replace_completed_trace_or_queue_manual_reference(page):
    old = SpectrumTrace((1e6, 2e6), (-10., -10.), datetime.now(timezone.utc), "TRAC1")
    page._latest_trace = replace(old, powers_dbm=(-60., -60.))
    completed = page._latest_trace
    page.start_averaging()
    page.cancel_averaging()
    page._controller.call.reset_mock()
    page.read_once()
    page.acquire_reference_once()
    page._controller.call.assert_not_called()
    page._result("single_sweep", old)
    assert page._latest_trace is completed
    assert not page._fetch_pending
    page.read_once()
    page._controller.call.assert_called_once_with("single_sweep", "TRAC1")


def test_failed_cancelled_request_clears_discard_state_and_stops_new_series(page):
    page.start_averaging()
    page.cancel_averaging()
    page.start_averaging()
    page._error("single_sweep", "transport timeout")
    assert not page._discard_cancelled_average_frame
    assert not page._fetch_pending
    assert not page._averaging_active
    # Communication failure is reported, not silently retried in a new series.
    assert "Averaging stopped" in page.info.text()
    page.average_count.setValue(1)
    page.start_averaging()
    sample = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(timezone.utc), "TRAC1")
    page._result("single_sweep", sample)
    assert page._averaged_trace.powers_dbm == pytest.approx(sample.powers_dbm)
