"""Repeated execution snapshots do not become repeated measurements/redraws."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


def test_repeated_snapshots_preserve_recording_time_and_avoid_redraws(monkeypatch, shell_qt_application):
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        shell_qt_application.processEvents()
        keithley, rigol = window.keithley_page, window.rigol_page
        keithley.set_execution_controlled(True)
        rigol.set_execution_controlled(True)
        recorded = keithley._history_started_at_utc + 2
        snapshot = {"measurement_A": {
            "actual": {"voltage_v": .01, "current_a": .001, "power_w": .00001},
            "recorded_at_utc": datetime.fromtimestamp(recorded, timezone.utc).isoformat(),
        }}
        rigol_snapshot = {"channel_1": {"actual": {"frequency_hz": 2000., "waveform": "SIN"}}}
        keithley_projection = Mock(wraps=keithley.apply_execution_readback)
        rigol_projection = Mock(wraps=rigol.apply_execution_readback)
        monkeypatch.setattr(keithley, "apply_execution_readback", keithley_projection)
        monkeypatch.setattr(rigol, "apply_execution_readback", rigol_projection)
        for _ in range(100):
            keithley.apply_execution_event("runner_heartbeat", {}, deepcopy(snapshot), {})
            rigol.apply_execution_event("runner_heartbeat", {}, deepcopy(rigol_snapshot), {})
        assert len(keithley._measurement_history["A"]) == 1
        assert keithley._measurement_history["A"][0]["recorded_at_s"] == pytest.approx(recorded)
        assert keithley._measurement_history["A"][0]["elapsed_s"] == pytest.approx(2, abs=1e-5)
        assert keithley_projection.call_count == 1
        assert rigol_projection.call_count == 1
        snapshot["measurement_A"]["recorded_at_utc"] = datetime.fromtimestamp(recorded + 1, timezone.utc).isoformat()
        keithley.apply_execution_event("measurement", {}, snapshot, {})
        assert len(keithley._measurement_history["A"]) == 2  # same values, new measurement
        rigol_snapshot["channel_1"]["actual"]["frequency_hz"] = 3000.
        rigol.apply_execution_event("action_finished", {}, rigol_snapshot, {})
        assert rigol_projection.call_count == 2
        assert window.isVisible() and window.width() >= 820
        for page in (keithley, rigol):
            page.set_execution_controlled(False)
            page.set_execution_controlled(True)
            assert not page._execution_readbacks
            assert page._last_execution_render is None
        assert not keithley._execution_measurement_records
    finally:
        window.close()
        shell_qt_application.processEvents()
