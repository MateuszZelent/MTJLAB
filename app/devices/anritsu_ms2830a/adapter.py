"""Safe Anritsu MS2830A spectrum and live-trace adapter."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
import hashlib
import math
import time

from app.devices.base import DeviceAdapter, InstrumentSession, SessionFactory, parse_identity, validate_identity
from app.devices.anritsu_ms2830a.hardware import (
    ANRITSU_PREAMPLIFIER_OPTIONS,
    ANRITSU_SIGNAL_GENERATOR_OPTIONS,
    parse_anritsu_hardware_catalog,
)
from app.devices.visa import PyVisaSessionFactory
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceCapabilities, DeviceIdentity, DeviceState
from app.domain.spectrum_correction import SweepEvidence
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.safety.anritsu import (
    ANRITSU_SWEEP_POINT_COUNTS,
    assert_anritsu_acquisition_allowed,
    normalize_anritsu_detector,
    validate_anritsu_advanced_spectrum,
    validate_anritsu_spectrum,
    validate_anritsu_signal_generator,
    validate_anritsu_trace_name,
)
from app.settings.models import AnritsuSettings, StationSettings
from .configuration import SignalGeneratorConfig, SpectrumConfig


@dataclass(frozen=True, slots=True)
class AnritsuConfigurationSnapshot:
    """Read-only snapshot of the analyser's current spectrum settings."""

    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    instrument_mode: str = ""


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

    def __post_init__(self) -> None:
        if self.vbw_filter_mode not in {None, "VID", "POW"}:
            raise ValueError("Reference VBW filter mode must be VID or POW.")
        if self.kind not in {"single", "averaged", "imported"}:
            raise ValueError(f"Unsupported reference kind {self.kind!r}.")
        if self.average_count < 1:
            raise ValueError("Reference average_count must be positive.")
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
        except Exception:
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

    def _prepare_trace_a(self) -> None:
        session = self._require_session()
        # Mainframe Remote Control 1.6.2: Native moves the indexed SCPI
        # header's number into the first argument. Never change the language
        # implicitly, and never send TRAC1:TYPE to a Native interpreter.
        language = self._read_remote_language(session)
        command = "TRAC:TYPE 1,WRIT" if language == "NAT" else "TRAC1:TYPE WRIT"
        session.write(command)
        self._check_scpi_errors(session, f"Trace A preparation ({command}, language={language})")

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
        session, self._session = self._session, None
        self._remote_entered = False
        self._sg_control_owned = False
        self._live = False
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
            self._stop_spectrum_acquisition(session)
        except Exception:
            self._live = False
            self._state = DeviceState.UNKNOWN
            raise
        self._live = False
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
            except Exception as exc:
                errors.append(exc)
        try:
            self._stop_spectrum_acquisition(session)
        except Exception as exc:
            errors.append(exc)
        if errors:
            self._live = False
            self._state = DeviceState.UNKNOWN
        else:
            self._live = False
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

    def _enter_spectrum_mode_with_rf_off(self) -> None:
        """Stop session-owned SG output before explicitly selecting Spectrum."""

        session = self._require_session()
        if self._sg_control_owned or self._sg_output_enabled or self._last_sg_config is not None:
            session.write("INST SG")
            session.write("OUTP 0")
            if self._parse_output_state(session.query("OUTP?")):
                self._state = DeviceState.UNKNOWN
                raise DeviceError("Anritsu SG did not confirm RF OUTPUT OFF.")
            self._sg_output_enabled = False
        session.write("INST SPECT")

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
        if self._last_sg_config is not None:
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
            except Exception:
                pass
            raise DeviceError(
                "Anritsu SG readback failed after commanding RF OUTPUT ON; "
                "attempted shutdown and marked state UNKNOWN."
            ) from exc

        self._sg_output_enabled = active
        self._state = DeviceState.OUTPUT_ON if active else DeviceState.OUTPUT_OFF
        if not active:
            try:
                session.write("OUTP 0")
            except Exception:
                pass
            self._state = DeviceState.UNKNOWN
            raise DeviceError("Anritsu SG did not confirm RF OUTPUT ON.")
        return True

    @property
    def _single_sweep_supported(self) -> bool:
        return self._settings.acquisition.single_sweep_mode == "standard_scpi_opc"

    def _assert_acquisition_allowed(self) -> None:
        assert_anritsu_acquisition_allowed(self._settings.safety)

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
        except Exception:
            # Conservative input-path fallback. Do not hide the original fault.
            try:
                if requested.attenuation_auto is not None or requested.attenuation_db is not None or requested.preamplifier_enabled is not None:
                    if has_preamp:
                        session.write("POW:GAIN OFF")
                    session.write("POW:ATT:AUTO OFF")
                    session.write("POW:ATT 60DB")
            except Exception:
                pass
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
        if requested_vbw_mode == "manual":
            if actual.vbw_hz is None or not math.isclose(
                actual.vbw_hz,
                float(requested.vbw_hz),
                rel_tol=1e-6,
                abs_tol=1.0,
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

    def configure_spectrum(self, config: SpectrumConfig) -> AnritsuConfigurationSnapshot:
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
        session = self._require_session()
        fields = config.changed_fields
        if fields is not None:
            allowed = {"start_hz", "stop_hz", "reference_level_dbm", "points", "vbw_mode"}
            if not fields or len(fields) != len(set(fields)) or set(fields) - allowed:
                raise SafetyViolation("Spectrum changes require explicit distinct supported fields.")
            before = self.read_current_configuration()
            if "SPECT" not in before.instrument_mode.upper():
                raise DeviceError("Spectrum parameter changes require confirmed Spectrum Analyzer mode.")
            for name in allowed - set(fields) - {"vbw_mode"}:
                if not math.isclose(float(getattr(before, name)), float(getattr(config, name)), rel_tol=1e-9, abs_tol=1e-9):
                    raise DeviceError(f"Anritsu unselected {name} differs from the planned baseline; re-read and recompile.")
            changed = []
            for name in fields:
                if name == "vbw_mode":
                    same = self._read_vbw_filter_mode() == config.vbw_mode
                elif name == "points":
                    same = before.points == config.points
                else:
                    same = math.isclose(float(getattr(before, name)), float(getattr(config, name)), rel_tol=1e-12, abs_tol=1e-9)
                if not same:
                    changed.append(name)
            fields = tuple(changed)
        else:
            self._enter_spectrum_mode_with_rf_off()
        if fields is None or fields:
            self._configuration_generation += 1
            self._cached_grid = None
        frequency_fields = ("start_hz", "stop_hz")
        if fields is not None and config.start_hz >= before.stop_hz:
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
        actual = self.read_current_configuration()
        mismatches: list[str] = []
        if config.vbw_mode is not None and (config.changed_fields is None or "vbw_mode" in config.changed_fields) and self._read_vbw_filter_mode() != config.vbw_mode:
            mismatches.append("VBW Video/Power mode")
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
        return actual

    def _ensure_acquisition_remote(self) -> None:
        # GPIB REN is transport control, not an SCPI header or a sweep mode.
        # Do this once when measurement starts, never during discovery/identity.
        if self._settings.connection.resource.upper().startswith("GPIB") and not self._remote_entered:
            self._require_session().ensure_remote()
            self._remote_entered = True

    @staticmethod
    def _check_scpi_errors(session: InstrumentSession, context: str) -> None:
        """Preserve transient front-panel errors in the caller's fault/log."""
        errors = []
        for _ in range(16):
            response = session.query("SYST:ERR?").strip()
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

    def start_live(self, ensure_continuous: bool = False) -> AnritsuConfigurationSnapshot:
        """Start Live polling, optionally ensuring free-running measurement."""

        snapshot = self.read_current_configuration()
        if "SPECT" not in snapshot.instrument_mode.upper():
            raise DeviceError(
                f"Read-only Live requires Spectrum Analyzer mode; current mode is "
                f"{snapshot.instrument_mode!r}. Select Spectrum Analyzer on the instrument."
            )
        if ensure_continuous:
            session = self._require_session()
            self._ensure_acquisition_remote()
            self._check_scpi_errors(session, "before Live preparation")
            # Live owns the expectation that every poll can observe the Trace
            # A measurement being refreshed. This changes the trace display
            # mode once at Live startup; individual timer ticks remain reads.
            self._prepare_trace_a()
            # Do not probe TRAC:TYPE? here. Although documented for Spectrum
            # Analyzer mode, MS2830A firmware can leave the query unanswered
            # in measurement applications where trace-type control is not
            # available (for example SEM/Spurious contexts). Reading TRAC1
            # does not require this query; repeated identical frames are
            # detected by the UI and reported without breaking Live.
            continuous_response = session.query("INIT:CONT?").strip().upper()
            if continuous_response in {"1", "+1", "ON"}:
                continuous = True
            elif continuous_response in {"0", "+0", "OFF"}:
                continuous = False
            else:
                raise DeviceError(
                    f"Anritsu returned invalid INIT:CONT? response {continuous_response!r}."
                )
            if not continuous:
                # The MS2830A command explicitly selects Continuous mode and
                # starts continuous measurement.
                session.write("INIT:MODE:CONT")
            self._check_scpi_errors(session, "Live preparation (TRAC1:TYPE WRIT / INIT:MODE:CONT)")
        self._live = True
        return snapshot

    def stop_live(self) -> None:
        # Stop only application polling.  Turning INIT:CONT OFF here would
        # freeze the front-panel trace and make the next current-trace read
        # return stale data (or -999 after a range change).
        self._live = False

    @property
    def live(self) -> bool:
        return self._live

    def start_single_sweep(self) -> None:
        """Start one qualified SCPI sweep for a recipe checkpoint.

        This command family is intentionally unavailable until the current
        Anritsu firmware has been qualified in the station profile.  It is not
        used for the user-facing Live polling loop.
        """

        self._assert_acquisition_allowed()
        if not self._single_sweep_supported:
            raise SafetyViolation(
                "The recipe requires the qualified Anritsu single-sweep protocol; "
                "the current profile permits Live/Fetch only."
            )
        session = self._require_session()
        if "SPECT" not in session.query("INST?").strip().upper():
            raise SafetyViolation("Single spectrum acquisition requires an explicit Spectrum Analyzer configuration first.")
        self._ensure_acquisition_remote()
        self._check_scpi_errors(session, "before single-sweep preparation")
        # A fresh acquisition must update Trace A, including when the previous
        # display mode was VIEW/hold. Use the same trace-specific preparation
        # as Live; TRAC:TYPE? times out on the target MS2830A and must not be
        # probed (or retried after a timeout). This prepares the acquisition
        # buffer without changing the operator's RF/bandwidth settings.
        self._prepare_trace_a()
        # MS2830A Spectrum Analyzer Remote Control, section 2.7 documents this
        # exact pair for a single measurement. INIT:MODE:SING both selects
        # Single and starts the sweep; *WAI holds the following command until
        # the sweep is complete. Generic INIT:IMM + *OPC? is deliberately not
        # used because it did not produce a valid trace on the target firmware.
        session.write("INIT:MODE:SING")
        session.write("*WAI")
        # DeviceState represents the connection/output safety state, not the
        # transient acquisition state; the analyser has no energy output here.
        self._state = DeviceState.VERIFIED

    def wait_complete(self, *, deadline_s: float | None = None) -> None:
        """Confirm completion after the queued ``*WAI`` with a hard deadline."""

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
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("Timed out waiting for the Anritsu single sweep to complete.")
                # The VISA call itself must not outlive the application-level
                # deadline. Some backends reject a zero-millisecond timeout.
                # *WAI queues this query behind the entire sweep. Its response
                # needs the acquisition budget, not the shorter ordinary I/O budget.
                session.timeout = max(1, int(remaining * 1000))
                response = session.query("INIT:SWP?").strip()
                if response in {"0", "+0"}:
                    self._check_scpi_errors(session, "INIT:MODE:SING / *WAI / INIT:SWP?")
                    self._state = DeviceState.VERIFIED
                    return
                if response not in {"1", "+1"}:
                    raise DeviceError(
                        f"Anritsu returned invalid INIT:SWP? response {response!r}."
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except Exception as exc:
            try:
                self.abort_acquisition()
            except Exception as abort_error:
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
    ) -> SpectrumTrace:
        """Synchronise one trace; recipes keep Single between checkpoints."""

        trace = validate_anritsu_trace_name(trace)
        if timeout_s is not None and (
            type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0
        ):
            raise SafetyViolation("Anritsu acquisition timeout must be finite and positive.")
        started = datetime.now(timezone.utc)
        session = self._require_session()
        if type(restore_continuous) is not bool:
            raise SafetyViolation("Anritsu restore_continuous must be a boolean.")
        continuous_before = (
            self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition")
            if restore_continuous else False
        )
        self.start_single_sweep()
        self.wait_complete(deadline_s=timeout_s)
        completed = datetime.now(timezone.utc)
        result = self.fetch_trace(trace)
        if continuous_before:
            session.write("INIT:MODE:CONT")
            if not self._parse_switch(session.query("INIT:CONT?"), "continuous acquisition"):
                raise DeviceError("Anritsu did not restore the confirmed Continuous acquisition mode.")
            self._check_scpi_errors(session, "restoring Continuous acquisition")
        self._acquisition_sequence += 1
        return replace(
            result,
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
        return self._read_ascii_trace(session, trace, prepare_ascii=True)

    def fetch_current_trace(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read the currently displayed trace using the proven library sequence."""

        trace = validate_anritsu_trace_name(trace)
        session = self._require_session()
        return self._read_ascii_trace(session, trace, prepare_ascii=True)

    def fetch_current_trace_fast(self, trace: str = "TRAC1") -> SpectrumTrace:
        """Read a binary trace with a freshly verified axis; cache only axis allocation."""

        trace = validate_anritsu_trace_name(trace)
        self._assert_acquisition_allowed()
        session = self._require_session()
        points = AnritsuAdapter._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
        start_hz = float(session.query("FREQ:STAR?"))
        stop_hz = float(session.query("FREQ:STOP?"))
        return self._read_binary_trace(
            session, trace, points=points, start_hz=start_hz, stop_hz=stop_hz
        )

    def _read_binary_trace(
        self,
        session: InstrumentSession,
        trace: str,
        *,
        points: int,
        start_hz: float,
        stop_hz: float,
    ) -> SpectrumTrace:
        # Verify transfer state, but do not reprogram it at every Live tick.
        # Querying also detects another client changing byte order or ASCII mode.
        self._check_scpi_errors(session, "before binary trace transfer")
        format_changed = session.query("FORM?").strip().upper() != "REAL,32"
        if format_changed:
            session.write("FORM REAL,32")
        border_changed = session.query("FORM:BORD?").strip().upper() != "SWAP"
        if border_changed:
            session.write("FORM:BORD SWAP")
        self._check_scpi_errors(session, "binary transfer setup (FORM / FORM:BORD)")
        if format_changed and session.query("FORM?").strip().upper() != "REAL,32":
            raise DeviceError("Anritsu did not confirm REAL,32 transfer format.")
        if border_changed and session.query("FORM:BORD?").strip().upper() != "SWAP":
            raise DeviceError("Anritsu did not confirm SWAP transfer byte order.")
        raw_values = session.query_binary_values(
            f"TRAC? {trace}", datatype="f", is_big_endian=False
        )
        self._check_scpi_errors(session, "binary TRAC? " + trace)
        points_after = self._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
        start_after = float(session.query("FREQ:STAR?"))
        stop_after = float(session.query("FREQ:STOP?"))
        if (points_after, start_after, stop_after) != (points, start_hz, stop_hz):
            self._cached_grid = None
            raise DeviceError("Anritsu frequency grid changed during binary trace acquisition.")
        values = tuple(float(v) for v in raw_values)
        if len(values) != points:
            raise DeviceError(
                f"Anritsu returned {len(values)} binary trace points; expected {points}."
            )
        if points < 2:
            raise DeviceError("Anritsu returned fewer than two trace points.")
        if not all(math.isfinite(value) for value in (start_hz, stop_hz, *values)):
            raise DeviceError("Anritsu returned NaN or infinity in the binary trace.")
        if start_hz < 0 or stop_hz <= start_hz:
            raise DeviceError("Anritsu returned an invalid binary frequency grid.")
        invalid_points = sum(value <= -998.0 for value in values)
        if invalid_points:
            raise DeviceError(
                "Anritsu returned the unmeasured/error sentinel "
                f"for {invalid_points} of {points} trace points."
            )
        if (
            self._cached_grid is not None
            and self._cached_grid[0] == start_hz
            and self._cached_grid[1] == stop_hz
            and self._cached_grid[2] == points
        ):
            frequencies = self._cached_grid[3]
        else:
            step = (stop_hz - start_hz) / (points - 1)
            frequencies = tuple(start_hz + index * step for index in range(points))
            self._cached_grid = (start_hz, stop_hz, points, frequencies)

        return SpectrumTrace(
            frequencies_hz=frequencies,
            powers_dbm=values,
            acquired_at_utc=datetime.now(timezone.utc),
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
    ) -> SpectrumTrace:
        try:
            start_before = float(session.query("FREQ:STAR?"))
            stop_before = float(session.query("FREQ:STOP?"))
            points = AnritsuAdapter._parse_integer(session.query("SWE:POIN?"), "sweep point count", minimum=2)
            # Check transfer state rather than rewriting ASCII at every point.
            # The format can change after Live or another instrument client.
            AnritsuAdapter._check_scpi_errors(session, "before ASCII trace transfer")
            if prepare_ascii and session.query("FORM?").strip().upper().split(",", 1)[0] != "ASC":
                session.write("FORM ASC")
                AnritsuAdapter._check_scpi_errors(session, "ASCII FORM ASC preparation")
                if session.query("FORM?").strip().upper().split(",", 1)[0] != "ASC":
                    raise DeviceError("Anritsu did not confirm ASCII transfer format.")
            raw = session.query(f"TRAC? {trace}")
            AnritsuAdapter._check_scpi_errors(session, "ASCII FORM ASC / TRAC? " + trace)
            values = tuple(float(item) for item in raw.split(",") if item.strip())
            # Read the axis again after the trace transfer to guarantee temporal coherence
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
        if not all(math.isfinite(value) for value in (start, stop, *values)):
            raise DeviceError("Anritsu returned NaN or infinity in the trace.")
        invalid_points = sum(value == -999.0 for value in values)
        if invalid_points:
            raise DeviceError(
                "Anritsu returned the documented -999.0 unmeasured/error sentinel "
                f"for {invalid_points} of {points} trace points; no valid completed "
                "spectrum is available."
            )
        if stop <= start:
            raise DeviceError("Anritsu returned an invalid trace frequency axis.")
        step = (stop - start) / (points - 1)
        return SpectrumTrace(
            frequencies_hz=tuple(start + index * step for index in range(points)),
            powers_dbm=values,
            acquired_at_utc=datetime.now(timezone.utc),
            trace_name=trace,
        )
