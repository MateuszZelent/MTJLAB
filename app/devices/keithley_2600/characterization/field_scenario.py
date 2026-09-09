"""Read-only, immutable review of the exact field-series execution snapshot."""

from dataclasses import dataclass

from app.devices.keithley_2600.characterization.field_series import FieldSeriesConfig, FieldSeriesRunner
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.domain.errors import SafetyViolation
from app.domain.quantities import format_quantity_auto


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    title: str
    detail: str = ""
    children: tuple["ScenarioStep", ...] = ()


@dataclass(frozen=True, slots=True)
class FieldScenario:
    config: FieldSeriesConfig
    initial_policies: tuple[tuple[str, str], ...]
    restore_policies: tuple[tuple[str, str], ...]
    steps: tuple[ScenarioStep, ...]
    sample_setpoints_si: tuple[float, ...]


def build_field_scenario(config, settings, initial_policies, restore_policies) -> FieldScenario:
    """Validate without I/O; keep every field target, including zero/repeats."""
    FieldSeriesRunner.validate(config, settings)
    for policies in (initial_policies, restore_policies):
        if set(policies) != {"A", "B"} or any(
            p not in {"stop", "warn_clamp", "skip"} for p in policies.values()
        ):
            raise SafetyViolation("A and B must have confirmed compliance policies before review.")
    q = format_quantity_auto
    sweep = config.sweep
    dimension = "current" if sweep.mode == "current" else "voltage"
    compliance_dimension = "voltage" if sweep.mode == "current" else "current"
    points = tuple(float(p) for p in KeithleyCharacterizationRunner.sweep_setpoints_excluding_zero(
        sweep.start_level_si, sweep.stop_level_si, sweep.points_count))

    def hardware(channel, source):
        source_dimension = "current" if source.mode == "current" else "voltage"

        def range_text(auto, value, dim):
            return "AUTO" if auto else q(value, dim) if value is not None else "UNSPECIFIED"

        return ScenarioStep(f"Channel {channel}: inherited hardware settings", children=(
            ScenarioStep("Source range", range_text(source.source_autorange, source.source_range_si, source_dimension)),
            ScenarioStep("Measure V range", range_text(source.measure_voltage_autorange, source.measure_voltage_range_si, "voltage")),
            ScenarioStep("Measure I range", range_text(source.measure_current_autorange, source.measure_current_range_si, "current")),
            ScenarioStep("Sense / NPLC / settling", f"{source.sense_mode}; NPLC {source.nplc:g}; {q(source.settle_time_s, 'time')}"),
            ScenarioStep("Compliance limit", q(source.compliance_si, "voltage" if source.mode == "current" else "current")),
        ))

    steps = [ScenarioStep("1. Prepare with both outputs OFF", children=(
        ScenarioStep("Confirm A OFF and B OFF", "Recheck policies. Apply both reviewed card configurations while OFF and verify device readback before enabling either output; block on mismatch."),
        ScenarioStep("Channel A → Stop on compliance", f"Adapter: {initial_policies['A']}; restore after series: {restore_policies['A']}"),
        ScenarioStep("Channel B → Stop on compliance", f"Adapter: {initial_policies['B']}; restore after series: {restore_policies['B']}"),
        hardware("A", KeithleyCharacterizationRunner.source_request_for_level(sweep, points[0])),
        hardware("B", config.field_source),
    ))]
    for index, target in enumerate(config.currents_a):
        initialization = (
            "Configure B at 0 A while OFF; confirm configuration; enable B at 0 A."
            if index == 0 else
            "Normally B stays ON at the previous target. After a skipped B target: configure B at 0 A while OFF, then enable at 0 A."
        )
        steps.append(ScenarioStep(f"{index + 2}. Field target {index + 1}/{len(config.currents_a)}: B = {q(target, 'current')}", children=(
            ScenarioStep("Confirm A OFF; prepare B", initialization),
            ScenarioStep("Ramp B to target", f"Step ≤ {q(config.ramp_step_a, 'current')}; wait {q(config.ramp_settle_s, 'time')} per step; read B after each step."),
            ScenarioStep("Wait and read B", (
                f"Wait {q(config.stabilization_s, 'time')}; {config.stable_readings} readings; tolerance max({q(config.current_tolerance_a, 'current')}, {config.current_tolerance_relative * 100:g}% of target)."
                if config.verify_current_stability else
                f"Wait {q(config.stabilization_s, 'time')}; read actual B current/voltage and check compliance. No additional current-tolerance or stability criterion.")),
            ScenarioStep(f"Configure A at {q(points[0], dimension)} while OFF, then enable A", f"First actual setpoint: {q(points[0], dimension)}. Compliance: {q(sweep.compliance_si, compliance_dimension)}."),
            ScenarioStep(f"Acquire A: {q(sweep.start_level_si, dimension)} → {q(sweep.stop_level_si, dimension)}", f"{sweep.points_count} grid points, {len(points)} actual nonzero points. Read B before/after every A measurement; settling {q(sweep.dwell_time_s, 'time')}."),
            ScenarioStep("Finish this curve", "Attempt A ramp to zero; switch A OFF and confirm. Persist acquired data. B remains active for the next target unless a stop/fault occurs."),
        )))
    steps.extend((
        ScenarioStep("Conditional branches — apply at every target", children=(
            ScenarioStep("B compliance → skip this target", "Stop A; confirm both outputs OFF; retain diagnostics and partial data; verify recovery. Only then continue to the next list item from B = 0 A, with a new history segment."),
            ScenarioStep("A compliance → end this curve", "Retain partial data and confirm A OFF. " + (
                "Verify A recovery, then continue to the next B target." if config.continue_after_sample_compliance else "End the entire series; do not start the next target.")),
            ScenarioStep("Cancel / communication or storage fault", "Stop the series and attempt both outputs OFF. Unconfirmed OFF/recovery blocks further measurement."),
            ScenarioStep("Maximum hold per target", f"{q(config.max_field_hold_s, 'time')}; checked between operations. This is not an independent hardware watchdog."),
        )),
        ScenarioStep("Finish series and restore", children=(
            ScenarioStep("Normal completion", "Ramp B toward 0 A, then switch both outputs OFF and confirm. On cancellation/fault, prioritize OFF."),
            ScenarioStep("Restore both policies only after both OFF", f"A → {restore_policies['A']}; B → {restore_policies['B']}. A failed restoration keeps the run blocked for recovery."),
            ScenarioStep("Save results", "A separate dataset per field-list item, preserving order and repeated currents. Skipped items remain recorded with their reason."),
            ScenarioStep("Offline common-window analysis", (
                f"Measured current {q(config.analysis_current_window_a[0], 'current')} to "
                f"{q(config.analysis_current_window_a[1], 'current')}; "
                f"reference item {config.analysis_reference_index + 1 if config.analysis_reference_index is not None else 'none'}. "
                "No extra measurement or setpoint change."
                if config.analysis_current_window_a is not None else
                "No fit window selected. Summary keeps measured curves and diagnostics; fitted comparison is unavailable.")),
        )),
    ))
    return FieldScenario(config, tuple(sorted(initial_policies.items())), tuple(sorted(restore_policies.items())), tuple(steps), points)
