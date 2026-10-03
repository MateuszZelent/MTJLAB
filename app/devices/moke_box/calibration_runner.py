"""Synchronous calibration service executed on a run worker, never the GUI.

The caller holds exclusive leases on both controllers. Adapter methods remain
on their transport owner threads through the existing RunDeviceAdapter proxy.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from app.devices.lakeshore_475.models import FieldUnit, GaussmeterReading, MeasurementMode, validate_operation_status
from app.devices.moke_box.calibration import (
    CalibrationPoint,
    CalibrationRequest,
    MokeCalibration,
    build_branches,
)
from app.domain.errors import ConfigurationError, DeviceError, RunInterrupted
from app.safety.moke_box import MokeRampProgress
from app.storage.moke_calibration_store import MokeCalibrationRepository, MokeCalibrationRunStore

logger = logging.getLogger(__name__)


class WorkflowCancellation:
    """Combine GUI stop and both controller lease interrupts without Qt queues."""

    def __init__(self, events: tuple[Event, ...]) -> None:
        self.events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.events[0].wait(min(remaining, 0.02))
        return True


@dataclass(frozen=True, slots=True)
class CalibrationRunResult:
    model: MokeCalibration
    path: Path
    points: tuple[CalibrationPoint, ...]
    dac_zero_confirmed: bool


def _configuration_key(reading) -> tuple[object, ...]:
    snapshot = reading.snapshot if isinstance(reading, GaussmeterReading) else reading
    return (snapshot.mode, snapshot.unit, snapshot.probe_type_code, snapshot.dc_resolution_code)


def _snapshot_document(reading: GaussmeterReading) -> dict[str, object]:
    document = asdict(reading.snapshot)
    document["timestamp_utc"] = reading.snapshot.timestamp_utc.isoformat()
    document["operation_event_code"] = reading.operation_event_code
    return document


class MokeCalibrationRunner:
    def __init__(self, moke: object, reference: object, directory: str | Path,
                 *, cancel: Event | None = None,
                 progress: Callable[[CalibrationPoint], None] | None = None,
                 voltage_progress: Callable[[MokeRampProgress], None] | None = None,
                 interruption_events: tuple[Event, ...] = ()) -> None:
        self.moke = moke
        self.reference = reference
        self.directory = Path(directory)
        self.cancel = cancel if cancel is not None else Event()
        self.progress = progress
        self.voltage_progress = voltage_progress
        self.interruption_events = interruption_events
        self._ramp_cancel = WorkflowCancellation((self.cancel, *interruption_events))

    def _check_stop(self) -> None:
        if self.cancel.is_set() or any(event.is_set() for event in self.interruption_events):
            raise RunInterrupted("MOKE field calibration was stopped.")

    def _remaining(self, deadline: float) -> float:
        self._check_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MOKE calibration point exceeded its deadline.")
        return remaining

    def _reference_reading(self, deadline: float) -> GaussmeterReading:
        with self.reference.io_timeout(self._remaining(deadline)):
            reading = self.reference.read_measurement(
                require_fresh=True, deadline_s=deadline, cancel=self._ramp_cancel)
        self._remaining(deadline)
        validate_operation_status(reading.snapshot.operation_status_code)
        validate_operation_status(reading.operation_event_code)
        if reading.operation_event_code is None or not reading.operation_event_code & 4:
            raise DeviceError("Lake Shore did not confirm a new field conversion.")
        return reading

    def run(self, request: CalibrationRequest, *, armed: bool = False) -> CalibrationRunResult:
        if armed is not True:
            raise ConfigurationError("Explicitly arm the immutable calibration before starting it.")
        self._check_stop()
        profile = self.moke.get_control_profile()
        if profile is None:
            raise ConfigurationError("MOKE voltage output is not qualified.")
        request.plan.validate(profile)
        request.trajectory.validate(profile)
        if max(profile.minimum_settling_s, request.trajectory.settling_s) + request.samples_per_point * request.sample_interval_s >= request.point_timeout_s:
            raise ConfigurationError("Qualified settling time and sampling must fit the calibration point deadline.")
        if (request.context.profile_fingerprint != profile.fingerprint
                or request.context.binding_id != profile.binding_id
                or request.context.simulation != profile.simulation):
            raise ConfigurationError("Calibration context does not match the actual MOKE profile.")
        identity = self.reference.identity
        if identity is None or identity.idn != request.context.reference_idn or "MODEL475" not in identity.idn.upper():
            raise ConfigurationError("Calibration requires the identified Lake Shore Model 475.")
        preflight_deadline = time.monotonic() + request.point_timeout_s
        with self.reference.io_timeout(self._remaining(preflight_deadline)):
            baseline = self.reference.read_snapshot(deadline_s=preflight_deadline, cancel=self._ramp_cancel)
        if baseline.mode is not MeasurementMode.DC:
            raise ConfigurationError("Static signed field calibration requires Lake Shore DC mode.")
        if baseline.unit not in {FieldUnit.GAUSS, FieldUnit.TESLA}:
            raise ConfigurationError("Set Lake Shore field units to G or T before calibration; Oe and A/m are not B units.")
        validate_operation_status(baseline.operation_status_code)
        initial_reading = self._reference_reading(preflight_deadline)
        if initial_reading.mode is not MeasurementMode.DC or _configuration_key(initial_reading) != _configuration_key(baseline):
            raise ConfigurationError("Lake Shore configuration changed during calibration preflight.")
        self._check_stop()
        # Establish the durable run before the first possible DAC mutation.
        store = MokeCalibrationRunStore(self.directory, request)
        points: list[CalibrationPoint] = []
        status = "faulted"
        primary_error: BaseException | None = None
        zero_confirmed = False
        raw_hash = ""
        try:
            self.moke.configure_voltage_plan(request.trajectory)
            self._check_stop()
            self.moke.arm_voltage_plan(request.trajectory)
            store.event("calibration_armed", {
                "profile_fingerprint": profile.fingerprint,
                "control_profile": asdict(profile),
                "effective_settling_s": max(profile.minimum_settling_s, request.trajectory.settling_s),
                "hardware_waits_skipped": profile.simulation,
                "reference_preflight": _snapshot_document(initial_reading),
                "reference_preflight_field_t": initial_reading.field_t,
            })
            count = len(request.plan.targets_v)
            for ordinal, target in enumerate(request.trajectory.targets_v):
                self._check_stop()
                cycle, within = divmod(ordinal, 2 * count)
                direction = "ascending" if within < count else "descending"
                deadline = time.monotonic() + request.point_timeout_s
                with self.moke.io_timeout(min(request.point_timeout_s, profile.ramp_timeout_s) + 5):
                    result = self.moke.ramp_vout(profile.channel, target, cancel=self._ramp_cancel,
                                                 deadline_s=request.point_timeout_s, progress=self.voltage_progress)
                self._check_stop()
                if cycle < request.warmup_cycles:
                    continue
                fields, stamps, snapshots, hall, raw = [], [], [], [], []
                for _sample in range(request.samples_per_point):
                    self._check_stop()
                    if _sample and not profile.simulation:
                        self._ramp_cancel.wait(min(request.sample_interval_s, self._remaining(deadline)))
                    self._remaining(deadline)
                    # Never use cached units or the old 16-bit MOKE batch framing.
                    reading = self._reference_reading(deadline)
                    if reading.mode is not MeasurementMode.DC or reading.field_t is None:
                        raise DeviceError("Lake Shore left DC mode during calibration.")
                    if _configuration_key(reading) != _configuration_key(baseline):
                        raise DeviceError("Lake Shore unit, mode, resolution or probe changed during calibration.")
                    fields.append(reading.field_t)
                    stamps.append(reading.timestamp_utc.isoformat())
                    snapshots.append(_snapshot_document(reading))
                    if request.acquire_hall:
                        with self.moke.io_timeout(self._remaining(deadline)):
                            hall_reading = self.moke.read_hall_voltage(1)
                        hall.append(hall_reading.voltage_v)
                        raw.append(hall_reading.raw_codes[0])
                with self.moke.io_timeout(self._remaining(deadline)):
                    actual = self.moke.read_vouts()[profile.channel]
                self._remaining(deadline)
                if abs(actual - result.actual_v) > 0.001:
                    raise DeviceError("MOKE DAC changed during reference acquisition.")
                point = CalibrationPoint(
                    len(points), cycle - request.warmup_cycles, direction,
                    target, result.applied_v, actual, tuple(fields), tuple(stamps), tuple(snapshots),
                    tuple(hall), tuple(raw),
                )
                # Preserve noisy evidence too. It must never become an active model.
                store.append(point)
                points.append(point)
                if self.progress is not None:
                    self.progress(point)
                if statistics.pstdev(fields) > request.maximum_stddev_t:
                    raise DeviceError("Reference field did not meet the selected stability threshold.")
            ascending, descending = build_branches(tuple(points))
            if max((*ascending.stddev_t, *descending.stddev_t)) > request.maximum_stddev_t:
                raise DeviceError("Repeat cycles exceed the selected field repeatability threshold.")
            status = "completed"
        except BaseException as exc:  # noqa: BLE001 - preserve interruption while always shutting down and closing data
            primary_error = exc
            status = "aborted" if isinstance(exc, RunInterrupted) else "faulted"
        finally:
            try:
                with self.moke.io_timeout(profile.ramp_timeout_s + 5):
                    stop = self.moke.stop_vout(progress=self.voltage_progress)
                zero_confirmed = stop.safe_target_confirmed
                if not zero_confirmed:
                    raise DeviceError("DAC zero was not confirmed after calibration.")
            except BaseException as exc:  # noqa: BLE001 - continue all shutdown and persistence actions
                status = "faulted"
                primary_error = primary_error or exc
                try:
                    self.moke.emergency_off()
                except Exception:
                    logger.exception("MOKE emergency shutdown failed during calibration cleanup")
            try:
                store.event("calibration_finished", {
                    "status": status, "dac_zero_confirmed": zero_confirmed,
                    "kepco_power_state": "unknown", "committed_points": len(points),
                    "error": str(primary_error) if primary_error else None,
                })
            except BaseException as exc:  # noqa: BLE001 - persistence failure must not skip file close
                primary_error = primary_error or exc
                status = "faulted"
            try:
                raw_hash = store.close(status)
            except BaseException as exc:  # noqa: BLE001 - retain the primary failure after close
                primary_error = primary_error or exc
        if primary_error is not None:
            raise primary_error
        model = MokeCalibration(
            request.context, datetime.now(UTC).isoformat(), ascending, descending,
            store.run_id, raw_hash,
        )
        MokeCalibrationRepository(self.directory).save(model)
        # Saving a completed model does not activate it or energize any output.
        return CalibrationRunResult(model, store.path, tuple(points), zero_confirmed)
