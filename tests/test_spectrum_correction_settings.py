from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.spectrum_correction import TemporalAverageMode
from app.settings.models import SpectrumCorrectionSettings


def test_defaults_are_opt_in_and_do_not_fabricate_reference_validity():
    settings = SpectrumCorrectionSettings()
    config = settings.processor_config()
    assert not settings.enabled
    assert config.maximum_reference_age_s is None
    assert config.average_mode == TemporalAverageMode.EMA_PREVIEW
    assert config.working_memory_limit_bytes == 64 * 1024 * 1024


def test_explicit_units_normalize_once_at_processor_boundary():
    settings = SpectrumCorrectionSettings.model_validate({
        "temporal_average": {"mode": "window", "time_constant": "500 ms", "window_frames": 10},
        "reference_policy": {"maximum_age": "1e2 s"}, "render_interval": "25 ms",
    })
    assert settings.processor_config().time_constant_s == .5
    assert settings.processor_config().maximum_reference_age_s == 100


@pytest.mark.parametrize("payload", [
    {"calibration_duration": "60 Hz"}, {"calibration_duration": "60"},
    {"maximum_gap": "0 s"}, {"render_interval": "1 ms"},
    {"reference_policy": {"maximum_age": "-2 s"}},
    {"calibration_min_sweeps": True}, {"processing_queue_frames": 0},
    {"working_memory_limit_mib": 0}, {"archive_raw_frames": False},
    {"method": "automatic_notch"},
])
def test_invalid_or_lossy_settings_fail_closed(payload):
    with pytest.raises((ValidationError, ValueError)):
        SpectrumCorrectionSettings.model_validate(payload)
