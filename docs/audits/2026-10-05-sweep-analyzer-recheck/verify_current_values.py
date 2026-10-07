"""Positive qualification of explicitly authored current analyzer values in SIM."""
import json
from pathlib import Path

import h5py

from app.settings import SettingsRepository
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute


def test_current_values_with_explicit_advanced_block(tmp_path, monkeypatch):
    current = SettingsRepository('.config/settings.yml').load().settings.anritsu
    defaults = current.safety.defaults
    settings = audit_settings(tmp_path)
    source = f"""    - {{id: analyzer, type: configure_anritsu, start_frequency: '{defaults['start_frequency']}', stop_frequency: '{defaults['stop_frequency']}', reference_level: '{defaults['reference_level']}', points: {defaults['sweep_points']}}}
    - id: input-path
      type: configure_anritsu_advanced
      rbw_mode: auto
      vbw_mode: manual
      vbw: '{defaults['vbw']}'
      detector: '{defaults['detector']}'
      attenuation_mode: manual
      attenuation: '{defaults['attenuation']}'
      preamplifier_enabled: false
      sweep_time_mode: auto
    - id: points
      type: repeat
      count: 3
      children:
        - {{id: wait, type: wait, duration: '3 s'}}
        - {{id: spectrum, type: acquire_spectrum, average_count: 1}}
"""
    plan = compile_source(settings, source)
    path = tmp_path / 'current-analyzer.h5'
    result, waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    assert result.stored_points == 3 and waits == [3.0] * 3
    with h5py.File(path) as file:
        assert len(file['spectra']) == 3
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    output = Path('docs/audits/2026-10-05-sweep-analyzer-recheck')
    (output / 'current-values-result.json').write_text(json.dumps({
        'simulation_only': True, 'checkpoints': result.stored_points,
        'frequency_start': defaults['start_frequency'], 'frequency_stop': defaults['stop_frequency'],
        'trace_points': defaults['sweep_points'], 'vbw': defaults['vbw'],
        'waits_recorded_seconds': waits, 'real_waits_bypassed': True,
        'pythat_valid': True, 'archive_bytes': path.stat().st_size,
        'scope': 'Explicit advanced block; video/power mode remains unauthored.',
    }, indent=2), encoding='utf-8')
