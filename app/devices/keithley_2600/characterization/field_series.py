"""Two-channel characterization execution under an externally reserved session.

The caller owns the modal, exclusive reservation and temporary policy transaction.
This runner independently requires both channels OFF and both policies STOP.
Every acquired value is passed synchronously to a durable journal before proceeding.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
import threading
import time
from typing import Any, Callable, Literal

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.keithley_2600.adapter import build_keithley_ramp_levels
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset, CharacterizationSweepConfig, FieldLineObservation,
)
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.domain.errors import DeviceError, RunInterrupted, SafetyViolation
from app.domain.quantities import parse_quantity
from app.safety.keithley import validate_keithley_source
from app.settings.models import StationSettings

FIELD_POINT_IO_ALLOWANCE_S = 0.5
FIELD_TARGET_SETUP_ALLOWANCE_S = 2.0


def planned_field_ramp_step_counts(
    config: "FieldSeriesConfig", *, max_points: int
) -> tuple[int, ...]:
    """Return nominal B updates from zero, through all targets, and back to zero."""

    return tuple(
        len(
            build_keithley_ramp_levels(
                start,
                stop,
                config.ramp_step_a,
                max_points=max_points,
            )
        )
        for start, stop in zip(
            (0.0, *config.currents_a),
            (*config.currents_a, 0.0),
        )
    )


def estimated_field_target_durations_s(
    config: "FieldSeriesConfig", *, max_points: int
) -> tuple[float, ...]:
    """Conservative per-target wall-time estimates, including A/B I/O allowance."""
    sample_points = len(KeithleyCharacterizationRunner.sweep_setpoints_excluding_zero(
        config.sweep.start_level_si, config.sweep.stop_level_si, config.sweep.points_count))
    acquisition = (config.stabilization_s + FIELD_TARGET_SETUP_ALLOWANCE_S
                   + sample_points * (config.sweep.dwell_time_s + FIELD_POINT_IO_ALLOWANCE_S))
    durations = []
    previous = 0.0
    for index, target in enumerate(config.currents_a):
        normal_steps = len(build_keithley_ramp_levels(
            previous, target, config.ramp_step_a, max_points=max_points))
        recovery_steps = len(build_keithley_ramp_levels(
            0.0, target, config.ramp_step_a, max_points=max_points))
        duration = max(normal_steps, recovery_steps) * config.ramp_settle_s + acquisition
        if index == len(config.currents_a) - 1:
            duration += len(build_keithley_ramp_levels(
                target, 0.0, config.ramp_step_a, max_points=max_points)) * config.ramp_settle_s
        durations.append(duration)
        previous = target
    return tuple(durations)


@dataclass(frozen=True, slots=True)
class FieldSeriesConfig:
    sweep: CharacterizationSweepConfig
    field_source: KeithleySourceRequest
    currents_a: tuple[float, ...]
    ramp_step_a: float
    ramp_settle_s: float
    stabilization_s: float
    stable_readings: int
    current_tolerance_a: float
    current_tolerance_relative: float
    max_field_hold_s: float
    continue_after_sample_compliance: bool = True
    analysis_current_window_a: tuple[float, float] | None = None
    analysis_reference_index: int | None = None
    verify_current_stability: bool = True  # Historical series used explicit tolerances.


@dataclass(frozen=True, slots=True)
class FieldSeriesEntry:
    index: int
    demanded_current_a: float
    history_segment: int
    status: Literal["completed", "sample_compliance", "skipped_field_compliance", "cancelled"]
    dataset: CharacterizationDataset | None
    detail: str = ""


class _Cancelled(RunInterrupted):
    pass


class FieldSeriesRunner:
    """Sequential A/B ownership, observable field compliance and verified shutdown."""

    def __init__(self, device: Any, settings: StationSettings, config: FieldSeriesConfig,
                 write_event: Callable[[str, dict], None],
                 save_curve: Callable[[FieldSeriesEntry], None],
                 cancel_event: threading.Event | None = None):
        self.device, self.settings, self.config = device, settings, config
        self.write_event, self.save_curve = write_event, save_curve
        self.cancel = cancel_event if cancel_event is not None else threading.Event()
        self.entries: list[FieldSeriesEntry] = []
        self.output_off_confirmed = False
        self._index = -1
        self._target = 0.0
        self._hold_started = 0.0

    @staticmethod
    def validate(config: FieldSeriesConfig, settings: StationSettings) -> None:
        window = config.analysis_current_window_a
        if window is not None and (len(window) != 2 or not all(
            isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in window
        ) or window[0] >= window[1]):
            raise SafetyViolation("Analysis window must contain two finite increasing currents.")
        reference = config.analysis_reference_index
        if reference is not None and (window is None or type(reference) is not int or not 0 <= reference < len(config.currents_a)):
            raise SafetyViolation("Analysis reference requires a window and a valid field-list index.")
        if config.sweep.channel != "A" or config.field_source.channel != "B":
            raise SafetyViolation("Field series requires sample channel A and field-line channel B.")
        if config.field_source.mode != "current":
            raise SafetyViolation("Field line must use the manually verified current-source mode.")
        KeithleyCharacterizationRunner.validate_preflight(config.sweep, settings)
        KeithleyCharacterizationRunner.sweep_setpoints_excluding_zero(
            config.sweep.start_level_si, config.sweep.stop_level_si, config.sweep.points_count,
        )
        limits = settings.keithley.safety.channels["B"].lab_limits
        if not config.currents_a or len(config.currents_a) > limits.sweep_points_max:
            raise SafetyViolation("Field list is empty or exceeds channel B's point limit.")
        values = (*config.currents_a, config.ramp_step_a, config.ramp_settle_s,
                  config.stabilization_s, config.current_tolerance_a,
                  config.current_tolerance_relative, config.max_field_hold_s)
        if not all(math.isfinite(value) for value in values):
            raise SafetyViolation("Field series parameters must be finite SI values.")
        if (config.ramp_step_a <= 0 or config.ramp_settle_s <= 0
                or config.stabilization_s < 0 or config.current_tolerance_a < 0
                or not 0 <= config.current_tolerance_relative < 1
                or config.stable_readings < 1 or config.max_field_hold_s <= 0
                or type(config.verify_current_stability) is not bool
                or (config.verify_current_stability and
                    (config.current_tolerance_a <= 0 or config.stable_readings < 2))):
            raise SafetyViolation("Invalid field ramp, stability, tolerance or hold-time parameters.")
        if config.ramp_step_a > parse_quantity(limits.ramp_current_step_max, "current").si_value:
            raise SafetyViolation("Field ramp step exceeds channel B's own limit.")
        if limits.point_settle_time.enabled:
            minimum = parse_quantity(limits.point_settle_time.min, "time").si_value
            maximum = parse_quantity(limits.point_settle_time.max, "time").si_value
            if not minimum <= config.ramp_settle_s <= maximum:
                raise SafetyViolation("Field ramp settling time exceeds channel B's own bounds.")
        for value in (0.0, *config.currents_a):
            validate_keithley_source(settings.keithley.safety.channels["B"],
                                     replace(config.field_source, level_si=value))
        # Validate every transition and every potential restart after a skipped
        # compliance point before issuing any mutation, including final zero.
        for start, stop in zip((0.0, *config.currents_a), (*config.currents_a, 0.0)):
            for origin in (start, 0.0):
                build_keithley_ramp_levels(origin, stop, config.ramp_step_a,
                                           max_points=limits.sweep_points_max)
        estimates = estimated_field_target_durations_s(
            config, max_points=limits.sweep_points_max)
        required = max(estimates)
        if required >= config.max_field_hold_s:
            raise SafetyViolation(
                f"Safety timeout per B value is {config.max_field_hold_s:g} s; "
                f"enter at least {math.ceil(required + 1):g} s for this procedure."
            )

    def _off(self, channels=("A", "B")) -> None:
        errors = []
        for channel in channels:
            try:
                self.device.set_output(channel, False)
                self.device.confirm_output_off(channel)
            except Exception as exc:
                errors.append(f"{channel}: {exc}")
        if errors:
            self.output_off_confirmed = False
            raise DeviceError("OUTPUT OFF could not be confirmed: " + "; ".join(errors))
        if set(channels) == {"A", "B"}:
            self.output_off_confirmed = True

    def _checkpoint(self, kind: str, payload: dict) -> None:
        self.write_event(kind, {"field_index": self._index, "time_epoch": time.time(), **payload})

    def _check(self) -> None:
        if self.cancel.is_set():
            raise _Cancelled()
        if time.monotonic() - self._hold_started >= self.config.max_field_hold_s:
            raise DeviceError("Field-line exposure deadline exceeded.")
        for channel in ("A", "B"):
            if self.device.compliance_policy(channel) != "stop":
                raise SafetyViolation(f"Channel {channel} lost its STOP compliance policy.")

    def _wait(self, seconds: float) -> None:
        self._check()
        remaining = self.config.max_field_hold_s - (time.monotonic() - self._hold_started)
        if self.cancel.wait(min(seconds, max(0.0, remaining))):
            raise _Cancelled()
        self._check()

    def _read(self, expected: float, *, enforce_tolerance: bool) -> FieldLineObservation:
        self._check()
        measurement = self.device.measure("B")
        observation = FieldLineObservation(
            demanded_current_a=expected, measured_current_a=float(measurement.current_a),
            measured_voltage_v=float(measurement.voltage_v), power_w=float(measurement.power_w),
            timestamp_epoch=time.time(), compliance_active=bool(measurement.compliance_detected),
        )
        if not all(math.isfinite(value) for value in (
                observation.measured_current_a, observation.measured_voltage_v, observation.power_w)):
            raise DeviceError("Invalid field-line measurement.")
        self._checkpoint("field_observation", asdict(observation))
        self._check()
        if observation.compliance_active:
            self.device.confirm_output_off("B")
            return observation
        self.device.assert_output_state("B", expected_enabled=True)
        tolerance = max(self.config.current_tolerance_a,
                        abs(expected) * self.config.current_tolerance_relative)
        if self.config.verify_current_stability and enforce_tolerance and abs(observation.measured_current_a - expected) > tolerance:
            raise DeviceError("Measured field-line current is outside the requested tolerance.")
        return observation

    def _ramp(self, start: float, stop: float) -> bool:
        self.device.confirm_output_off("A")
        limits = self.settings.keithley.safety.channels["B"].lab_limits
        for level in build_keithley_ramp_levels(start, stop, self.config.ramp_step_a,
                                               max_points=limits.sweep_points_max):
            self._check()
            self.device.update_source_level("B", mode="current", level_si=level)
            self._wait(self.config.ramp_settle_s)
            if self._read(level, enforce_tolerance=False).compliance_active:
                return False
        return True

    def _stabilize(self) -> bool:
        self._wait(self.config.stabilization_s)
        if not self.config.verify_current_stability:
            return not self._read(self._target, enforce_tolerance=False).compliance_active
        currents = []
        for _ in range(self.config.stable_readings):
            observation = self._read(self._target, enforce_tolerance=True)
            if observation.compliance_active:
                return False
            currents.append(observation.measured_current_a)
            self._wait(self.config.ramp_settle_s)
        tolerance = max(self.config.current_tolerance_a,
                        abs(self._target) * self.config.current_tolerance_relative)
        if max(currents) - min(currents) > tolerance:
            raise DeviceError("Field-line current did not stabilize.")
        return True

    def _recover(self, channel: str) -> None:
        self.device.confirm_output_off(channel)
        result = self.device.recover_from_compliance(channel, "keep_off")
        if not isinstance(result, dict) or result.get("outputs_confirmed_off") is not True:
            raise DeviceError(f"Compliance recovery for {channel} was not confirmed.")
        self.device.confirm_output_off(channel)
        self._checkpoint("compliance_recovered", {"channel": channel})

    def run(self) -> tuple[FieldSeriesEntry, ...]:
        self.validate(self.config, self.settings)
        if self.cancel.is_set():
            return ()
        first_level = KeithleyCharacterizationRunner.sweep_setpoints_excluding_zero(
            self.config.sweep.start_level_si, self.config.sweep.stop_level_si,
            self.config.sweep.points_count)[0]
        for channel in ("A", "B"):
            self.device.confirm_output_off(channel)
            if self.device.compliance_policy(channel) != "stop":
                raise SafetyViolation(f"Channel {channel} must confirm STOP before a field series.")
        # Apply both reviewed card requests while both channels remain OFF.
        # No previous manual Apply operation is required.
        for channel in ("A", "B"):
            KeithleyCharacterizationRunner.prepare_new_sweep_output(self.device, channel)
        for request in (KeithleyCharacterizationRunner.source_request_for_level(
                self.config.sweep, float(first_level)), self.config.field_source):
            applied = self.device.configure_source(request)
            if not isinstance(applied, KeithleySourceRequest):
                raise DeviceError("Source configuration was not confirmed.")
            KeithleyCharacterizationRunner.assert_applied_configuration_matches_request(request, applied)
        active_b, current_b, segment = False, 0.0, 0
        try:
            for self._index, self._target in enumerate(self.config.currents_a):
                if active_b:
                    self._check()
                self._hold_started = time.monotonic()
                self._check()
                self.device.confirm_output_off("A")
                self._checkpoint("field_start", {"target_a": self._target, "history_segment": segment})
                self._check()
                if not active_b:
                    request = replace(self.config.field_source, level_si=0.0)
                    applied = self.device.configure_source(request)
                    if not isinstance(applied, KeithleySourceRequest):
                        raise DeviceError("Field configuration was not confirmed.")
                    KeithleyCharacterizationRunner.assert_applied_configuration_matches_request(request, applied)
                    self._check()
                    self.device.set_output("B", True)
                    self.output_off_confirmed = False
                    active_b, current_b = True, 0.0
                field_ok = self._ramp(current_b, self._target) and self._stabilize()
                dataset = None
                if field_ok:
                    dataset = KeithleyCharacterizationRunner.run_sweep(
                        self.device, self.config.sweep, cancel_event=self.cancel,
                        on_point=lambda point: self._checkpoint("sample_point", asdict(point)),
                        read_field=lambda: self._read(self._target, enforce_tolerance=True),
                        on_raw_measurement=lambda data: self._checkpoint("sample_raw", data),
                    )
                    dataset = replace(dataset, field_line_current_a=self._target,
                                      field_sequence_index=self._index, field_history_segment=segment)
                    field_ok = dataset.completion_status != "stopped_on_field_compliance"
                if not field_ok:
                    self._off()
                    entry = FieldSeriesEntry(self._index, self._target, segment,
                                             "skipped_field_compliance", dataset,
                                             "Field compliance; target skipped, both outputs confirmed OFF.")
                    self.save_curve(entry)
                    self.entries.append(entry)
                    self._checkpoint("field_skipped", {"reason": entry.detail})
                    self._recover("B")
                    # An A compliance could coincide with the B event.
                    if dataset is not None and any(p.compliance_active for p in dataset.points):
                        self._recover("A")
                    active_b, current_b = False, 0.0
                    segment += 1
                    continue
                status = ("sample_compliance" if dataset.completion_status == "stopped_on_compliance"
                          else "cancelled" if dataset.completion_status == "cancelled" else "completed")
                entry = FieldSeriesEntry(self._index, self._target, segment, status, dataset)
                self.save_curve(entry)
                self.entries.append(entry)
                self._check()
                current_b = self._target
                if status == "cancelled":
                    break
                if status == "sample_compliance":
                    if not self.config.continue_after_sample_compliance:
                        break
                    self._recover("A")
            if active_b and not self.cancel.is_set():
                self._ramp(current_b, 0.0)
        except _Cancelled:
            self._checkpoint("cancelled", {})
        finally:
            self._off()
        return tuple(self.entries)
