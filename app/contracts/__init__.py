"""Stable contracts shared by the application shell and device modules."""

from app.contracts.device_module import (
    DeviceModule, DeviceModuleRegistry, DevicePageFactory, OperationDispatcher, RecipeExtension,
)

__all__ = ["DeviceModule", "DeviceModuleRegistry", "DevicePageFactory", "OperationDispatcher", "RecipeExtension"]
