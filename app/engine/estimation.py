"""Conservative pre-run duration and storage estimation."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_TIME, parse_quantity
from app.engine.compiler import ExecutionPlan
from app.settings.models import StationSettings


@dataclass(frozen=True, slots=True)
class PlanEstimate:
    nominal_duration_s: float
    retry_upper_duration_s: float
    uncompressed_hdf5_bytes: int
    csv_bytes: int
    checkpoints: int
    spectra: int
    spectrum_values: int
    warnings: tuple[str, ...]

    @property
    def total_upper_bytes(self) -> int:
        return self.uncompressed_hdf5_bytes + self.csv_bytes


class PlanEstimator:
    """Estimate without contacting hardware or claiming instrument timing certainty."""

    def __init__(self, settings: StationSettings) -> None:
        execution = settings.execution
        self._settings = settings
        self._command_overhead_s = parse_quantity(
            execution.get("estimated_command_overhead", "25 ms"),
            DIMENSION_TIME,
        ).si_value
        self._spectrum_base_s = parse_quantity(
            execution.get("estimated_spectrum_base_time", "100 ms"),
            DIMENSION_TIME,
        ).si_value
        self._transfer_rate = float(
            execution.get("estimated_spectrum_transfer_rate_points_per_second", 100_000)
        )
        self._line_frequency_hz = parse_quantity(
            execution.get("estimated_line_frequency", "50 Hz"),
            DIMENSION_FREQUENCY,
        ).si_value
        finite_positive = (
            self._command_overhead_s,
            self._spectrum_base_s,
            self._transfer_rate,
            self._line_frequency_hz,
        )
        if not all(math.isfinite(value) and value > 0 for value in finite_positive):
            raise ConfigurationError("Execution estimation parameters must be finite and positive.")

    def estimate(self, plan: ExecutionPlan) -> PlanEstimate:
        nominal = 0.0
        latest_spectrum_points = int(self._settings.anritsu.safety.defaults.get("sweep_points", 1001))
        spectrum_values = 0
        retryable_operations = 0
        energized = False
        warnings: list[str] = []
        for action in plan.actions:
            nominal += self._command_overhead_s
            if action.kind == "wait":
                nominal += float(action.payload["duration_s"])
            elif action.kind == "configure_keithley":
                request = action.payload["request"]
                nominal += request.settle_time_s
            elif action.kind == "measure_keithley":
                # One atomic measure.iv() integration, conservatively allowing
                # two line cycles per configured NPLC.
                nominal += 2.0 / self._line_frequency_hz
                retryable_operations += 1
            elif action.kind == "measure_moke_hall":
                # One read-only TCP request plus a four-byte AD7734 reply.
                # The generic command overhead above remains the conservative
                # estimate; count it as retryable because a failed read closes
                # the MOKE session before a retry can reconnect.
                retryable_operations += 1
            elif action.kind == "configure_anritsu":
                latest_spectrum_points = int(action.payload["config"].points)
                retryable_operations += 1
            elif action.kind == "configure_anritsu_advanced":
                retryable_operations += 1
            elif action.kind in {"acquire_reference", "acquire_spectrum"}:
                average_count = int(action.payload.get("average_count", 1))
                nominal += average_count * (
                    self._spectrum_base_s
                    + latest_spectrum_points / self._transfer_rate
                )
                if action.kind == "acquire_spectrum":
                    spectrum_values += latest_spectrum_points
                    if action.payload.get("store_processed", False):
                        spectrum_values += latest_spectrum_points
                retryable_operations += average_count
                if average_count > 1:
                    warnings.append(
                        f"{action.node_id}: averages {average_count} complete spectra."
                    )
            elif action.kind == "ramp_keithley_to_zero":
                nominal += min(float(action.payload["deadline_s"]), 1.0)
                retryable_operations += 1
            elif action.kind in {"configure_rigol", "configure_rigol_output"}:
                retryable_operations += 1
            elif action.kind in {"set_rigol_output", "set_keithley_output"}:
                energized = energized or bool(action.payload["enabled"])

        retry_count = int(self._settings.execution.get("retry_count", 1))
        retry_backoff = parse_quantity(
            self._settings.execution.get("retry_backoff", "250 ms"),
            DIMENSION_TIME,
        ).si_value
        retry_upper = nominal + retryable_operations * retry_count * (
            self._command_overhead_s + retry_backoff
        )
        # Upper bound uses uncompressed float64 payload plus conservative HDF5
        # object/metadata overhead. Compression is deliberately not promised.
        hdf5_bytes = (
            128 * 1024
            + len(plan.actions) * 512
            + plan.total_points * 2048
            + spectrum_values * 8
        )
        if spectrum_values:
            hdf5_bytes += latest_spectrum_points * 8
        csv_bytes = plan.total_points * 1024 if self._settings.storage.get("write_csv_summary") else 0
        if energized:
            warnings.append("The plan contains OUTPUT ON actions and requires explicit DUT/ARM review.")
        if plan.total_points == 0:
            warnings.append("The plan stores no checkpoints.")
        if plan.total_points >= 2_000:
            warnings.append("Large run: qualify duration and available disk space before ARM.")
        if plan.total_spectra and spectrum_values == 0:
            warnings.append("Spectrum size could not be estimated from the configuration sequence.")
        return PlanEstimate(
            nominal_duration_s=nominal,
            retry_upper_duration_s=retry_upper,
            uncompressed_hdf5_bytes=hdf5_bytes,
            csv_bytes=csv_bytes,
            checkpoints=plan.total_points,
            spectra=plan.total_spectra,
            spectrum_values=spectrum_values,
            warnings=tuple(warnings),
        )
