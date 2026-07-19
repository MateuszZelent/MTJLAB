# Hardware-in-the-Loop qualification

This procedure is the executable gate between automated tests and a station approved for physical
measurements. The qualification tool uses the same validated settings, production adapters, recipe
compiler, safety interlocks, execution policy and HDF5 writer as the GUI. It does not expose raw SCPI.

Passing a simulation is not hardware qualification. A generated report is evidence from one session;
it does not approve `settings.yml` and it does not replace the responsible engineer's review.

## 1. Safety boundary

The tool has two modes:

- `passive` sends identification queries and safe-state commands only. For Rigol and Keithley it
  forces and verifies OUTPUT OFF through their production connection workflow. For Anritsu it may
  send `ABORT`; optional trace acquisition reads the trace currently displayed and does not start a
  new sweep.
- `recipe` compiles and executes a declarative qualification recipe. Any recipe containing OUTPUT ON
  is classified as energized and requires all gates in section 4.

The default action is fail-closed. A timeout, malformed response, identity mismatch, inability to
prove OUTPUT OFF, or failed disconnect produces `failed`. A missing/disabled requested instrument
produces `incomplete`, never `passed`. An unknown device name produces `blocked`.

Never run energized qualification on the target DUT. Use a traceable dummy load with independently
verified ratings, approved wiring and a reachable physical E-STOP/interlock.

## 2. Provision the qualification account

### Lake Shore Model 475 (read-only) gate

Keep `devices.lakeshore_gaussmeter.enabled: false` until a service-role operator
archives a signed report for the exact instrument serial number. The adapter is
read-only: the acceptable captured traffic contains only `*IDN?`, `UNIT?`,
`RDGMODE?`, `RANGE?`, `AUTO?`, `TYPE?`, `RDGFIELD?`, `RDGFRQ?` and
`RDGPEAK?`; it must contain no write command, including `*CLS`.

Record these cases in the report:

1. exact `LSCI,MODEL475,...` identity, serial and firmware;
2. selected RS-232 9600/19200/38400/57600 baud with 7O1 and CR/LF, or the
   identified GPIB resource;
3. DC readings in gauss and tesla, including the recorded G-to-T conversion;
4. explicit rejection of Oe and A/m by the application;
5. DC on two ranges, RMS with frequency, and negative/positive peak readings;
6. autorange and probe type readback without sending a configuration command;
7. overload, malformed response, cable removal and power-loss behaviour;
8. stable 500 ms live readout with no overlapping queries;
9. a recipe checkpoint, HDF5/thaTEC readback and recovery verification;
10. captured traffic proving no device write command and clean disconnect.

Do not use a simulation result as a substitute for this physical HIL gate.

Qualification requires the `service` role bound to the authenticated operating-system account. See
[`ACCESS_CONTROL.md`](ACCESS_CONTROL.md). Restart the application or terminal session after changing
role assignments. The report records the OS account, host and roles.

## 3. Safe-state qualification

Start with all source outputs physically disabled and execute:

```powershell
python -m app.qualification --settings .config/settings.yml `
  --output-directory qualification passive
```

To qualify only selected instruments:

```powershell
python -m app.qualification --settings .config/settings.yml `
  --output-directory qualification passive --devices rigol,keithley
```

Reading the currently displayed Anritsu trace is opt-in because it may transfer many points:

```powershell
python -m app.qualification --settings .config/settings.yml `
  --output-directory qualification passive --devices anritsu --read-anritsu-trace
```

Before accepting the result, verify in the JSON report:

1. `overall_status` is `passed` (not `incomplete`, `blocked` or `simulation_passed`);
2. every requested instrument has the expected VISA resource, full IDN, serial and firmware;
3. Rigol and Keithley connection cases report `output_off`;
4. every connected device has a successful `safe_shutdown` case;
5. Anritsu configuration and optional trace contain finite, credible values;
6. `QualificationReport.verify_file(...)` accepts the evidence digest;
7. the associated JSONL audit contains a continuous session and no unexplained error.

If the analyser reports an installed signal-generator option (020/120/021/121), the passive
`safe_shutdown` case also enters SG mode, writes `OUTP 0`, verifies the RF output readback, returns
to Spectrum mode and sends `ABORT`. Failure to prove RF OFF fails the case. This safe-state action
does not require enabling SG control in the profile.

## 4. Energized recipe qualification

An energized run is accepted only when all of these independent gates are true:

- the OS account has the `service` role;
- the station profile is `approved`;
- the recipe passes the normal compiler and all DUT/device limits;
- `--allow-energized` is present;
- `LAB_CONTROL_ENABLE_ENERGIZED_HIL` equals exactly `YES` in that terminal;
- a non-empty, traceable `--dummy-load-id` is supplied;
- `--interlock-confirmed` is present;
- the exact confirmation phrase is supplied.

Example, only after the laboratory checklist has been signed:

```powershell
$env:LAB_CONTROL_ENABLE_ENERGIZED_HIL = "YES"
python -m app.qualification --settings .config/settings.yml `
  --output-directory qualification recipe recipes/hil_minimum.yml `
  --allow-energized --dummy-load-id "LOAD-50OHM-001" --interlock-confirmed `
  --confirmation "I CONFIRM DUMMY LOAD AND PHYSICAL INTERLOCK"
```

The recipe is never translated into arbitrary commands. It is executed by `RecipeRunner`; OUTPUT ON
still needs the normal configure/ARM/enable sequence, compliance is applied before Keithley output,
the watchdog is active, checkpoints are flushed to HDF5, and final shutdown uses the hashed shutdown
manifest. A recipe failure remains a failed qualification even if shutdown succeeds.

Do not create a generic high-power example recipe in the repository. Qualification recipes must use
the minimum values approved for the actual dummy load and station cabling.

### 4.1. Anritsu signal-generator qualification

The repository defaults SG control to `unverified` and RF permission to `false`. Do not bypass these
defaults merely because `*OPT?` reports an SG option. Before enabling the production path:

1. run passive qualification and verify that `anritsu.safe_shutdown` proves RF OFF;
2. confirm that the installed option and firmware accept the qualified basic-SCPI sequence on the
   selected VISA transport: `INST SG`, `OUTP 0`, `UNIT:POW DBM`, `FREQ`, `POW` and their readbacks;
3. use a calibrated RF load/power sensor rated above the proposed profile maximum;
4. define station frequency and power ranges under `devices.anritsu.signal_generator` and keep them
   no wider than the independently verified hardware path;
5. set `control_protocol: basic_scpi`, enable `signal_generator_output_allowed`, save and approve the
   changed profile;
6. execute a minimum-power recipe containing configure → ARM → RF ON → checkpoint → RF OFF;
7. verify physical output power/frequency, the one-shot ARM expiry, E-STOP and every shutdown path;
8. repeat after a timeout and transport disconnect. An ambiguous RF state must remain a failed run.

An energized SG recipe must also declare `dut_limits.anritsu.max_signal_generator_output`; the
compiler rejects ARM/RF ON without it. The recipe power must satisfy both this DUT limit and the
station profile. Spectrum acquisition explicitly commands and verifies RF OFF before changing back
to `INST SPECT`, so switching tabs or reading a spectrum is never treated as an implicit RF control.

### 4.2. Anritsu advanced Spectrum Analyzer qualification

The application may read the current advanced settings without changing the analyser. Writing RBW,
VBW, detector, input attenuation, preamplifier or sweep time is disabled by default. Qualify one
exact firmware/transport combination before enabling it:

1. complete passive qualification and record `*IDN?`, `*OPT?`, firmware and current Spectrum mode;
2. connect a rated 50 ohm load or independently limited source and begin with preamplifier OFF and
   60 dB internal attenuation;
3. verify query-only readback for `BAND:AUTO?`, `BAND?`, `BAND:VID:AUTO?`, `BAND:VID?`, `DET?`,
   `POW:ATT:AUTO?`, `POW:ATT?`, `SWE:TIME:AUTO?` and `SWE:TIME?`; query `POW:GAIN?` only when a
   detected option supports the preamplifier;
4. test auto and minimum approved manual values, then verify every readback and the analyser front
   panel; attenuation must remain in 0..60 dB on a 2 dB step and preamplifier enable needs separate
   profile permission;
5. inject timeout/readback mismatch and prove the fallback attempts preamplifier OFF, attenuation
   AUTO OFF and 60 dB, then marks the device state unknown;
6. verify that unsupported CISPR detectors and an absent preamplifier option remain unavailable;
7. set `advanced_spectrum.control_protocol: standard_scpi`, add the exact reported firmware to
   `qualified_firmware`, save and approve the changed safety profile;
8. run one qualified recipe action and confirm that the HDF5 checkpoint/audit contains the actual
   readback, not only requested values;
9. acquire a reference, change one of RBW/VBW/detector/attenuation/preamp/sweep-time settings and
   prove that reference processing is blocked until compatible settings are restored.

Never add a wildcard or model-only entry to `qualified_firmware`. A firmware change invalidates this
qualification until the sequence is repeated.

## 5. Evidence

Each session creates:

- `qualification/HIL-<UTC>-<id>.json` — ordered cases, timestamps, durations, risk class, IDNs,
  capabilities, profile/settings hash, operator identity, result paths and SHA-256 digest;
- `qualification/audit/*.jsonl` — append-only event audit with actor and case results;
- `qualification/runs/*.h5` — for recipe qualification, the normal thaTEC/PyThat-compatible result
  including recipe/settings snapshots, operator context and device metadata.

The JSON digest covers the entire evidence document except its own `evidence_sha256` field. Verify it
without touching hardware:

```powershell
python -c "from app.qualification import QualificationReport; QualificationReport.verify_file(r'qualification/HIL-...json'); print('OK')"
```

Store the JSON, JSONL, HDF5/CSV, the signed wiring checklist and dummy-load calibration identifier
together. If any file is edited, regenerate or formally supersede the qualification; do not repair
the digest manually.

## 6. Required sequence for station release

Execute and review these stages in order:

1. passive IDN/capabilities/error checks;
2. force and prove all outputs OFF;
3. configure minimum values while outputs remain OFF and verify readback;
4. one point on the dummy load;
5. a 2×2 sweep with checkpoint verification;
6. E-STOP at each phase, controlled compliance and cable-disconnect fault injection;
7. a complete 100×20 run with exactly 2000 records or a valid partial fault artifact;
8. a timed 2000-spectrum run and soak/restart recovery;
9. PyThat/xarray read and round-trip through the laboratory inventory system;
10. review and profile approval by the responsible engineer.

Stages 3–8 need station-specific qualification recipes and supervised actions. Software must not
automatically disconnect cables, force compliance or infer dummy-load ratings. Record those actions
as controlled test steps alongside the generated evidence.

## 7. Simulation rehearsal

The complete file/report workflow can be rehearsed without VISA hardware:

```powershell
python -m app.qualification --settings .config/settings.yml `
  --output-directory qualification-sim --simulate passive --read-anritsu-trace
```

Simulation resolves an isolated `SIMULATION` identity with all roles and labels the report
`simulation_passed`. It also replaces physical VISA resources and station-specific serial bindings
only in memory; the source profile, its hash and original approval state remain recorded in the
evidence. Such a report must never be used to approve a physical profile.

The command exits with code `0` only for `passed` or `simulation_passed`. A valid report whose
overall status is `failed`, `blocked` or `incomplete` is still written and verified, but the command
returns code `1`; a failure before report completion returns code `2`. Automation must check both
the exit code and the signed `overall_status`.

## 8. THATEC result interoperability

Open every generated result through **Results** before accepting a simulation
or HIL run. The browser reads the public THATEC tree directly and does not
require Lab Control's private `/run` or `/points` groups.

1. Select the file and expand **Measurements**, **Devices**, **Labbook** and
   **Post-process**.
2. Select a spectrum row, select checkpoint zero, and confirm the plotted
   trace has the row's declared number of frequency points.
3. Select every device record and verify that the public `/devices` table
   contains the complete flattened station configuration, including its
   connection and safety settings.
4. Confirm that selecting a measurement row exposes its THATEC definition,
   shape, timestamps and axis metadata in the inspector.

For interoperability qualification, repeat the same inspection with a real
THATEC/eLab result. The two files must use the same Results workflow; the
external file must not be labelled unreadable merely because it has no
application-private metadata.
