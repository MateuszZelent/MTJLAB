"""Cancel cannot promote a late background to a successful recording."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace


def test_stopping_reference_with_enough_frames_aborts_without_finalizing():
    cpu = Mock()
    workspace = SimpleNamespace(_recording_failed=False, _live_timings_s={}, _archive_path="test.h5", archive_label=Mock(),
        _interleaved=None, _started_monotonic=time.monotonic()-10, _kind="reference",
        progress=Mock(), _duration_s=1, _processor_config=SimpleNamespace(minimum_reference_sweeps=3),
        _stopping=True, _cpu=cpu, _message=Mock(), _request=Mock())
    SpectrumCorrectionWorkspace._processed(workspace, "frame", {
        "committed_point_count": 3, "reference_count": 3})
    cpu.finish_reference.assert_not_called()
    cpu.stop_session.assert_called_once_with("aborted")
    workspace._request.assert_not_called()


def test_late_reference_does_not_replace_profile_after_cancel():
    previous = object()
    workspace = SimpleNamespace(_recording_failed=False, _stopping=True, _profile=previous, _cpu=Mock())
    SpectrumCorrectionWorkspace._processed(workspace, "reference", object())
    assert workspace._profile is previous
    workspace._cpu.stop_session.assert_called_once_with("aborted")


@pytest.mark.parametrize("operation", ["start", "frame", "reference", "snapshot",
                                       "operator_state_confirmation", "processing_change"])
def test_faulted_recording_ignores_late_success(operation):
    workspace = SimpleNamespace(_recording_failed=True)
    # No state or device access is possible on this deliberately minimal object.
    SpectrumCorrectionWorkspace._processed(workspace, operation, object())
