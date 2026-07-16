"""Validated representation of `.config/settings.yml`.

Values intentionally remain strings at this boundary.  Safety code converts
them through :mod:`app.domain.quantities`, requiring a dimension every time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileSettings(StrictModel):
    id: str
    name: str
    state: Literal["unverified", "approved", "revoked"]
    approved_by: str | None = None
    approved_at: str | None = None
    approval_note: str | None = None
    lock_outputs_when_unverified: bool = True


class ConnectionSettings(StrictModel):
    resource: str | None
    visa_backend: str = "system"
    timeout: str
    read_termination: str | None = None
    write_termination: str | None = None

    @model_validator(mode="after")
    def validate_timeout(self) -> "ConnectionSettings":
        if parse_quantity(self.timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("timeout musi być dodatni")
        return self


class IdentitySettings(StrictModel):
    expected_vendor_contains: str
    expected_models: tuple[str, ...] = ()
    require_serial_match: bool = False
    expected_serial: str | None = None
    required_options: tuple[str, ...] = ()

    @model_validator(mode="after")
    def serial_rule_is_complete(self) -> "IdentitySettings":
        if self.require_serial_match and not self.expected_serial:
            raise ValueError("expected_serial jest wymagany przy require_serial_match")
        return self


class RangeSettings(StrictModel):
    min: str
    max: str
    max_abs: str | None = None

    def checked(self, dimension: str) -> "RangeSettings":
        lower = parse_quantity(self.min, dimension)
        upper = parse_quantity(self.max, dimension)
        if lower.si_value > upper.si_value:
            raise ConfigurationError("minimalna wartość zakresu jest większa od maksymalnej")
        if self.max_abs is not None and parse_quantity(self.max_abs, dimension).si_value < 0:
            raise ConfigurationError("max_abs nie może być ujemne")
        return self


class CurrentEstimateSettings(RangeSettings):
    enforcement: Literal["preflight_model_only"]


class IntegerRangeSettings(StrictModel):
    min: int
    max: int

    @model_validator(mode="after")
    def validate_order(self) -> "IntegerRangeSettings":
        if self.min > self.max or self.min < 1:
            raise ValueError("Nieprawidłowy zakres całkowity")
        return self


class ImpedanceSettings(StrictModel):
    min: str
    nominal: str | None = None

    @model_validator(mode="after")
    def validate_impedance(self) -> "ImpedanceSettings":
        if parse_quantity(self.min, DIMENSION_RESISTANCE).si_value <= 0:
            raise ValueError("minimalna impedancja DUT musi być dodatnia")
        if self.nominal is not None and parse_quantity(self.nominal, DIMENSION_RESISTANCE).si_value <= 0:
            raise ValueError("nominalna impedancja DUT musi być dodatnia")
        return self


class RigolChannelLimits(StrictModel):
    frequency: RangeSettings
    high_level: RangeSettings
    low_level: RangeSettings
    amplitude_vpp: RangeSettings
    offset: RangeSettings
    estimated_load_current: CurrentEstimateSettings
    estimated_load_power: RangeSettings
    declared_dut_impedance: ImpedanceSettings
    settle_time: RangeSettings
    modulation_rate: RangeSettings
    sweep_duration: RangeSettings
    sweep_steps: IntegerRangeSettings
    burst_period: RangeSettings
    burst_cycles: IntegerRangeSettings

    @model_validator(mode="after")
    def validate_dimensions(self) -> "RigolChannelLimits":
        self.frequency.checked(DIMENSION_FREQUENCY)
        self.high_level.checked(DIMENSION_VOLTAGE)
        self.low_level.checked(DIMENSION_VOLTAGE)
        self.amplitude_vpp.checked(DIMENSION_VOLTAGE)
        self.offset.checked(DIMENSION_VOLTAGE)
        self.estimated_load_current.checked(DIMENSION_CURRENT)
        self.estimated_load_power.checked(DIMENSION_POWER)
        self.settle_time.checked(DIMENSION_TIME)
        self.modulation_rate.checked(DIMENSION_FREQUENCY)
        self.sweep_duration.checked(DIMENSION_TIME)
        self.burst_period.checked(DIMENSION_TIME)
        return self


class RigolChannelSettings(StrictModel):
    enabled: bool
    allowed_waveforms: tuple[str, ...]
    lab_limits: RigolChannelLimits
    defaults: dict[str, Any]


class RigolSafety(StrictModel):
    allow_output_enable: bool = False
    outputs_off_on_connect: bool = True
    outputs_off_on_disconnect: bool = True
    require_declared_dut_impedance: bool = True
    require_external_current_sensor_for_runtime_trip: bool = True
    fixed_source_resistance: str
    channels: dict[str, RigolChannelSettings]

    @model_validator(mode="after")
    def validate_source_resistance(self) -> "RigolSafety":
        if parse_quantity(self.fixed_source_resistance, DIMENSION_RESISTANCE).si_value <= 0:
            raise ValueError("fixed_source_resistance musi być dodatnia")
        if set(self.channels) - {"1", "2"} or not self.channels:
            raise ValueError("Rigol musi mieć kanały 1 i/lub 2")
        return self


class RigolSettings(StrictModel):
    enabled: bool
    display_name: str
    connection: ConnectionSettings
    identity: IdentitySettings
    capabilities: dict[str, Any]
    safety: RigolSafety


class KeithleyChannelLimits(StrictModel):
    source_current: RangeSettings
    source_voltage: RangeSettings
    current_compliance: RangeSettings
    voltage_compliance: RangeSettings
    measured_current_trip: RangeSettings
    measured_voltage_trip: RangeSettings
    max_abs_power: str
    ramp_current_step_max: str
    ramp_voltage_step_max: str
    sweep_points_max: int = 1000
    point_settle_time: RangeSettings | None = None

    @model_validator(mode="after")
    def validate_dimensions(self) -> "KeithleyChannelLimits":
        self.source_current.checked(DIMENSION_CURRENT)
        self.source_voltage.checked(DIMENSION_VOLTAGE)
        self.current_compliance.checked(DIMENSION_CURRENT)
        self.voltage_compliance.checked(DIMENSION_VOLTAGE)
        self.measured_current_trip.checked(DIMENSION_CURRENT)
        self.measured_voltage_trip.checked(DIMENSION_VOLTAGE)
        if parse_quantity(self.max_abs_power, DIMENSION_POWER).si_value <= 0:
            raise ValueError("max_abs_power musi być dodatnie")
        if parse_quantity(self.ramp_current_step_max, DIMENSION_CURRENT).si_value <= 0:
            raise ValueError("ramp_current_step_max musi być dodatnie")
        if parse_quantity(self.ramp_voltage_step_max, DIMENSION_VOLTAGE).si_value <= 0:
            raise ValueError("ramp_voltage_step_max musi być dodatnie")
        if self.sweep_points_max < 2:
            raise ValueError("sweep_points_max musi wynosić co najmniej 2")
        if self.point_settle_time is not None:
            self.point_settle_time.checked(DIMENSION_TIME)
        return self


class KeithleyChannelSettings(StrictModel):
    enabled: bool
    allowed_source_modes: tuple[Literal["current", "voltage", "measure_only"], ...]
    lab_limits: KeithleyChannelLimits
    defaults: dict[str, Any]


class KeithleySafety(StrictModel):
    allow_output_enable: bool = False
    outputs_off_on_connect: bool = True
    outputs_off_on_disconnect: bool = True
    output_off_mode: str
    stop_on_compliance: bool = True
    stop_on_overpower: bool = True
    channels: dict[Literal["A", "B"], KeithleyChannelSettings]


class KeithleySettings(StrictModel):
    enabled: bool
    display_name: str
    connection: ConnectionSettings
    identity: IdentitySettings
    driver: dict[str, Any]
    safety: KeithleySafety


class OptionalRangeSettings(StrictModel):
    """Range intentionally left incomplete until RF limits are approved."""

    min: str | None
    max: str | None

    def checked_if_complete(self, dimension: str) -> "OptionalRangeSettings":
        if (self.min is None) != (self.max is None):
            raise ConfigurationError("Zakres opcjonalny musi mieć oba końce albo żadnego.")
        if self.min is not None and self.max is not None:
            RangeSettings(min=self.min, max=self.max).checked(dimension)
        return self


class RfInputSettings(StrictModel):
    max_expected_power_at_connector: str | None = None
    external_attenuation: str
    minimum_internal_attenuation: str | None = None
    preamplifier_allowed: bool = False
    dc_input_allowed: bool = False

    @model_validator(mode="after")
    def validate_expected_power(self) -> "RfInputSettings":
        if self.max_expected_power_at_connector is not None:
            parse_quantity(self.max_expected_power_at_connector, DIMENSION_DBM)
        return self


class AnritsuSafety(StrictModel):
    acquisition_allowed: bool = False
    require_rf_input_limit_definition: bool = True
    signal_generator_output_allowed: bool = False
    rf_input: RfInputSettings
    frequency: OptionalRangeSettings
    reference_level: OptionalRangeSettings
    sweep_points: IntegerRangeSettings
    defaults: dict[str, Any]

    @model_validator(mode="after")
    def validate_optional_ranges(self) -> "AnritsuSafety":
        # Null RF ranges intentionally lock acquisition until a lab owner fills them in.
        self.frequency.checked_if_complete(DIMENSION_FREQUENCY)
        self.reference_level.checked_if_complete(DIMENSION_DBM)
        if self.acquisition_allowed:
            if self.frequency.min is None or self.reference_level.min is None:
                raise ValueError("Akwizycja Anritsu wymaga kompletnych limitów częstotliwości i reference level.")
            if self.require_rf_input_limit_definition and self.rf_input.max_expected_power_at_connector is None:
                raise ValueError("Akwizycja Anritsu wymaga max_expected_power_at_connector.")
        return self


class AnritsuAcquisitionSettings(StrictModel):
    """Only a qualified protocol may be used for recipe checkpoints."""

    single_sweep_mode: Literal["unverified", "standard_scpi_opc"] = "unverified"
    operation_complete_timeout: str = "30 s"
    application_average_count: int = 200

    @model_validator(mode="after")
    def validate_timeout(self) -> "AnritsuAcquisitionSettings":
        if parse_quantity(self.operation_complete_timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("operation_complete_timeout musi być dodatni")
        if not 1 <= self.application_average_count <= 9999:
            raise ValueError("application_average_count musi być w zakresie 1..9999")
        return self


class AnritsuSettings(StrictModel):
    enabled: bool
    display_name: str
    connection: ConnectionSettings
    identity: IdentitySettings
    safety: AnritsuSafety
    acquisition: AnritsuAcquisitionSettings = Field(default_factory=AnritsuAcquisitionSettings)


class DevicesSettings(StrictModel):
    rigol: RigolSettings
    keithley: KeithleySettings
    anritsu: AnritsuSettings


class StationSettings(StrictModel):
    schema_version: Literal[1]
    profile: ProfileSettings
    application: dict[str, Any]
    units: dict[str, Any]
    execution: dict[str, Any]
    storage: dict[str, Any]
    ui: dict[str, Any]
    devices: DevicesSettings
    recipe_defaults: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_devices(self) -> "StationSettings":
        return self

    @property
    def outputs_locked(self) -> bool:
        # A malformed or manually edited profile must never turn an
        # unverified/revoked state into permission to energise a DUT.
        return self.profile.state != "approved"

    @property
    def rigol(self) -> RigolSettings:
        return self.devices.rigol

    @property
    def keithley(self) -> KeithleySettings:
        return self.devices.keithley

    @property
    def anritsu(self) -> AnritsuSettings:
        return self.devices.anritsu
