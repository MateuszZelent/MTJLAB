"""Numerical equivalence and publication invariants of Live optimizations."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionViewSnapshot
from app.domain.spectrum_correction import CorrectionQuality
from app.spectrum.narrow_spikes import _median, filter_narrow_spikes
from tests.test_background_display_filters import corrected_page as corrected_page  # noqa: PLC0414
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def reference_median(values, window):
    frames = np.lib.stride_tricks.sliding_window_view(
        np.pad(values, window // 2, mode="edge"), window,
    )
    return np.median(frames, axis=1)


@pytest.mark.parametrize("window", [9, 81, 221, 257])
@pytest.mark.parametrize("scale", [1., 1e-12])
def test_native_rank_filter_matches_centered_numpy_median(window, scale):
    rng = np.random.default_rng(12)
    for values in (rng.normal(size=1025), np.arange(1025.), np.zeros(1025),
                   np.tile([1., -1., 0., 0., 1.], 205)):
        values = values * scale
        np.testing.assert_array_equal(_median(values, window), reference_median(values, window))
        np.testing.assert_array_equal(_median(values[::-1], window), reference_median(values[::-1], window))


def test_native_median_preserves_replacement_mask_and_protected_resonance():
    x = np.linspace(200e6, 2.4e9, 10001)
    y = np.random.default_rng(9).normal(0, 1e-13, x.size)
    y += 2e-11 * np.exp(-4 * np.log(2) * ((x - 1.2e9) / 100e6) ** 2)
    y[2500] += 8e-11
    y[4000] -= 9e-11
    parameters = {"protected_regions_hz": ((745e6, 755e6),)}
    native = filter_narrow_spikes(x, y, **parameters)
    with patch("app.spectrum.narrow_spikes._median", side_effect=reference_median):
        previous = filter_narrow_spikes(x, y, **parameters)
    assert native == previous
    assert 2500 not in native.peak_indices and 4000 in native.peak_indices


def test_unchanged_snapshot_skips_render_but_new_frame_and_stale_quality_publish(corrected_page):
    application, page, context, source, _controller = corrected_page
    page.resize(1450, 900)
    page.show()
    application.processEvents()
    workspace = page.correction_workspace
    workspace._context = context
    workspace._latest_result = None
    raw = SpectrumTrace(tuple(context.frequencies_hz), tuple(np.full(1001, -90.)),
                        datetime.fromtimestamp(source.completed_at_s, UTC), "TRAC1")
    published = []
    workspace.display_changed.connect(lambda *args: published.append(args))
    with patch.object(workspace.raw_plot, "set_trace", wraps=workspace.raw_plot.set_trace) as raw_draw, \
            patch.object(workspace.corrected_plot, "set_trace", wraps=workspace.corrected_plot.set_trace) as corrected_draw:
        workspace._receive_committed_view(CorrectionViewSnapshot(source, raw))
        assert len(published) == 1
        for _ in range(20):
            workspace._receive_committed_view(CorrectionViewSnapshot(replace(source), raw))
            workspace._render()
        assert len(published) == 1
        assert not raw_draw.called and not corrected_draw.called
        stale = replace(source, quality=CorrectionQuality.STALE)
        workspace._receive_committed_view(CorrectionViewSnapshot(stale, raw))
        workspace._render()
        assert len(published) == 2 and "stale" in published[-1][2]
        latest = replace(stale, frame_id=source.frame_id + 1)
        workspace._receive_committed_view(CorrectionViewSnapshot(latest, raw))
        workspace._render()
        assert len(published) == 3
        page._open_recording_setup()
        page.recording_tabs.setCurrentIndex(1)
        application.processEvents()
        assert raw_draw.called and corrected_draw.called
        assert workspace.corrected_plot.isVisible() and workspace.corrected_plot.height() > 70
        np.testing.assert_array_equal(workspace.corrected_plot._traces["Signed residual"][1], latest.values_w)


def test_filtered_preview_refreshes_stale_quality_without_a_new_sweep(corrected_page):
    application, page, context, source, _controller = corrected_page
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    workspace = page.correction_workspace
    old = workspace._profile
    policy = page._station_settings.anritsu.spectrum_correction
    updated = policy.model_copy(update={"reference_policy": policy.reference_policy.model_copy(update={"maximum_age": "1 s"})})
    settings = page._station_settings
    page._station_settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "anritsu": settings.anritsu.model_copy(update={"spectrum_correction": updated}),
    })})
    workspace._profile = replace(old, started_at_s=1, completed_at_s=2)
    page._shared_background_changed()
    assert page._cleanup_result is None and "stale" in page.analysis_status.text().lower()
