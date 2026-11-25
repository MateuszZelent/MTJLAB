"""Safe Anritsu MS2830A spectrum and live-trace adapter."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime

import numpy as np

from app.devices.anritsu_ms2830a.hardware import (
    ANRITSU_PREAMPLIFIER_OPTIONS,
    ANRITSU_SIGNAL_GENERATOR_OPTIONS,
    parse_anritsu_hardware_catalog,
)
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
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.domain.spectrum_averaging import (
    ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS,
    ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS,
    InstrumentAverageProvenance,
)
from app.domain.spectrum_correction import SweepEvidence
from app.safety.anritsu import (
    ANRITSU_CISPR_OPTIONS,
    ANRITSU_SWEEP_POINT_COUNTS,
    assert_anritsu_acquisition_allowed,
    normalize_anritsu_detector,
    validate_anritsu_advanced_spectrum,
    validate_anritsu_signal_generator,
    validate_anritsu_spectrum,
    validate_anritsu_trace_name,
)
from app.settings.models import AnritsuSettings, StationSettings

from .averaging import instrument_average_count_for_duration
from .configuration import SignalGeneratorConfig, SpectrumConfig
from .sweep_timing import (
    FRAME_FIELDS,
    GATE_FIELDS,
    SPECTRUM_TIMING_FIRMWARE,
    TIMING_PARAMETERS,
    TRIGGER_FIELDS,
    SpectrumTimingSettings,
    SpectrumTimingSnapshot,
)

_VERTICAL_SCALE_QUERY = "DISP:WIND:TRAC:Y:SPAC?"
_QUALIFIED_TRACE_PROTOCOL_FIRMWARE = frozenset({"7.03.00", "sim-1.0"})
_LIVE_BINARY_READBACK = (
    f"SWE:POIN?;FREQ:STAR?;FREQ:STOP?;FORM?;FORM:BORD?;TRAC:STOR:MODE? 1;{_VERTICAL_SCALE_QUERY};SYST:ERR?"
)
_TRACE_STORAGE_MODES = frozenset({"OFF", "AVER", "LAV", "MAXH", "MINH"})
_BINARY_TRACE_FORMAT = ("REAL,32", "SWAP")
_SWEEP_STATUS_TIMEOUT_MS = 500


@dataclass(frozen=True, slots=True)
class AnritsuConfigurationSnapshot:
    """Read-only snapshot of the analyser's current spectrum settings."""

    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    instrument_mode: str = ""
    synchronized: bool = False
    rf_off_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class AnritsuFullConfigurationReadback:
    start_hz: float
    stop_hz: float
    center_hz: float
    span_hz: float
    reference_level_dbm: float
    points: int
    rbw_auto: bool
    rbw_hz: float
    vbw_auto: bool
    vbw_mode: str  # "VID" or "POW"
    vbw_hz: float | None
    sweep_time_auto: bool
    sweep_time_s: float
    attenuation_auto: bool
    attenuation_db: float
    detector: str
    continuous_sweep: bool
    average_count: int
    instrument_mode: str = ""
    preamplifier_enabled: bool = False
    timing_context: str | None = None


@dataclass(frozen=True, slots=True)
class AdvancedSpectrumConfig:
    rbw_auto: bool | None = None
    rbw_hz: float | None = None
    vbw_mode: str | None = None
    vbw_hz: float | None = None
    detector: str | None = None
    attenuation_auto: bool | None = None
    attenuation_db: float | None = None
    preamplifier_enabled: bool | None = None
    sweep_time_auto: bool | None = None
    sweep_time_s: float | None = None
    vbw_filter_mode: str | None = None


@dataclass(frozen=True, slots=True)
class AdvancedSpectrumSnapshot:
    rbw_auto: bool
    rbw_hz: float
    vbw_mode: str
    vbw_hz: float | None
    detector: str
    attenuation_auto: bool
    attenuation_db: float
    preamplifier_enabled: bool
    sweep_time_auto: bool
    sweep_time_s: float
    instrument_mode: str
    vbw_filter_mode: str | None = None
    timing_context: str | None = None


@dataclass(frozen=True, slots=True)
class SignalGeneratorSnapshot:
    frequency_hz: float
    power_dbm: float
    output_enabled: bool
    instrument_mode: str


@dataclass(frozen=True, slots=True)
class SpectrumTrace:
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    acquired_at_utc: datetime
    trace_name: str
    sweep_evidence: SweepEvidence = SweepEvidence.UNKNOWN
    sweep_id: str | None = None
    acquisition_started_at_utc: datetime | None = None
    acquisition_completed_at_utc: datetime | None = None
    configuration_generation: int = 0
    instrument_average: InstrumentAverageProvenance | None = None


@dataclass(frozen=True, slots=True)
class InstrumentAverageTarget:
    """A finite analyser average specified by sweeps or nominal seconds."""

    frame_count: int | None = None
    duration_s: float | None = None

    def __post_init__(self) -> None:
        if (self.frame_count is None) == (self.duration_s is None):
            raise SafetyViolation("Select either a sweep count or a duration for Anritsu averaging.")
        if self.frame_count is not None and (
            type(self.frame_count) is not int
            or not ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS <= self.frame_count <= ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS
        ):
            raise SafetyViolation("Anritsu instrument averaging requires an integer count of 2 to 9999 sweeps.")
        if self.duration_s is not None and (
            type(self.duration_s) not in (int, float) or not 0 < self.duration_s < math.inf
        ):
            raise SafetyViolation("Anritsu instrument averaging duration must be finite and positive in seconds.")


@dataclass(frozen=True, slots=True)
class InstrumentAverageResult:
    """Completed linear-power mean and the analyser's nominal per-sweep time."""

    trace: SpectrumTrace
    count: int
    nominal_sweep_time_s: float
    configuration: tuple[AnritsuFullConfigurationReadback, AdvancedSpectrumSnapshot] | None = None


@dataclass(frozen=True, slots=True)
class _InstrumentAverageSettings:
    storage_mode: str
    count: int
    write_mode: str
    data_format: str
    byte_order: str
    other_storage_modes: tuple[str, ...] = ("OFF", "OFF", "OFF", "OFF", "OFF")


@dataclass(frozen=True, slots=True)
class ReferenceSpectrum:
    """A reproducible reference trace with acquisition provenance."""

    trace: SpectrumTrace
    kind: str
    average_count: int
    acquired_at_utc: datetime
    source_device_idn: str = ""
    firmware: str = ""
    hardware_options: tuple[str, ...] = ()
    reference_level_dbm: float | None = None
    advanced_configuration_known: bool = False
    rbw_auto: bool | None = None
    rbw_hz: float | None = None
    vbw_mode: str = ""
    vbw_hz: float | None = None
    detector: str = ""
    attenuation_auto: bool | None = None
    attenuation_db: float | None = None
    preamplifier_enabled: bool | None = None
    sweep_time_auto: bool | None = None
    sweep_time_s: float | None = None
    source_file: str = ""
    notes: str = ""
    saved_to_file: bool = False
    grid_hash: str = ""
    vbw_filter_mode: str | None = None
    timing_context: str | None = None

    def __post_init__(self) -> None:
        if self.vbw_filter_mode not in {None, "VID", "POW"}:
            raise ValueError("Reference VBW filter mode must be VID or POW.")
        if self.kind not in {"single", "averaged", "imported"}:
            raise ValueError(f"Unsupported reference kind {self.kind!r}.")
        if self.average_count < 1:
            raise ValueError("Reference average_count must be positive.")
        if self.instrument_average is not None and self.instrument_average.sweep_count != self.average_count:
            raise ValueError("Reference count differs from its Anritsu average.")
        if len(self.trace.frequencies_hz) != len(self.trace.powers_dbm) or len(self.trace.frequencies_hz) < 2:
            raise ValueError("Reference trace must contain matching frequency and power arrays.")
        if not all(math.isfinite(value) for value in (*self.trace.frequencies_hz, *self.trace.powers_dbm)):
            raise ValueError("Reference trace contains non-finite values.")
        if any(right <= left for left, right in zip(self.trace.frequencies_hz, self.trace.frequencies_hz[1:])):
            raise ValueError("Reference frequency grid must be strictly increasing.")
        optional_numeric = (
            self.reference_level_dbm,
            self.rbw_hz,
            self.vbw_hz,
            self.attenuation_db,
            self.sweep_time_s,
        )
        if any(value is not None and not math.isfinite(value) for value in optional_numeric):
            raise ValueError("Reference acquisition metadata contains a non-finite value.")
        if self.advanced_configuration_known:
            if self.rbw_auto is None or self.rbw_hz is None or self.rbw_hz <= 0:
                raise ValueError("Known advanced reference metadata requires a valid RBW state.")
            if self.vbw_mode not in {"auto", "manual", "off"}:
                raise ValueError("Known advanced reference metadata requires a valid VBW mode.")
            if self.vbw_mode != "off" and (self.vbw_hz is None or self.vbw_hz <= 0):
                raise ValueError("Known advanced reference metadata requires a valid VBW value.")
            if not self.detector:
                raise ValueError("Known advanced reference metadata requires a detector.")
            if self.attenuation_auto is None or self.attenuation_db is None:
                raise ValueError("Known advanced reference metadata requires attenuation state.")
            if self.attenuation_db < 0:
                raise ValueError("Reference attenuation cannot be negative.")
            if self.preamplifier_enabled is None:
                raise ValueError("Known advanced reference metadata requires preamplifier state.")
            if self.sweep_time_auto is None or self.sweep_time_s is None or self.sweep_time_s <= 0:
                raise ValueError("Known advanced reference metadata requires sweep-time state.")
        expected_hash = self.hash_grid(self.trace.frequencies_hz)
        if self.grid_hash and self.grid_hash != expected_hash:
            raise ValueError("Reference grid hash does not match its frequency data.")
        if not self.grid_hash:
            object.__setattr__(self, "grid_hash", expected_hash)

    @property
    def instrument_average(self) -> InstrumentAverageProvenance | None:
        return self.trace.instrument_average

    @staticmethod
    def hash_grid(frequencies_hz: tuple[float, ...]) -> str:
        payload = ",".join(format(value, ".17g") for value in frequencies_hz).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @property
    def start_hz(self) -> float:
        return self.trace.frequencies_hz[0]

    @property
    def stop_hz(self) -> float:
        return self.trace.frequencies_hz[-1]

    @property
    def points(self) -> int:
        return len(self.trace.frequencies_hz)


class AnritsuAdapter(DeviceAdapter):
    """Spectrum acquisition is explicit; RF generator output is never auto-enabled."""

    def __init__(self, station: StationSettings, *, session_factory: SessionFactory | None = None) -> None:
        super().__init__()
        self._station = station
        self._settings: AnritsuSettings = station.anritsu
        self._factory = session_factory or PyVisaSessionFactory()
        self._session: InstrumentSession | None = None
        self._live = False
        self._passive_live = False
        self._live_synchronized = False
        self._pending_synchronized_sweep: tuple[datetime, float] | None = None
        self._live_storage_before: tuple[bool, str] | None = None
        self._group_live_queries = False
        self._last_sg_config: SignalGeneratorConfig | None = None
        self._sg_control_owned = False
        self._sg_output_enabled = False
        self._options_query_failed = False
        self._cached_grid: tuple[float, float, int, tuple[float, ...]] | None = None
        self._configuration_generation = 0
        self._acquisition_sequence = 0
        self._remote_entered = False

    @classmethod
    def _read_hardware_options(cls, session: InstrumentSession) -> tuple[bool, tuple[str, ...]]:
        """Best-effort read of installed options without making connection depend on it.

        Returns (success: bool, options: tuple[str, ...]).
        """

        original_timeout = session.timeout
        try:
            # *OPT? is not a documented MS2830A header. Use its hardware
            # catalogue and the installed language without changing either
            # the language or the active measurement application.
            session.timeout = max(1, min(original_timeout, 2000))
            native = cls._read_remote_language(session) == "NAT"
            if native:
                # Station firmware 7.03.00 rejects OPTINFO? HARD in SPECT.
                # Spectrum acquisition does not need optional hardware discovery.
                # Keep capabilities unknown instead of probing unsupported
                # headers or switching to CONFIG during connection.
                return False, ()
            return True, parse_anritsu_hardware_catalog(session.query("SYST:HARD:OPT:CAT?"))
        except (DeviceError, ConnectionError, OSError, ValueError, TypeError):
            return False, ()
        finally:
            session.timeout = original_timeout

    @staticmethod
    def _read_remote_language(session: InstrumentSession) -> str:
        language = session.query("SYST:LANG?").strip().strip('"').upper()
        if language == "NATIVE":
            language = "NAT"
        if language not in {"SCPI", "NAT"}:
            raise DeviceError(f"Anritsu returned unknown remote language {language!r}.")
        return language

    def _prepare_trace_a(self, *, language: str | None = None) -> str:
        session = self._require_session()
        # Mainframe Remote Control 1.6.2: Native moves the indexed SCPI
        # header's number into the first argument. Never change the language
        # implicitly, and never send TRAC1:TYPE to a Native interpreter.
        resolved_language = (
            self._read_remote_language(session) if language is None else language
        )
        command = "TRAC:TYPE 1,WRIT" if resolved_language == "NAT" else "TRAC1:TYPE WRIT"
        session.write(command)
        self._check_scpi_errors(
            session, f"Trace A preparation ({command}, language={resolved_language})"
        )
        return resolved_language

    def _require_session(self) -> InstrumentSession:
        if self._session is None:
            raise ConnectionError("Anritsu is not connected.")
        return self._session

    def connect(self) -> DeviceIdentity:
        if self._session is not None:
            if self._identity is None:
                raise ConnectionError("Anritsu has a session without a verified identity.")
            return self._identity
        if not self._settings.enabled:
            raise SafetyViolation("Anritsu is disabled in the station profile.")
        resource = self._settings.connection.resource
        if not resource:
            raise ConnectionError("No Anritsu VISA resource is configured in settings.yml.")
        timeout = int(parse_quantity(self._settings.connection.timeout, DIMENSION_TIME).si_value * 1000)
        session = self._open_session(self._factory, resource, self._settings.connection.visa_backend, timeout)
        try:
            # Empty strings preserve the VISA backend defaults. Assigning an
            # empty terminator can make MS2830A queries time out over GPIB.
            if self._settings.connection.read_termination:
                session.read_termination = self._settings.connection.read_termination
            if self._settings.connection.write_termination:
                session.write_termination = self._settings.connection.write_termination
            identity = parse_identity(resource, session.query("*IDN?"))
            validate_identity(
                identity,
                vendor_contains=self._settings.identity.expected_vendor_contains,
                expected_models=self._settings.identity.expected_models,
                expected_serial=self._settings.identity.expected_serial,
                require_serial_match=self._settings.identity.require_serial_match,
            )
            success, hardware_options = self._read_hardware_options(session)
            self._options_query_failed = not success
            if not success and self._settings.identity.required_options:
                raise ConnectionError(
                    "Cannot verify profile-required Anritsu hardware options: "
                    "the option catalogue is unavailable in the current remote language/application."
                )
            missing_options = set(self._settings.identity.required_options) - set(
                hardware_options
            )
            if missing_options:
                raise ConnectionError(
                    "Anritsu is missing profile-required hardware option(s): "
                    + ", ".join(sorted(missing_options))
                )
            self._session = session
            self._identity = identity
            has_sg = bool(
                ANRITSU_SIGNAL_GENERATOR_OPTIONS.intersection(hardware_options)
            )
            self._capabilities = DeviceCapabilities(
                device_name="anritsu",
                model=identity.model or "MS2830A",
                firmware=identity.firmware,
                features=frozenset(
                    {"spectrum_trace", "live_trace"}
                    | ({"synchronized_single_sweep"} if self._single_sweep_supported else set())
                    | ({"spectrum_timing"} if self._supports_sweep_timing() else set())
                    | (
                        {"signal_generator"}
                        if has_sg
                        else set()
                    )
                ),
                hardware_options=hardware_options,
            )
            # Identification is read-only. Explicit SG configuration establishes
            # RF OFF before configuring it; spectrum-only sessions do not own RF.
            self._state = DeviceState.VERIFIED
            return identity
        except Exception:
            session.close()
            self._session = None
            self._identity = None
            self._capabilities = None
            self._last_sg_config = None
            self._sg_output_enabled = False
            self._state = DeviceState.DISCONNECTED
            raise

    def disconnect(self) -> None:
        session = self._session
        if (
            session is not None
            and self._settings.safety.outputs_off_on_disconnect
            and (
                self._sg_control_owned or self._sg_output_enabled
                or (self._last_sg_config is not None)
            )
        ):
            self.emergency_off()
            if self._state == DeviceState.UNKNOWN:
                raise DeviceError(
                    "Cannot disconnect Anritsu because RF OUTPUT OFF could not be verified. "
                    "Keep the session open and use E-STOP or remove RF power externally."
                )
        try:
            self.stop_live()
        finally:
            session, self._session = self._session, None
            self._remote_entered = False
            self._sg_control_owned = False
            self._live_storage_before = None
            self._last_sg_config = None
            self._sg_output_enabled = False
            if session is not None:
                try:
                    session.close()
                finally:
                    self._identity = None
                    self._capabilities = None
                    self._state = DeviceState.DISCONNECTED

    @staticmethod
    def _stop_spectrum_acquisition(session) -> None:
        # MS2830A Spectrum Analyzer Remote Control, section 2.7, p. 2-321:
        # use the documented short header accepted by the target firmware.
        session.write("ABOR")
        status = session.query("INIT:SWP?").strip()
        if status not in {"0", "+0"}:
            raise DeviceError(f"Anritsu did not confirm sweep stopped: INIT:SWP?={status!r}.")

    def abort_acquisition(self) -> bool:
        """Stop spectrum acquisition without selecting or controlling SG/RF."""
        session = self._require_session()
        try:
            try:
                self._stop_spectrum_acquisition(session)
            finally:
                self.stop_live()
        except Exception:
            self._state = DeviceState.UNKNOWN
            raise
        self._state = DeviceState.VERIFIED
        return True

    def emergency_off(self) -> bool:
        """Best-effort RF OFF followed by acquisition abort.

        When the SG option is installed, E-STOP may explicitly change the
        active application because proving the energy source OFF has priority
        over preserving the front-panel mode.
        """

        if self._session is None:
            return False
        session = self._session
        errors: list[Exception] = []
        has_generator = bool(
            (self._capabilities is not None and self._capabilities.supports("signal_generator"))
            or self._sg_output_enabled
            or (self._last_sg_config is not None)
        )
        if has_generator:
            try:
                session.write("INST SG")
                session.write("OUTP 0")
                if self._parse_output_state(session.query("OUTP?")):
                    raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF during E-STOP.")
                self._sg_output_enabled = False
                session.write("INST SPECT")
            except Exception as exc:  # noqa: BLE001 - E-STOP must attempt all remaining shutdown steps
                errors.append(exc)
        try:
            self._stop_spectrum_acquisition(session)
        except Exception as exc:  # noqa: BLE001 - E-STOP must attempt all remaining shutdown steps
            errors.append(exc)
        try:
            self.stop_live()
        except Exception as exc:  # noqa: BLE001 - E-STOP must attempt all remaining shutdown steps
            errors.append(exc)
        if errors:
            self._state = DeviceState.UNKNOWN
        else:
            self._state = DeviceState.VERIFIED
        return not errors

    def apply_limit_settings(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Anritsu limit update requires StationSettings.")
        self.assert_limit_only_update(self._settings, station.anritsu)
        if (
            self._session is not None
            and self._capabilities is not None
            and self._capabilities.supports("signal_generator")
        ):
            session = self._session
            try:
                session.write("INST SG")
                output_enabled = self._parse_output_state(session.query("OUTP?"))
                session.write("INST SPECT")
            except Exception:
                self.emergency_off()
                raise
            if output_enabled:
                raise SafetyViolation(
                    "Anritsu limits can change without reconnecting only when the RF "
                    "generator output is confirmed OFF."
                )
        self._station = station
        self._settings = station.anritsu
        self._last_sg_config = None

    def refresh_station_context(self, station: object) -> None:
        if not isinstance(station, StationSettings):
            raise TypeError("Anritsu context refresh requires StationSettings.")
        self._station = station
        self._settings = station.anritsu

    def _assert_signal_generator_supported(self) -> None:
        if self._capabilities is None or not self._capabilities.supports("signal_generator"):
            raise SafetyViolation(
                "The connected Anritsu did not report an installed signal-generator option."
            )

    def _enter_spectrum_mode_with_rf_off(self, *, require_rf_off_proof: bool = False) -> bool:
        """Stop session-owned SG output before explicitly selecting Spectrum.

        Returns True only when SG RF OUTPUT OFF was physically commanded and
        confirmed via ``OUTP?`` before entering Spectrum Analyzer mode.
        """

        session = self._require_session()
        rf_off_confirmed = False
        if require_rf_off_proof:
            self._assert_signal_generator_supported()
        if (
            require_rf_off_proof
            or self._sg_control_owned
            or self._sg_output_enabled
            or self._last_sg_config is not None
        ):
            session.write("INST SG")
            session.write("OUTP 0")
            if self._parse_output_state(session.query("OUTP?")):
                self._state = DeviceState.UNKNOWN
                raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF.")
            self._sg_output_enabled = False
            rf_off_confirmed = True
        session.write("INST SPECT")
        return rf_off_confirmed

    @staticmethod
    def _parse_output_state(response: str) -> bool:
        normalized = response.strip().upper()
        if normalized in {"1", "+1", "ON"}:
            return True
        if normalized in {"0", "+0", "OFF"}:
            return False
        raise DeviceError(f"Anritsu returned invalid SG output state {response!r}.")

    def read_signal_generator_configuration(self) -> SignalGeneratorSnapshot:
        """Read SG state without silently changing the active application."""

        self._assert_signal_generator_supported()
        session = self._require_session()
        mode = session.query("INST?").strip()
        if "SG" not in mode.upper():
            raise DeviceError(
                f"Signal-generator readback requires explicit SG mode; current mode is {mode!r}."
            )
        try:
            frequency_hz = float(session.query("FREQ?"))
            power_dbm = float(session.query("POW?"))
            output_enabled = self._parse_output_state(session.query("OUTP?"))
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned invalid SG configuration data.") from exc
        if not math.isfinite(frequency_hz) or not math.isfinite(power_dbm) or frequency_hz <= 0:
            raise DeviceError("Anritsu returned non-finite or invalid SG configuration data.")
        self._sg_output_enabled = output_enabled
        self._state = DeviceState.OUTPUT_ON if output_enabled else DeviceState.OUTPUT_OFF
        return SignalGeneratorSnapshot(frequency_hz, power_dbm, output_enabled, mode)

    def assert_signal_generator_output_state(self, *, expected_enabled: bool) -> bool:
        """Confirm RF state and the last validated frequency/power snapshot."""

        cached_output = self._sg_output_enabled
        snapshot = self.read_signal_generator_configuration()
        if snapshot.output_enabled != cached_output:
            self.emergency_off()
            raise DeviceError(
                "Anritsu SG RF OUTPUT changed outside the configured control path."
            )
        if self._last_sg_config is not None and not (
            math.isclose(
                snapshot.frequency_hz,
                self._last_sg_config.frequency_hz,
                rel_tol=1e-9,
                abs_tol=1.0,
            )
            and math.isclose(
                snapshot.power_dbm,
                self._last_sg_config.power_dbm,
                rel_tol=0.0,
                abs_tol=0.01,
            )
        ):
            if snapshot.output_enabled:
                self.emergency_off()
            raise DeviceError(
                "Anritsu SG readback no longer matches the validated frequency/power."
            )
        if snapshot.output_enabled != expected_enabled:
            if snapshot.output_enabled:
                self.emergency_off()
            raise DeviceError(
                "Anritsu SG RF OUTPUT is "
                f"{'ON' if snapshot.output_enabled else 'OFF'}; expected "
                f"{'ON' if expected_enabled else 'OFF'}."
            )
        return snapshot.output_enabled

    def configure_signal_generator(self, config: SignalGeneratorConfig) -> SignalGeneratorSnapshot:
        """Explicitly enter SG mode, force RF OFF, configure and verify readback."""

        self._assert_signal_generator_supported()
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=config.frequency_hz,
            power_dbm=config.power_dbm,
        )
        session = self._require_session()
        self._sg_control_owned = True
        session.write("INST SG")
        session.write("OUTP 0")
        if self._parse_output_state(session.query("OUTP?")):
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF before configuration.")
        session.write("UNIT:POW DBM")
        session.write(f"FREQ {config.frequency_hz:.12g}HZ")
        session.write(f"POW {config.power_dbm:.12g}")
        actual = self.read_signal_generator_configuration()
        mismatches: list[str] = []
        if not math.isclose(actual.frequency_hz, config.frequency_hz, rel_tol=1e-9, abs_tol=1.0):
            mismatches.append(
                f"frequency requested={config.frequency_hz:g} Hz actual={actual.frequency_hz:g} Hz"
            )
        if not math.isclose(actual.power_dbm, config.power_dbm, rel_tol=0.0, abs_tol=0.01):
            mismatches.append(
                f"power requested={config.power_dbm:g} dBm actual={actual.power_dbm:g} dBm"
            )
        if actual.output_enabled:
            mismatches.append("RF output is ON")
        if mismatches:
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu SG configuration readback mismatch: " + "; ".join(mismatches))
        self._last_sg_config = config
        self._state = DeviceState.OUTPUT_OFF
        return actual

    def update_signal_generator(
        self, config: SignalGeneratorConfig
    ) -> SignalGeneratorSnapshot:
        """Update validated SG setpoints without changing confirmed RF state."""

        self._assert_signal_generator_supported()
        if self._last_sg_config is None:
            raise SafetyViolation(
                "Configure and verify the Anritsu SG before a live setpoint update."
            )
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=config.frequency_hz,
            power_dbm=config.power_dbm,
        )
        session = self._require_session()
        before = self.read_signal_generator_configuration()
        selected = {"frequency_hz", "power_dbm"} if config.changed_fields is None else set(config.changed_fields)
        if not selected or not selected <= {"frequency_hz", "power_dbm"}:
            raise SafetyViolation("Invalid selected signal generator fields.")
        config = replace(config, **{
            key: getattr(before, key) for key in {"frequency_hz", "power_dbm"} - selected
        })
        validate_anritsu_signal_generator(
            self._settings, frequency_hz=config.frequency_hz, power_dbm=config.power_dbm,
        )
        try:
            if "frequency_hz" in selected and not math.isclose(before.frequency_hz, config.frequency_hz, rel_tol=1e-12, abs_tol=1e-6):
                session.write(f"FREQ {config.frequency_hz:.12g}HZ")
            if "power_dbm" in selected and not math.isclose(before.power_dbm, config.power_dbm, rel_tol=0, abs_tol=1e-9):
                session.write(f"POW {config.power_dbm:.12g}")
            actual = self.read_signal_generator_configuration()
        except Exception:
            self.emergency_off()
            raise
        if actual.output_enabled != before.output_enabled:
            self.emergency_off()
            raise DeviceError(
                "Anritsu SG RF OUTPUT state changed during a live setpoint update."
            )
        mismatches: list[str] = []
        if not math.isclose(
            actual.frequency_hz, config.frequency_hz, rel_tol=1e-9, abs_tol=1.0
        ):
            mismatches.append("frequency")
        if not math.isclose(
            actual.power_dbm, config.power_dbm, rel_tol=0.0, abs_tol=0.01
        ):
            mismatches.append("power")
        if mismatches:
            self.emergency_off()
            raise DeviceError(
                "Anritsu SG live-update readback mismatch: "
                + ", ".join(mismatches)
            )
        self._last_sg_config = config
        self._sg_output_enabled = actual.output_enabled
        self._state = (
            DeviceState.OUTPUT_ON
            if actual.output_enabled
            else DeviceState.OUTPUT_OFF
        )
        return actual

    def set_signal_generator_output(self, enabled: bool) -> bool:
        """Apply one explicit RF transition; OFF is always available."""

        self._assert_signal_generator_supported()
        session = self._require_session()
        if not enabled:
            self._sg_control_owned = True
            session.write("OUTP 0")
            active = self._parse_output_state(session.query("OUTP?"))
            self._sg_output_enabled = active
            self._state = DeviceState.UNKNOWN if active else DeviceState.OUTPUT_OFF
            if active:
                raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF.")
            return False
        if not self._settings.safety.signal_generator_output_allowed:
            raise SafetyViolation("Anritsu SG RF output is locked by the station profile.")
        if self._last_sg_config is None:
            raise SafetyViolation("Configure and verify the Anritsu SG before RF OUTPUT ON.")
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=self._last_sg_config.frequency_hz,
            power_dbm=self._last_sg_config.power_dbm,
        )
        snapshot = self.read_signal_generator_configuration()
        if snapshot.output_enabled:
            raise SafetyViolation(
                "Anritsu SG RF output is already ON; use RF OFF before a new "
                "validated transition."
            )
        validate_anritsu_signal_generator(
            self._settings,
            frequency_hz=snapshot.frequency_hz,
            power_dbm=snapshot.power_dbm,
        )
        if not (
            math.isclose(
                snapshot.frequency_hz,
                self._last_sg_config.frequency_hz,
                rel_tol=1e-9,
                abs_tol=1.0,
            )
            and math.isclose(
                snapshot.power_dbm,
                self._last_sg_config.power_dbm,
                rel_tol=0.0,
                abs_tol=0.01,
            )
        ):
            raise SafetyViolation(
                "Anritsu SG readback changed after configuration; apply and verify "
                "the visible frequency and power again before RF OUTPUT ON."
            )
        self._state = DeviceState.UNKNOWN
        self._sg_output_enabled = True
        session.write("OUTP 1")
        try:
            active = self._parse_output_state(session.query("OUTP?"))
        except Exception as exc:
            self._sg_output_enabled = True
            self._state = DeviceState.UNKNOWN
            try:
                session.write("OUTP 0")
                readback = self._parse_output_state(session.query("OUTP?"))
                if not readback:
                    self._sg_output_enabled = False
                    self._state = DeviceState.OUTPUT_OFF
            except (DeviceError, ConnectionError, OSError, RuntimeError, ValueError) as shutdown_error:
                exc.add_note(f"Anritsu SG fallback RF OFF also failed: {shutdown_error}")
            raise DeviceError(
                "Anritsu SG readback failed after commanding RF OUTPUT ON; "
                "attempted shutdown and marked state UNKNOWN."
            ) from exc

        self._sg_output_enabled = active
        self._state = DeviceState.OUTPUT_ON if active else DeviceState.OUTPUT_OFF
        if not active:
            fallback_error: str | None = None
            try:
                session.write("OUTP 0")
            except (DeviceError, ConnectionError, OSError, RuntimeError) as shutdown_error:
                fallback_error = str(shutdown_error)
            self._state = DeviceState.UNKNOWN
            error = DeviceError("Anritsu SG did not confirm RF OUTPUT ON.")
            if fallback_error is not None:
                error.add_note(f"Anritsu SG fallback RF OFF also failed: {fallback_error}")
            raise error
        return True

    @property
    def _single_sweep_supported(self) -> bool:
        return self._settings.acquisition.single_sweep_mode == "standard_scpi_opc"

    def _assert_acquisition_allowed(self) -> None:
        assert_anritsu_acquisition_allowed(self._settings.safety)

    def _supports_sweep_timing(self) -> bool:
        identity = self._identity
        return (
            identity is not None and identity.model.upper() == "MS2830A"
            and identity.firmware.strip() in SPECTRUM_TIMING_FIRMWARE
        )

    def _supports_qualified_trace_protocol(self) -> bool:
        identity = self._identity
        if identity is None or identity.model.upper() != "MS2830A":
            return False
        firmware = identity.firmware.strip()
        return (
            firmware in _QUALIFIED_TRACE_PROTOCOL_FIRMWARE
            or firmware in self._settings.advanced_spectrum.qualified_firmware
        )

    @staticmethod
    def _timing_value(session: InstrumentSession, field: str):
        parameter = TIMING_PARAMETERS[field]
        return parameter.parse(field, session.query(parameter.header + "?"))

    def _synchronized_timing(self, session: InstrumentSession) -> bool:
        return self._supports_sweep_timing() and (
            self._timing_value(session, "trigger_source") != "IMM"
            or self._timing_value(session, "gate_enabled")
        )

    def _sweep_timing_snapshot(
        self, session: InstrumentSession, *, detailed: bool,
        sweep_time_auto: bool, continuous: bool,
    ) -> SpectrumTimingSnapshot:
        options = self._capabilities.hardware_options if self._capabilities else ()
        rbw_filter_switchable = bool(ANRITSU_CISPR_OPTIONS.intersection(options))
        values = {
            field: self._timing_value(session, field)
            for field in ("sweep_rule", "trigger_source", "gate_enabled")
        }
        read_fields: set[str] = set()
        if detailed:
            read_fields.update(TIMING_PARAMETERS.keys() - values.keys() - {"rbw_filter"})
        else:
            read_fields.update(TRIGGER_FIELDS.get(values["trigger_source"], ()))
            if values["gate_enabled"]:
                values["gate_source"] = self._timing_value(session, "gate_source")
                read_fields.update(GATE_FIELDS[1:])
            if values["trigger_source"] == "FRAM" or values.get("gate_source") == "FRAM":
                read_fields.update(FRAME_FIELDS)
        if rbw_filter_switchable:
            read_fields.add("rbw_filter")
        for field in sorted(read_fields):
            values[field] = self._timing_value(session, field)
        settings = SpectrumTimingSettings(**values)
        method = session.query("SWE:RUL:RTYP?").strip().upper()
        if method not in {"SWE", "FFT"}:
            raise DeviceError(f"Anritsu returned an invalid effective sweep method {method!r}.")
        width = None
        if method == "FFT":
            response = session.query("SWE:RUL:FFT:RWID?")
            if response.strip() != "***":
                width = self._parse_integer(response, "actual FFT width")
        return SpectrumTimingSnapshot(settings, method, width, sweep_time_auto, continuous, rbw_filter_switchable)

    def read_sweep_timing(self) -> SpectrumTimingSnapshot:
        """Read the effective method and editable Spectrum timing settings."""
        if not self._supports_sweep_timing():
            raise SafetyViolation("Sweep / FFT and timing readback is unverified for this Anritsu firmware.")
        session = self._require_session()
        if session.query("INST?").strip().upper() != "SPECT":
            raise SafetyViolation("Sweep / FFT and timing require Spectrum Analyzer mode.")
        snapshot = self._sweep_timing_snapshot(
            session, detailed=True,
            sweep_time_auto=self._parse_switch(session.query("SWE:TIME:AUTO?"), "sweep-time auto"),
            continuous=self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition"),
        )
        self._check_scpi_errors(session, "reading Anritsu Spectrum timing")
        return snapshot

    def _timing_context(self, session, *, sweep_time_auto: bool, continuous: bool) -> str | None:
        if self._supports_sweep_timing():
            return self._sweep_timing_snapshot(
                session, detailed=False, sweep_time_auto=sweep_time_auto, continuous=continuous,
            ).context()
        return None

    def configure_sweep_timing(self, requested: SpectrumTimingSettings) -> SpectrumTimingSnapshot:
        """Apply receiver-only changes while idle and verify settings or restore the baseline."""
        if not isinstance(requested, SpectrumTimingSettings):
            raise TypeError("Anritsu Spectrum timing requires explicit timing settings.")
        self._assert_advanced_firmware_qualified()
        self._assert_acquisition_allowed()
        if self._live:
            raise SafetyViolation("Stop Anritsu Live before changing Sweep / FFT, Trigger or Gate.")
        before = self.read_sweep_timing()
        for field, parameter in TIMING_PARAMETERS.items():
            parameter.validate(field, getattr(requested, field))
        requested.validate_sources()
        original = before.settings
        if requested.rbw_filter != original.rbw_filter and not before.rbw_filter_switchable:
            raise SafetyViolation("CISPR RBW requires a verified Anritsu 016/116 option.")
        if (requested.gate_enabled or not before.sweep_time_auto) and requested.sweep_rule != "OSW":
            raise SafetyViolation("Select Sweep only for Gate or manual sweep time.")
        if requested.fft_width_hz != original.fft_width_hz and requested.sweep_rule != "PFFT":
            raise SafetyViolation("FFT width can be changed with FFT priority.")
        if not requested.gate_enabled and any(
            getattr(original, field) != getattr(requested, field) for field in GATE_FIELDS
        ):
            raise SafetyViolation("Enable Gate to edit its source and timing.")
        options = self._capabilities.hardware_options if self._capabilities else ()
        if any(getattr(requested, field) == "SG" and getattr(original, field) != "SG"
               for field in ("trigger_source", "gate_source")) and not ANRITSU_SIGNAL_GENERATOR_OPTIONS.intersection(options):
            raise SafetyViolation("SG marker requires a verified signal generator option.")
        if requested == original:
            return before

        session = self._require_session()
        self._configuration_generation += 1
        self._cached_grid = None
        rbw_before = None
        if requested.rbw_filter != original.rbw_filter:
            rbw_before = session.query("BAND:AUTO?"), session.query("BAND?"), session.query("DET?")
        try:
            self._pause_for_timing_configuration(session)
            self._write_timing_changes(session, original, requested)
            actual = self.read_sweep_timing()
            self._verify_timing_settings(requested, actual.settings)
            return replace(actual, continuous=self._restore_timing_acquisition(session, before.continuous))
        except Exception as exc:
            try:
                self._pause_for_timing_configuration(session)
                current = self.read_sweep_timing().settings
                self._write_timing_changes(session, current, original)
                if rbw_before is not None:
                    rbw_auto, rbw_hz, detector = rbw_before
                    session.write("DET " + normalize_anritsu_detector(detector))
                    session.write("BAND:AUTO " + rbw_auto.strip())
                    if not self._parse_switch(rbw_auto, "RBW auto"):
                        session.write(f"BAND {float(rbw_hz):.12g}HZ")
                self._verify_timing_settings(original, self.read_sweep_timing().settings)
                self._restore_timing_acquisition(session, before.continuous)
            except Exception as restore_error:  # noqa: BLE001 - report restoration failure with original fault
                self._state = DeviceState.UNKNOWN
                raise DeviceError(f"{exc} Anritsu timing restoration failed: {restore_error}") from exc
            raise

    def _pause_for_timing_configuration(self, session: InstrumentSession) -> None:
        session.write("INIT:CONT OFF")
        self._stop_spectrum_acquisition(session)
        self._check_scpi_errors(session, "pausing for Spectrum timing")

    def _restore_timing_acquisition(self, session: InstrumentSession, continuous: bool) -> bool:
        if continuous:
            session.write("INIT:MODE:CONT")
        actual = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
        self._check_scpi_errors(session, "restoring Spectrum acquisition mode")
        if actual != continuous:
            raise DeviceError("Anritsu did not confirm the previous acquisition mode.")
        return actual

    def _write_timing_changes(self, session, previous: SpectrumTimingSettings, target: SpectrumTimingSettings) -> None:
        changed = {field for field in TIMING_PARAMETERS if getattr(previous, field) != getattr(target, field)}

        def write(field: str, value: object) -> None:
            session.write(TIMING_PARAMETERS[field].command(value))
            self._check_scpi_errors(session, f"setting Anritsu {field.replace('_', ' ')}")

        for field in ("rbw_filter", *FRAME_FIELDS):
            if field in changed:
                write(field, getattr(target, field))
        gate_edits = changed.intersection(GATE_FIELDS)
        gate_on = previous.gate_enabled
        if (gate_on or target.gate_enabled or gate_edits) and previous.trigger_source != "IMM":
            write("trigger_source", "IMM")
        if not gate_on and (target.gate_enabled or gate_edits):
            write("gate_enabled", True)
            gate_on = True
        if "gate_level_dbm" in gate_edits and self._timing_value(session, "gate_source") != "WIF":
            write("gate_source", "WIF")
        for field in GATE_FIELDS[1:]:
            if field in gate_edits:
                write(field, getattr(target, field))
        if target.gate_enabled and self._timing_value(session, "gate_source") != target.gate_source:
            write("gate_source", target.gate_source)
        # Trigger and Gate are mutually exclusive on the MS2830A. Gate OFF
        # also resets the effective Gate source to Free run.
        if gate_on and not target.gate_enabled:
            write("gate_enabled", False)
        for field in (name for source_fields in TRIGGER_FIELDS.values() for name in source_fields):
            if field in changed:
                write(field, getattr(target, field))
        if self._timing_value(session, "trigger_source") != target.trigger_source:
            write("trigger_source", target.trigger_source)
        if "fft_width_hz" in changed:
            if self._timing_value(session, "sweep_rule") != "PFFT":
                write("sweep_rule", "PFFT")
            write("fft_width_hz", target.fft_width_hz)
        if self._timing_value(session, "sweep_rule") != target.sweep_rule:
            write("sweep_rule", target.sweep_rule)

    @staticmethod
    def _verify_timing_settings(requested: SpectrumTimingSettings, actual: SpectrumTimingSettings) -> None:
        differences = [
            f"{field.replace('_', ' ')} requested={getattr(requested, field)!r} actual={getattr(actual, field)!r}"
            for field, parameter in TIMING_PARAMETERS.items()
            if field != "gate_source" or requested.gate_enabled
            if not parameter.matches(getattr(requested, field), getattr(actual, field))
        ]
        if differences:
            raise DeviceError("Anritsu Spectrum timing readback mismatch: " + ", ".join(differences))

    def read_current_configuration(self) -> AnritsuConfigurationSnapshot:
        """Query current settings without changing the analyser or the safety profile."""

        session = self._require_session()
        try:
            instrument_mode = session.query("INST?").strip()
            if "SPECT" not in instrument_mode.upper():
                raise DeviceError("Spectrum readback requires Spectrum Analyzer mode.")
            start_hz = float(session.query("FREQ:STAR?"))
            stop_hz = float(session.query("FREQ:STOP?"))
            reference_level_dbm = float(session.query("DISP:WIND:TRAC:Y:RLEV?"))
            points = self._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned an invalid current-configuration response.") from exc
        if not all(math.isfinite(value) for value in (start_hz, stop_hz, reference_level_dbm)):
            raise DeviceError("Anritsu returned a non-finite current-configuration value.")
        if start_hz <= 0 or stop_hz <= start_hz:
            raise DeviceError("Anritsu returned an invalid current frequency range.")
        if points not in ANRITSU_SWEEP_POINT_COUNTS:
            raise DeviceError(f"Anritsu returned unsupported sweep point count {points}.")
        return AnritsuConfigurationSnapshot(
            start_hz, stop_hz, reference_level_dbm, points, instrument_mode
        )

    def read_full_configuration(self) -> AnritsuFullConfigurationReadback:
        """Query complete front-panel parameters in a fast burst for reconciliation."""

        session = self._require_session()
        basic = self.read_current_configuration()
        try:
            mode = basic.instrument_mode
            start_hz, stop_hz = basic.start_hz, basic.stop_hz
            # Both values are determined by the validated range. Avoid two
            # redundant queries and continuing a session after their timeout.
            center_hz = (start_hz + stop_hz) / 2.0
            span_hz = stop_hz - start_hz
            reference_level_dbm, points = basic.reference_level_dbm, basic.points
            rbw_auto = self._parse_switch(session.query("BAND:AUTO?"), "RBW auto")
            rbw_hz = float(session.query("BAND?"))
            vbw_auto = self._parse_switch(session.query("BAND:VID:AUTO?"), "VBW auto")
            vbw_mode = self._read_vbw_filter_mode()
            vbw_response = session.query("BAND:VID?").strip().upper()
            vbw_hz = None if vbw_response == "OFF" else float(vbw_response)
            sweep_time_auto = self._parse_switch(
                session.query("SWE:TIME:AUTO?"), "sweep-time auto"
            )
            sweep_time_s = float(session.query("SWE:TIME?"))
            attenuation_auto = self._parse_switch(
                session.query("POW:ATT:AUTO?"), "attenuation auto"
            )
            attenuation_db = float(session.query("POW:ATT?"))
            detector = normalize_anritsu_detector(session.query("DET?"))
            continuous_sweep = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
            preamplifier_enabled = (
                self._parse_switch(session.query("POW:GAIN?"), "preamplifier")
                if self._capabilities is not None
                and ANRITSU_PREAMPLIFIER_OPTIONS.intersection(self._capabilities.hardware_options)
                else False
            )
            average_count = self._parse_integer(session.query("AVER:COUN?"), "average count")
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned invalid full configuration data.") from exc
        if not math.isfinite(rbw_hz) or rbw_hz <= 0:
            raise DeviceError("Anritsu returned a non-finite or non-positive RBW.")
        if vbw_hz is not None and (not math.isfinite(vbw_hz) or vbw_hz <= 0):
            raise DeviceError("Anritsu returned a non-finite or non-positive VBW.")
        if not math.isfinite(sweep_time_s) or sweep_time_s <= 0:
            raise DeviceError("Anritsu returned a non-finite or non-positive sweep time.")
        if not math.isfinite(attenuation_db) or attenuation_db < 0:
            raise DeviceError("Anritsu returned a non-finite or negative attenuation.")

        return AnritsuFullConfigurationReadback(
            start_hz=start_hz,
            stop_hz=stop_hz,
            center_hz=center_hz,
            span_hz=span_hz,
            reference_level_dbm=reference_level_dbm,
            points=points,
            rbw_auto=rbw_auto,
            rbw_hz=rbw_hz,
            vbw_auto=vbw_auto,
            vbw_mode=vbw_mode,
            vbw_hz=vbw_hz,
            sweep_time_auto=sweep_time_auto,
            sweep_time_s=sweep_time_s,
            attenuation_auto=attenuation_auto,
            attenuation_db=attenuation_db,
            detector=detector,
            continuous_sweep=continuous_sweep,
            average_count=average_count,
            instrument_mode=mode,
            preamplifier_enabled=preamplifier_enabled,
            timing_context=self._timing_context(session, sweep_time_auto=sweep_time_auto, continuous=continuous_sweep),
        )

    def read_acquisition_configuration(self) -> tuple[AnritsuFullConfigurationReadback, AdvancedSpectrumSnapshot]:
        """Read one fresh configuration for acquisition and its provenance.

        Advanced fields are already in the full readback; deriving the second
        view avoids another burst of identical queries, without caching state.
        """
        full = self.read_full_configuration()
        numeric = (full.rbw_hz, full.attenuation_db, full.sweep_time_s)
        if full.vbw_hz is not None:
            numeric += (full.vbw_hz,)
        if not all(math.isfinite(value) for value in numeric):
            raise DeviceError("Anritsu returned non-finite advanced Spectrum data.")
        advanced = AdvancedSpectrumSnapshot(
            rbw_auto=full.rbw_auto, rbw_hz=full.rbw_hz,
            vbw_mode="auto" if full.vbw_auto else ("off" if full.vbw_hz is None else "manual"),
            vbw_hz=full.vbw_hz, detector=full.detector,
            attenuation_auto=full.attenuation_auto, attenuation_db=full.attenuation_db,
            preamplifier_enabled=full.preamplifier_enabled,
            sweep_time_auto=full.sweep_time_auto, sweep_time_s=full.sweep_time_s,
            instrument_mode=full.instrument_mode, vbw_filter_mode=full.vbw_mode,
            timing_context=full.timing_context,
        )
        return full, advanced

    def _read_vbw_filter_mode(self) -> str:
        mode = self._require_session().query("BAND:VID:MODE?").strip().upper()
        if mode not in {"VID", "POW"}:
            raise DeviceError(f"Invalid Anritsu Video/Power readback: {mode!r}.")
        return mode

    @staticmethod
    def _parse_integer(response: str, parameter: str, *, minimum: int = 1) -> int:
        try:
            value = float(response)
        except (TypeError, ValueError) as exc:
            raise DeviceError(f"Anritsu returned invalid {parameter} {response!r}.") from exc
        if not math.isfinite(value) or not value.is_integer() or value < minimum:
            raise DeviceError(f"Anritsu returned invalid {parameter} {response!r}.")
        return int(value)

    @staticmethod
    def _parse_switch(response: str, parameter: str) -> bool:
        normalized = response.strip().upper()
        if normalized in {"1", "+1", "ON"}:
            return True
        if normalized in {"0", "+0", "OFF"}:
            return False
        raise DeviceError(f"Anritsu returned invalid {parameter} state {response!r}.")

    def read_advanced_spectrum_configuration(self) -> AdvancedSpectrumSnapshot:
        """Query advanced Spectrum settings without modifying the instrument."""

        session = self._require_session()
        mode = session.query("INST?").strip()
        if "SPECT" not in mode.upper():
            raise DeviceError(
                f"Advanced Spectrum readback requires Spectrum Analyzer mode; current mode is {mode!r}."
            )
        try:
            rbw_auto = self._parse_switch(session.query("BAND:AUTO?"), "RBW auto")
            rbw_hz = float(session.query("BAND?"))
            vbw_auto = self._parse_switch(session.query("BAND:VID:AUTO?"), "VBW auto")
            vbw_response = session.query("BAND:VID?").strip().upper()
            vbw_hz = None if vbw_response == "OFF" else float(vbw_response)
            detector = normalize_anritsu_detector(session.query("DET?"))
            attenuation_auto = self._parse_switch(
                session.query("POW:ATT:AUTO?"), "attenuation auto"
            )
            attenuation_db = float(session.query("POW:ATT?"))
            preamplifier_enabled = (
                self._parse_switch(session.query("POW:GAIN?"), "preamplifier")
                if self._capabilities is not None
                and ANRITSU_PREAMPLIFIER_OPTIONS.intersection(
                    self._capabilities.hardware_options
                )
                else False
            )
            sweep_time_auto = self._parse_switch(
                session.query("SWE:TIME:AUTO?"), "sweep-time auto"
            )
            sweep_time_s = float(session.query("SWE:TIME?"))
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned invalid advanced Spectrum data.") from exc
        numeric = (rbw_hz, attenuation_db, sweep_time_s)
        if vbw_hz is not None:
            numeric += (vbw_hz,)
        if not all(math.isfinite(value) for value in numeric):
            raise DeviceError("Anritsu returned non-finite advanced Spectrum data.")
        if rbw_hz <= 0 or (vbw_hz is not None and vbw_hz <= 0) or sweep_time_s <= 0 or attenuation_db < 0:
            raise DeviceError("Anritsu returned out-of-range advanced Spectrum data.")
        vbw_mode = "auto" if vbw_auto else ("off" if vbw_hz is None else "manual")
        return AdvancedSpectrumSnapshot(
            rbw_auto=rbw_auto,
            rbw_hz=rbw_hz,
            vbw_mode=vbw_mode,
            vbw_hz=vbw_hz,
            detector=detector,
            attenuation_auto=attenuation_auto,
            attenuation_db=attenuation_db,
            preamplifier_enabled=preamplifier_enabled,
            sweep_time_auto=sweep_time_auto,
            sweep_time_s=sweep_time_s,
            instrument_mode=mode,
            vbw_filter_mode=self._read_vbw_filter_mode(),
            timing_context=self._timing_context(session, sweep_time_auto=sweep_time_auto, continuous=False),
        )

    def _assert_advanced_firmware_qualified(self) -> None:
        protocol = self._settings.advanced_spectrum
        if protocol.control_protocol != "standard_scpi":
            raise SafetyViolation(
                "Anritsu advanced Spectrum Analyzer control is unverified for this firmware."
            )
        firmware = self._identity.firmware.strip() if self._identity is not None else ""
        if firmware not in protocol.qualified_firmware:
            raise SafetyViolation(
                f"Anritsu firmware {firmware or 'unknown'!r} is not in the qualified advanced-control list."
            )

    def configure_advanced_spectrum(
        self, config: AdvancedSpectrumConfig
    ) -> AdvancedSpectrumSnapshot:
        """Apply qualified input-path/bandwidth controls and verify every readback."""

        self._assert_advanced_firmware_qualified()
        requested = config
        if requested.vbw_filter_mode not in {None, "VID", "POW"}:
            raise SafetyViolation("VBW filter mode must be VID or POW.")
        before = self.read_advanced_spectrum_configuration()
        for value, mode, current_mode in (
            (requested.rbw_hz, requested.rbw_auto, before.rbw_auto),
            (requested.attenuation_db, requested.attenuation_auto, before.attenuation_auto),
            (requested.sweep_time_s, requested.sweep_time_auto, before.sweep_time_auto),
        ):
            if value is not None and mode is None and current_mode:
                raise SafetyViolation("A manual value requires an explicit manual-mode selection or confirmed manual baseline.")
        if requested.vbw_hz is not None and requested.vbw_mode is None and before.vbw_mode != "manual":
            raise SafetyViolation("A manual VBW requires an explicit manual-mode selection or confirmed manual baseline.")
        config = AdvancedSpectrumConfig(**{
            item.name: getattr(requested, item.name) if getattr(requested, item.name) is not None else getattr(before, item.name)
            for item in fields(requested)
        })
        options = self._capabilities.hardware_options if self._capabilities is not None else ()
        validate_anritsu_advanced_spectrum(
            self._settings,
            rbw_auto=config.rbw_auto,
            rbw_hz=config.rbw_hz,
            vbw_mode=config.vbw_mode,
            vbw_hz=config.vbw_hz,
            detector=config.detector,
            attenuation_auto=config.attenuation_auto,
            attenuation_db=config.attenuation_db,
            preamplifier_enabled=config.preamplifier_enabled,
            sweep_time_auto=config.sweep_time_auto,
            sweep_time_s=config.sweep_time_s,
            hardware_options=options,
        )
        self._assert_advanced_firmware_qualified()
        session = self._require_session()
        changed = set()
        for item in fields(requested):
            target = getattr(requested, item.name)
            current = getattr(before, item.name)
            if target is None:
                continue
            if item.name == "detector":
                target, current = normalize_anritsu_detector(target), normalize_anritsu_detector(current)
            same = math.isclose(target, current, rel_tol=1e-12, abs_tol=1e-12) if type(target) is float and type(current) is float else target == current
            if not same:
                changed.add(item.name)
        for mode, value in (("rbw_auto", "rbw_hz"), ("vbw_mode", "vbw_hz"),
                            ("attenuation_auto", "attenuation_db"), ("sweep_time_auto", "sweep_time_s")):
            if mode in changed and getattr(requested, value) is not None:
                changed.add(value)
        has_preamp = bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        detector = normalize_anritsu_detector(config.detector)
        vbw_mode = config.vbw_mode.strip().lower()
        if before.preamplifier_enabled and {"attenuation_auto", "attenuation_db"} & changed and config.preamplifier_enabled:
            raise SafetyViolation("Disable the Anritsu preamplifier explicitly before changing input attenuation.")
        if changed:
            self._configuration_generation += 1
            self._cached_grid = None
        try:
            if has_preamp and "preamplifier_enabled" in changed and config.preamplifier_enabled is False:
                session.write("POW:GAIN OFF")
            if "attenuation_auto" in changed:
                session.write("POW:ATT:AUTO ON" if config.attenuation_auto else "POW:ATT:AUTO OFF")
            if "attenuation_db" in changed:
                session.write(f"POW:ATT {config.attenuation_db:.12g}DB")
            if "detector" in changed:
                session.write(f"DET {detector}")
            if "rbw_auto" in changed:
                session.write("BAND:AUTO ON" if config.rbw_auto else "BAND:AUTO OFF")
            if "rbw_hz" in changed:
                session.write(f"BAND {config.rbw_hz:.12g}HZ")
            if "vbw_mode" in changed:
                session.write("BAND:VID:AUTO ON" if vbw_mode == "auto" else "BAND:VID:AUTO OFF")
                if vbw_mode == "off":
                    session.write("BAND:VID OFF")
            if "vbw_filter_mode" in changed:
                session.write(f"BAND:VID:MODE {requested.vbw_filter_mode}")
            if "vbw_hz" in changed:
                session.write(f"BAND:VID {config.vbw_hz:.12g}HZ")
            if "sweep_time_auto" in changed:
                session.write("SWE:TIME:AUTO ON" if config.sweep_time_auto else "SWE:TIME:AUTO OFF")
            if "sweep_time_s" in changed:
                session.write(f"SWE:TIME {config.sweep_time_s:.12g}S")
            if has_preamp and "preamplifier_enabled" in changed and config.preamplifier_enabled is True:
                session.write("POW:GAIN ON")
            actual = self.read_advanced_spectrum_configuration()
            self._verify_advanced_spectrum_readback(config, actual)
            return actual
        except Exception as exc:
            # Conservative input-path fallback. Do not hide the original fault.
            try:
                if requested.attenuation_auto is not None or requested.attenuation_db is not None or requested.preamplifier_enabled is not None:
                    if has_preamp:
                        session.write("POW:GAIN OFF")
                    session.write("POW:ATT:AUTO OFF")
                    session.write("POW:ATT 60DB")
            except (DeviceError, ConnectionError, OSError, RuntimeError) as fallback_error:
                exc.add_note(f"Anritsu conservative input-path fallback failed: {fallback_error}")
            self._state = DeviceState.UNKNOWN
            raise

    @staticmethod
    def _verify_advanced_spectrum_readback(
        requested: AdvancedSpectrumConfig, actual: AdvancedSpectrumSnapshot
    ) -> None:
        mismatches: list[str] = []
        if requested.vbw_filter_mode is not None and actual.vbw_filter_mode != requested.vbw_filter_mode:
            mismatches.append("VBW Video/Power mode")
        if actual.rbw_auto != requested.rbw_auto:
            mismatches.append("RBW auto state")
        if not requested.rbw_auto and not math.isclose(
            actual.rbw_hz, float(requested.rbw_hz), rel_tol=1e-6, abs_tol=1.0
        ):
            mismatches.append("RBW")
        requested_vbw_mode = requested.vbw_mode.strip().lower()
        if actual.vbw_mode != requested_vbw_mode:
            mismatches.append("VBW mode")
        if requested_vbw_mode == "manual" and (
            actual.vbw_hz is None
            or not math.isclose(
                actual.vbw_hz,
                float(requested.vbw_hz),
                rel_tol=1e-6,
                abs_tol=1.0,
            )
        ):
            mismatches.append("VBW")
        if normalize_anritsu_detector(actual.detector) != normalize_anritsu_detector(requested.detector):
            mismatches.append("detector")
        if actual.attenuation_auto != requested.attenuation_auto:
            mismatches.append("attenuation auto state")
        if not requested.attenuation_auto and not math.isclose(
            actual.attenuation_db,
            float(requested.attenuation_db),
            rel_tol=0.0,
            abs_tol=0.01,
        ):
            mismatches.append("attenuation")
        if actual.preamplifier_enabled != requested.preamplifier_enabled:
            mismatches.append("preamplifier")
        if actual.sweep_time_auto != requested.sweep_time_auto:
            mismatches.append("sweep-time auto state")
        if not requested.sweep_time_auto and not math.isclose(
            actual.sweep_time_s,
            float(requested.sweep_time_s),
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            mismatches.append("sweep time")
        if mismatches:
            raise DeviceError(
                "Anritsu advanced configuration readback mismatch: " + ", ".join(mismatches)
            )

    def configure_spectrum(
        self,
        config: SpectrumConfig,
        *,
        require_rf_off_proof: bool = False,
    ) -> AnritsuConfigurationSnapshot:
        validate_anritsu_trace_name(config.trace)
        if config.vbw_mode not in {None, "VID", "POW"}:
            raise SafetyViolation("VBW filter mode must be VID or POW.")
        validate_anritsu_spectrum(
            self._settings.safety,
            start_hz=config.start_hz,
            stop_hz=config.stop_hz,
            reference_level_dbm=config.reference_level_dbm,
            points=config.points,
        )
        fields = config.changed_fields
        writes_bandwidth = fields is None and any(
            value is not None
            for value in (config.rbw_auto, config.rbw_hz, config.vbw_auto, config.vbw_hz)
        )
        if writes_bandwidth:
            self._assert_advanced_firmware_qualified()
            if config.rbw_hz is not None and config.rbw_auto is not False:
                raise SafetyViolation("Manual Anritsu RBW requires rbw_auto=False.")
            if config.vbw_hz is not None and config.vbw_auto is not False:
                raise SafetyViolation("Manual Anritsu VBW requires vbw_auto=False.")
            vbw_mode_str = (
                "auto"
                if config.vbw_auto is True
                else (
                    "manual"
                    if config.vbw_auto is False and config.vbw_hz is not None
                    else ("off" if config.vbw_auto is False else None)
                )
            )
            options = self._capabilities.hardware_options if self._capabilities is not None else ()
            validate_anritsu_advanced_spectrum(
                self._settings,
                rbw_auto=config.rbw_auto,
                rbw_hz=config.rbw_hz,
                vbw_mode=vbw_mode_str,
                vbw_hz=config.vbw_hz,
                detector=None,
                attenuation_auto=None,
                attenuation_db=None,
                preamplifier_enabled=False,
                sweep_time_auto=None,
                sweep_time_s=None,
                hardware_options=options,
            )
        session = self._require_session()
        if fields is not None:
            allowed = {"start_hz", "stop_hz", "reference_level_dbm", "points", "vbw_mode"}
            if not fields or len(fields) != len(set(fields)) or set(fields) - allowed:
                raise SafetyViolation("Spectrum changes require explicit distinct supported fields.")
        if require_rf_off_proof and not config.is_complete_configuration:
            raise SafetyViolation(
                "RF OFF mode-transition proof requires a complete Spectrum Analyzer configuration."
            )
        if require_rf_off_proof:
            self._assert_signal_generator_supported()

        rf_off_confirmed = False
        before: AnritsuConfigurationSnapshot | None = None
        needs_mode_transition = fields is None or (
            config.is_complete_configuration
            and (
                require_rf_off_proof
                or self._sg_control_owned
                or self._sg_output_enabled
                or self._last_sg_config is not None
            )
        )
        if fields is not None and not needs_mode_transition:
            if config.is_complete_configuration:
                current_mode = session.query("INST?").strip().upper()
                if "SPECT" not in current_mode:
                    needs_mode_transition = True
            if not needs_mode_transition:
                before = self.read_current_configuration()
                if "SPECT" not in before.instrument_mode.upper():
                    raise DeviceError("Spectrum parameter changes require confirmed Spectrum Analyzer mode.")
                for name in {"start_hz", "stop_hz", "reference_level_dbm", "points"} - set(fields):
                    if not math.isclose(
                        float(getattr(before, name)),
                        float(getattr(config, name)),
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    ):
                        raise DeviceError(
                            f"Anritsu unselected {name} differs from the planned baseline; re-read and recompile."
                        )

        try:
            if needs_mode_transition:
                rf_off_confirmed = self._enter_spectrum_mode_with_rf_off(
                    require_rf_off_proof=require_rf_off_proof
                )
            if fields is not None:
                if before is None:
                    before = self.read_current_configuration()
                    if "SPECT" not in before.instrument_mode.upper():
                        raise DeviceError(
                            "Spectrum parameter changes require confirmed Spectrum Analyzer mode."
                        )
                    for name in {"start_hz", "stop_hz", "reference_level_dbm", "points"} - set(fields):
                        if not math.isclose(
                            float(getattr(before, name)),
                            float(getattr(config, name)),
                            rel_tol=1e-9,
                            abs_tol=1e-9,
                        ):
                            raise DeviceError(
                                f"Anritsu unselected {name} differs from the planned baseline; re-read and recompile."
                            )
                before_stop_hz = before.stop_hz
                changed = []
                for name in fields:
                    if name == "vbw_mode":
                        same = self._read_vbw_filter_mode() == config.vbw_mode
                    elif name == "points":
                        same = before.points == config.points
                    else:
                        same = math.isclose(
                            float(getattr(before, name)),
                            float(getattr(config, name)),
                            rel_tol=1e-12,
                            abs_tol=1e-9,
                        )
                    if not same:
                        changed.append(name)
                fields = tuple(changed)
            else:
                try:
                    before_stop_hz = float(session.query("FREQ:STOP?"))
                except (TypeError, ValueError) as exc:
                    raise DeviceError(
                        "Anritsu returned an invalid current stop frequency before configuration."
                    ) from exc
                if not math.isfinite(before_stop_hz):
                    raise DeviceError(
                        "Anritsu returned a non-finite current stop frequency before configuration."
                    )
            if fields is None or fields:
                self._configuration_generation += 1
                self._cached_grid = None
            frequency_fields = ("start_hz", "stop_hz")
            if config.start_hz >= before_stop_hz:
                # Move the upper endpoint first when shifting the entire window
                # upwards, so the analyser cannot clamp an invalid interim range.
                frequency_fields = ("stop_hz", "start_hz")
            for name in frequency_fields:
                if fields is None or name in fields:
                    header = "FREQ:STAR" if name == "start_hz" else "FREQ:STOP"
                    session.write(f"{header} {getattr(config, name):.12g}HZ")
            if fields is None or "reference_level_dbm" in fields:
                session.write(f"DISP:WIND:TRAC:Y:RLEV {config.reference_level_dbm:.12g}")
            if fields is None or "points" in fields:
                session.write(f"SWE:POIN {config.points}")
            if fields is None and config.rbw_auto is True:
                session.write("BAND:AUTO ON")
            elif fields is None and config.rbw_auto is False and config.rbw_hz is not None:
                session.write("BAND:AUTO OFF")
                session.write(f"BAND {config.rbw_hz:.12g}HZ")
            if (fields is None or "vbw_mode" in fields) and config.vbw_mode in {"VID", "POW"}:
                session.write(f"BAND:VID:MODE {config.vbw_mode}")
            if fields is None and config.vbw_auto is True:
                session.write("BAND:VID:AUTO ON")
            elif fields is None and config.vbw_auto is False and config.vbw_hz is not None:
                session.write("BAND:VID:AUTO OFF")
                session.write(f"BAND:VID {config.vbw_hz:.12g}HZ")
            elif fields is None and config.vbw_auto is False:
                session.write("BAND:VID:AUTO OFF")
                session.write("BAND:VID OFF")
            if writes_bandwidth:
                self._check_scpi_errors(session, "configuring Anritsu spectrum bandwidth")
            # TRAC? TRAC1 reads Trace A.  In VIEW mode that buffer is documented
            # to remain unchanged even while the analyser continues measuring.
            # An explicit Apply action therefore restores Trace A to WRITE so the
            # next passive current-buffer read can actually contain a new frame.
            if fields is None and config.prepare_current_buffer:
                self._prepare_trace_a()
            # A range/point-count change invalidates the current TRAC1 buffer.  If
            # an earlier recipe or front-panel action left the analyser in Single,
            # passive reads would then return -999 forever.  Restore the normal
            # free-running Spectrum mode once per explicit Apply action.  Manual
            # Read and each Live timer tick remain pure current-buffer reads.
            if fields is None and config.prepare_current_buffer:
                session.write("INIT:MODE:CONT")
            if fields is None or fields:
                self._check_scpi_errors(session, "configuring Anritsu spectrum")
            actual = self.read_current_configuration()
            mismatches: list[str] = []
            if (
                config.vbw_mode is not None
                and (config.changed_fields is None or "vbw_mode" in config.changed_fields)
                and self._read_vbw_filter_mode() != config.vbw_mode
            ):
                mismatches.append("VBW Video/Power mode")
            if writes_bandwidth:
                if config.rbw_auto is not None:
                    actual_rbw_auto = self._parse_switch(session.query("BAND:AUTO?"), "RBW auto")
                    if actual_rbw_auto != config.rbw_auto:
                        mismatches.append("RBW auto state")
                    if config.rbw_auto is False:
                        try:
                            actual_rbw_hz = float(session.query("BAND?"))
                        except (TypeError, ValueError) as exc:
                            raise DeviceError("Anritsu returned invalid RBW readback.") from exc
                        if not math.isfinite(actual_rbw_hz) or not math.isclose(
                            actual_rbw_hz,
                            float(config.rbw_hz),
                            rel_tol=1e-6,
                            abs_tol=1.0,
                        ):
                            mismatches.append("RBW")
                if config.vbw_auto is not None:
                    actual_vbw_auto = self._parse_switch(
                        session.query("BAND:VID:AUTO?"), "VBW auto"
                    )
                    if actual_vbw_auto != config.vbw_auto:
                        mismatches.append("VBW auto state")
                    if config.vbw_auto is False:
                        vbw_response = session.query("BAND:VID?").strip().upper()
                        if config.vbw_hz is None:
                            if vbw_response != "OFF":
                                mismatches.append("VBW")
                        else:
                            if vbw_response == "OFF":
                                mismatches.append("VBW")
                            else:
                                try:
                                    actual_vbw_hz = float(vbw_response)
                                except (TypeError, ValueError) as exc:
                                    raise DeviceError(
                                        "Anritsu returned invalid VBW readback."
                                    ) from exc
                                if not math.isfinite(actual_vbw_hz) or not math.isclose(
                                    actual_vbw_hz,
                                    float(config.vbw_hz),
                                    rel_tol=1e-6,
                                    abs_tol=1.0,
                                ):
                                    mismatches.append("VBW")
            if not math.isclose(actual.start_hz, config.start_hz, rel_tol=0.0, abs_tol=1.0):
                mismatches.append(f"start requested={config.start_hz:g} Hz actual={actual.start_hz:g} Hz")
            if not math.isclose(actual.stop_hz, config.stop_hz, rel_tol=0.0, abs_tol=1.0):
                mismatches.append(f"stop requested={config.stop_hz:g} Hz actual={actual.stop_hz:g} Hz")
            if not math.isclose(
                actual.reference_level_dbm,
                config.reference_level_dbm,
                rel_tol=0.0,
                abs_tol=0.01,
            ):
                mismatches.append(
                    "reference level requested="
                    f"{config.reference_level_dbm:g} dBm actual={actual.reference_level_dbm:g} dBm"
                )
            if actual.points != config.points:
                mismatches.append(f"points requested={config.points} actual={actual.points}")
            if mismatches:
                raise DeviceError("Anritsu configuration readback mismatch: " + "; ".join(mismatches))
            return replace(actual, rf_off_confirmed=rf_off_confirmed)
        except Exception:
            self._state = DeviceState.UNKNOWN
            raise

    def _ensure_acquisition_remote(self) -> None:
        # GPIB REN is transport control, not an SCPI header or a sweep mode.
        # Do this once when measurement starts, never during discovery/identity.
        if self._settings.connection.resource.upper().startswith("GPIB") and not self._remote_entered:
            self._require_session().ensure_remote()
            self._remote_entered = True

    @staticmethod
    def _check_scpi_errors(
        session: InstrumentSession, context: str, *, first_response: str | None = None
    ) -> None:
        """Preserve transient front-panel errors in the caller's fault/log."""
        errors = []
        for index in range(16):
            response = (
                first_response if index == 0 and first_response is not None else session.query("SYST:ERR?")
            ).strip()
            try:
                code = int(response.split(",", 1)[0])
            except ValueError as exc:
                raise DeviceError(f"Anritsu invalid SYST:ERR? during {context}: {response!r}.") from exc
            if code == 0:
                if errors:
                    raise DeviceError(f"Anritsu SCPI error during {context}: " + "; ".join(errors))
                return
            errors.append(response)
        raise DeviceError(f"Anritsu error queue did not empty during {context}: " + "; ".join(errors))

    def start_live(
        self, ensure_continuous: bool = False, *, allow_synchronized: bool = False,
    ) -> AnritsuConfigurationSnapshot:
        """Start Continuous reads or prepare Live polling of completed single sweeps."""

        if not ensure_continuous or not self._live:
            self.stop_live()
        self._group_live_queries = False
        try:
            snapshot = self.read_current_configuration()
            synchronized = ensure_continuous and self._synchronized_timing(self._require_session())
            if synchronized and not (allow_synchronized and self._single_sweep_supported):
                raise SafetyViolation(
                    "Active Trigger or Gate requires a completed Single sweep. "
                    "Choose Single sweep or Anritsu instrument averaging for this acquisition."
                )
            if ensure_continuous:
                if synchronized or (
                    self._settings.safety.acquisition_allowed
                    and self._supports_qualified_trace_protocol()
                ):
                    self._prepare_unaveraged_live(synchronized=synchronized)
                else:
                    self._prepare_passive_continuous_live()
        except Exception:
            self.stop_live()
            raise
        self._live = True
        self._passive_live = not ensure_continuous
        self._live_synchronized = synchronized
        return replace(snapshot, synchronized=synchronized)

    def _prepare_passive_continuous_live(self) -> None:
        """Enable free-running Trace A updates without mutating trace storage modes."""
        session = self._require_session()
        self._ensure_acquisition_remote()
        self._check_scpi_errors(session, "before Live preparation")
        self._prepare_trace_a()
        continuous = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
        if not continuous:
            session.write("INIT:MODE:CONT")
        self._check_scpi_errors(session, "Live preparation")

    def _prepare_unaveraged_live(self, *, synchronized: bool = False) -> None:
        self._assert_acquisition_allowed()
        session = self._require_session()
        self._ensure_acquisition_remote()
        self._check_scpi_errors(session, "before Live preparation")
        # TRAC:TYPE? can time out in some measurement applications. Prepare
        # WRIT using the qualified command, just as for triggered acquisitions.
        native = self._prepare_trace_a() == "NAT"
        continuous = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
        if synchronized and any(mode != "OFF" for mode in self._read_trace_storage_modes(session, native)[1:]):
            raise SafetyViolation("Set Trace B–F storage to Off for triggered or gated Live acquisition.")
        current_mode = session.query(self._trace_storage_query(1, native)).strip().upper()
        if current_mode not in _TRACE_STORAGE_MODES:
            raise DeviceError(f"Anritsu returned invalid Trace A storage mode {current_mode!r}.")
        storage_changed = False
        if self._live_storage_before is None:
            self._live_storage_before = native, current_mode
            if current_mode != "OFF":
                storage_changed = True
                session.write(self._trace_storage_command(1, "OFF", native))
                self._check_scpi_errors(session, "disabling Trace A storage for Live")
                current_mode = session.query(self._trace_storage_query(1, native)).strip().upper()
        self._assert_live_storage_off(current_mode)
        if synchronized:
            self._pause_for_timing_configuration(session)
        elif not continuous or storage_changed:
            session.write("INIT:MODE:CONT")
        self._check_scpi_errors(session, "Live preparation")
        if self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition") == synchronized:
            raise DeviceError("Anritsu did not confirm the requested Live acquisition mode.")
        self._group_live_queries = native and self._supports_grouped_native_queries()

    def _supports_grouped_native_queries(self) -> bool:
        identity = self._identity
        return (
            identity is not None
            and identity.model.upper() == "MS2830A"
            and identity.firmware.strip() == "7.03.00"
            and identity.resource.upper().startswith(("GPIB", "TCPIP", "USB"))
        )

    def stop_live(self) -> None:
        """Restore the trace storage setting while leaving front-panel acquisition active."""

        self._live = False
        self._passive_live = False
        self._group_live_queries = False
        self._live_synchronized = False
        try:
            self._cancel_synchronized_live_sweep()
        finally:
            if self._live_storage_before is not None:
                native, previous = self._live_storage_before
                if previous != "OFF":
                    self._restore_trace_storage_modes(self._require_session(), native, [(1, previous)])
                self._live_storage_before = None

    def _cancel_synchronized_live_sweep(self) -> None:
        if self._pending_synchronized_sweep is not None:
            self._pending_synchronized_sweep = None
            try:
                session = self._require_session()
                self._pause_for_timing_configuration(session)
                self._restore_timing_acquisition(session, continuous=False)
            except Exception:
                self._state = DeviceState.UNKNOWN
                raise

    def poll_synchronized_live(self, trace: str = "TRAC1") -> SpectrumTrace | None:
        """Advance one triggered/gated Live sweep without queuing a blocking VISA barrier."""
        trace = validate_anritsu_trace_name(trace)
        if not self._live_synchronized:
            raise SafetyViolation("Start triggered or gated Live before polling completed sweeps.")
        session = self._require_session()
        try:
            if self._pending_synchronized_sweep is None:
                timeout = parse_quantity(self._settings.acquisition.operation_complete_timeout, DIMENSION_TIME).si_value
                self._pending_synchronized_sweep = datetime.now(UTC), time.monotonic() + timeout
                self.start_single_sweep(queue_wait=False)
                return None
            started, deadline = self._pending_synchronized_sweep
            if time.monotonic() >= deadline:
                raise DeviceError("Anritsu timed out waiting for a Trigger/Gate sweep. Check the selected source.")
            previous_timeout = session.timeout
            try:
                session.timeout = min(previous_timeout, _SWEEP_STATUS_TIMEOUT_MS)
                status = self._parse_switch(session.query("INIT:SWP?"), "pending sweep")
            finally:
                session.timeout = previous_timeout
            if status:
                return None
            completed = datetime.now(UTC)
            result = self.fetch_current_trace_fast(trace)
            self._pending_synchronized_sweep = None
            return self._stamp_single_sweep(result, started, completed)
        except Exception as exc:
            try:
                self.stop_live()
            except Exception as stop_error:  # noqa: BLE001 - preserve original Trigger/Gate failure
                exc.add_note(f"Anritsu Trigger/Gate stop failed: {stop_error}")
            raise

    @property
    def live(self) -> bool:
        return self._live

    def start_single_sweep(
        self,
        *,
        queue_wait: bool = True,
        _prechecked_language: str | None = None,
    ) -> None:
        """Start one qualified Single measurement, optionally queuing a SCPI barrier."""

        self._assert_acquisition_allowed()
        if not self._single_sweep_supported:
            raise SafetyViolation(
                "The recipe requires the qualified Anritsu single-sweep protocol; "
                "the current profile permits Live/Fetch only."
            )
        session = self._require_session()
        if _prechecked_language is None:
            if "SPECT" not in session.query("INST?").strip().upper():
                raise SafetyViolation(
                    "Single spectrum acquisition requires an explicit Spectrum Analyzer configuration first."
                )
            self._ensure_acquisition_remote()
            self._check_scpi_errors(session, "before single-sweep preparation")
        # A fresh acquisition must update Trace A, including when the previous
        # display mode was VIEW/hold. Use the same trace-specific preparation
        # as Live; TRAC:TYPE? times out on the target MS2830A and must not be
        # probed (or retried after a timeout). This prepares the acquisition
        # buffer without changing the operator's RF/bandwidth settings.
        self._prepare_trace_a(language=_prechecked_language)
        # MS2830A Spectrum Analyzer Remote Control, section 2.7 documents this
        # command for Single measurement. *WAI holds the following command,
        # including status queries, until the measurement is complete.
        session.write("INIT:MODE:SING")
        if queue_wait:
            session.write("*WAI")
        # DeviceState represents the connection/output safety state, not the
        # transient acquisition state; the analyser has no energy output here.
        self._state = DeviceState.VERIFIED

    def wait_complete(self, *, deadline_s: float | None = None, poll_after_s: float | None = None) -> None:
        """Confirm completion within the deadline using the selected synchronization path."""

        if not self._single_sweep_supported:
            raise SafetyViolation("No qualified Anritsu single-sweep protocol is configured.")
        timeout = deadline_s
        if timeout is None:
            timeout = parse_quantity(self._settings.acquisition.operation_complete_timeout, DIMENSION_TIME).si_value
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise SafetyViolation("Anritsu acquisition deadline must be finite and positive.")
        session = self._require_session()
        deadline = time.monotonic() + timeout
        original_timeout_ms = session.timeout
        try:
            if poll_after_s is not None:
                # During a Live average, leave GPIB idle while the analyzer accumulates.
                # INIT:SWP? can then report busy without waiting behind a queued *WAI.
                time.sleep(min(poll_after_s, max(0.0, timeout - 0.1)))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("Timed out waiting for the Anritsu single sweep to complete.")
                # The VISA call itself must not outlive the application-level
                # deadline. Some backends reject a zero-millisecond timeout.
                timeout_ms = max(1, int(remaining * 1000))
                if poll_after_s is not None:
                    timeout_ms = min(timeout_ms, original_timeout_ms, _SWEEP_STATUS_TIMEOUT_MS)
                session.timeout = timeout_ms
                response = session.query("INIT:SWP?").strip()
                if response in {"0", "+0"}:
                    self._check_scpi_errors(session, "confirming Single measurement completion")
                    self._state = DeviceState.VERIFIED
                    return
                if response not in {"1", "+1"}:
                    raise DeviceError(
                        f"Anritsu returned invalid INIT:SWP? response {response!r}."
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except Exception as exc:
            try:
                session.timeout = original_timeout_ms
                self.abort_acquisition()
            except Exception as abort_error:  # noqa: BLE001 - preserve original wait failure
                self._state = DeviceState.UNKNOWN
                exc.add_note(f"Anritsu acquisition abort also failed: {abort_error}")
            raise
        finally:
            session.timeout = original_timeout_ms

    def acquire_single_sweep(
        self,
        trace: str = "TRAC1",
        *,
        timeout_s: float | None = None,
        restore_continuous: bool = True,
        binary: bool = False,
        _prechecked_language: str | None = None,
    ) -> SpectrumTrace:
        """Synchronise one trace; recipes keep Single between checkpoints."""

        trace = validate_anritsu_trace_name(trace)
        if timeout_s is not None and (
            type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0
        ):
            raise SafetyViolation("Anritsu acquisition timeout must be finite and positive.")
        if type(restore_continuous) is not bool:
            raise SafetyViolation("Anritsu restore_continuous must be a boolean.")
        if type(binary) is not bool:
            raise SafetyViolation("Anritsu binary transfer flag must be a boolean.")
        if binary and not self._supports_qualified_trace_protocol():
            raise SafetyViolation("Binary single-sweep transfer is unverified for this Anritsu firmware.")
        started = datetime.now(UTC)
        session = self._require_session()
        continuous_before = (
            self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
            if restore_continuous else False
        )
        self._cancel_synchronized_live_sweep()
        synchronized = self._synchronized_timing(session)
        self.start_single_sweep(
            queue_wait=not synchronized,
            _prechecked_language=_prechecked_language,
        )
        self.wait_complete(deadline_s=timeout_s, poll_after_s=0.0 if synchronized else None)
        completed = datetime.now(UTC)
        result = self.fetch_current_trace_fast(trace) if binary else self.fetch_trace(trace)
        if continuous_before:
            session.write("INIT:MODE:CONT")
            if not self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition"):
                raise DeviceError("Anritsu did not restore the confirmed Continuous acquisition mode.")
            self._check_scpi_errors(session, "restoring Continuous acquisition")
        return self._stamp_single_sweep(result, started, completed)

    def _stamp_single_sweep(self, trace: SpectrumTrace, started: datetime, completed: datetime) -> SpectrumTrace:
        self._acquisition_sequence += 1
        return replace(
            trace,
            sweep_evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
            sweep_id=str(self._acquisition_sequence),
            acquisition_started_at_utc=started,
            acquisition_completed_at_utc=completed,
            configuration_generation=self._configuration_generation,
        )

    def fetch_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read one trace for a validated recipe/single-sweep workflow."""

        trace = validate_anritsu_trace_name(trace)
        self._assert_acquisition_allowed()
        session = self._require_session()
        return self._read_ascii_trace(
            session,
            trace,
            prepare_ascii=True,
            configuration_generation=self._configuration_generation,
            check_vertical_scale=self._supports_qualified_trace_protocol(),
        )

    def acquire_unaveraged_sweep(
        self,
        trace: str = "TRAC1",
        *,
        restore_continuous: bool = False,
        binary: bool = False,
    ) -> SpectrumTrace:
        """Acquire one sweep with instrument trace storage temporarily off."""

        trace = validate_anritsu_trace_name(trace)
        if type(restore_continuous) is not bool:
            raise SafetyViolation("Anritsu restore_continuous must be a boolean.")
        if type(binary) is not bool:
            raise SafetyViolation("Anritsu binary transfer flag must be a boolean.")
        self._assert_acquisition_allowed()
        if not self._single_sweep_supported:
            raise SafetyViolation("Application averaging requires the qualified Anritsu single-sweep protocol.")
        if binary and not self._supports_qualified_trace_protocol():
            raise SafetyViolation("Binary unaveraged sweep transfer is unverified for this Anritsu firmware.")
        session = self._require_session()
        if session.query("INST?").strip().upper() != "SPECT":
            raise SafetyViolation("Application averaging requires Spectrum Analyzer mode.")
        self._ensure_acquisition_remote()
        self._check_scpi_errors(session, "before application averaging")
        language = self._read_remote_language(session)
        native = language == "NAT"
        continuous_before = (
            self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
            if restore_continuous else False
        )
        previous = self._read_trace_storage_modes(session, native)
        changed: list[tuple[int, str]] = []
        failure: Exception | None = None
        try:
            # Even when fetching Trace A, another trace's storage can keep
            # INIT:MODE:SING sweeping until its accumulation completes.
            for index, mode in enumerate(previous, start=1):
                if mode != "OFF":
                    changed.append((index, mode))
                    session.write(self._trace_storage_command(index, "OFF", native))
            if changed:
                self._check_scpi_errors(session, "disabling trace storage")
                if any(mode != "OFF" for mode in self._read_trace_storage_modes(session, native)):
                    raise DeviceError("Anritsu did not confirm trace storage OFF before starting the sweep.")
            result = self.acquire_single_sweep(
                trace,
                restore_continuous=False,
                binary=binary,
                _prechecked_language=language,
            )
            if any(mode != "OFF" for mode in self._read_trace_storage_modes(session, native)):
                raise DeviceError("Anritsu trace storage mode changed during acquisition.")
        except Exception as exc:
            failure = exc
            raise
        finally:
            if changed:
                try:
                    self._restore_trace_storage_modes(session, native, changed)
                except Exception as restore_error:  # retain original acquisition fault
                    if failure is None:
                        raise
                    failure.add_note(f"Anritsu trace storage restoration also failed: {restore_error}")
                    if isinstance(failure, DeviceError):
                        raise DeviceError(f"{failure} {restore_error}") from failure
        if continuous_before:
            session.write("INIT:MODE:CONT")
            if not self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition"):
                self._state = DeviceState.UNKNOWN
                raise DeviceError("Anritsu did not restore the confirmed Continuous acquisition mode.")
            self._check_scpi_errors(session, "restoring Continuous acquisition")
        return result

    def acquire_instrument_average(
        self, duration_s: float | None = None, *, acquisition_timeout_s: float, sweep_count: int | None = None,
    ) -> tuple[SpectrumTrace, int, float]:
        """Acquire one Trace A linear-power mean and retain Single between recipe points."""

        return self._acquire_instrument_average(
            InstrumentAverageTarget(frame_count=sweep_count, duration_s=duration_s),
            acquisition_timeout_s=acquisition_timeout_s,
            restore_continuous=self._live,
        )

    def acquire_manual_instrument_average(
        self, target: InstrumentAverageTarget, *, live: bool = False,
    ) -> InstrumentAverageResult:
        """Acquire one mean for the manual worker and restore the operating state."""

        configuration = None
        if not live and self._single_sweep_supported:
            self._assert_acquisition_allowed()
            configuration = self.read_acquisition_configuration()
        timeout_s = parse_quantity(self._settings.acquisition.operation_complete_timeout, DIMENSION_TIME).si_value
        trace, count, sweep_time_s = self._acquire_instrument_average(
            target, acquisition_timeout_s=timeout_s, restore_continuous=True, live=live,
        )
        if configuration is not None:
            full, _advanced = configuration
            if (self.read_acquisition_configuration() != configuration
                    or len(trace.frequencies_hz) != full.points
                    or not math.isclose(trace.frequencies_hz[0], full.start_hz, rel_tol=1e-9, abs_tol=1.0)
                    or not math.isclose(trace.frequencies_hz[-1], full.stop_hz, rel_tol=1e-9, abs_tol=1.0)):
                raise DeviceError("Analyzer settings changed during manual averaging.")
        return InstrumentAverageResult(
            replace(trace, trace_name=f"TRAC1_ANRITSU_LAV{count}"), count, sweep_time_s, configuration,
        )

    def _acquire_instrument_average(
        self, target: InstrumentAverageTarget, *, acquisition_timeout_s: float, restore_continuous: bool,
        live: bool = False,
    ) -> tuple[SpectrumTrace, int, float]:
        operation_started = time.monotonic()
        self._assert_acquisition_allowed()
        if not self._single_sweep_supported:
            raise SafetyViolation("Instrument averaging requires the qualified Anritsu single-sweep protocol.")
        if not self._supports_qualified_trace_protocol():
            raise SafetyViolation(
                "Anritsu instrument averaging is unverified for this model/firmware."
            )
        if type(acquisition_timeout_s) not in (int, float) or not 0 < acquisition_timeout_s < math.inf:
            raise SafetyViolation("Anritsu acquisition timeout must be finite and positive in seconds.")
        if target.duration_s is not None and target.duration_s >= acquisition_timeout_s:
            raise SafetyViolation("Instrument averaging time must be positive and shorter than the acquisition timeout.")
        session = self._require_session()
        if session.query("INST?").strip().upper() != "SPECT":
            raise SafetyViolation("Instrument averaging requires Spectrum Analyzer mode.")
        self._cancel_synchronized_live_sweep()
        synchronized = self._synchronized_timing(session)
        self._check_scpi_errors(session, "before instrument averaging")
        try:
            sweep_time_s = float(session.query("SWE:TIME?"))
        except ValueError as exc:
            raise DeviceError("Anritsu returned an invalid sweep time for instrument averaging.") from exc
        if not math.isfinite(sweep_time_s) or sweep_time_s <= 0:
            raise DeviceError("Anritsu returned a non-positive or non-finite sweep time.")
        count = (target.frame_count if target.frame_count is not None
                 else instrument_average_count_for_duration(target.duration_s, sweep_time_s))
        nominal_duration_s = count * sweep_time_s
        if nominal_duration_s >= acquisition_timeout_s:
            raise SafetyViolation("Required instrument sweeps exceed the acquisition deadline.")

        self._ensure_acquisition_remote()
        language = self._read_remote_language(session)
        native = language == "NAT"
        before = self._read_instrument_average_settings(session, native)
        continuous_before = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
        live_before, grouped_before, raw_live = self._live, self._group_live_queries, self._live_storage_before
        synchronized_live_before = self._live_synchronized
        if raw_live is not None:
            if raw_live[0] != native:
                raise DeviceError("Anritsu remote language changed during Live acquisition.")
            self._assert_live_storage_off(before.storage_mode)

        # Keep the pre-Live backup intact. A sweep timeout calls abort_acquisition(),
        # which must not restore that backup while the temporary settings are active.
        self._live = self._group_live_queries = self._live_synchronized = False
        self._live_storage_before = None
        type_query = self._trace_write_mode_query(native)
        restore = []
        acquisition_attempted = False
        failure = None
        try:
            if before.count != count:
                restore.append(f"AVER:COUN {before.count}")
                session.write(f"AVER:COUN {count}")
            # Even when averaging Trace A, Traces B..F storage can alter
            # INIT:MODE:SING accumulation and must be temporarily OFF.
            for index, other_mode in enumerate(before.other_storage_modes, start=2):
                if other_mode != "OFF":
                    restore.append(self._trace_storage_command(index, other_mode, native))
                    session.write(self._trace_storage_command(index, "OFF", native))
            if before.storage_mode != "LAV":
                restore.append(self._trace_storage_command(1, before.storage_mode, native))
                session.write(self._trace_storage_command(1, "LAV", native))
            self._confirm_instrument_average(session, native, "LAV", count)
            if before.write_mode != "WRIT":
                trace_type = f"TRAC:TYPE 1,{before.write_mode}" if native else f"TRAC1:TYPE {before.write_mode}"
                restore.append(trace_type)

            acquisition_attempted = True
            started_at = datetime.now(UTC)
            try:
                self.start_single_sweep(
                    queue_wait=not (live or synchronized),
                    _prechecked_language=language,
                )
            except Exception as exc:
                # The VISA write may have reached the analyser before reporting an error.
                try:
                    self.abort_acquisition()
                except Exception as abort_error:  # noqa: BLE001 - retain the acquisition failure
                    exc.add_note(f"Anritsu instrument averaging abort also failed: {abort_error}")
                raise
            self.wait_complete(
                deadline_s=acquisition_timeout_s,
                poll_after_s=nominal_duration_s if live else (0.0 if synchronized else None),
            )
            completed_at = datetime.now(UTC)
            count_query = "SWEEPCOUNT?" if native else "TRAC:SWE:COUN?"
            completed_count = self._parse_integer(session.query(count_query), "completed sweep count", minimum=0)
            self._check_scpi_errors(session, "instrument average completion")
            if completed_count != count:
                raise DeviceError(f"Instrument average completed {completed_count} of {count} requested sweeps.")
            if before.data_format != "ASC,0":
                restore.extend((f"FORM {before.data_format}", f"FORM:BORD {before.byte_order}"))
            trace = self.fetch_trace("TRAC1")
            write_mode = session.query(type_query).strip().upper()
            self._confirm_instrument_average(session, native, "LAV", count)
            if write_mode != "WRIT":
                raise DeviceError("Anritsu Trace A write mode changed during instrument averaging.")
            trace = self._stamp_single_sweep(trace, started_at, completed_at)
        except Exception as exc:
            failure = exc
            raise
        finally:
            try:
                continuous_after = continuous_before and (restore_continuous or not acquisition_attempted)
                self._restore_instrument_average(
                    session, native, before, restore,
                    continuous=continuous_after,
                    restart_continuous=continuous_after and acquisition_attempted,
                )
            except DeviceError as exc:
                if failure is not None:
                    raise DeviceError(f"{failure} {exc}") from failure
                raise
            finally:
                self._live_storage_before = raw_live
            if self._state != DeviceState.UNKNOWN:
                self._live, self._group_live_queries = live_before, grouped_before
                self._live_synchronized = synchronized_live_before
        evidence = InstrumentAverageProvenance(
            completed_count, sweep_time_s,
            requested_sweep_count=target.frame_count,
            target_duration_s=target.duration_s,
            elapsed_s=time.monotonic() - operation_started,
        )
        return replace(trace, instrument_average=evidence), completed_count, sweep_time_s

    @staticmethod
    def _trace_write_mode_query(native: bool) -> str:
        return "TRAC:TYPE? 1" if native else "TRAC1:TYPE?"

    def _read_instrument_average_settings(self, session: InstrumentSession, native: bool) -> _InstrumentAverageSettings:
        storage_modes = self._read_trace_storage_modes(session, native)
        mode = storage_modes[0]
        count = self._parse_integer(
            session.query("AVER:COUN?"), "average count", minimum=1,
        )
        write_mode = session.query(self._trace_write_mode_query(native)).strip().upper()
        data_format = session.query("FORM?").replace(" ", "").strip().upper()
        if data_format == "ASC":
            data_format = "ASC,0"
        byte_order = session.query("FORM:BORD?").strip().upper()
        self._check_scpi_errors(session, "instrument averaging settings")
        if mode not in _TRACE_STORAGE_MODES or count > ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS:
            raise DeviceError("Anritsu returned invalid Trace A averaging settings.")
        if write_mode not in {"WRIT", "VIEW", "BLAN"}:
            raise DeviceError(f"Anritsu returned invalid Trace A write mode {write_mode!r}.")
        if data_format not in {"ASC,0", "REAL,32", "INT,32"} or byte_order not in {"SWAP", "NORM"}:
            raise DeviceError(f"Anritsu returned invalid trace format {data_format!r} or byte order {byte_order!r}.")
        return _InstrumentAverageSettings(
            mode,
            count,
            write_mode,
            data_format,
            byte_order,
            other_storage_modes=storage_modes[1:],
        )

    def _confirm_instrument_average(
        self, session: InstrumentSession, native: bool, mode: str, count: int
    ) -> None:
        storage_modes = self._read_trace_storage_modes(session, native)
        actual_mode = storage_modes[0]
        actual_count = self._parse_integer(
            session.query("AVER:COUN?"), "average count", minimum=ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS,
        )
        self._check_scpi_errors(session, "Trace A averaging settings")
        if any(other != "OFF" for other in storage_modes[1:]):
            raise DeviceError(
                f"Anritsu Traces B-F storage modes must remain OFF during instrument averaging (read {storage_modes[1:]!r})."
            )
        if (actual_mode, actual_count) != (mode, count):
            raise DeviceError(f"Anritsu Trace A averaging readback {actual_mode}/{actual_count} differs from {mode}/{count}.")

    def _restore_instrument_average(
        self, session: InstrumentSession, native: bool, previous: _InstrumentAverageSettings, commands: list[str],
        *, continuous: bool, restart_continuous: bool,
    ) -> None:
        try:
            if (self._read_remote_language(session) == "NAT") != native:
                raise DeviceError("Anritsu remote language changed during instrument averaging.")
        except Exception as exc:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(f"Anritsu averaging settings could not be restored: {exc}") from exc
        errors = []
        for command in commands:
            try:
                session.write(command)
            except Exception as exc:  # noqa: BLE001 - attempt the remaining restorations
                errors.append(f"{command}: {exc}")
        try:
            actual = self._read_instrument_average_settings(session, native)
            if actual != previous:
                raise DeviceError(f"Expected averaging settings {previous}, read {actual}.")
        except Exception as exc:  # noqa: BLE001 - report uncertain settings
            errors.append(str(exc))
        if errors:
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu averaging settings could not be restored: " + " | ".join(errors))
        if self._state == DeviceState.UNKNOWN:
            raise DeviceError("Anritsu could not confirm acquisition stopped after averaging.")
        try:
            if restart_continuous:
                session.write("INIT:MODE:CONT")
            actual_continuous = self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
            self._check_scpi_errors(session, "restoring acquisition mode after averaging")
            if actual_continuous != continuous:
                raise DeviceError("Anritsu did not confirm the expected sweep mode after averaging.")
        except Exception as exc:
            self._state = DeviceState.UNKNOWN
            raise DeviceError(f"Anritsu acquisition mode could not be restored: {exc}") from exc

    def _read_trace_storage_modes(self, session: InstrumentSession, native: bool) -> tuple[str, ...]:
        queries = tuple(self._trace_storage_query(index, native) for index in range(1, 7))
        if native and self._supports_grouped_native_queries():
            responses = session.query(";".join(queries)).split(";")
        else:
            responses = [session.query(query) for query in queries]
        modes = tuple(response.strip().upper() for response in responses)
        if len(modes) != len(queries) or not set(modes) <= _TRACE_STORAGE_MODES:
            raise DeviceError(f"Anritsu returned invalid trace storage modes {modes!r}.")
        return modes

    @staticmethod
    def _trace_storage_query(index: int, native: bool) -> str:
        return f"TRAC:STOR:MODE? {index}" if native else f"TRAC{index}:STOR:MODE?"

    @staticmethod
    def _trace_storage_command(index: int, mode: str, native: bool) -> str:
        return f"TRAC:STOR:MODE {index},{mode}" if native else f"TRAC{index}:STOR:MODE {mode}"

    def _restore_trace_storage_modes(
        self, session: InstrumentSession, native: bool, changed: list[tuple[int, str]]
    ) -> None:
        errors: list[str] = []
        for index, mode in reversed(changed):
            try:
                session.write(self._trace_storage_command(index, mode, native))
            except Exception as exc:  # noqa: BLE001 - attempt the remaining restorations
                errors.append(f"Trace {index} ({mode}): {exc}")
        try:
            self._check_scpi_errors(session, "restoring trace storage")
            actual = self._read_trace_storage_modes(session, native)
            for index, mode in changed:
                if actual[index - 1] != mode:
                    errors.append(f"Trace {index}: expected {mode}, read {actual[index - 1]}")
        except Exception as exc:  # noqa: BLE001 - report uncertain instrument state
            errors.append(str(exc))
        if errors:
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu trace storage restoration could not be confirmed: " + "; ".join(errors))

    def fetch_current_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read the currently displayed trace using the proven library sequence."""

        trace = validate_anritsu_trace_name(trace)
        passive_preview = self._live and self._passive_live
        if not passive_preview:
            self._assert_acquisition_allowed()
        session = self._require_session()
        return self._read_ascii_trace(
            session,
            trace,
            prepare_ascii=not passive_preview or self._settings.safety.acquisition_allowed,
            configuration_generation=self._configuration_generation,
            check_vertical_scale=self._supports_qualified_trace_protocol(),
        )

    def fetch_current_trace_fast(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read a binary trace with a freshly verified axis; cache only axis allocation."""

        trace = validate_anritsu_trace_name(trace)
        self._assert_acquisition_allowed()
        session = self._require_session()
        grouped = self._live and self._group_live_queries
        raw_live = self._live_storage_before
        if grouped:
            before, transfer_format = self._read_grouped_live_state(session, "before binary trace transfer")
        else:
            if raw_live is not None:
                self._assert_live_storage_off(session.query(self._trace_storage_query(1, raw_live[0])))
            before = self._read_binary_grid(session)
            self._check_scpi_errors(session, "before binary trace transfer")
            transfer_format = session.query("FORM?").strip().upper(), None
        self._prepare_binary_transfer(session, *transfer_format)
        raw_values = session.query_binary_values(
            f"TRAC? {trace}", datatype="f", is_big_endian=False
        )
        if grouped:
            after, confirmed_format = self._read_grouped_live_state(session, f"binary TRAC? {trace}")
            if confirmed_format != _BINARY_TRACE_FORMAT:
                raise DeviceError("Anritsu transfer format changed during binary trace acquisition.")
        else:
            self._check_scpi_errors(session, f"binary TRAC? {trace}")
            after = self._read_binary_grid(session)
            if raw_live is not None:
                self._assert_live_storage_off(session.query(self._trace_storage_query(1, raw_live[0])))
        if before != after:
            self._cached_grid = None
            raise DeviceError("Anritsu frequency grid changed during binary trace acquisition.")
        return self._decode_binary_trace(trace, raw_values, before)

    def _read_binary_grid(self, session: InstrumentSession) -> tuple[int, float, float]:
        if self._supports_qualified_trace_protocol():
            self._assert_log_trace_scale(session.query(_VERTICAL_SCALE_QUERY))
        points = self._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
        return points, float(session.query("FREQ:STAR?")), float(session.query("FREQ:STOP?"))

    def _read_grouped_live_state(
        self, session: InstrumentSession, context: str
    ) -> tuple[tuple[int, float, float], tuple[str, str]]:
        response = session.query(_LIVE_BINARY_READBACK)
        try:
            # Error text can contain semicolons. Keep it as the last response unit.
            points, start, stop, data_format, byte_order, storage, scale, error = response.split(";", 7)
            grid = self._parse_integer(points, "sweep point count", minimum=2), float(start), float(stop)
        except ValueError as exc:
            raise DeviceError(f"Anritsu returned invalid grouped Live readback: {response!r}.") from exc
        self._check_scpi_errors(session, context, first_response=error)
        self._assert_live_storage_off(storage)
        self._assert_log_trace_scale(scale)
        return grid, (data_format.strip().upper(), byte_order.strip().upper())

    @staticmethod
    def _assert_log_trace_scale(scale: str) -> None:
        # TRAC? returns dBm in Log scale and normalized voltage in Linear scale.
        if scale.strip().upper() != "LOG":
            raise DeviceError(f"Set the Anritsu amplitude axis to Log scale for dBm spectra (read {scale.strip()!r}).")

    @staticmethod
    def _assert_live_storage_off(mode: str) -> None:
        if mode.strip().upper() != "OFF":
            raise DeviceError(f"Anritsu Trace A storage mode changed during Live: {mode!r}, expected OFF.")

    def _prepare_binary_transfer(
        self, session: InstrumentSession, current_format: str, current_byte_order: str | None
    ) -> None:
        data_format, byte_order = _BINARY_TRACE_FORMAT
        format_changed = current_format != data_format
        if format_changed:
            session.write(f"FORM {data_format}")
        grouped = current_byte_order is not None
        if current_byte_order is None:
            current_byte_order = session.query("FORM:BORD?").strip().upper()
        border_changed = current_byte_order != byte_order
        if border_changed:
            session.write(f"FORM:BORD {byte_order}")
        if not grouped or format_changed or border_changed:
            self._check_scpi_errors(session, "binary transfer setup (FORM / FORM:BORD)")
        if format_changed and session.query("FORM?").strip().upper() != data_format:
            raise DeviceError("Anritsu did not confirm REAL,32 transfer format.")
        if border_changed and session.query("FORM:BORD?").strip().upper() != byte_order:
            raise DeviceError("Anritsu did not confirm SWAP transfer byte order.")

    @staticmethod
    def _build_frequency_grid(start_hz: float, stop_hz: float, points: int) -> tuple[float, ...]:
        return tuple(np.linspace(start_hz, stop_hz, points, dtype=np.float64).tolist())

    def _decode_binary_trace(
        self,
        trace: str,
        raw_values: list[float] | tuple[float, ...] | bytes | bytearray | memoryview | np.ndarray,
        grid: tuple[int, float, float],
    ) -> SpectrumTrace:
        points, start_hz, stop_hz = grid
        if isinstance(raw_values, (bytes, bytearray, memoryview)):
            arr = np.frombuffer(raw_values, dtype="<f4")
        else:
            arr = np.asarray(raw_values, dtype=np.float64)
        if arr.ndim != 1 or arr.size != points:
            raise DeviceError(
                f"Anritsu returned {arr.size} binary trace points; expected {points}."
            )
        if points < 2:
            raise DeviceError("Anritsu returned fewer than two trace points.")
        if not (math.isfinite(start_hz) and math.isfinite(stop_hz) and bool(np.isfinite(arr).all())):
            raise DeviceError("Anritsu returned NaN or infinity in the binary trace.")
        if start_hz <= 0 or stop_hz <= start_hz:
            raise DeviceError("Anritsu returned an invalid binary frequency grid.")
        invalid_points = int(np.count_nonzero(arr <= -998.0))
        if invalid_points:
            raise DeviceError(
                "Anritsu returned the documented -999.0 unmeasured/error sentinel "
                f"for {invalid_points} of {points} trace points."
            )
        values = tuple(arr.astype(np.float64, copy=False).tolist())
        if (
            self._cached_grid is not None
            and self._cached_grid[0] == start_hz
            and self._cached_grid[1] == stop_hz
            and self._cached_grid[2] == points
        ):
            frequencies = self._cached_grid[3]
        else:
            frequencies = self._build_frequency_grid(start_hz, stop_hz, points)
            self._cached_grid = (start_hz, stop_hz, points, frequencies)

        return SpectrumTrace(
            frequencies_hz=frequencies,
            powers_dbm=values,
            acquired_at_utc=datetime.now(UTC),
            trace_name=trace,
            configuration_generation=self._configuration_generation,
        )

    def acquire_fresh_trace(
        self,
        trace: str = "TRAC1",
        *,
        timeout_s: float = 5.0,
    ) -> SpectrumTrace:
        """Acquire a proven new sweep; never substitute an old continuous buffer."""
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise SafetyViolation("Anritsu acquisition timeout must be finite and positive.")
        return self.acquire_single_sweep(trace, timeout_s=timeout_s)

    @staticmethod
    def _read_ascii_trace(
        session: InstrumentSession,
        trace: str,
        *,
        prepare_ascii: bool = False,
        configuration_generation: int = 0,
        check_vertical_scale: bool = False,
    ) -> SpectrumTrace:
        try:
            if check_vertical_scale:
                AnritsuAdapter._assert_log_trace_scale(session.query(_VERTICAL_SCALE_QUERY))
            start_before = float(session.query("FREQ:STAR?"))
            stop_before = float(session.query("FREQ:STOP?"))
            points = AnritsuAdapter._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
            # Check transfer state rather than rewriting ASCII at every point.
            # The format can change after Live or another instrument client.
            AnritsuAdapter._check_scpi_errors(session, "before ASCII trace transfer")
            current_format = session.query("FORM?").strip().upper().split(",", 1)[0]
            if current_format != "ASC":
                if not prepare_ascii:
                    raise DeviceError(
                        "Cannot read the current trace without changing the instrument: "
                        f"FORM? returned {current_format!r}, not ASCII."
                    )
                session.write("FORM ASC")
                AnritsuAdapter._check_scpi_errors(session, "ASCII FORM ASC preparation")
                if session.query("FORM?").strip().upper().split(",", 1)[0] != "ASC":
                    raise DeviceError("Anritsu did not confirm ASCII transfer format.")
            raw = session.query(f"TRAC? {trace}")
            AnritsuAdapter._check_scpi_errors(session, "ASCII FORM ASC / TRAC? " + trace)
            values = tuple(float(item) for item in raw.split(",") if item.strip())
            # Read the axis again after the trace transfer to guarantee temporal coherence
            if check_vertical_scale:
                AnritsuAdapter._assert_log_trace_scale(session.query(_VERTICAL_SCALE_QUERY))
            start_after = float(session.query("FREQ:STAR?"))
            stop_after = float(session.query("FREQ:STOP?"))
            points_after = AnritsuAdapter._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
        except (TypeError, ValueError) as exc:
            raise DeviceError("Anritsu returned an invalid trace response.") from exc
        if not (
            math.isclose(start_before, start_after, rel_tol=1e-9, abs_tol=1.0)
            and math.isclose(stop_before, stop_after, rel_tol=1e-9, abs_tol=1.0)
            and points == points_after
        ):
            raise DeviceError(
                "Anritsu frequency grid parameters changed during trace acquisition; "
                "temporal coherence between frequency axis and power trace was lost."
            )
        start, stop = start_after, stop_after
        if len(values) != points:
            raise DeviceError(
                f"Anritsu returned {len(values)} trace points; expected {points}."
            )
        if points < 2:
            raise DeviceError("Anritsu returned fewer than two trace points.")
        arr = np.asarray(values, dtype=np.float64)
        if not (math.isfinite(start) and math.isfinite(stop) and bool(np.isfinite(arr).all())):
            raise DeviceError("Anritsu returned NaN or infinity in the trace.")
        invalid_points = int(np.count_nonzero(arr <= -998.0))
        if invalid_points:
            raise DeviceError(
                "Anritsu returned the documented -999.0 unmeasured/error sentinel "
                f"for {invalid_points} of {points} trace points; no valid completed "
                "spectrum is available."
            )
        if start <= 0 or stop <= start:
            raise DeviceError("Anritsu returned an invalid trace frequency axis.")
        return SpectrumTrace(
            frequencies_hz=AnritsuAdapter._build_frequency_grid(start, stop, points),
            powers_dbm=values,
            acquired_at_utc=datetime.now(UTC),
            trace_name=trace,
            configuration_generation=configuration_generation,
        )
