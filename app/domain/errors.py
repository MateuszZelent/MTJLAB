"""Domain exceptions whose messages are safe to present in the user interface."""


class LabControlError(RuntimeError):
    """Base exception for expected application errors."""


class ConfigurationError(LabControlError):
    """The local station profile is invalid or cannot be trusted."""


class SafetyViolation(LabControlError):
    """A requested operation violates an effective station or DUT limit."""


class DeviceError(LabControlError):
    """The instrument rejected a command or reported an operational error."""


class ConnectionError(DeviceError):
    """The instrument session is unavailable or its identity is unexpected."""


class ExecutionError(LabControlError):
    """A compiled measurement plan cannot continue safely."""

