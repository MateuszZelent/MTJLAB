"""Global difference test over verified closed raw REF/SIGNAL archives."""

import math

from app.spectrum.spectral_difference_test import (
    SpectralDifferenceTestConfig, difference_test_buffer_bytes, spectral_difference_test,
)
from .resonance_bootstrap_store import analyze_spectral_archives


def analyze_spectral_difference_archives(reference_source, signal_source, destination, *, block_sweeps,
                                     search_start_hz, search_stop_hz, discard_partial_tail=False,
                                     config=SpectralDifferenceTestConfig(), cancellation_check=None,
                                     progress_callback=None):
    if (isinstance(search_start_hz, bool) or isinstance(search_stop_hz, bool)
            or not math.isfinite(search_start_hz) or not math.isfinite(search_stop_hz)
            or search_start_hz < 0 or search_stop_hz <= search_start_hz):
        raise ValueError("Choose a finite nonnegative, increasing search interval in Hz.")

    def analyzer(frequencies, references, signals):
        mask = (frequencies >= search_start_hz) & (frequencies <= search_stop_hz)
        report = spectral_difference_test(frequencies, references, signals, mask, config=config,
            cancellation_check=cancellation_check, progress_callback=progress_callback)
        report["requested_search_interval_hz"] = [search_start_hz, search_stop_hz]
        return report

    return analyze_spectral_archives(reference_source, signal_source, destination, block_sweeps=block_sweeps,
        discard_partial_tail=discard_partial_tail, config=config, maximum_blocks=256,
        buffer_bytes=difference_test_buffer_bytes, analyzer=analyzer, cancellation_check=cancellation_check)
