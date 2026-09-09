"""Execution runner and preflight safety verification for Keithley characterization sweeps."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import Any, Literal

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    FieldLineObservation,
)
from app.domain.errors import DeviceError, RunInterrupted, SafetyViolation
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_POWER,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.safety.keithley import validate_source_range, validate_keithley_source
from app.settings.models import StationSettings


class KeithleyCharacterizationRunner:
    """Safely executes Keithley IV sweeps with preflight checks and clean safe shutdowns."""

    @staticmethod
    def prepare_new_sweep_output(device: Any, channel: str) -> None:
        """A new operator-started run acknowledges compliance without restoring a level."""
        device.confirm_output_off(channel)
        result = device.recover_from_compliance(channel, "keep_off")
        if not isinstance(result, dict) or result.get("outputs_confirmed_off") is not True:
            raise DeviceError(f"Compliance recovery for {channel} was not confirmed.")
        device.confirm_output_off(channel)

    @staticmethod
    def sweep_setpoints_excluding_zero(
        start_level_si: float,
        stop_level_si: float,
        points_count: int,
    ) -> np.ndarray:
        """Build sweep points while excluding numerical and exact zero."""
        setpoints = np.linspace(start_level_si, stop_level_si, max(2, points_count))
        scale = max(
            abs(float(start_level_si)),
            abs(float(stop_level_si)),
            np.finfo(float).tiny,
        )
        zero_tolerance = np.finfo(float).eps * scale * 8.0
        nonzero = setpoints[np.abs(setpoints) > zero_tolerance]
        if nonzero.size == 0:
            raise SafetyViolation("Characterization sweep contains no non-zero points.")
        return nonzero

    @staticmethod
    def source_request_for_level(
        config: CharacterizationSweepConfig,
        level_si: float,
    ) -> KeithleySourceRequest:
        """Build one sweep request without changing shared Keithley parameters."""
        return KeithleySourceRequest(
            channel=config.channel,
            mode=config.mode,
            level_si=float(level_si),
            compliance_si=config.compliance_si,
            nplc=config.nplc,
            settle_time_s=config.dwell_time_s,
            sense_mode=config.sense_mode,
            source_autorange=config.source_autorange,
            source_range_si=config.source_range_si,
            measure_voltage_autorange=config.measure_voltage_autorange,
            measure_voltage_range_si=config.measure_voltage_range_si,
            measure_current_autorange=config.measure_current_autorange,
            measure_current_range_si=config.measure_current_range_si,
        )

    @staticmethod
    def assert_applied_configuration_matches_request(
        previous: KeithleySourceRequest,
        applied: KeithleySourceRequest,
    ) -> None:
        """Require every non-swept parameter to match the reviewed card request."""
        shared_fields = (
            "channel",
            "mode",
            "compliance_si",
            "nplc",
            "settle_time_s",
            "sense_mode",
            "source_autorange",
            "source_range_si",
            "measure_voltage_autorange",
            "measure_voltage_range_si",
            "measure_current_autorange",
            "measure_current_range_si",
        )
        mismatches = [
            name
            for name in shared_fields
            if getattr(previous, name) != getattr(applied, name)
        ]
        if mismatches:
            raise SafetyViolation(
                "Keithley readback differs from the requested card configuration: "
                + ", ".join(mismatches)
                + ". Characterization output was not enabled."
            )

    @classmethod
    def validate_preflight(
        cls,
        config: CharacterizationSweepConfig,
        settings: StationSettings,
    ) -> None:
        """Verify that all sweep setpoints strictly satisfy lab and DUT limits."""
        channel_name = config.channel
        if channel_name not in settings.keithley.safety.channels:
            raise SafetyViolation(f"Unknown Keithley channel: {channel_name}")
        if config.compliance_policy != "stop":
            raise SafetyViolation(
                "Characterization requires 'Stop on compliance' on the normal "
                "Keithley card."
            )

        channel_settings = settings.keithley.safety.channels[channel_name]
        lab_limits = channel_settings.lab_limits

        if config.points_count < 2:
            raise SafetyViolation("Characterization points count must be at least 2.")
        if config.points_count > lab_limits.sweep_points_max:
            raise SafetyViolation(
                f"Characterization points count ({config.points_count}) exceeds "
                f"station lab limit ({lab_limits.sweep_points_max})."
            )

        if config.compliance_si <= 0:
            raise SafetyViolation("Compliance limit must be strictly positive.")

        if abs(config.stop_level_si - config.start_level_si) < 1e-15:
            raise SafetyViolation("Start and stop sweep levels cannot be identical.")

        # Verify start level, stop level, and zero level (only if 0.0 is within allowed channel range)
        dim_sweep = DIMENSION_CURRENT if config.mode == "current" else DIMENSION_VOLTAGE
        min_source_si = parse_quantity(
            lab_limits.source_current.min if config.mode == "current" else lab_limits.source_voltage.min,
            dim_sweep,
        ).si_value
        max_source_si = parse_quantity(
            lab_limits.source_current.max if config.mode == "current" else lab_limits.source_voltage.max,
            dim_sweep,
        ).si_value

        levels_to_check = [config.start_level_si, config.stop_level_si]
        if min_source_si <= 0.0 <= max_source_si:
            levels_to_check.append(0.0)

        for level in levels_to_check:
            req = cls.source_request_for_level(config, level)
            validate_keithley_source(channel_settings, req)

        # Explicit check for power envelope
        if lab_limits.max_abs_power_enabled:
            max_level = max(abs(config.start_level_si), abs(config.stop_level_si))
            peak_power = max_level * abs(config.compliance_si)
            max_abs_power_si = parse_quantity(lab_limits.max_abs_power, DIMENSION_POWER).si_value
            if peak_power > max_abs_power_si:
                raise SafetyViolation(
                    f"Peak characterization power ({peak_power * 1e3:.1f} mW) exceeds "
                    f"station limit ({max_abs_power_si * 1e3:.1f} mW)."
                )

    @classmethod
    def run_sweep(
        cls,
        device: Any,
        config: CharacterizationSweepConfig,
        cancel_event: threading.Event | None = None,
        on_point: Any | None = None,
        on_progress: Any | None = None,
        on_compliance: Any | None = None,
        read_field: Any | None = None,
        on_raw_measurement: Any | None = None,
    ) -> CharacterizationDataset:
        """Run the characterization sweep synchronously with guaranteed zero-ramp and shutdown."""
        channel = config.channel
        # Also guard direct runner callers before recovery/configure can touch hardware.
        for level in (config.start_level_si, config.stop_level_si, 0.0):
            validate_source_range(cls.source_request_for_level(config, level))
        setpoints = cls.sweep_setpoints_excluding_zero(
            config.start_level_si,
            config.stop_level_si,
            config.points_count,
        )
        zero_setpoint_omitted = len(setpoints) < max(2, config.points_count)
        points_count = len(setpoints)

        if config.compliance_policy != "stop":
            raise SafetyViolation(
                "Characterization requires the shared Keithley compliance policy "
                "to be 'stop'. Select 'Stop on compliance' on the normal Keithley card."
            )
        try:
            active_policy = device.compliance_policy(channel)
        except Exception as exc:
            raise SafetyViolation(
                "Keithley compliance policy could not be confirmed; no "
                "characterization output was enabled."
            ) from exc
        if active_policy != config.compliance_policy:
            raise SafetyViolation(
                f"Keithley channel {channel} compliance policy is {active_policy!r}, "
                f"but the shared card requires {config.compliance_policy!r}. "
                "No characterization output was enabled."
            )

        started_at = datetime.now(timezone.utc).isoformat()
        points: list[CharacterizationPoint] = []
        completion_status: Literal[
            "completed", "cancelled", "stopped_on_compliance", "stopped_on_field_compliance"
        ] = "completed"
        termination_detail = ""

        # 1. Configure the exact first sweep request while OUTPUT is OFF.  This
        # is the same complete request shape used by the normal Keithley card.
        init_req = cls.source_request_for_level(config, float(setpoints[0]))
        cls.prepare_new_sweep_output(device, channel)
        applied_request = device.configure_source(init_req)
        if not isinstance(applied_request, KeithleySourceRequest):
            raise DeviceError(
                "Keithley did not return the applied characterization configuration."
            )
        cls.assert_applied_configuration_matches_request(
            init_req,
            applied_request,
        )

        try:
            # 2. Enable output only after the shared stop policy is confirmed.
            device.set_output(channel, True)
            for idx, demanded in enumerate(setpoints):
                if cancel_event is not None and cancel_event.is_set():
                    completion_status = "cancelled"
                    termination_detail = "Sweep cancelled before applying the next setpoint."
                    break

                field_before: FieldLineObservation | None = read_field() if read_field else None
                if field_before is not None and field_before.compliance_active:
                    completion_status = "stopped_on_field_compliance"
                    termination_detail = "Field line reached compliance before the next sample point."
                    break

                # Apply setpoint with keyword arguments for real adapter and positional fallback
                try:
                    device.update_source_level(channel, mode=config.mode, level_si=float(demanded))
                except TypeError:
                    device.update_source_level(channel, float(demanded))

                device.assert_output_state(channel, expected_enabled=True)

                if config.dwell_time_s > 0:
                    if cancel_event is not None:
                        cancel_event.wait(config.dwell_time_s)
                    else:
                        time.sleep(config.dwell_time_s)

                if cancel_event is not None and cancel_event.is_set():
                    completion_status = "cancelled"
                    termination_detail = "Sweep cancelled before the next measurement."
                    break

                meas = device.measure(channel)
                v_meas = float(meas.voltage_v)
                i_meas = float(meas.current_a)
                p_meas = float(abs(meas.power_w))
                comp_active = bool(meas.compliance_detected)
                if on_raw_measurement is not None:
                    on_raw_measurement({
                        "index": idx, "demanded_si": float(demanded),
                        "measured_voltage_v": v_meas, "measured_current_a": i_meas,
                        "power_w": p_meas, "compliance_active": comp_active,
                        "timestamp_epoch": time.time(),
                    })
                field_after: FieldLineObservation | None = read_field() if read_field else None

                if comp_active and on_compliance is not None:
                    on_compliance(
                        f"Compliance detected on channel {channel}; stopping sweep: "
                        f"V={v_meas * 1e3:.1f} mV, I={i_meas * 1e3:.2f} mA"
                    )

                # Resistance calculations:
                # True sample resistance: V_meas / I_meas (avoid zero division)
                if abs(i_meas) > 1e-12:
                    true_r = v_meas / i_meas
                elif config.mode == "current" and abs(demanded) > 1e-12:
                    true_r = v_meas / demanded
                else:
                    true_r = float("nan")

                # Apparent resistance:
                # In current mode: V_meas / I_demanded (shows artificial drop when voltage-clamped)
                # In voltage mode: V_demanded / I_meas (shows artificial rise when current-clamped)
                if config.mode == "current":
                    app_r = (v_meas / demanded) if abs(demanded) > 1e-12 else true_r
                else:
                    app_r = (demanded / i_meas) if abs(i_meas) > 1e-12 else true_r

                pt = CharacterizationPoint(
                    index=idx,
                    demanded_si=float(demanded),
                    measured_voltage_v=v_meas,
                    measured_current_a=i_meas,
                    true_resistance_ohm=float(true_r),
                    apparent_resistance_ohm=float(app_r),
                    power_w=p_meas,
                    compliance_active=comp_active,
                    timestamp_epoch=time.time(),
                    field_before=field_before,
                    field_after=field_after,
                    valid=not (field_after is not None and field_after.compliance_active),
                )
                points.append(pt)

                if on_point is not None:
                    on_point(pt)
                if on_progress is not None:
                    on_progress(idx + 1, points_count)

                if not pt.valid:
                    completion_status = "stopped_on_field_compliance"
                    termination_detail = "Field line reached compliance; the last sample point is invalid."
                    break

                if comp_active:
                    completion_status = "stopped_on_compliance"
                    termination_detail = (
                        f"Compliance detected at point {idx + 1}/{points_count} "
                        f"(demanded={float(demanded):.9e} SI); no subsequent setpoint was applied."
                    )
                    break

        except RunInterrupted as exc:
            completion_status = "cancelled"
            termination_detail = str(exc) or "Sweep interrupted by operator."
        finally:
            # 3. Fail-safe shutdown: ramp to zero and disable output
            try:
                device.ramp_to_zero(channel)
            except Exception:
                pass
            shutdown_error: Exception | None = None
            try:
                device.set_output(channel, False)
            except Exception as exc:
                shutdown_error = exc
            if shutdown_error is not None:
                raise DeviceError(
                    f"Keithley channel {channel} OUTPUT OFF could not be confirmed "
                    "after characterization."
                ) from shutdown_error

        completed_at = datetime.now(timezone.utc).isoformat()
        checksum = CharacterizationDataset.calculate_checksum(points)
        return CharacterizationDataset(
            config=config,
            points=tuple(points),
            started_at_iso=started_at,
            completed_at_iso=completed_at,
            checksum_sha256=checksum,
            completion_status=completion_status,
            termination_detail=termination_detail,
            zero_setpoint_omitted=zero_setpoint_omitted,
        )


class CharacterizationWorker(QThread):
    """Off-GUI execution thread for running characterization sweeps."""

    point_acquired = Signal(object)
    progress_changed = Signal(int, int)
    compliance_event = Signal(str)
    finished_dataset = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        device: Any,
        config: CharacterizationSweepConfig,
        settings: StationSettings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._device = device
        self._config = config
        self._settings = settings
        self._cancel_event = threading.Event()
        # A temporary characterization policy is restored only after this
        # flag is confirmed.  Failures perform an independent OUTPUT-OFF
        # readback before the failed signal is delivered to the UI.
        self.output_off_confirmed = False

    def request_stop(self) -> None:
        """Signal the running sweep to safely terminate."""
        self._cancel_event.set()

    def run(self) -> None:
        try:
            KeithleyCharacterizationRunner.validate_preflight(self._config, self._settings)
            dataset = KeithleyCharacterizationRunner.run_sweep(
                device=self._device,
                config=self._config,
                cancel_event=self._cancel_event,
                on_point=self.point_acquired.emit,
                on_progress=self.progress_changed.emit,
                on_compliance=self.compliance_event.emit,
            )
            self.output_off_confirmed = True
            self.finished_dataset.emit(dataset)
        except Exception as exc:
            try:
                confirm_output_off = getattr(self._device, "confirm_output_off", None)
                if callable(confirm_output_off):
                    confirm_output_off(self._config.channel)
                else:
                    self._device.assert_output_state(
                        self._config.channel,
                        expected_enabled=False,
                    )
            except Exception as off_exc:
                self.output_off_confirmed = False
                self.failed.emit(
                    f"{exc} OUTPUT OFF could not be independently confirmed: {off_exc}"
                )
            else:
                self.output_off_confirmed = True
                self.failed.emit(str(exc))
