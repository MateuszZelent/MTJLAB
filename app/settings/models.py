"""Validated representation of `.config/settings.yml`.

Values intentionally remain strings at this boundary.  Safety code converts
them through :mod:`app.domain.quantities`, requiring a dimension every time.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DB,
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


RoleName = Literal["operator", "engineer", "service"]


class AccessControlSettings(StrictModel):
    """Local RBAC bound to the authenticated operating-system account."""

    identity_provider: Literal["operating_system"] = "operating_system"
    default_roles: tuple[RoleName, ...] = ("operator",)
    user_roles: dict[str, tuple[RoleName, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assignments(self) -> "AccessControlSettings":
        if not self.default_roles:
            raise ValueError("access_control.default_roles cannot be empty")
        normalized_users: set[str] = set()
        for username, roles in self.user_roles.items():
            if not username.strip():
                raise ValueError("access_control.user_roles contains an empty username")
            normalized = username.strip().replace("/", "\\").casefold()
            if normalized in normalized_users:
                raise ValueError(
                    "access_control.user_roles contains duplicate OS identities after normalization"
                )
            normalized_users.add(normalized)
            if not roles:
                raise ValueError(f"access_control.user_roles[{username!r}] cannot be empty")
        return self


class ConnectionSettings(StrictModel):
    resource: str | None
    visa_backend: str = "system"
    timeout: str
    read_termination: str | None = None
    write_termination: str | None = None

    @model_validator(mode="after")
    def validate_timeout(self) -> "ConnectionSettings":
        if parse_quantity(self.timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("timeout must be positive")
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
            raise ValueError("expected_serial is required when require_serial_match is enabled")
        return self


class RangeSettings(StrictModel):
    min: str
    max: str
    max_abs: str | None = None

    def checked(self, dimension: str) -> "RangeSettings":
        lower = parse_quantity(self.min, dimension)
        upper = parse_quantity(self.max, dimension)
        if lower.si_value > upper.si_value:
            raise ConfigurationError("the minimum range value is greater than the maximum")
        if self.max_abs is not None and parse_quantity(self.max_abs, dimension).si_value < 0:
            raise ConfigurationError("max_abs cannot be negative")
        return self


class CurrentEstimateSettings(RangeSettings):
    enforcement: Literal["preflight_model_only"]


class IntegerRangeSettings(StrictModel):
    min: int
    max: int

    @model_validator(mode="after")
    def validate_order(self) -> "IntegerRangeSettings":
        if self.min > self.max or self.min < 1:
            raise ValueError("Invalid integer range")
        return self


class ImpedanceSettings(StrictModel):
    min: str
    nominal: str | None = None

    @model_validator(mode="after")
    def validate_impedance(self) -> "ImpedanceSettings":
        if parse_quantity(self.min, DIMENSION_RESISTANCE).si_value <= 0:
            raise ValueError("minimum DUT impedance must be positive")
        if self.nominal is not None and parse_quantity(self.nominal, DIMENSION_RESISTANCE).si_value <= 0:
            raise ValueError("nominal DUT impedance must be positive")
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
            raise ValueError("fixed_source_resistance must be positive")
        if set(self.channels) - {"1", "2"} or not self.channels:
            raise ValueError("Rigol must define channel 1 and/or 2")
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
            raise ValueError("max_abs_power must be positive")
        if parse_quantity(self.ramp_current_step_max, DIMENSION_CURRENT).si_value <= 0:
            raise ValueError("ramp_current_step_max must be positive")
        if parse_quantity(self.ramp_voltage_step_max, DIMENSION_VOLTAGE).si_value <= 0:
            raise ValueError("ramp_voltage_step_max must be positive")
        if self.sweep_points_max < 2:
            raise ValueError("sweep_points_max must be at least 2")
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
            raise ConfigurationError("An optional range must define both limits or neither limit.")
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
        external = parse_quantity(self.external_attenuation, DIMENSION_DB).si_value
        if external < 0:
            raise ValueError("Anritsu external_attenuation cannot be negative")
        if self.minimum_internal_attenuation is not None:
            internal = parse_quantity(
                self.minimum_internal_attenuation, DIMENSION_DB
            ).si_value
            if not 0 <= internal <= 60 or not math.isclose(internal % 2, 0.0, abs_tol=1e-9):
                raise ValueError(
                    "Anritsu minimum_internal_attenuation must be 0..60 dB in 2 dB steps"
                )
        return self


class AnritsuSafety(StrictModel):
    acquisition_allowed: bool = False
    require_rf_input_limit_definition: bool = True
    signal_generator_output_allowed: bool = False
    outputs_off_on_disconnect: bool = True
    rf_input: RfInputSettings
    frequency: OptionalRangeSettings
    reference_level: OptionalRangeSettings
    sweep_points: IntegerRangeSettings
    defaults: dict[str, Any]

    @model_validator(mode="after")
    def validate_optional_ranges(self) -> "AnritsuSafety":
        # Null RF ranges intentionally lock acquisition until a lab owner fills them in.
        self.frequency.checked_if_complete(DIMENSION_FREQUENCY)
        if self.acquisition_allowed:
            if self.frequency.min is None:
                raise ValueError(
                    "Anritsu acquisition requires a complete frequency limit."
                )
            if self.require_rf_input_limit_definition and self.rf_input.max_expected_power_at_connector is None:
                raise ValueError(
                    "Anritsu acquisition requires max_expected_power_at_connector to be defined."
                )
        return self


class AnritsuAcquisitionSettings(StrictModel):
    """Only a qualified protocol may be used for recipe checkpoints."""

    single_sweep_mode: Literal["unverified", "standard_scpi_opc"] = "unverified"
    operation_complete_timeout: str = "30 s"
    application_average_count: int = 200

    @model_validator(mode="after")
    def validate_timeout(self) -> "AnritsuAcquisitionSettings":
        if parse_quantity(self.operation_complete_timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("operation_complete_timeout must be positive")
        if not 1 <= self.application_average_count <= 9999:
            raise ValueError("application_average_count must be in the range 1..9999")
        return self


class AnritsuSignalGeneratorSettings(StrictModel):
    """Fail-closed contract for the optional MS2830A vector signal generator."""

    control_protocol: Literal["unverified", "basic_scpi"] = "unverified"
    frequency: OptionalRangeSettings = Field(
        default_factory=lambda: OptionalRangeSettings(min=None, max=None)
    )
    power: OptionalRangeSettings = Field(
        default_factory=lambda: OptionalRangeSettings(min=None, max=None)
    )
    arm_ttl: str = "30 s"

    @model_validator(mode="after")
    def validate_contract(self) -> "AnritsuSignalGeneratorSettings":
        self.frequency.checked_if_complete(DIMENSION_FREQUENCY)
        self.power.checked_if_complete(DIMENSION_DBM)
        if parse_quantity(self.arm_ttl, DIMENSION_TIME).si_value <= 0:
            raise ValueError("Anritsu SG arm_ttl must be positive")
        return self


class AnritsuAdvancedSpectrumSettings(StrictModel):
    """Qualification gate for input-path and bandwidth SCPI controls."""

    control_protocol: Literal["unverified", "standard_scpi"] = "unverified"
    qualified_firmware: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_contract(self) -> "AnritsuAdvancedSpectrumSettings":
        normalized = tuple(value.strip() for value in self.qualified_firmware)
        if any(not value for value in normalized):
            raise ValueError("qualified_firmware entries cannot be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("qualified_firmware entries must be unique")
        if self.control_protocol == "standard_scpi" and not normalized:
            raise ValueError(
                "Qualified Anritsu advanced control requires at least one firmware version"
            )
        return self


class AnritsuSettings(StrictModel):
    enabled: bool
    display_name: str
    connection: ConnectionSettings
    identity: IdentitySettings
    safety: AnritsuSafety
    acquisition: AnritsuAcquisitionSettings = Field(default_factory=AnritsuAcquisitionSettings)
    signal_generator: AnritsuSignalGeneratorSettings = Field(
        default_factory=AnritsuSignalGeneratorSettings
    )
    advanced_spectrum: AnritsuAdvancedSpectrumSettings = Field(
        default_factory=AnritsuAdvancedSpectrumSettings
    )

    @model_validator(mode="after")
    def validate_signal_generator_permission(self) -> "AnritsuSettings":
        if self.safety.signal_generator_output_allowed:
            if self.signal_generator.control_protocol != "basic_scpi":
                raise ValueError(
                    "Anritsu SG output permission requires a qualified basic_scpi protocol."
                )
            if (
                self.signal_generator.frequency.min is None
                or self.signal_generator.power.min is None
            ):
                raise ValueError(
                    "Anritsu SG output permission requires complete frequency and power limits."
                )
        return self


class MokeBoxSettings(StrictModel):
    """MOKE Box profile; output control stays fail-closed until qualified."""

    enabled: bool = False
    display_name: str = "MOKE Box"
    endpoint: str | None = None
    timeout: str = "3 s"
    expected_model: str | None = None
    protocol_qualified: bool = False
    allow_vout_control: bool = False
    allowed_vout_channels: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_timeout(self) -> "MokeBoxSettings":
        if parse_quantity(self.timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("MOKE Box timeout must be positive")
        if self.allow_vout_control or self.allowed_vout_channels:
            raise ValueError("MOKE Box is read-only; VOUT control permissions are forbidden")
        if any(channel not in range(8) for channel in self.allowed_vout_channels):
            raise ValueError("MOKE VOUT channels must be in 0..7")
        return self


class LakeShoreGaussmeterSettings(StrictModel):
    """Fail-closed connection profile for the read-only Model 475 adapter."""

    enabled: bool = False
    display_name: str = "Lake Shore 475"
    resource: str | None = None
    visa_backend: str = "system"
    timeout: str = "3 s"
    baud_rate: Literal[9600, 19200, 38400, 57600] = 57600
    expected_serial: str | None = None
    require_serial_match: bool = False
    live_interval: str = "1 s"

    @model_validator(mode="after")
    def validate_timeout(self) -> "LakeShoreGaussmeterSettings":
        if parse_quantity(self.timeout, DIMENSION_TIME).si_value <= 0:
            raise ValueError("Lake Shore timeout must be positive")
        if parse_quantity(self.live_interval, DIMENSION_TIME).si_value < 0.5:
            raise ValueError("Lake Shore live_interval must be at least 500 ms")
        if self.require_serial_match and not self.expected_serial:
            raise ValueError("Lake Shore serial matching requires expected_serial")
        return self


class DevicesSettings(StrictModel):
    rigol: RigolSettings
    keithley: KeithleySettings
    anritsu: AnritsuSettings
    moke_box: MokeBoxSettings = Field(default_factory=MokeBoxSettings)
    lakeshore_gaussmeter: LakeShoreGaussmeterSettings = Field(
        default_factory=LakeShoreGaussmeterSettings
    )


class StationSettings(StrictModel):
    schema_version: Literal[1]
    profile: ProfileSettings
    access_control: AccessControlSettings = Field(default_factory=AccessControlSettings)
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

    @property
    def moke_box(self) -> MokeBoxSettings:
        return self.devices.moke_box

    @property
    def lakeshore_gaussmeter(self) -> LakeShoreGaussmeterSettings:
        return self.devices.lakeshore_gaussmeter
