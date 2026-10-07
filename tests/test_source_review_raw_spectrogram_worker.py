"""Unfiltered history assembly must never run on the GUI thread."""

from dataclasses import replace

import numpy as np
from PySide6.QtCore import QThread

from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_raw_matrix_is_built_off_gui_and_window_change_rebuilds(shared_page, monkeypatch):
    app, page, _context, _profile, trace, *_ = shared_page
    page._spectrogram_buffer.clear()
    page._spectrogram_buffer.append(trace, now=100.)
    page._spectrogram_buffer.append(replace(trace), now=131.)
    monkeypatch.setattr(page, "_selected_cleanup_modes", lambda: ())
    page._analysis_parameters = replace(page._analysis_parameters, temporal_average_frames=1)
    page._invalidate_spectrogram_filters()
    threads = []
    original_stack = np.stack

    def stack(*args, **kwargs):
        threads.append(QThread.currentThread() == app.thread())
        return original_stack(*args, **kwargs)

    def forbidden_snapshot(*args, **kwargs):
        raise AssertionError("GUI assembled a full history snapshot")

    monkeypatch.setattr(np, "stack", stack)
    monkeypatch.setattr(page._spectrogram_buffer, "snapshot", forbidden_snapshot)
    assert page._raw_spectrogram_matrix(30) is None
    wait_until(app, lambda: page._raw_spectrogram_matrix(30) is not None)
    short = page._raw_spectrogram_matrix(30)
    assert short[2].shape == (1, len(trace.powers_dbm))
    assert short[3:] == ("dBm", "Raw")
    np.testing.assert_array_equal(short[2][0], trace.powers_dbm)
    assert not short[2].flags.writeable
    assert page._raw_spectrogram_matrix(60) is None
    wait_until(app, lambda: page._raw_spectrogram_matrix(60) is not None)
    assert page._raw_spectrogram_matrix(60)[2].shape[0] == 2
    assert threads and not any(threads)
