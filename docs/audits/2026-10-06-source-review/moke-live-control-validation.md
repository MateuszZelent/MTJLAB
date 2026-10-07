# MOKE Live control — validation feedback

The Live switch was enabled after connection/profile/readback checks even when
the voltage or settling draft could not form a valid MokeVoltagePlan. Clicking
it ran `_prepare_manual`; validation failed and `_failed` immediately unchecked
the switch. Initial DAC readback is adopted as the draft, so a retained 2 V
outside operator/station limits also triggers this path.

The switch now requires a valid draft to start and exposes the validation
reason in the status and tooltip. Editing the draft into bounds unlocks it.
An already enabled switch remains usable during invalid edits so OFF is always
available; invalid drafts do not start a ramp. Zero recovery remains independent
of draft validity. Connection, profile, initial readback, authorization, lease
and adapter checks remain enforced.

Regression coverage renders the page, rejects a 2 V draft without mutation,
unlocks after a 0 mV edit, checks invalid edits during Live send nothing, and
saves a screenshot. Hardware behavior was not exercised for this UI change.
