"""Whitelisted TSP adapter for a dual-channel Keithley 2600-family SMU."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
import time
from typing import Literal

from app.devices.base import (
    DeviceAdapter,
    InstrumentSession,
    SessionFactory,
    parse_identity,
    validate_identity,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.safety.keithley import (
    KEITHLEY_2602A_CURRENT_RANGES,
    KEITHLEY_2602A_MAX_CURRENT_RANGE_A,
    KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V,
    KEITHLEY_2602A_VOLTAGE_RANGES,
    KeithleySourceRequest,
    quantize_keithley_value,
    validate_keithley_measurement,
    validate_keithley_source,
)
from app.settings.models import KeithleyChannelSettings, KeithleySettings, StationSettings


KeithleyDutOffMode = Literal["normal", "high_impedance"]


@dataclass(frozen=True, slots=True)
class KeithleyMeasurement:
    channel: Literal["A", "B"]
    voltage_v: float
    current_a: float
    power_w: float
    output_enabled: bool
    measurement_path_connected: bool = True
    compliance_detected: bool = False
    compliance_stop_required: bool = False


@dataclass(frozen=True, slots=True)
class KeithleyChannelConfigurationReadback:
    """Read-only snapshot of one channel's active hardware configuration."""

    channel: Literal["A", "B"]
    output_enabled: bool
    output_off_mode: Literal["normal", "high_impedance", "zero"]
    source_mode: Literal["current", "voltage"]
    source_level_si: float
    compliance_si: float
    source_autorange: bool
    source_range_si: float
    nplc: float
    sense_mode: Literal["2wire", "4wire"]
    measure_voltage_autorange: bool
    measure_voltage_range_v: float
    measure_current_autorange: bool
    measure_current_range_a: float


@dataclass(frozen=True, slots=True)
class KeithleyConfigurationReadback:
    """Complete read-only hardware snapshot for both Keithley SMU channels."""

    channels: tuple[
        KeithleyChannelConfigurationReadback,
        KeithleyChannelConfigurationReadback,
    ]


@dataclass(frozen=True, slots=True)
class KeithleyOutputOffModeResult:
    """Confirmed channel-specific relay mode while OUTPUT remains OFF."""

    channel: Literal["A", "B"]
    mode: KeithleyDutOffMode
    output_enabled: bool


@dataclass(frozen=True, slots=True)
class KeithleyRampRequest:
    channel: Literal["A", "B"]
    target_si: float
    max_step_si: float
    settle_time_s: float
    deadline_s: float = 10.0

    def __post_init__(self) -> None:
        values = (self.target_si, self.max_step_si, self.settle_time_s, self.deadline_s)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Keithley ramp values must be finite.")
        if self.max_step_si <= 0 or self.settle_time_s < 0 or self.deadline_s <= 0:
            raise ValueError("Ramp step/deadline must be positive and settling time non-negative.")


@dataclass(frozen=True, slots=True)
class KeithleyRampResult:
    channel: Literal["A", "B"]
    start_si: float
    target_si: float
    levels_si: tuple[float, ...]
    final_measurement: KeithleyMeasurement


def build_keithley_ramp_levels(
    start_si: float,
    target_si: float,
    max_step_si: float,
    *,
    max_points: int,
) -> tuple[float, ...]:
    """Return finite inclusive target levels with no step above ``max_step_si``."""

    if not all(math.isfinite(value) for value in (start_si, target_si, max_step_si)):
        raise SafetyViolation("Keithley ramp boundaries and step must be finite.")
    if max_step_si <= 0 or max_points < 1:
        raise SafetyViolation("Keithley ramp step and point limit must be positive.")
    steps = max(1, math.ceil(abs(target_si - start_si) / max_step_si))
    if steps > max_points:
        raise SafetyViolation(
            f"Keithley ramp requires {steps} points; configured maximum is {max_points}."
        )
    delta = target_si - start_si
    return tuple(target_si if index == steps else start_si + delta * index / steps for index in range(1, steps + 1))


class KeithleyAdapter(DeviceAdapter):
    """Safe subset of TSP; raw Lua and dynamic namespace objects are never exposed."""

    # Series 2601A/2602A nominal hardware ranges, Reference Manual Rev. E,
    # section 2 "Available ranges". A range assignment is a requested maximum;
    # reading rangeY returns the selected hardware range, not that request.
    _MODEL_2602A_VOLTAGE_RANGES = KEITHLEY_2602A_VOLTAGE_RANGES
    _MODEL_2602A_CURRENT_RANGES = KEITHLEY_2602A_CURRENT_RANGES

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: KeithleySettings = station.keithley
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._last_request: dict[str, KeithleySourceRequest] = {}
        self._output_states: dict[Literal["A", "B"], bool] = {"A": False, "B": False}

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Keithley is not connected.")
        return self._session

    @staticmethod
    def _smu(channel: Literal["A", "B"]) -> str:
        return "smua" if channel == "A" else "smub"

    def _channel_settings(self, channel: Literal["A", "B"]) -> KeithleyChannelSettings:
        return self._settings.safety.channels[channel]

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            return self._identity_or_raise()
        if not self._settings.enabled:
            raise SafetyViolation("Keithley is disabled in the station profile.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("No Keithley VISA resource is configured in settings.yml.")
        timeout = int(parse_quantity(self._settings.connection.timeout, DIMENSION_TIME).si_value * 1000)
        session = self._factory.open(resource, self._settings.connection.visa_backend, timeout)
        try:
            if self._settings.connection.read_termination is not None:
                session.read_termination = self._settings.connection.read_termination
            if self._settings.connection.write_termination is not None:
                session.write_termination = self._settings.connection.write_termination
            identity = parse_identity(resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._settings.identity.expected_vendor_contains,
                expected_models=self._settings.identity.expected_models,
                expected_serial=self._settings.identity.expected_serial,
                require_serial_match=self._settings.identity.require_serial_match,
            )
            self._session = session
            self._identity = identity
            self._capabilities = DeviceCapabilities(
                device_name="keithley",
                model=identity.model or "2600",
                firmware=identity.firmware,
                features=frozenset(
                    {"source_current", "source_voltage", "measure_iv", "ramp_to_zero", "ramp_to_level"}
                ),
            )
            self._state = DeviceState.VERIFIED
            # Connection establishes identity and observes the existing output
            # state only. It must not alter a front-panel or another client's
            # configuration, relay state, source state, or diagnostic queue.
            self._read_output_states()
            self._update_aggregate_output_state()
            return identity
        except Exception:
            try:
                session.close()
            finally:
                self._session = None
                self._identity = None
                self._capabilities = None
                self._state = DeviceState.DISCONNECTED
            raise

    def _identity_or_raise(self) -> DeviceIdentity:
        if self._identity is None:
            raise ConnectionError("Keithley has a session without a verified identity.")
        return self._identity

    def disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            if self._settings.safety.outputs_off_on_disconnect:
                try:
                    session.write("smua.source.output = smua.OUTPUT_OFF")
                    session.write("smub.source.output = smub.OUTPUT_OFF")
                except Exception:
                    pass
            try:
                session.close()
            finally:
                self._identity = None
                self._capabilities = None
                self._last_request.clear()
                self._output_states = {"A": False, "B": False}
                self._state = DeviceState.DISCONNECTED

    def _write_all_outputs_off(self) -> None:
        session = self._require_session()
        session.write("smua.source.output = smua.OUTPUT_OFF")
        session.write("smub.source.output = smub.OUTPUT_OFF")

    def set_dut_output_off_mode(
        self,
        channel: Literal["A", "B"],
        mode: KeithleyDutOffMode,
    ) -> KeithleyOutputOffModeResult:
        """Temporarily isolate or reconnect one confirmed-OFF SMU channel."""

        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        constants = {
            "normal": "OUTPUT_NORMAL",
            "high_impedance": "OUTPUT_HIGH_Z",
        }
        if mode not in constants:
            raise SafetyViolation(
                "Keithley DUT output-off mode must be normal or high_impedance."
            )
        smu = self._smu(channel)
        session = self._require_session()
        active_before = self._output_is_enabled(channel)
        self._output_states[channel] = active_before
        self._update_aggregate_output_state()
        if active_before:
            raise SafetyViolation(
                f"Keithley channel {channel} DUT connection mode can change only "
                "after OUTPUT is confirmed OFF."
            )

        constant = constants[mode]
        mutation_started = False
        try:
            session.write(f"{smu}.source.offmode = {smu}.{constant}")
            mutation_started = True
            response = session.query(
                f"print({smu}.source.offmode == {smu}.{constant})"
            )
            if not self._parse_boolean_readback(f"{smu}.source.offmode", response):
                raise DeviceError(
                    f"Keithley did not confirm {smu}.source.offmode = {constant}."
                )
            active_after = self._output_is_enabled(channel)
        except Exception:
            if mutation_started:
                self._state = DeviceState.UNKNOWN
            raise
        if active_after:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise DeviceError(
                f"Keithley channel {channel} OUTPUT changed while switching DUT "
                "connection mode. Both outputs were commanded OFF."
            )
        self._output_states[channel] = False
        self._update_aggregate_output_state()
        return KeithleyOutputOffModeResult(channel, mode, False)

    def _ensure_normal_output_off_mode(
        self,
        channel: Literal["A", "B"],
    ) -> None:
        """Establish NORMAL only while OFF; never close the relay under load."""

        smu = self._smu(channel)
        active = self._output_is_enabled(channel)
        normal = self._query_boolean(
            f"{smu}.source.offmode == {smu}.OUTPUT_NORMAL"
        )
        if normal:
            return
        if active:
            self._fail_measurement_output_invariant(
                f"Keithley channel {channel} is energized with a non-NORMAL "
                "output-off mode."
            )
        self.set_dut_output_off_mode(channel, "normal")

    def emergency_off(self) -> None:
        if self._session is None:
            return
        try:
            self._write_all_outputs_off()
            states = self._read_output_states()
            if any(states.values()):
                raise DeviceError("Keithley did not confirm OUTPUT OFF during E-STOP.")
        except Exception:
            self._state = DeviceState.UNKNOWN
        else:
            self._output_states = {"A": False, "B": False}
            self._state = DeviceState.OUTPUT_OFF

    def apply_limit_settings(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Keithley limit update requires StationSettings.")
        self.assert_limit_only_update(self._settings, station.keithley)
        if self._session is not None:
            try:
                states = self._read_output_states()
            except Exception:
                self.emergency_off()
                raise
            if any(states.values()):
                raise SafetyViolation(
                    "Keithley limits can change without reconnecting only when OUTPUT A "
                    "and OUTPUT B are confirmed OFF."
                )
        self._station = station
        self._settings = station.keithley
        self._last_request.clear()

    def refresh_station_context(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Keithley context refresh requires StationSettings.")
        self._station = station
        self._settings = station.keithley

    def _clear_errors(self) -> None:
        self._require_session().write("errorqueue.clear()")

    def read_errors(self, limit: int = 20) -> list[str]:
        session = self._require_session()
        errors: list[str] = []
        for _ in range(limit):
            try:
                count = int(float(session.query("print(errorqueue.count)")))
            except (TypeError, ValueError) as exc:
                raise DeviceError("Keithley returned an invalid errorqueue.count value.") from exc
            if count <= 0:
                return errors
            errors.append(session.query("print(errorqueue.next())"))
        errors.append("Keithley error queue did not drain.")
        return errors

    def _check_errors(self) -> None:
        errors = self.read_errors()
        if errors:
            raise DeviceError("Keithley reported an error: " + "; ".join(errors))

    @staticmethod
    def _parse_boolean_readback(field: str, response: str) -> bool:
        normalized = response.strip().upper()
        if normalized in {"1", "1.0", "TRUE", "ON"}:
            return True
        if normalized in {"0", "0.0", "FALSE", "OFF"}:
            return False
        try:
            numeric = float(normalized)
        except ValueError as exc:
            raise DeviceError(
                f"Keithley returned invalid boolean readback for {field}: "
                f"{response!r}."
            ) from exc
        if numeric == 1:
            return True
        if numeric == 0:
            return False
        raise DeviceError(
            f"Keithley returned invalid boolean readback for {field}: {response!r}."
        )

    def _query_boolean(self, expression: str) -> bool:
        response = self._require_session().query(f"print({expression})")
        return self._parse_boolean_readback(expression, response)

    def _query_finite_float(self, field: str) -> float:
        response = self._require_session().query(f"print({field})").strip()
        try:
            value = float(response)
        except ValueError as exc:
            raise DeviceError(
                f"Keithley returned invalid numeric readback for {field}: "
                f"{response!r}."
            ) from exc
        if not math.isfinite(value):
            raise DeviceError(
                f"Keithley returned non-finite numeric readback for {field}: "
                f"{response!r}."
            )
        return value

    def _query_enum(
        self,
        field: str,
        choices: tuple[tuple[str, str], ...],
    ) -> str:
        smu = field.split(".", 1)[0]
        matches = [
            value
            for value, constant in choices
            if self._query_boolean(f"{field} == {smu}.{constant}")
        ]
        if len(matches) != 1:
            raise DeviceError(
                f"Keithley returned an unknown or ambiguous value for {field}."
            )
        return matches[0]

    def read_configuration(self) -> KeithleyConfigurationReadback:
        """Query both channels without writing or changing either OUTPUT state."""

        self._require_session()
        snapshots: list[KeithleyChannelConfigurationReadback] = []
        observed_outputs: dict[Literal["A", "B"], bool] = {}
        for channel in ("A", "B"):
            smu = self._smu(channel)
            output_enabled = self._output_is_enabled(channel)
            output_off_mode = self._query_enum(
                f"{smu}.source.offmode",
                (
                    ("normal", "OUTPUT_NORMAL"),
                    ("high_impedance", "OUTPUT_HIGH_Z"),
                    ("zero", "OUTPUT_ZERO"),
                ),
            )
            source_mode = self._query_enum(
                f"{smu}.source.func",
                (
                    ("current", "OUTPUT_DCAMPS"),
                    ("voltage", "OUTPUT_DCVOLTS"),
                ),
            )
            suffix = "i" if source_mode == "current" else "v"
            compliance_field = "limitv" if source_mode == "current" else "limiti"
            source_autorange = self._query_enum(
                f"{smu}.source.autorange{suffix}",
                (("off", "AUTORANGE_OFF"), ("on", "AUTORANGE_ON")),
            )
            sense_mode = self._query_enum(
                f"{smu}.sense",
                (("2wire", "SENSE_LOCAL"), ("4wire", "SENSE_REMOTE")),
            )
            measure_voltage_autorange = self._query_enum(
                f"{smu}.measure.autorangev",
                (("off", "AUTORANGE_OFF"), ("on", "AUTORANGE_ON")),
            )
            measure_current_autorange = self._query_enum(
                f"{smu}.measure.autorangei",
                (("off", "AUTORANGE_OFF"), ("on", "AUTORANGE_ON")),
            )
            snapshots.append(
                KeithleyChannelConfigurationReadback(
                    channel=channel,
                    output_enabled=output_enabled,
                    output_off_mode=output_off_mode,  # type: ignore[arg-type]
                    source_mode=source_mode,  # type: ignore[arg-type]
                    source_level_si=self._query_finite_float(
                        f"{smu}.source.level{suffix}"
                    ),
                    compliance_si=self._query_finite_float(
                        f"{smu}.source.{compliance_field}"
                    ),
                    source_autorange=source_autorange == "on",
                    source_range_si=self._query_finite_float(
                        f"{smu}.source.range{suffix}"
                    ),
                    nplc=self._query_finite_float(f"{smu}.measure.nplc"),
                    sense_mode=sense_mode,  # type: ignore[arg-type]
                    measure_voltage_autorange=measure_voltage_autorange == "on",
                    measure_voltage_range_v=self._query_finite_float(
                        f"{smu}.measure.rangev"
                    ),
                    measure_current_autorange=measure_current_autorange == "on",
                    measure_current_range_a=self._query_finite_float(
                        f"{smu}.measure.rangei"
                    ),
                )
            )
            observed_outputs[channel] = output_enabled
        self._output_states.update(observed_outputs)
        self._update_aggregate_output_state()
        return KeithleyConfigurationReadback((snapshots[0], snapshots[1]))

    def configure_source(self, request: KeithleySourceRequest) -> None:
        """Set function, range-safe level and compliance while output is guaranteed OFF."""

        if request.channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        channel = self._channel_settings(request.channel)
        validate_keithley_source(channel, request)
        self._validate_model_hardware_request(request)
        # Sweep interpolation can produce a decimal that is valid in the
        # station profile but not representable by the selected 2602A source
        # range.  Quantize before the first TSP write and validate the exact
        # request that will be sent and recorded as the applied state.
        request = self._quantize_source_request(request)
        validate_keithley_source(channel, request)
        self._validate_model_hardware_request(request)
        smu = self._smu(request.channel)
        session = self._require_session()
        session.write(f"{smu}.source.output = {smu}.OUTPUT_OFF")
        self._output_states[request.channel] = False
        self._ensure_normal_output_off_mode(request.channel)
        if request.mode == "measure_only":
            self._configure_measurement_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.measure.nplc = {request.nplc:.12g}")
            self._check_errors()
            self._verify_applied_configuration(request)
            self._last_request[request.channel] = request
            self._update_aggregate_output_state()
            return
        if request.mode == "current":
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCAMPS")
            session.write(f"{smu}.source.limitv = {request.compliance_si:.12g}")
            self._configure_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.source.leveli = {request.level_si:.12g}")
        else:
            session.write(f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS")
            session.write(f"{smu}.source.limiti = {request.compliance_si:.12g}")
            self._configure_ranges_and_sense(session, smu, request)
            session.write(f"{smu}.source.levelv = {request.level_si:.12g}")
        session.write(f"{smu}.measure.nplc = {request.nplc:.12g}")
        self._check_errors()
        self._verify_applied_configuration(request)
        self._last_request[request.channel] = request
        self._update_aggregate_output_state()

    def _verify_applied_configuration(
        self, expected: KeithleySourceRequest
    ) -> None:
        """Read back every programmed source and measurement-path parameter."""

        session = self._require_session()
        smu = self._smu(expected.channel)
        mismatches: list[str] = []

        def query(field: str) -> str:
            return session.query(f"print({field})").strip()

        def numeric(field: str, expected_value: float) -> None:
            response = query(field)
            try:
                actual = float(response)
            except ValueError as exc:
                raise DeviceError(
                    f"Keithley returned invalid numeric readback for {field}: "
                    f"{response!r}."
                ) from exc
            if not math.isclose(
                actual, expected_value, rel_tol=1e-9, abs_tol=1e-12
            ):
                mismatches.append(
                    f"{field} {actual:.12g} != {expected_value:.12g}"
                )

        def selected_range(
            field: str, requested_value: float, available: tuple[float, ...]
        ) -> None:
            response = query(field)
            try:
                actual = float(response)
            except ValueError as exc:
                raise DeviceError(
                    f"Keithley returned invalid numeric readback for {field}: "
                    f"{response!r}."
                ) from exc
            expected_range = self._selected_hardware_range(requested_value, available)
            if not math.isclose(actual, expected_range, rel_tol=1e-9, abs_tol=1e-12):
                mismatches.append(
                    f"{field} selected {actual:.12g}; expected hardware range "
                    f"{expected_range:.12g} for request {requested_value:.12g}"
                )

        def enum(field: str, expected_name: str, expected_numeric: int) -> None:
            response = query(field).upper()
            normalized = response.rsplit(".", 1)[-1]
            if normalized == expected_name:
                return
            try:
                numeric_response = float(response)
            except ValueError:
                numeric_response = math.nan
            if math.isfinite(numeric_response) and math.isclose(
                numeric_response,
                float(expected_numeric),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                return
            mismatches.append(
                f"{field} {response!r} != {expected_name} ({expected_numeric})"
            )

        if self._output_is_enabled(expected.channel):
            mismatches.append(f"{smu}.source.output is ON after configuration")
        numeric(f"{smu}.measure.nplc", expected.nplc)
        enum(
            f"{smu}.sense",
            "SENSE_LOCAL" if expected.sense_mode == "2wire" else "SENSE_REMOTE",
            0 if expected.sense_mode == "2wire" else 1,
        )
        enum(
            f"{smu}.measure.autorangev",
            "AUTORANGE_ON" if expected.measure_voltage_autorange else "AUTORANGE_OFF",
            1 if expected.measure_voltage_autorange else 0,
        )
        enum(
            f"{smu}.measure.autorangei",
            "AUTORANGE_ON" if expected.measure_current_autorange else "AUTORANGE_OFF",
            1 if expected.measure_current_autorange else 0,
        )
        if expected.measure_voltage_range_si is not None:
            selected_range(
                f"{smu}.measure.rangev",
                expected.measure_voltage_range_si,
                self._MODEL_2602A_VOLTAGE_RANGES,
            )
        if expected.measure_current_range_si is not None:
            selected_range(
                f"{smu}.measure.rangei",
                expected.measure_current_range_si,
                self._MODEL_2602A_CURRENT_RANGES,
            )
        if expected.mode != "measure_only":
            suffix = "i" if expected.mode == "current" else "v"
            enum(
                f"{smu}.source.func",
                "OUTPUT_DCAMPS"
                if expected.mode == "current"
                else "OUTPUT_DCVOLTS",
                0 if expected.mode == "current" else 1,
            )
            numeric(f"{smu}.source.level{suffix}", expected.level_si)
            numeric(
                f"{smu}.source.{'limitv' if expected.mode == 'current' else 'limiti'}",
                expected.compliance_si,
            )
            enum(
                f"{smu}.source.autorange{suffix}",
                "AUTORANGE_ON" if expected.source_autorange else "AUTORANGE_OFF",
                1 if expected.source_autorange else 0,
            )
            if expected.source_range_si is not None:
                selected_range(
                    f"{smu}.source.range{suffix}",
                    expected.source_range_si,
                    self._MODEL_2602A_CURRENT_RANGES
                    if expected.mode == "current"
                    else self._MODEL_2602A_VOLTAGE_RANGES,
                )
        if mismatches:
            raise DeviceError(
                "Keithley configuration readback mismatch: "
                + "; ".join(mismatches)
            )

    @staticmethod
    def _selected_hardware_range(
        requested_value: float, available: tuple[float, ...]
    ) -> float:
        requested = abs(requested_value)
        for hardware_range in available:
            if requested <= hardware_range or math.isclose(
                requested, hardware_range, rel_tol=1e-12, abs_tol=1e-15
            ):
                return hardware_range
        raise SafetyViolation(
            f"Requested range {requested_value:.12g} SI exceeds the documented "
            f"2602A hardware maximum {available[-1]:.12g} SI."
        )

    def _validate_model_hardware_request(self, request: KeithleySourceRequest) -> None:
        """Apply immutable 2602A limits independently of editable YAML limits."""

        model = self._identity_or_raise().model.upper().replace(" ", "")
        if "2602A" not in model:
            raise SafetyViolation(
                f"Keithley hardware limits are not qualified for model {model!r}."
            )
        for value, ranges in (
            (request.measure_voltage_range_si, self._MODEL_2602A_VOLTAGE_RANGES),
            (request.measure_current_range_si, self._MODEL_2602A_CURRENT_RANGES),
        ):
            if value is not None:
                self._selected_hardware_range(value, ranges)
        if request.mode == "measure_only":
            return
        if request.mode == "current":
            if abs(request.level_si) > KEITHLEY_2602A_MAX_CURRENT_RANGE_A:
                raise SafetyViolation("2602A source current must not exceed ±3 A.")
            if not 0.01 <= request.compliance_si <= KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V:
                raise SafetyViolation(
                    "2602A voltage compliance must be between 10 mV and 40 V."
                )
            if abs(request.level_si) > 1.0 and request.compliance_si > 6.0:
                raise SafetyViolation(
                    "2602A continuous I-source operation above 1 A is limited "
                    "to 6 V compliance."
                )
            ranges = self._MODEL_2602A_CURRENT_RANGES
        else:
            if abs(request.level_si) > KEITHLEY_2602A_MAX_VOLTAGE_RANGE_V:
                raise SafetyViolation("2602A source voltage must not exceed ±40 V.")
            if not 10e-9 <= request.compliance_si <= KEITHLEY_2602A_MAX_CURRENT_RANGE_A:
                raise SafetyViolation(
                    "2602A current compliance must be between 10 nA and 3 A."
                )
            if abs(request.level_si) > 6.0 and request.compliance_si > 1.0:
                raise SafetyViolation(
                    "2602A continuous V-source operation above 6 V is limited "
                    "to 1 A compliance."
                )
            ranges = self._MODEL_2602A_VOLTAGE_RANGES
        if request.source_range_si is not None:
            self._selected_hardware_range(request.source_range_si, ranges)

    @staticmethod
    def _quantize_source_request(request: KeithleySourceRequest) -> KeithleySourceRequest:
        if request.mode == "measure_only":
            return request
        level_dimension = (
            DIMENSION_CURRENT if request.mode == "current" else DIMENSION_VOLTAGE
        )
        compliance_dimension = (
            DIMENSION_VOLTAGE if request.mode == "current" else DIMENSION_CURRENT
        )
        source_range = (
            request.source_range_si if not request.source_autorange else None
        )
        return replace(
            request,
            level_si=quantize_keithley_value(
                request.level_si,
                level_dimension,
                requested_range_si=source_range,
            ),
            compliance_si=quantize_keithley_value(
                request.compliance_si,
                compliance_dimension,
            ),
        )

    def update_source_level(
        self,
        channel: Literal["A", "B"],
        *,
        mode: Literal["current", "voltage"],
        level_si: float,
    ) -> float:
        """Change one source setpoint without cycling OUTPUT or other parameters."""

        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        current = self._last_request.get(channel)
        if current is None or current.mode == "measure_only":
            raise SafetyViolation(
                "Configure a Keithley source before updating its source level."
            )
        if current.mode != mode:
            raise SafetyViolation(
                f"Keithley channel {channel} is configured for {current.mode}, not {mode}."
            )
        raw_updated = replace(current, level_si=float(level_si))
        channel_settings = self._channel_settings(channel)
        validate_keithley_source(channel_settings, raw_updated)
        self._validate_model_hardware_request(raw_updated)
        updated = self._quantize_source_request(raw_updated)
        validate_keithley_source(channel_settings, updated)
        self._validate_model_hardware_request(updated)
        smu = self._smu(channel)
        suffix = "i" if mode == "current" else "v"
        session = self._require_session()
        output_before = self._output_is_enabled(channel)
        try:
            session.write(f"{smu}.source.level{suffix} = {updated.level_si:.12g}")
            self._check_errors()
            actual = self._query_finite_float(f"{smu}.source.level{suffix}")
            output_after = self._output_is_enabled(channel)
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        if output_after != output_before:
            self._fail_measurement_output_invariant(
                "Keithley OUTPUT state changed during a source-level update."
            )
        if not math.isclose(actual, updated.level_si, rel_tol=1e-9, abs_tol=1e-12):
            raise DeviceError(
                f"Keithley source-level readback {actual:.12g} does not match "
                f"{updated.level_si:.12g} SI."
            )
        self._last_request[channel] = updated
        self._output_states[channel] = output_after
        self._update_aggregate_output_state()
        return actual

    def update_source_compliance(
        self,
        channel: Literal["A", "B"],
        *,
        mode: Literal["current", "voltage"],
        compliance_si: float,
    ) -> float:
        """Change compliance with full validation and no OUTPUT state transition."""

        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        current = self._last_request.get(channel)
        if current is None or current.mode == "measure_only":
            raise SafetyViolation(
                "Configure a Keithley source before updating compliance."
            )
        if current.mode != mode:
            raise SafetyViolation(
                f"Keithley channel {channel} is configured for {current.mode}, not {mode}."
            )
        raw_updated = replace(current, compliance_si=float(compliance_si))
        channel_settings = self._channel_settings(channel)
        validate_keithley_source(channel_settings, raw_updated)
        self._validate_model_hardware_request(raw_updated)
        updated = self._quantize_source_request(raw_updated)
        validate_keithley_source(channel_settings, updated)
        self._validate_model_hardware_request(updated)
        smu = self._smu(channel)
        field = "limitv" if mode == "current" else "limiti"
        session = self._require_session()
        output_before = self._output_is_enabled(channel)
        try:
            session.write(f"{smu}.source.{field} = {updated.compliance_si:.12g}")
            self._check_errors()
            actual = self._query_finite_float(f"{smu}.source.{field}")
            output_after = self._output_is_enabled(channel)
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        if output_after != output_before:
            self._fail_measurement_output_invariant(
                "Keithley OUTPUT state changed during a compliance update."
            )
        if not math.isclose(
            actual, updated.compliance_si, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise DeviceError(
                f"Keithley compliance readback {actual:.12g} does not match "
                f"{updated.compliance_si:.12g} SI."
            )
        self._last_request[channel] = updated
        self._output_states[channel] = output_after
        self._update_aggregate_output_state()
        return actual

    def quick_update_source_level(
        self,
        channel: Literal["A", "B"],
        *,
        mode: Literal["current", "voltage"],
        level_si: float,
    ) -> float:
        """Apply a validated quick target directly and verify its readback."""

        current = self._last_request.get(channel)
        if current is None or current.mode != mode:
            raise SafetyViolation(
                f"Configure Keithley {channel} for {mode} before quick control."
            )
        # A floating quick control is only another UI entry point; it must
        # never bypass the exact same configured source and compliance
        # limits as the regular device page.  Validate before even querying
        # OUTPUT so an invalid target causes no VISA traffic.
        validate_keithley_source(
            self._channel_settings(channel),
            replace(current, level_si=float(level_si)),
        )
        return self.update_source_level(channel, mode=mode, level_si=level_si)

    def quick_control_snapshot(self) -> dict[str, float]:
        """Return source levels from configurations already verified by readback."""

        return {
            f"keithley.{channel}.{request.mode}": request.level_si
            for channel, request in self._last_request.items()
            if request.mode in {"current", "voltage"}
        }

    @staticmethod
    def _configure_ranges_and_sense(
        session: InstrumentSession, smu: str, request: KeithleySourceRequest
    ) -> None:
        """Write only whitelisted range/sense settings with source output OFF."""

        source_suffix = "i" if request.mode == "current" else "v"
        commands = [
            f"{smu}.source.autorange{source_suffix} = {smu}.{'AUTORANGE_ON' if request.source_autorange else 'AUTORANGE_OFF'}",
            *KeithleyAdapter._measurement_range_and_sense_commands(smu, request),
        ]
        if request.source_range_si is not None:
            commands.append(f"{smu}.source.range{source_suffix} = {request.source_range_si:.12g}")
        for command in commands:
            session.write(command)

    def _output_is_enabled(self, channel: Literal["A", "B"]) -> bool:
        smu = self._smu(channel)
        response = self._require_session().query(f"print({smu}.source.output)").strip().upper()
        if response in {"ON", "OUTPUT_ON"}:
            return True
        if response in {"OFF", "OUTPUT_OFF"}:
            return False
        try:
            value = float(response)
        except ValueError as exc:
            raise DeviceError("Keithley returned an invalid source.output state.") from exc
        if value == 1:
            return True
        if value == 0:
            return False
        raise DeviceError(f"Keithley returned unknown source.output state {response!r}.")

    def _read_output_states(self) -> dict[Literal["A", "B"], bool]:
        states = {channel: self._output_is_enabled(channel) for channel in ("A", "B")}
        self._output_states.update(states)
        return states

    def _update_aggregate_output_state(self) -> None:
        self._state = DeviceState.OUTPUT_ON if any(self._output_states.values()) else DeviceState.OUTPUT_OFF

    @staticmethod
    def _configure_measurement_ranges_and_sense(
        session: InstrumentSession, smu: str, request: KeithleySourceRequest
    ) -> None:
        for command in KeithleyAdapter._measurement_range_and_sense_commands(smu, request):
            session.write(command)

    @staticmethod
    def _measurement_range_and_sense_commands(smu: str, request: KeithleySourceRequest) -> list[str]:
        commands = [
            f"{smu}.measure.autorangev = {smu}.{'AUTORANGE_ON' if request.measure_voltage_autorange else 'AUTORANGE_OFF'}",
            f"{smu}.measure.autorangei = {smu}.{'AUTORANGE_ON' if request.measure_current_autorange else 'AUTORANGE_OFF'}",
            f"{smu}.sense = {smu}.{'SENSE_LOCAL' if request.sense_mode == '2wire' else 'SENSE_REMOTE'}",
        ]
        if request.measure_voltage_range_si is not None:
            commands.append(f"{smu}.measure.rangev = {request.measure_voltage_range_si:.12g}")
        if request.measure_current_range_si is not None:
            commands.append(f"{smu}.measure.rangei = {request.measure_current_range_si:.12g}")
        return commands

    def set_output(self, channel: Literal["A", "B"], enabled: bool) -> bool:
        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        settings = self._channel_settings(channel)
        if enabled:
            if not self._settings.safety.allow_output_enable:
                raise SafetyViolation(
                    "Keithley OUTPUT ON is disabled in the station configuration."
                )
            request = self._last_request.get(channel)
            if request is None:
                raise SafetyViolation("Configure a safe Keithley source before enabling OUTPUT.")
            validate_keithley_source(settings, request)
            self._ensure_normal_output_off_mode(channel)
            # Re-read the complete programmed source immediately before the
            # single energising transition. This also proves OUTPUT is still
            # OFF and detects front-panel or remote changes after configure.
            self._verify_applied_configuration(request)
        smu = self._smu(channel)
        try:
            self._require_session().write(
                f"{smu}.source.output = {smu}.OUTPUT_ON" if enabled else f"{smu}.source.output = {smu}.OUTPUT_OFF"
            )
            self._check_errors()
            active = self._output_is_enabled(channel)
        except Exception:
            self.emergency_off()
            raise
        if active != enabled:
            if enabled:
                self.emergency_off()
            raise DeviceError("Keithley did not confirm the requested output state.")
        self._output_states[channel] = active
        self._update_aggregate_output_state()
        return active

    def measure(self, channel: Literal["A", "B"]) -> KeithleyMeasurement:
        if channel not in {"A", "B"}:
            raise SafetyViolation("Keithley channel must be A or B.")
        smu = self._smu(channel)
        session = self._require_session()
        expected_output_states = dict(self._output_states)
        observed_before = self._read_output_states()
        if observed_before != expected_output_states:
            self._fail_measurement_output_invariant(
                "Keithley output readback changed outside the configured control path "
                "before measurement."
            )
        self._ensure_normal_output_off_mode(channel)
        try:
            # One TSP acquisition keeps I and V from the same measurement
            # instant. Keithley returns current first, then voltage.
            response = session.query(f"print({smu}.measure.iv())").strip()
            values = [item for item in re.split(r"[,;\t\s]+", response) if item]
            if len(values) != 2:
                raise ValueError(f"expected two IV values, received {len(values)}")
            current, voltage = (float(item) for item in values)
            if not (math.isfinite(current) and math.isfinite(voltage)):
                raise ValueError("non-finite IV result")
        except (TypeError, ValueError) as exc:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise DeviceError("Keithley returned an invalid I/V measurement.") from exc
        observed_after = self._read_output_states()
        if observed_after != observed_before:
            self._fail_measurement_output_invariant(
                "Keithley measurement changed an OUTPUT state unexpectedly."
            )
        request = self._last_request.get(channel)
        try:
            hardware_compliance = self._query_boolean(f"{smu}.source.compliance")
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        compliance_detected = hardware_compliance or self._at_compliance_limit(
            request, voltage=voltage, current=current
        )
        stop_required = compliance_detected and self._settings.safety.stop_on_compliance
        if stop_required:
            self.emergency_off()
            self._state = DeviceState.COMPLIANCE
        try:
            validate_keithley_measurement(
                self._channel_settings(channel),
                voltage,
                current,
            )
        except SafetyViolation as exc:
            # A manual read must be as fail-safe as a recipe checkpoint: trip
            # limits are laboratory boundaries, so both outputs are disabled.
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise SafetyViolation(
                f"Keithley channel {channel} measurement safety trip: {exc} "
                f"Measured I={current:.9g} A, V={voltage:.9g} V and "
                f"|P|={abs(voltage * current):.9g} W. Both outputs were "
                "commanded OFF and confirmed OFF."
            ) from exc
        try:
            self._check_errors()
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        return KeithleyMeasurement(
            channel,
            voltage,
            current,
            voltage * current,
            self._output_states[channel],
            True,
            compliance_detected,
            stop_required,
        )

    def _fail_measurement_output_invariant(self, message: str) -> None:
        """Force both channels OFF after an uncommanded output transition."""

        self.emergency_off()
        if self._state is not DeviceState.UNKNOWN:
            self._state = DeviceState.FAULT
        raise DeviceError(message + " Both outputs were forced OFF.")

    @staticmethod
    def _at_compliance_limit(
        request: KeithleySourceRequest | None, *, voltage: float, current: float
    ) -> bool:
        """Conservatively infer compliance from a measured limiting quantity.

        The exact status attribute varies across 2600 firmware.  Until that
        model-specific probe has been qualified, reaching the programmed limit
        itself is sufficient evidence to enter the safe compliance policy.
        """

        if request is None or request.mode == "measure_only" or request.compliance_si <= 0:
            return False
        limiting_value = voltage if request.mode == "current" else current
        tolerance = max(abs(request.compliance_si) * 1e-4, 1e-9)
        return abs(limiting_value) >= abs(request.compliance_si) - tolerance

    def ramp_to_zero(self, channel: Literal["A", "B"], *, deadline_s: float = 10.0) -> None:
        request = self._last_request.get(channel)
        if request is None or request.mode == "measure_only":
            self.set_output(channel, False)
            return
        limits = self._channel_settings(channel).lab_limits
        step = (
            parse_quantity(limits.ramp_current_step_max, "current").si_value
            if request.mode == "current"
            else parse_quantity(limits.ramp_voltage_step_max, "voltage").si_value
        )
        level = request.level_si
        started = time.monotonic()
        try:
            while abs(level) > step:
                if time.monotonic() - started > deadline_s:
                    raise DeviceError("Keithley ramp-to-zero timed out.")
                level -= step if level > 0 else -step
                self.update_source_level(channel, mode=request.mode, level_si=level)
                if request.settle_time_s:
                    time.sleep(request.settle_time_s)
            self.update_source_level(channel, mode=request.mode, level_si=0.0)
            self.set_output(channel, False)
        except Exception:
            self.emergency_off()
            raise

    def ramp_to_level(self, request: KeithleyRampRequest) -> KeithleyRampResult:
        """Ramp an already active source without ever enabling an output.

        The actual starting level is queried from the instrument. Every point
        is checked against the configured source/DUT envelope and followed by an
        atomic I/V measurement. Any transport, compliance or limit failure
        attempts to turn both SMU outputs off.
        """

        channel = request.channel
        current_request = self._last_request.get(channel)
        if current_request is None or current_request.mode == "measure_only":
            raise SafetyViolation("Configure a Current or Voltage source before using a ramp.")
        try:
            output_enabled = self._output_is_enabled(channel)
        except Exception:
            self.emergency_off()
            raise
        if not output_enabled:
            self._output_states[channel] = False
            self._update_aggregate_output_state()
            raise SafetyViolation("Manual ramp requires the selected Keithley OUTPUT to be ON.")
        channel_settings = self._channel_settings(channel)
        limits = channel_settings.lab_limits
        dimension = "current" if current_request.mode == "current" else "voltage"
        configured_step = parse_quantity(
            limits.ramp_current_step_max
            if current_request.mode == "current"
            else limits.ramp_voltage_step_max,
            dimension,
        ).si_value
        step_tolerance = max(abs(configured_step), 1.0) * 1e-12
        if request.max_step_si > configured_step + step_tolerance:
            raise SafetyViolation(
                f"Requested ramp step {request.max_step_si:.12g} SI exceeds configured "
                f"maximum {configured_step:.12g} SI."
            )
        if limits.point_settle_time is not None and limits.point_settle_time.enabled:
            minimum_settle = parse_quantity(limits.point_settle_time.min, DIMENSION_TIME).si_value
            maximum_settle = parse_quantity(limits.point_settle_time.max, DIMENSION_TIME).si_value
            if not minimum_settle <= request.settle_time_s <= maximum_settle:
                raise SafetyViolation(
                    f"Ramp settling time must be within {minimum_settle:.12g}.."
                    f"{maximum_settle:.12g} s."
                )
        target_request = replace(current_request, level_si=request.target_si)
        validate_keithley_source(channel_settings, target_request)
        smu = self._smu(channel)
        field = "leveli" if current_request.mode == "current" else "levelv"
        session = self._require_session()
        try:
            response = session.query(f"print({smu}.source.{field})")
            start_si = float(response.strip())
        except (TypeError, ValueError) as exc:
            self.emergency_off()
            raise DeviceError("Keithley returned an invalid source level before ramping.") from exc
        if not math.isfinite(start_si):
            self.emergency_off()
            raise DeviceError("Keithley returned a non-finite source level before ramping.")
        start_request = replace(current_request, level_si=start_si)
        try:
            validate_keithley_source(channel_settings, start_request)
        except Exception:
            self.emergency_off()
            if self._state is not DeviceState.UNKNOWN:
                self._state = DeviceState.FAULT
            raise
        levels = build_keithley_ramp_levels(
            start_si,
            request.target_si,
            request.max_step_si,
            max_points=limits.sweep_points_max,
        )
        predicted_s = len(levels) * request.settle_time_s
        if predicted_s > request.deadline_s:
            raise SafetyViolation(
                f"Ramp dwell alone requires {predicted_s:.3g} s, exceeding the "
                f"{request.deadline_s:.3g} s deadline."
            )
        started = time.monotonic()
        final_measurement: KeithleyMeasurement | None = None
        try:
            for level in levels:
                if time.monotonic() - started > request.deadline_s:
                    raise DeviceError("Keithley manual ramp exceeded its deadline.")
                step_request = replace(current_request, level_si=level)
                validate_keithley_source(channel_settings, step_request)
                self.update_source_level(
                    channel, mode=current_request.mode, level_si=level
                )
                if request.settle_time_s:
                    time.sleep(request.settle_time_s)
                if time.monotonic() - started > request.deadline_s:
                    raise DeviceError("Keithley manual ramp exceeded its deadline.")
                final_measurement = self.measure(channel)
                if final_measurement.compliance_stop_required:
                    raise SafetyViolation("Keithley reached compliance during the manual ramp.")
            if final_measurement is None:
                raise DeviceError("Keithley ramp produced no measurement checkpoint.")
            if not self._output_is_enabled(channel):
                raise DeviceError("Keithley OUTPUT switched off unexpectedly during the ramp.")
        except Exception:
            self.emergency_off()
            if self._state not in {DeviceState.UNKNOWN, DeviceState.COMPLIANCE}:
                self._state = DeviceState.FAULT
            raise
        self._last_request[channel] = target_request
        self._output_states[channel] = True
        self._update_aggregate_output_state()
        return KeithleyRampResult(channel, start_si, request.target_si, levels, final_measurement)
