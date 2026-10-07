"""Execute blocking Keithley preflight/readback through its existing device proxy."""
from threading import Event

from PySide6.QtCore import QThread

from app.domain.errors import SafetyViolation
from .output_state import require_output_off


class OutputProofWorker(QThread):
    def __init__(self, proxy, channel, config, settings, parent=None, *, field_series=False):
        super().__init__(parent)
        self.proxy, self.channel, self.config = proxy, channel, config
        self.settings = settings.model_copy(deep=True)
        self.field_series = field_series
        self.cancelled = Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            if self.cancelled.is_set():
                return
            if not getattr(self.proxy, "connected", True):
                raise SafetyViolation("Keithley instrument is not connected.")
            if self.field_series:
                policies = {}
                for channel in ("A", "B"):
                    if self.cancelled.is_set():
                        return
                    policy = str(self.proxy.compliance_policy(channel))
                    if policy not in {"stop", "warn_clamp", "skip"}:
                        raise SafetyViolation(f"Keithley channel {channel} returned an unknown compliance policy.")
                    policies[channel] = policy
                    if self.cancelled.is_set():
                        return
                    require_output_off(self.proxy, channel)
                self.result = policies
                return
            if self.config is None:
                # Existing recovery contract: confirm_output_off may perform
                # emergency OFF if readback is ON/uncertain. Never enable here.
                require_output_off(self.proxy, self.channel)
                self.result = True
                return
            readback = self.proxy.read_configuration()
            channel = next((item for item in getattr(readback, "channels", ())
                            if str(getattr(item, "channel", "")) == self.channel), None)
            if channel is None or getattr(channel, "output_enabled", None) is not False:
                raise SafetyViolation(f"Keithley channel {self.channel} OUTPUT is ON or unconfirmed. Turn it OFF before characterization.")
            if self.cancelled.is_set():
                return
            policy = str(self.proxy.compliance_policy(self.channel))
            if policy not in {"stop", "warn_clamp", "skip"}:
                raise SafetyViolation("Keithley returned an unknown compliance policy.")
            self.result = policy
        except Exception as exc:
            self.error = str(exc)
