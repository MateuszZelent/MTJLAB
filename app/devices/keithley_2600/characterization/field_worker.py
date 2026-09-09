"""Reserved series lifecycle: both policies, durable acquisition, safe rollback."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal

from app.devices.keithley_2600.characterization.field_series import FieldSeriesConfig, FieldSeriesRunner
from app.devices.keithley_2600.characterization.field_storage import FieldSeriesStore
from app.domain.errors import SafetyViolation


@dataclass(frozen=True, slots=True)
class FieldSeriesOutcome:
    directory: Path
    status: str
    outputs_off: bool
    policies_restored: bool
    errors: tuple[str, ...]


def restore_field_policies(device, originals: dict[str, str]) -> tuple[bool, bool, list[str]]:
    """Attempt both shutdowns, restore neither policy until both OFF confirmed."""
    errors = []
    for channel in ("A", "B"):
        try:
            device.set_output(channel, False)
            device.confirm_output_off(channel)
        except Exception as exc:
            errors.append(f"{channel} OUTPUT OFF: {exc}")
    if errors:
        return False, False, errors
    for channel in ("A", "B"):
        try:
            result = device.set_compliance_policy(channel, originals[channel])
            if result != originals[channel] or device.compliance_policy(channel) != originals[channel]:
                raise SafetyViolation("Policy readback mismatch")
        except Exception as exc:
            errors.append(f"{channel} policy restore: {exc}")
    return True, not errors, errors


class FieldSeriesWorker(QThread):
    event = Signal(str, object)
    policies_changed = Signal(object)
    finished_series = Signal(object)

    def __init__(self, device, settings, config: FieldSeriesConfig, directory: Path,
                 expected_policies: dict[str, str], restore_policies: dict[str, str], parent=None,
                 *, reviewed_scenario=None, inventory_target=None):
        super().__init__(parent)
        self.device, self.settings, self.config, self.directory = device, settings, config, directory
        self.expected_policies = dict(expected_policies)
        self.restore_policies = dict(restore_policies)
        self.cancel = device.interruption_event if device.interruption_event is not None else threading.Event()
        self.outcome = None
        self.reviewed_scenario = reviewed_scenario
        self.inventory_target = dict(inventory_target) if inventory_target is not None else None

    def request_stop(self):
        self.cancel.set()

    def run(self):
        errors = []
        store = None
        status = "fault"
        policy_transaction_started = False
        try:
            FieldSeriesRunner.validate(self.config, self.settings)
            if self.reviewed_scenario is not None:
                reviewed = self.reviewed_scenario
                if (reviewed.config != self.config
                        or dict(reviewed.initial_policies) != self.expected_policies
                        or dict(reviewed.restore_policies) != self.restore_policies):
                    raise SafetyViolation("Execution snapshot differs from the reviewed scenario.")
            for policies in (self.expected_policies, self.restore_policies):
                if set(policies) != {"A", "B"} or any(
                    policy not in {"stop", "warn_clamp", "skip"} for policy in policies.values()
                ):
                    raise SafetyViolation("Both channels require a known compliance policy.")
            for channel in ("A", "B"):
                self.device.confirm_output_off(channel)
                if self.device.compliance_policy(channel) != self.expected_policies[channel]:
                    raise SafetyViolation("Policy changed since confirmation; series blocked.")
            store = FieldSeriesStore(self.directory, self.config, {
                "initial_policies": self.expected_policies,
                "restore_policies": self.restore_policies,
                "keithley_settings": self.settings.keithley.model_dump(mode="json"),
                "identity": str(self.device.identity),
                "reviewed_scenario": asdict(self.reviewed_scenario) if self.reviewed_scenario else None,
                "inventory_target": self.inventory_target,
            })
            for channel in ("A", "B"):
                if self.cancel.is_set():
                    raise SafetyViolation("Series cancelled before output enable.")
                # A setter may mutate and then raise. Rollback must therefore
                # cover the attempted call, not just successful readback.
                policy_transaction_started = True
                result = self.device.set_compliance_policy(channel, "stop")
                if result != "stop" or self.device.compliance_policy(channel) != "stop":
                    raise SafetyViolation(f"Channel {channel} did not confirm STOP.")
            self.policies_changed.emit({"A": "stop", "B": "stop"})

            def journal(kind, data):
                store.write_event(kind, data)
                self.event.emit(kind, data)

            def save(entry):
                store.save_curve(entry)
                self.event.emit("curve_saved", {"index": entry.index, "status": entry.status,
                                               "dataset": entry.dataset})

            runner = FieldSeriesRunner(self.device, self.settings, self.config, journal, save, self.cancel)
            entries = runner.run()
            if self.cancel.is_set():
                status = "cancelled"
            elif len(entries) < len(self.config.currents_a) or (
                entries and entries[-1].status == "sample_compliance"
                and not self.config.continue_after_sample_compliance
            ):
                status = "stopped_on_compliance"
            elif any(entry.status == "skipped_field_compliance" for entry in entries):
                status = "completed_with_skips"
            else:
                status = "completed"
        except Exception as exc:
            errors.append(str(exc))
            status = "cancelled" if self.cancel.is_set() else "fault"
        finally:
            if policy_transaction_started:
                off, restored, restore_errors = restore_field_policies(self.device, self.restore_policies)
            else:
                # Preflight never owned a policy change. Do not overwrite a
                # policy changed since the operator's confirmation.
                restore_errors = []
                for channel in ("A", "B"):
                    try:
                        self.device.confirm_output_off(channel)
                    except Exception as exc:
                        restore_errors.append(f"{channel} OUTPUT OFF: {exc}")
                off, restored = not restore_errors, True
            errors.extend(restore_errors)
            if restored and policy_transaction_started:
                self.policies_changed.emit(dict(self.restore_policies))
            if not restored or not off:
                status = "fault"
            if store is not None:
                try:
                    store.close(status, outputs_confirmed_off=off, detail="; ".join(errors))
                except Exception as exc:
                    errors.append(f"Closing series data: {exc}")
                    status = "fault"
            self.outcome = FieldSeriesOutcome(self.directory, status, off, restored, tuple(errors))
            self.finished_series.emit(self.outcome)


class FieldPolicyRecoveryWorker(QThread):
    finished_recovery = Signal(bool, bool, object)

    def __init__(self, device, originals, parent=None):
        super().__init__(parent)
        self.device, self.originals = device, originals

    def run(self):
        off, restored, errors = restore_field_policies(self.device, self.originals)
        self.finished_recovery.emit(off, restored, errors)
