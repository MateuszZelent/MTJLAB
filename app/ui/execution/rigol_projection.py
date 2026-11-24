"""One voltage representation of a planned Rigol carrier, without hardware I/O."""

import math
from dataclasses import dataclass, field, replace

from app.domain.quantities import format_quantity_auto
from app.safety.rigol_current import (
    estimate_rigol_current,
    quantize_rigol_frequency,
    quantize_rigol_level_pair,
    quantize_rigol_level_update,
)

VOLTAGE_FIELDS = ('high_level', 'low_level', 'amplitude', 'offset')


@dataclass
class RigolPlanProjection:
    changes: list[dict[str, float]] = field(default_factory=list)
    masked: list[set[str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    envelopes: dict[int, dict] = field(default_factory=dict)

    def summary(self):
        lines = []
        for channel, envelope in self.envelopes.items():
            low, high = envelope['low'], envelope['high']
            fmt = lambda value: format_quantity_auto(value, 'voltage')
            modes = '/'.join(sorted(envelope['waveforms']))
            loads = '/'.join(sorted(envelope['loads']))
            peak = max(abs(low), abs(high))
            open_peak = envelope['open_peak']
            open_text = (f"; nominal open-circuit peak {fmt(open_peak)}"
                         if not math.isclose(peak, open_peak, rel_tol=1e-12, abs_tol=1e-12) else '')
            if envelope['unknown_load']:
                open_text = '; nominal open-circuit peak unconfirmed (LOAD preserved on instrument)'
            lines.append(
                f"CH{channel} · {modes} · LOAD {loads} · range {fmt(low)} … {fmt(high)}; "
                f"planned max |V| {fmt(peak)}{open_text}."
            )
        if not lines:
            return ''
        waveforms = set().union(*(envelope['waveforms'] for envelope in self.envelopes.values()))
        explanation = []
        if 'DC' in waveforms:
            explanation.append('DC: HighL = LowL = offset, one voltage; these values are not added.')
        if waveforms - {'DC'}:
            explanation.append('AC: HighL = offset + Vpp/2; LowL = offset − Vpp/2. HighL/LowL include offset.')
        return ('Rigol carrier levels with OUTPUT ON (not measured DUT voltage):\n' + '\n'.join(lines)
                + '\n' + ' '.join(explanation))


def project_rigol(actions, level_modes=None):
    """Prefer the card's mode; DC always has one voltage and no frequency.

    Explicit actions are authoritative. A stale sweep context must not undo an
    authored final zero. Unknown baselines remain unknown; equal endpoints alone
    are never used to infer that an instrument is in DC mode.
    """
    result = RigolPlanProjection()
    configs = {}
    known_loads = {}
    outputs = {}
    displayed = {}
    level_modes = level_modes or {}
    ac_offset_channels = set()
    for action in actions:
        payload = action.payload
        channel = payload.get('channel')
        kind = action.kind
        changed_channel = None
        if kind == 'configure_rigol':
            config = payload['config']
            channel = config.channel
            selected = config.changed_fields
            baseline = configs.get(channel)
            if baseline is not None and selected is not None:
                config = replace(baseline, **{name: getattr(config, name) for name in selected})
            if selected is None or 'output_load' in selected:
                known_loads[channel] = config.output_load
            elif channel in known_loads:
                config = replace(config, output_load=known_loads[channel])
            if baseline is not None or selected is None or {'waveform', 'frequency_hz', 'high_level_v', 'low_level_v'} <= set(selected):
                high, low = quantize_rigol_level_pair(config.high_level_v, config.low_level_v, waveform=config.waveform)
                configs[channel] = replace(config, high_level_v=high, low_level_v=low,
                                           frequency_hz=quantize_rigol_frequency(config.frequency_hz))
                changed_channel = channel
            outputs[channel] = False
        elif kind == 'configure_rigol_output':
            config = payload['config']
            channel = config.channel
            if config.changed_fields is None or 'output_load' in config.changed_fields:
                known_loads[channel] = config.output_load
                if channel in configs:
                    configs[channel] = replace(configs[channel], output_load=config.output_load)
            outputs[channel] = False
        elif kind == 'update_rigol_levels' and channel in configs:
            before = configs[channel]
            high, low = quantize_rigol_level_update(before.high_level_v, before.low_level_v,
                                                   payload['high_level_v'], payload['low_level_v'], waveform=before.waveform)
            configs[channel] = replace(before, high_level_v=high, low_level_v=low)
            changed_channel = channel
        elif kind == 'update_rigol_frequency' and channel in configs:
            configs[channel] = replace(configs[channel], frequency_hz=quantize_rigol_frequency(payload['frequency_hz']))
            changed_channel = channel
        elif kind == 'set_rigol_output':
            outputs[channel] = payload['enabled']

        values = {}
        if changed_channel is not None:
            channel = changed_channel
            config = configs[channel]
            prefix = f'rigol.{channel}.'
            dc = config.waveform.upper() == 'DC'
            mode = level_modes.get(channel, level_modes.get(str(channel), 'Amplitude / Offset'))
            high_low = str(mode).startswith('High Level')
            if dc:
                values[prefix + 'offset'] = config.high_level_v
                result.labels[prefix + 'offset'] = f'CH{channel} · DC level'
            else:
                if config.waveform.upper() != 'NOIS':
                    values[prefix + 'frequency'] = config.frequency_hz
                    result.labels[prefix + 'frequency'] = f'CH{channel} · frequency'
                if high_low:
                    values[prefix + 'high_level'] = config.high_level_v
                    values[prefix + 'low_level'] = config.low_level_v
                    result.labels[prefix + 'high_level'] = f'CH{channel} · HighL'
                    result.labels[prefix + 'low_level'] = f'CH{channel} · LowL'
                else:
                    values[prefix + 'amplitude'] = float(f'{config.high_level_v - config.low_level_v:.15g}')
                    values[prefix + 'offset'] = float(f'{(config.high_level_v + config.low_level_v) / 2:.15g}')
                    ac_offset_channels.add(channel)
                    result.labels[prefix + 'amplitude'] = f'CH{channel} · amplitude (Vpp)'
            active = set(values)
            for target in displayed.get(channel, set()) - active:
                values[target] = math.nan  # Stop the lane across an inapplicable waveform mode.
            displayed[channel] = active
        result.changes.append(values)
        result.masked.append({f'rigol.{ch}.{name}' for ch in configs for name in (*VOLTAGE_FIELDS, 'frequency')})
        if kind not in {'configure_rigol', 'configure_rigol_output', 'update_rigol_levels', 'set_rigol_output'}:
            continue
        for ch, config in configs.items():
            if outputs.get(ch) is not True:
                continue
            load_known = ch in known_loads
            estimate = (estimate_rigol_current(high_level=config.high_level_v, low_level=config.low_level_v,
                                               output_load=known_loads[ch]) if load_known else None)
            envelope = result.envelopes.setdefault(ch, {'low': config.low_level_v, 'high': config.high_level_v,
                                                        'open_peak': 0., 'waveforms': set(), 'loads': set(),
                                                        'unknown_load': False})
            envelope['low'] = min(envelope['low'], config.low_level_v)
            envelope['high'] = max(envelope['high'], config.high_level_v)
            if estimate is not None:
                envelope['open_peak'] = max(envelope['open_peak'], abs(estimate.open_circuit_high_v), abs(estimate.open_circuit_low_v))
            else:
                envelope['unknown_load'] = True
            envelope['waveforms'].add(config.waveform.upper())
            envelope['loads'].add(str(known_loads[ch]) if load_known else 'unconfirmed')
    for channel in ac_offset_channels:
        result.labels[f'rigol.{channel}.offset'] = f'CH{channel} · offset / DC level'
    return result
