"""Execution runner and preflight safety verification for Keithley characterization sweeps."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
)
from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_POWER, parse_quantity
from app.safety.keithley import validate_keithley_source
from app.settings.models import StationSettings


class KeithleyCharacterizationRunner:
    """Safely executes Keithley IV sweeps with preflight checks and clean safe shutdowns."""

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

        channel_settings = settings.keithley.safety.channels[channel_name]
        lab_limits = channel_settings.lab_limits

        if config.points_count < 2:
            raise SafetyViolation("Liczba punktów charakterystyki musi wynosić co najmniej 2.")
        if config.points_count > lab_limits.sweep_points_max:
            raise SafetyViolation(
                f"Liczba punktów charakterystyki ({config.points_count}) przekracza "
                f"maksymalny dopuszczalny limit stacji ({lab_limits.sweep_points_max})."
            )

        if config.compliance_si <= 0:
            raise SafetyViolation("Wartość limitu compliance musi być ściśle dodatnia.")

        if abs(config.stop_level_si - config.start_level_si) < 1e-15:
            raise SafetyViolation("Poziom początkowy i końcowy przemiatania nie mogą być identyczne.")

        # Verify start level, stop level, and zero level
        for level in (config.start_level_si, config.stop_level_si, 0.0):
            req = KeithleySourceRequest(
                channel=channel_name,
                mode=config.mode,
                level_si=level,
                compliance_si=config.compliance_si,
                nplc=1.0,
                settle_time_s=config.dwell_time_s,
                sense_mode=config.sense_mode,
            )
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
    ) -> CharacterizationDataset:
        """Run the characterization sweep synchronously with guaranteed zero-ramp and shutdown."""
        channel = config.channel
        points_count = max(2, config.points_count)
        setpoints = np.linspace(config.start_level_si, config.stop_level_si, points_count)

        started_at = datetime.now(timezone.utc).isoformat()
        points: list[CharacterizationPoint] = []

        # 1. Temporarily configure compliance policy to warn_clamp during characterization
        # to prevent premature stop when testing high-resistance samples in compliance.
        original_policy_stop: bool | None = None
        if hasattr(device, "compliance_policy"):
            try:
                current_policy = device.compliance_policy(channel)
                original_policy_stop = (current_policy == "stop")
            except Exception:
                pass
        try:
            device.set_compliance_policy(channel, False)
        except Exception:
            pass

        # 2. Configure initial safe state (OUTPUT OFF, initial 0 level)
        init_req = KeithleySourceRequest(
            channel=channel,
            mode=config.mode,
            level_si=0.0,
            compliance_si=config.compliance_si,
            nplc=1.0,
            settle_time_s=config.dwell_time_s,
            sense_mode=config.sense_mode,
        )
        device.configure_source(init_req)

        # 3. Enable output
        device.set_output(channel, True)

        try:
            for idx, demanded in enumerate(setpoints):
                if cancel_event is not None and cancel_event.is_set():
                    break

                # Apply setpoint
                device.update_source_level(channel, float(demanded))
                if config.dwell_time_s > 0:
                    time.sleep(config.dwell_time_s)

                if cancel_event is not None and cancel_event.is_set():
                    break

                meas = device.measure(channel)
                v_meas = float(meas.voltage_v)
                i_meas = float(meas.current_a)
                p_meas = float(abs(meas.power_w))
                comp_active = bool(meas.compliance_detected)

                if comp_active and on_compliance is not None:
                    on_compliance(f"Compliance active on channel {channel}: V={v_meas * 1e3:.1f} mV, I={i_meas * 1e3:.2f} mA")

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
                )
                points.append(pt)

                if on_point is not None:
                    on_point(pt)
                if on_progress is not None:
                    on_progress(idx + 1, points_count)

        finally:
            # 4. Fail-safe shutdown: ramp to zero and disable output
            try:
                device.ramp_to_zero(channel)
            except Exception:
                pass
            try:
                device.set_output(channel, False)
            except Exception:
                pass
            if original_policy_stop is not None:
                try:
                    device.set_compliance_policy(channel, original_policy_stop)
                except Exception:
                    pass

        completed_at = datetime.now(timezone.utc).isoformat()
        checksum = CharacterizationDataset.calculate_checksum(points)
        return CharacterizationDataset(
            config=config,
            points=tuple(points),
            started_at_iso=started_at,
            completed_at_iso=completed_at,
            checksum_sha256=checksum,
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
            self.finished_dataset.emit(dataset)
        except Exception as exc:
            self.failed.emit(str(exc))
