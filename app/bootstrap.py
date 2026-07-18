"""Application composition root for device modules and their controllers."""

from __future__ import annotations

from PySide6.QtCore import QObject

from app.contracts import DeviceModuleRegistry
from app.devices.registry import built_in_device_registry
from app.settings.models import StationSettings
from app.ui.workers import DeviceController


class StationComposition:
    """Build runtime services without leaking concrete adapters into the UI shell."""

    def __init__(
        self,
        settings: StationSettings,
        *,
        simulation: bool,
        registry: DeviceModuleRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.simulation = simulation
        self.registry = registry or built_in_device_registry()

    def create_controller(self, key: str, parent: QObject) -> DeviceController:
        module = self.registry.get(key)
        return DeviceController(
            self.create_adapter(key),
            parent,
            dispatcher=module.dispatch,
        )

    def create_adapter(self, key: str, *, settings: StationSettings | None = None):
        """Create a replacement adapter after a profile update."""

        return self.registry.get(key).create_adapter(
            settings or self.settings,
            simulation=self.simulation,
        )

    def create_controllers(self, keys: tuple[str, ...], parent: QObject) -> dict[str, DeviceController]:
        return {key: self.create_controller(key, parent) for key in keys}
