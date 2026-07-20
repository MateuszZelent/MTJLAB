# Instrument safety contract

## Source map

- `app/settings/models.py` and `app/resources/settings.template.yml`: lab envelopes, locks, approval, timeouts.
- `app/domain/dut.py`: experiment-specific DUT envelopes in SI.
- `app/safety/*.py`: device and DUT validation.
- `app/engine/compiler.py`: preflight, action order, approved finally actions.
- `app/engine/policy.py`: retries, deadlines, heartbeat, watchdog grace.
- `app/engine/runner.py`: state, compliance, cancellation, checkpoints, safe boundaries, shutdown.
- `app/devices/*/adapter.py`: last boundary before protocol commands.
- `app/audit/logger.py`: redacted operator/device/action evidence.
- `app/domain/readiness.py`: readiness evidence.

## Safety states

`DISCONNECTED`, `CONNECTED`, `VERIFIED`, `ARMED`, `RUNNING`, `PAUSED`, `STOPPING`, `SAFE`, `FAULT`, and `UNKNOWN` communicate evidence. Prefer `UNKNOWN` over unconfirmed `SAFE`.

## Retry classification

Read-only queries may be retryable. Configuration is retryable only with output confirmed off and idempotent behavior. Enable/disable, ramps, and state changes are not blindly retryable after timeout. Uncertain state triggers shutdown and confirmation.

## Minimum fault matrix

| Fault | Required behavior |
| --- | --- |
| validation failure | Send no mutation |
| configure failure | Keep/force output off; record fault |
| enable timeout | Treat unknown; shutdown and query |
| compliance/trip | Save diagnostics, disable, abort |
| operator stop | Run approved finally/shutdown actions |
| watchdog timeout | Request stop; independent shutdown |
| storage failure | Stop energizing workflow; shutdown |
| shutdown failure | Continue others; report `FAULT`/`UNKNOWN` |
| readback mismatch | Stop; preserve requested and actual values |

A safe change includes tests proving command order, boundary rejection, fault injection, continued shutdown, state reporting, and audit evidence. Simulator success alone does not prove undocumented hardware behavior.
