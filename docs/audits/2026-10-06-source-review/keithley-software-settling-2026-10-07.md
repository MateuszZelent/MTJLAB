# Keithley settling time — review, 2026-10-07

## Meaning and defaults

The local station profile and the template store `settling_time: 100 ms` for
both A and B. The shared source configuration panel creates that same default;
`KeithleyPage._default_form_snapshot` loads the channel preference. The field
is visible on the main source panel, below compliance. It is separate from
NPLC, which is hidden on the manual page by default.

This is application software timing, not a programmable Keithley delay.
`source.delay`, `measure.delay`, and `measure.delayfactor` are read separately
by the adapter; configuring `settle_time_s` does not write those properties.
The saved `measure_delay` preference is `auto`; actual hardware state still
requires readback.

The compiler converts settling in a selected-parameter device block into a
Wait before the block's child measurements. `ramp_to_zero` uses the source
request's software settling time between intermediate steps. A literal
`configure_keithley` action only stores that preference; it does not insert
an unconditional sleep after every subsequent operation.

## Reported modal defect

`RecipePage._edit_legacy_keithley_configuration` supplied `0 s` when YAML
omitted both `settle_time` and `settling_time`. The modal therefore displayed
a fabricated zero for a preserved field, conflicting with its 1 ms–10 s
point-range indicator. The editor now displays the matching channel's current
form preference, or its Settings preference if no form provider is available.
Explicit authored values still take precedence. Accepting an unchanged modal
does not add the omitted field to YAML.

The field tooltip now states that this is software timing and does not program
the instrument's delay properties.

## Current smoke recipe

`anritsu_background_reference_smoke_test.yml` omits settling from its literal
Keithley configuration. Its changed-fields list therefore does not contain
`settle_time_s`. The existing source request's software preference is preserved
when one is available. With a fresh adapter and no previous software request,
hardware readback cannot recover this software-only value: the internal
request's fallback remains 0 s. This review does not change execution defaults
or silently add a delay to the recipe.

The recipe explicitly waits 5 s after enabling A, then 3 s before each of its
10 spectra. Those waits run independently of the software source preference.

## Verification

Focused tests render the main page and modal for A/B, verify a 100 ms default
and a per-channel 250 ms edited preference, and assert accepting preserves
the omission in YAML. Six focused tests passed and exited normally; Ruff
passed. The broader configuration/partial-baseline/off-before-patch group
reported 61 passing tests, but its Python process hung during final shutdown
and was stopped after reporting results. No hardware was contacted. Rendered examples:

- [Main Keithley page](keithley-main-settling.png)
- [Preserved settling in the recipe modal](keithley-preserved-settling.png)
