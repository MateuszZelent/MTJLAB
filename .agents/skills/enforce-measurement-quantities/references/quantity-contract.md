# Quantity contract

## Authoritative sources

- `app/domain/quantities.py`: vocabulary, dimensions, SI scales, finite validation, engineering display.
- `app/recipes/parameter_registry.py`: stable recipe target, dimension, and canonical unit.
- `app/ui/common/formatting.py`: human-facing formatting only.
- `app/safety/*.py`: lab and DUT comparisons after SI normalization.

## Current canonical dimensions

| Dimension | Internal base | Typical suffix | Human examples |
| --- | --- | --- | --- |
| voltage | V | `_v` | `67 mV`, `1e-3 V` |
| current | A | `_a` | `1 mA`, `2e-6 A` |
| power | W | `_w` | `10 mW` |
| frequency | Hz | `_hz` | `10 MHz`, `4e9 Hz` |
| resistance | ohm | `_ohm` | `50 ohm`, `1 Mohm` |
| time | s | `_s` | `250 ms`, `1e-6 s` |
| magnetic field | T | `_t` | `100 mT` |
| logarithmic power | dBm | `_dbm` | `-20 dBm` |
| logarithmic ratio | dB | `_db` | `20 dB` |
| ratio | 1 internally | descriptive suffix | `50 %` parses to `0.5` |

## Prefix hazards

- `m` means `1e-3`; `M` means `1e6` in conventional display.
- `u`, `µ`, and `μ` may be distinct Unicode input; normalize input and keep display stable.
- dBm is logarithmic absolute power. Its `m` is not the milli prefix.
- Names such as `frequency_hz` and `power_dbm` are schema contracts.

## Required invariants

Human-authored safety inputs include units; calculations use finite normalized values; dimensions are checked before limits; wire conversion occurs once; persisted arrays carry unit/scale metadata; display rounding never feeds execution or safety calculations.
