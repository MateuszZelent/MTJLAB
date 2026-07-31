"""Typed, unit-aware values that can accompany a manually saved spectrum."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.domain.quantities import format_quantity_auto


@dataclass(frozen=True, slots=True)
class ManualMetadataValue:
    """One last-confirmed device value offered by the manual HDF5 saver.

    ``value_si`` is already normalised at the device boundary.  The key is a
    stable persisted identity and deliberately carries a unit suffix where the
    value is dimensional (for example ``keithley.B.current_a``).
    """

    key: str
    device: str
    label: str
    dimension: str | None
    unit: str
    value_si: float
    source: str = "last confirmed value"

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Manual metadata requires a stable key.")
        if not self.device.strip() or not self.label.strip():
            raise ValueError("Manual metadata requires a device and label.")
        if not self.unit.strip():
            raise ValueError("Manual metadata requires an explicit unit.")
        if not math.isfinite(float(self.value_si)):
            raise ValueError(f"Manual metadata {self.key!r} is not finite.")

    @property
    def display_value(self) -> str:
        """Format the normalised value for the selection dialog only."""

        if self.dimension:
            try:
                return format_quantity_auto(self.value_si, self.dimension)
            except (TypeError, ValueError):
                pass
        return f"{self.value_si:.9g} {self.unit}"

