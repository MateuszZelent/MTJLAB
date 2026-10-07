"""Conservative pre-run duration and storage estimation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_TIME, parse_quantity
from app.domain.recipe_spectrum import MAX_RECIPE_SWEEP_JSON_BYTES
from app.engine.compiler import ExecutionPlan, RecipeCompiler, action_can_energize
from app.engine.policy import ExecutionPolicy
from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS
from app.settings.models import StationSettings


def require_storage_capacity(path: str | Path, required_bytes: int) -> None:
    """Check the destination volume before connecting or enabling instruments."""
    if type(required_bytes) is not int or required_bytes < 0:
        raise ConfigurationError("Storage estimate must be a nonnegative byte count.")
    target = Path(path).expanduser().resolve()
    directory = target.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    free = shutil.disk_usage(directory).free
    reserve = max(64 * 1024 * 1024, required_bytes // 10)
    if free < required_bytes + reserve:
        raise ConfigurationError(
            f"Insufficient free space on {directory}: need {required_bytes + reserve} bytes "
            f"including reserve; available {free} bytes."
        )


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
    public_import_upper_bytes: int = 0

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
        # A settings default is neither a hardware readback nor a command in
        # this plan. Reserve the supported maximum until the plan establishes
        # an explicit analyzer configuration.
        latest_spectrum_points = max(ANRITSU_SWEEP_POINT_COUNTS)
        spectrum_points_known = False
        unknown_points_reported = False
        spectrum_values = 0
        public_spectrum_values = 0
        spectrum_extra_bytes = 0
        prepared_plan_bytes = 0
        energized = any(action_can_energize(action) for action in plan.actions)
        nplc_by_channel = {}
        active_smu_channels = set()
        payload_budgets = {}
        largest_setpoints = 0
        policy = ExecutionPolicy.from_settings(self._settings)
        operation_deadline_budget = 0.0
        retry_deadline_budget = 0.0
        warnings: list[str] = []
        for action in plan.actions:
            # Full MOKE trajectories are recorded once. Subsequent device
            # snapshots contain only the bounded current voltage/profile.
            snapshot_payload = {key: value for key, value in action.payload.items() if key != "plan"}
            payload_size = len(json.dumps(RecipeCompiler._canonicalize(snapshot_payload), ensure_ascii=False).encode("utf-8"))
            if action.kind == "configure_moke_box" and "plan" in action.payload:
                prepared_plan_bytes += 3 * len(json.dumps(RecipeCompiler._canonicalize(action.payload["plan"])).encode("utf-8"))
            module = next((name for name in ("rigol", "keithley", "anritsu", "moke") if name in action.kind), action.kind)
            payload_budgets[module] = max(payload_budgets.get(module, 0), payload_size)
            largest_setpoints = max(largest_setpoints, len(action.setpoints_si))
            operation_deadline_budget += policy.deadline_for(action)
            if policy.retry_candidate(action):
                retry_deadline_budget += policy.deadline_for(action) + policy.retry_backoff_s
            nominal += self._command_overhead_s
            if action.kind == "wait":
                nominal += float(action.payload["duration_s"])
            elif action.kind == "configure_keithley":
                request = action.payload["request"]
                nplc_by_channel[request.channel] = request.nplc
                active_smu_channels.discard(request.channel)
            elif action.kind == "measure_keithley":
                # One atomic measure.iv() integration, conservatively allowing
                # two line cycles per configured NPLC.
                channel = action.payload["channel"]
                configured_nplc = nplc_by_channel.get(channel)
                if configured_nplc is None:
                    defaults = self._settings.keithley.safety.channels[channel].defaults
                    configured_nplc = float(defaults.get("nplc", 1))
                nominal += 2.0 * configured_nplc / self._line_frequency_hz
            elif action.kind == "configure_anritsu":
                latest_spectrum_points = int(action.payload["config"].points)
                spectrum_points_known = True
            elif action.kind in {"acquire_reference", "acquire_spectrum"}:
                if not spectrum_points_known and not unknown_points_reported:
                    warnings.append(
                        f"Analyzer point count is not established by the plan; "
                        f"storage and transfer estimates reserve {latest_spectrum_points} points per spectrum."
                    )
                    unknown_points_reported = True
                if action.payload.get("source_file"):
                    spectrum_extra_bytes += 8 * latest_spectrum_points * 8 + 16384
                    warnings.append(f"{action.node_id}: loads a verified file; no reference sweeps are acquired.")
                    continue
                average_count = int(action.payload.get("average_count", 1))
                nominal += average_count * sum(
                    self._command_overhead_s + 2 * nplc_by_channel.get(channel, 1) / self._line_frequency_hz
                    for channel in active_smu_channels
                )
                delay_budget = max(0, average_count - 1) * float(action.payload.get("inter_sweep_delay_s", 0.0))
                nominal += delay_budget
                minimum_duration = float(action.payload.get("minimum_duration_s", 0.0))
                sources_per_attempt = 9999 if minimum_duration else average_count
                # A failed final identity readback can leave every raw frame
                # committed. Retrying starts a new block without removing it.
                attempts = 1 + policy.retry_count
                storage_count = sources_per_attempt * attempts
                if minimum_duration:
                    nominal += minimum_duration
                    warnings.append(f"{action.node_id}: collects for at least {minimum_duration:g} s and {average_count} sweeps; storage reserves up to {storage_count} raw sweeps across {attempts} attempts.")
                # Each individual raw source is durable in addition to the
                # public averaged spectrum/reference representation.
                spectrum_values += storage_count * latest_spectrum_points
                # Raw power is counted above. Each source also retains its
                # own grid, bounded provenance JSON and HDF5 object overhead.
                spectrum_extra_bytes += storage_count * (
                    latest_spectrum_points * 8 + MAX_RECIPE_SWEEP_JSON_BYTES + 8192
                )
                nominal += average_count * (
                    self._spectrum_base_s
                    + latest_spectrum_points / self._transfer_rate
                )
                if action.kind == "acquire_spectrum":
                    public_spectrum_values += latest_spectrum_points
                    spectrum_values += latest_spectrum_points
                    # Private mean grid plus the public copy of its power.
                    spectrum_extra_bytes += 2 * latest_spectrum_points * 8
                    if action.payload.get("store_processed", False):
                        public_spectrum_values += latest_spectrum_points
                        spectrum_values += latest_spectrum_points
                        spectrum_extra_bytes += latest_spectrum_points * 8
                else:
                    # A reference mean retains its own Hz and dBm arrays.
                    spectrum_extra_bytes += 2 * latest_spectrum_points * 8 + sources_per_attempt * 8
                if average_count > 1:
                    warnings.append(
                        f"{action.node_id}: averages {average_count} complete spectra."
                    )
            elif action.kind == "ramp_keithley_to_zero":
                nominal += min(float(action.payload["deadline_s"]), 1.0)
            elif action.kind in {"update_moke_voltage", "stop_moke_voltage"}:
                # A physically qualified ramp and its hold may consume the
                # complete deadline; simulator shortcuts are not a lab model.
                nominal += float(action.payload["ramp_timeout_s"])
            elif action.kind in {"set_rigol_output", "set_keithley_output"}:
                if action.kind == "set_keithley_output":
                    if action.payload["enabled"]:
                        active_smu_channels.add(action.payload["channel"])
                    else:
                        active_smu_channels.discard(action.payload["channel"])

        retry_count = policy.retry_count
        retry_upper = max(nominal, operation_deadline_budget) + retry_count * retry_deadline_budget
        # Upper bound uses uncompressed float64 payload plus conservative HDF5
        # object/metadata overhead. Compression is deliberately not promised.
        # Each persisted event carries the current requested/actual device
        # snapshots. Size their payloads from this plan (including calibration
        # and MOKE trajectories), instead of assuming a 512-byte event. The
        # allowance includes JSON labels, output/safety state and HDF5 objects.
        snapshot_budget = 8192 + 3 * sum(payload_budgets.values()) + 1024 * largest_setpoints
        events_per_action = 8 + 2 * retry_count
        checkpoint_budget = 32768 + snapshot_budget + 1024 * largest_setpoints
        hdf5_bytes = (
            512 * 1024
            + 3 * len(plan.recipe_source.encode("utf-8"))
            + 3 * len(self._settings.model_dump_json().encode("utf-8"))
            + (len(plan.actions) * events_per_action + 32) * (snapshot_budget + 4096)
            + plan.total_points * checkpoint_budget
            + spectrum_values * 8
            + spectrum_extra_bytes
            + prepared_plan_bytes
        )
        if spectrum_values:
            hdf5_bytes += latest_spectrum_points * 8
        csv_bytes = plan.total_points * (8192 + 1024 * largest_setpoints) if self._settings.storage.get("write_csv_summary") else 0
        if energized:
            warnings.append("The plan contains OUTPUT ON or MOKE DAC update actions and requires explicit DUT and cabling review.")
        if plan.total_points == 0:
            warnings.append("The plan stores no checkpoints.")
        if plan.total_points >= 2_000:
            warnings.append("Large run: qualify duration and available disk space before OUTPUT ON.")
        if plan.total_spectra and spectrum_values == 0:
            warnings.append("Spectrum size could not be estimated from the configuration sequence.")
        from app.storage.resource_budget import IMPORT_BASE_BYTES, IMPORT_WORKING_COPY_FACTOR
        public_import_upper = IMPORT_BASE_BYTES + IMPORT_WORKING_COPY_FACTOR * (
            public_spectrum_values * 8 + plan.total_points * (64 + 8 * largest_setpoints) * 16
        )
        return PlanEstimate(
            nominal_duration_s=nominal,
            retry_upper_duration_s=retry_upper,
            uncompressed_hdf5_bytes=hdf5_bytes,
            csv_bytes=csv_bytes,
            checkpoints=plan.total_points,
            spectra=plan.total_spectra,
            spectrum_values=spectrum_values,
            warnings=tuple(warnings),
            public_import_upper_bytes=public_import_upper,
        )
