# Keithley 2602A driver audit — 2026-07-21

## Scope and source

This audit covers every command emitted or queried by
`app/devices/keithley_2600/adapter.py` for the configured and identity-checked
Keithley 2602A. The normative source is the *Series 2600A System SourceMeter
Reference Manual*, 2600AS-901-01 Rev. E (August 2011), with the 2.1.6 firmware
release notes used for the installed firmware generation.

All values cross the application boundary as explicit quantities, are converted
to SI once, and are emitted as finite locale-independent Lua numbers using
12 significant digits. TSP uses volts, amperes and seconds; NPLC is a
dimensionless number of power-line cycles.

## Command matrix

| Command or attribute | Direction | Type / unit | Driver decision and audit result |
|---|---:|---|---|
| `*IDN?` | query | SCPI identity string | Used before any configuration; vendor and exact model are checked. Correct. |
| `smuX.source.output` | R/W | enum: `OUTPUT_OFF=0`, `OUTPUT_ON=1` | Uses named constants and confirms numeric readback. Correct. Measurement never writes this attribute. |
| `smuX.source.offmode` | R/W | enum: `NORMAL=0`, `ZERO=1`, `HIGH_Z=2` | Uses named constants. Confirmation now accepts Keithley scientific boolean output such as `1.00000E+00`. |
| `smuX.source.func` | R/W | enum: `OUTPUT_DCAMPS=0`, `OUTPUT_DCVOLTS=1` | Correct named constants and readback. |
| `smuX.source.leveli` | R/W | A | Finite SI current, validated against editable station limits and immutable 2602A ±3 A limit, then read back. Correct. |
| `smuX.source.levelv` | R/W | V | Finite SI voltage, validated against editable station limits and immutable 2602A ±40 V limit, then read back. Correct. |
| `smuX.source.limitv` | R/W | V | Voltage compliance for current-source mode. Immutable 2602A interval 10 mV…40 V is enforced in addition to YAML limits. Correct. |
| `smuX.source.limiti` | R/W | A | Current compliance for voltage-source mode. Immutable 2602A interval 10 nA…3 A is enforced in addition to YAML limits. Correct. |
| `smuX.source.autorangei/v` | R/W | enum: `AUTORANGE_OFF=0`, `AUTORANGE_ON=1` | Correct named constants and readback. |
| `smuX.source.rangei/v` | R/W | A or V | A write is a maximum-expected-value request; a read returns the selected physical range. Fixed: no longer compared 1:1 with the request. |
| `smuX.measure.autorangei/v` | R/W | enum: `AUTORANGE_OFF=0`, `AUTORANGE_ON=1` | Correct named constants and readback. |
| `smuX.measure.rangei/v` | R/W | A or V | Same selection semantics as source range. Fixed to expect the smallest documented hardware range that covers the request. |
| `smuX.sense` | R/W | enum: `SENSE_LOCAL=0`, `SENSE_REMOTE=1` | 2-wire maps to LOCAL and 4-wire to REMOTE. Correct. Calibration sense is intentionally not exposed. |
| `smuX.measure.nplc` | R/W | PLC, 0.001…25 | Correct unit and interval. Fixed: values must be representable at the firmware's 0.001 PLC resolution, preventing silent hardware rounding. |
| `smuX.measure.iv()` | query | returns current A, then voltage V | Parsing order is correct and requires exactly two finite values. Measurement remains available with OUTPUT OFF. |
| `smuX.source.compliance` | query | boolean | Added as the authoritative documented compliance state; conservative I/V limit inference remains as a secondary trip condition. |
| `errorqueue.clear()` | write | function | Used after safe connection setup. Correct. |
| `errorqueue.count` | query | integer represented numerically | Scientific numeric responses are accepted. Correct. |
| `errorqueue.next()` | query | code, message, severity, node | Tab-separated Keithley response is retained in the user-visible error. Correct. |

## 2602A physical range semantics

The documented nominal voltage ranges are 100 mV, 1 V, 6 V and 40 V. The
documented nominal current ranges are 100 nA, 1 µA, 10 µA, 100 µA, 1 mA,
10 mA, 100 mA, 1 A and 3 A.

The continuous operating envelope is also enforced independently of YAML:
current-source levels above 1 A are limited to at most 6 V compliance, and
voltage-source levels above 6 V are limited to at most 1 A compliance. The
driver does not use the manual's optional pulse-mode extensions.

For example, writing `smuX.measure.rangev = 0.067` asks the instrument to choose
a range covering 67 mV. A correct 2602A readback is `0.1` (the 100 mV range),
not `0.067`. The previous equality check generated a false configuration error
and is the direct cause of the mismatch shown in the UI.

## Timing and non-device settings

The form's “settling time” is an application dwell used between ramp points. It
is not written to Keithley and must not be presented as hardware readback.
`smuX.measure.delay` is a separate instrument feature, expressed in seconds;
the current driver deliberately does not modify it. A single `measure.iv()`
integration is governed by NPLC.

## Failure behaviour after this audit

- Configuration always begins by writing and confirming OUTPUT OFF for the
  selected channel; it never enables an output.
- Source-level and compliance changes validate before VISA traffic, check the
  Keithley error queue, verify finite readback and verify unchanged OUTPUT state.
- A transport error, device error, malformed readback, unexpected OUTPUT
  transition or post-measurement queue error forces both channels OFF.
- Ramp points now use the same validated/read-back source-level update path as
  direct controls instead of an unchecked raw write.
- YAML remains the user-editable laboratory/DUT envelope. It cannot expand the
  immutable 2602A hardware boundaries enforced by the adapter.

## Qualification boundary

This is a documentation and automated-software audit. Final production
qualification still requires the repository HIL procedure against the physical
2602A/firmware 2.1.6, including output-off relay confirmation, range readbacks,
positive and negative source levels, both source modes, both channels,
compliance and injected VISA/error-queue faults.
