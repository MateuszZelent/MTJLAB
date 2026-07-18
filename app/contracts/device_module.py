"""Typed, explicit composition contracts for vertical device modules.

This is deliberately an in-process registry, not a dynamic plugin mechanism.
Instrument modules are reviewed and shipped with the application, while the shell
only depends on this small public contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from app.devices.base import DeviceAdapter
from app.settings.models import StationSettings


class OperationDispatcher(Protocol):
    """Execute a device-specific high-level operation on its worker thread."""

    def __call__(self, adapter: DeviceAdapter, operation: str, payload: object) -> object: ...


AdapterFactory = Callable[[StationSettings, bool], DeviceAdapter]
DevicePageFactory = Callable[[object, StationSettings], object]


@dataclass(frozen=True, slots=True)
class RecipeExtension:
    """Declarative recipe surface owned by one device module.

    The contract intentionally contains data only.  Qt factories and compiler
    hooks can be added alongside a versioned recipe schema without making the
    device registry depend on any UI implementation.
    """

    module_key: str
    parameter_definitions: tuple[Mapping[str, str], ...] = ()
    library_block_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceModule:
    """Public manifest of one hardware family.

    ``dispatch`` must expose only validated high-level actions.  It must never
    provide a raw SCPI or arbitrary-code escape hatch.
    """

    key: str
    display_name: str
    settings_key: str | None
    adapter_factory: AdapterFactory
    dispatch: OperationDispatcher
    capabilities: frozenset[str] = field(default_factory=frozenset)
    enabled_by_default: bool = True
    recipe_extension: RecipeExtension | None = None
    page_factory: DevicePageFactory | None = None

    def create_adapter(self, settings: StationSettings, *, simulation: bool) -> DeviceAdapter:
        return self.adapter_factory(settings, simulation)

    def create_page(self, controller: object, settings: StationSettings) -> object:
        if self.page_factory is None:
            raise ValueError(f"Device module {self.key!r} does not provide a manual page.")
        return self.page_factory(controller, settings)


class DeviceModuleRegistry:
    """Immutable lookup used by composition roots and generic UI services."""

    def __init__(self, modules: Iterable[DeviceModule]) -> None:
        module_list = tuple(modules)
        indexed = {module.key: module for module in module_list}
        if not indexed:
            raise ValueError("At least one device module must be registered.")
        if len(indexed) != len(module_list):
            raise ValueError("Device module keys must be unique.")
        for module in module_list:
            extension = module.recipe_extension
            if extension is not None and extension.module_key != module.key:
                raise ValueError(
                    f"Recipe extension key {extension.module_key!r} does not match "
                    f"device module {module.key!r}."
                )
        self._modules = indexed

    def get(self, key: str) -> DeviceModule:
        try:
            return self._modules[key]
        except KeyError as exc:
            raise ValueError(f"Unknown device module {key!r}.") from exc

    def enabled_modules(self) -> tuple[DeviceModule, ...]:
        return tuple(module for module in self._modules.values() if module.enabled_by_default)

    def all_modules(self) -> tuple[DeviceModule, ...]:
        return tuple(self._modules.values())

    def recipe_parameter_definitions(
        self, *, enabled_only: bool = True
    ) -> tuple[Mapping[str, str], ...]:
        """Aggregate recipe controls without importing device UI packages."""

        modules = self.enabled_modules() if enabled_only else self.all_modules()
        definitions = tuple(
            definition
            for module in modules
            if module.recipe_extension is not None
            for definition in module.recipe_extension.parameter_definitions
        )
        targets = [definition.get("target") for definition in definitions]
        if len(targets) != len(set(targets)):
            raise ValueError("Recipe parameter targets must be unique across modules.")
        return definitions
