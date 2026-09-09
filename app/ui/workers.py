"""Qt-thread ownership for all VISA sessions.

Every adapter is constructed once and all of its methods run on one dedicated
QObject/QThread pair.  GUI code only emits queued operation requests.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Event, RLock

from PySide6.QtCore import QEventLoop, QMetaObject, QObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter
from app.devices.base import DeviceAdapter
from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.contracts import OperationDispatcher
from app.contracts import DeviceModuleRegistry
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.domain.errors import RunInterrupted, SafetyViolation


class _RunAccess:
    """Admission is checked again on the transport thread, including queued calls."""

    def __init__(self):
        self.lock = RLock()
        self.owner: object | None = None
        self.interrupted = Event()
        self.in_flight = 0
        self.closing = False

    def begin_shutdown(self):
        with self.lock:
            self.closing = True
            self.interrupted.set()

    def assert_open(self):
        with self.lock:
            if self.closing:
                raise SafetyViolation("Instrument controller is shutting down.")

    @staticmethod
    def safe(operation, args, kwargs):
        if operation in {"emergency_off", "confirm_output_off"}:
            return True
        if operation in {"set_output", "set_output_group"}:
            enabled = args[1] if len(args) > 1 else kwargs.get("enabled")
            return enabled is False
        return False

    def acquire(self):
        with self.lock:
            self.assert_open()
            if self.owner is not None or self.in_flight:
                raise SafetyViolation("Instrument is busy; wait for the active operation to finish.")
            self.owner = object()
            self.interrupted = Event()
            return self.owner, self.interrupted

    def release(self, owner):
        with self.lock:
            if owner is not self.owner or self.in_flight:
                raise SafetyViolation("Cannot release an inactive or busy instrument reservation.")
            self.owner = None

    @contextmanager
    def enter(self, owner, operation, args=(), kwargs=None):
        kwargs = kwargs or {}
        with self.lock:
            safe = self.safe(operation, args, kwargs)
            if self.closing and not safe:
                raise SafetyViolation("Instrument controller is shutting down.")
            if owner is not None and owner is not self.owner:
                raise SafetyViolation("Instrument reservation has expired.")
            if self.owner is not None and owner is not self.owner:
                if not safe:
                    raise SafetyViolation("Instrument is reserved by characterization; operation blocked.")
                self.interrupted.set()
            if self.owner is not None and self.interrupted.is_set() and not safe:
                if operation not in {"state", "identity", "capabilities", "connected",
                                     "compliance_policy", "read_configuration", "last_source_request",
                                     "set_compliance_policy"}:
                    raise RunInterrupted("Characterization was interrupted by an output-off action.")
            self.in_flight += 1
        try:
            yield
        finally:
            with self.lock:
                self.in_flight -= 1


@dataclass(slots=True)
class _RunCall:
    """One synchronous call from the run worker to an adapter owner thread."""

    member: str
    args: tuple[object, ...] = ()
    kwargs: dict[str, object] = field(default_factory=dict)
    read_attribute: bool = False
    completed: Event = field(default_factory=Event)
    result: object = None
    error: BaseException | None = None
    timeout_s: float | None = None
    owner: object | None = None


class RunDeviceAdapter:
    """Duck-typed adapter proxy used exclusively by :class:`RunWorker`.

    The proxy intentionally never exposes the underlying adapter. Every call
    returns to the dedicated InstrumentWorker thread that owns the transport.
    """

    def __init__(self, controller: "DeviceController", owner: object | None = None,
                 interruption_event: Event | None = None) -> None:
        self._controller = controller
        self._owner = owner
        self.interruption_event = interruption_event
        self._active_timeout_s: float | None = None

    def release(self) -> None:
        self._controller.release_run_lease(self._owner)

    @contextmanager
    def io_timeout(self, timeout_s: float):
        """Scope an I/O timeout without accessing the underlying session outside its owner thread."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Operation I/O timeout must be finite and positive.")
        previous = self._active_timeout_s
        self._active_timeout_s = timeout_s if previous is None else min(previous, timeout_s)
        try:
            yield
        finally:
            self._active_timeout_s = previous

    @property
    def state(self) -> object:
        return self._controller.read_for_run("state", owner=self._owner)

    @property
    def identity(self) -> object:
        return self._controller.read_for_run("identity", owner=self._owner)

    @property
    def capabilities(self) -> object:
        return self._controller.read_for_run("capabilities", owner=self._owner)

    @property
    def connected(self) -> bool:
        return bool(self._controller.read_for_run("connected", owner=self._owner))

    def __getattr__(self, member: str) -> Callable[..., object]:
        if member.startswith("_"):
            raise AttributeError(member)

        def invoke(*args: object, **kwargs: object) -> object:
            return self._controller.call_for_run(
                member, *args, timeout_s=self._active_timeout_s, owner=self._owner, **kwargs
            )

        return invoke


class RecipePreflightWorker(QObject):
    """Compile and estimate an immutable recipe snapshot off the GUI thread."""

    succeeded = Signal(object, object, object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(
        self,
        settings: StationSettings,
        source: str,
        origin: str,
        *,
        outputs_forced_off: bool = False,
        device_registry: DeviceModuleRegistry | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._source = source
        self._origin = origin
        self._outputs_forced_off = bool(outputs_forced_off)
        self._device_registry = device_registry

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            recipe = parse_recipe_text(self._source, origin=self._origin)
            plan = RecipeCompiler(
                self._settings,
                cancellation_requested=thread.isInterruptionRequested,
                outputs_forced_off=self._outputs_forced_off,
                device_registry=self._device_registry,
            ).compile(recipe)
            if thread.isInterruptionRequested():
                self.cancelled.emit()
                return
            estimate = PlanEstimator(self._settings).estimate(plan)
            if thread.isInterruptionRequested():
                self.cancelled.emit()
                return
            self.succeeded.emit(recipe, plan, estimate)
        except Exception as exc:
            if thread.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class InstrumentWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    state_changed = Signal(str)
    capabilities_changed = Signal(object)
    shutdown_complete = Signal()
    traffic = Signal(str)

    def __init__(
        self,
        adapter: DeviceAdapter,
        dispatcher: OperationDispatcher | None = None,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._run_access: _RunAccess | None = None
        self._attach_traffic_logger()

    def _attach_traffic_logger(self) -> None:
        factory = getattr(self._adapter, "_factory", None)
        setter = getattr(factory, "set_traffic_callback", None)
        if callable(setter):
            setter(self.traffic.emit)

    @Slot(str, object)
    def execute(self, operation: str, payload: object) -> None:
        try:
            if self._run_access is None:
                result = self._dispatch(operation, payload)
            else:
                args = payload if isinstance(payload, tuple) else (payload,)
                with self._run_access.enter(None, operation, args):
                    result = self._dispatch(operation, payload)
            self.state_changed.emit(self._adapter.state.value)
            if operation == "connect":
                self.capabilities_changed.emit(self._adapter.capabilities)
            elif operation in {"disconnect", "replace_adapter"}:
                self.capabilities_changed.emit(None)
            self.completed.emit(operation, result)
        except Exception as exc:
            self.state_changed.emit(self._adapter.state.value)
            self.failed.emit(operation, str(exc))

    @Slot()
    def shutdown(self) -> None:
        if self._run_access is not None:
            self._run_access.begin_shutdown()
        try:
            self._adapter.emergency_off()
        except Exception:
            pass
        try:
            self._adapter.disconnect()
        except Exception:
            pass
        self.shutdown_complete.emit()

    @Slot(object)
    def invoke_for_run(self, request: _RunCall) -> None:
        """Run a lease call in this worker's adapter-owning thread."""

        try:
            if self._run_access is not None:
                with self._run_access.enter(request.owner, request.member, request.args, request.kwargs):
                    self._invoke_member(request)
            else:
                self._invoke_member(request)
        except BaseException as exc:
            request.error = exc
        finally:
            self.state_changed.emit(self._adapter.state.value)
            request.completed.set()

    def _invoke_member(self, request: _RunCall) -> None:
        member = getattr(self._adapter, request.member)
        if request.read_attribute:
            if request.args or request.kwargs:
                raise ValueError("A run attribute request cannot include arguments.")
            request.result = member
        else:
            if not callable(member):
                raise TypeError(f"Adapter member {request.member!r} is not callable.")
            if request.timeout_s is not None and hasattr(self._adapter, "io_timeout"):
                with self._adapter.io_timeout(request.timeout_s):
                    request.result = member(*request.args, **request.kwargs)
            else:
                request.result = member(*request.args, **request.kwargs)

    def _dispatch(self, operation: str, payload: object) -> object:
        if operation == "replace_adapter":
            if not isinstance(payload, DeviceAdapter):
                raise ValueError("replace_adapter requires an instrument adapter.")
            failure: Exception | None = None
            try:
                self._adapter.emergency_off()
            except Exception as exc:
                failure = exc
            try:
                self._adapter.disconnect()
            except Exception as exc:
                failure = failure or exc
            self._adapter = payload
            self._attach_traffic_logger()
            if failure is not None:
                raise failure
            return None
        if operation == "test_communication":
            if self._adapter.state.value != "disconnected":
                raise ValueError("Communication test requires a disconnected instrument.")
            identity = None
            try:
                identity = self._adapter.connect()
                capabilities = self._adapter.capabilities
                return {
                    "idn": identity.idn,
                    "vendor": identity.manufacturer,
                    "model": identity.model,
                    "serial": identity.serial,
                    "firmware": identity.firmware,
                    "features": tuple(capabilities.features),
                    "hardware_options": capabilities.hardware_options,
                }
            finally:
                try:
                    self._adapter.emergency_off()
                except Exception:
                    pass
                try:
                    self._adapter.disconnect()
                except Exception:
                    pass
        if operation == "connect":
            return self._adapter.connect()
        if operation == "disconnect":
            return self._adapter.disconnect()
        if operation == "emergency_off":
            return self._adapter.emergency_off()
        if operation == "refresh_station_context":
            return self._adapter.refresh_station_context(payload)
        if operation == "apply_limit_settings":
            return self._adapter.apply_limit_settings(payload)
        if self._dispatcher is not None:
            return self._dispatcher(self._adapter, operation, payload)
        # Compatibility path for existing callers that construct a controller
        # without a module manifest. New code must pass DeviceModule.dispatch.
        if isinstance(self._adapter, RigolAdapter):
            if operation == "configure":
                return self._adapter.configure_channel(payload)  # type: ignore[arg-type]
            if operation == "configure_output":
                return self._adapter.configure_output(payload)  # type: ignore[arg-type]
            if operation == "set_output":
                channel, enabled = payload  # type: ignore[misc]
                return self._adapter.set_output(channel, enabled)
            if operation == "set_output_group":
                channels, enabled = payload  # type: ignore[misc]
                return self._adapter.set_output_group(tuple(channels), bool(enabled))
            if operation == "configure_modulation":
                return self._adapter.configure_modulation(payload)  # type: ignore[arg-type]
            if operation == "configure_sweep":
                return self._adapter.configure_frequency_sweep(payload)  # type: ignore[arg-type]
            if operation == "trigger_sweep":
                return self._adapter.trigger_frequency_sweep(payload)  # type: ignore[arg-type]
            if operation == "configure_burst":
                return self._adapter.configure_burst(payload)  # type: ignore[arg-type]
            if operation == "trigger_burst":
                return self._adapter.trigger_burst(payload)  # type: ignore[arg-type]
            if operation == "synchronize_phases":
                return self._adapter.synchronize_phases()
        if isinstance(self._adapter, KeithleyAdapter):
            if operation == "configure":
                return self._adapter.configure_source(payload)  # type: ignore[arg-type]
            if operation == "read_configuration":
                return self._adapter.read_configuration()
            if operation == "set_output":
                channel, enabled = payload  # type: ignore[misc]
                return self._adapter.set_output(channel, enabled)
            if operation == "measure":
                return self._adapter.measure(payload)  # type: ignore[arg-type]
            if operation == "recover_from_compliance":
                channel, choice = payload  # type: ignore[misc]
                return self._adapter.recover_from_compliance(channel, choice)
            if operation == "set_compliance_policy":
                channel, stop_on_compliance = payload  # type: ignore[misc]
                return self._adapter.set_compliance_policy(
                    channel, stop_on_compliance
                )
            if operation == "ramp_to_zero":
                return self._adapter.ramp_to_zero(payload)  # type: ignore[arg-type]
            if operation == "ramp_to_level":
                return self._adapter.ramp_to_level(payload)  # type: ignore[arg-type]
        if isinstance(self._adapter, AnritsuAdapter):
            if operation == "read_configuration":
                return self._adapter.read_current_configuration()
            if operation == "read_advanced_spectrum":
                return self._adapter.read_advanced_spectrum_configuration()
            if operation == "configure_advanced_spectrum":
                return self._adapter.configure_advanced_spectrum(payload)  # type: ignore[arg-type]
            if operation == "configure":
                return self._adapter.configure_spectrum(payload)  # type: ignore[arg-type]
            if operation == "start_live":
                return self._adapter.start_live(bool(payload))
            if operation == "stop_live":
                return self._adapter.stop_live()
            if operation == "fetch_trace":
                return self._adapter.fetch_trace(str(payload or "TRAC1"))
            if operation == "fetch_current_trace":
                return self._adapter.fetch_current_trace(str(payload or "TRAC1"))
            if operation == "single_sweep":
                return self._adapter.acquire_single_sweep(str(payload or "TRAC1"))
            if operation == "read_signal_generator":
                return self._adapter.read_signal_generator_configuration()
            if operation == "configure_signal_generator":
                return self._adapter.configure_signal_generator(payload)  # type: ignore[arg-type]
            if operation == "set_signal_generator_output":
                return self._adapter.set_signal_generator_output(bool(payload))
        raise ValueError(f"Unsupported operation {operation!r}.")


class DeviceController(QObject):
    """Thread-safe façade used by pages in the main window."""

    request = Signal(str, object)
    result = Signal(str, object)
    error = Signal(str, str)
    state_changed = Signal(str)
    capabilities_changed = Signal(object)
    traffic = Signal(str)
    run_request = Signal(object)
    reservation_changed = Signal(bool)

    def __init__(
        self,
        adapter: DeviceAdapter,
        parent: QObject | None = None,
        *,
        dispatcher: OperationDispatcher | None = None,
    ) -> None:
        super().__init__(parent)
        self._operation_guard: Callable[[str, object], None] | None = None
        self._thread = QThread(self)
        self._worker = InstrumentWorker(adapter, dispatcher=dispatcher)
        self._run_access = _RunAccess()
        self._worker._run_access = self._run_access
        self._worker.moveToThread(self._thread)
        self.request.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self.result)
        self._worker.failed.connect(self.error)
        self._worker.state_changed.connect(self.state_changed)
        self._worker.capabilities_changed.connect(self.capabilities_changed)
        self._worker.traffic.connect(self.traffic)
        self.run_request.connect(
            self._worker.invoke_for_run,
            Qt.ConnectionType.QueuedConnection,
        )
        self._thread.start()

    def call(self, operation: str, payload: object = None) -> None:
        if self._operation_guard is not None:
            try:
                self._operation_guard(operation, payload)
            except Exception as exc:
                self.error.emit(operation, str(exc))
                return
        self.request.emit(operation, payload)

    def set_operation_guard(self, guard: Callable[[str, object], None] | None) -> None:
        """Install a GUI-thread interlock evaluated before a command is queued."""

        self._operation_guard = guard

    def adapter_for_run(self) -> RunDeviceAdapter:
        """Return an adapter-shaped proxy without transferring thread ownership."""

        return RunDeviceAdapter(self)

    def acquire_run_lease(self) -> RunDeviceAdapter:
        """Reserve the whole instrument, preventing interleaved channel commands."""
        owner, interruption = self._run_access.acquire()
        self.reservation_changed.emit(True)
        return RunDeviceAdapter(self, owner, interruption)

    def release_run_lease(self, owner: object) -> None:
        self._run_access.release(owner)
        self.reservation_changed.emit(False)

    def call_for_run(
        self,
        method: str,
        *args: object,
        timeout_s: float | None = None,
        owner: object | None = None,
        **kwargs: object,
    ) -> object:
        """Synchronously invoke one adapter method for an active recipe run."""

        self._run_access.assert_open()
        request = _RunCall(method, tuple(args), dict(kwargs), timeout_s=timeout_s, owner=owner)
        self.run_request.emit(request)
        deadline_s = (timeout_s + 15.0) if timeout_s is not None else 60.0
        if not request.completed.wait(timeout=deadline_s):
            raise TimeoutError(
                f"Call to {method!r} on instrument worker timed out after {deadline_s:.1f} s."
            )
        if request.error is not None:
            raise request.error
        return request.result

    def read_for_run(self, attribute: str, *, owner: object | None = None) -> object:
        """Read one adapter property through its owning worker thread."""

        self._run_access.assert_open()
        request = _RunCall(attribute, read_attribute=True, owner=owner)
        self.run_request.emit(request)
        deadline_s = 10.0
        if not request.completed.wait(timeout=deadline_s):
            raise TimeoutError(
                f"Reading attribute {attribute!r} on instrument worker timed out after {deadline_s:.1f} s."
            )
        if request.error is not None:
            raise request.error
        return request.result

    def reconfigure(self, adapter: DeviceAdapter) -> None:
        """Safely discard the session before applying a newly saved profile."""

        self.call("replace_adapter", adapter)

    def close(self) -> bool:
        self._run_access.begin_shutdown()
        if self._thread.isRunning():
            wait_loop = QEventLoop()
            self._worker.shutdown_complete.connect(wait_loop.quit)
            QMetaObject.invokeMethod(
                self._worker,
                "shutdown",
                Qt.ConnectionType.QueuedConnection,
            )
            QTimer.singleShot(3_000, wait_loop.quit)
            wait_loop.exec()
            try:
                self._worker.shutdown_complete.disconnect(wait_loop.quit)
            except (RuntimeError, TypeError):
                pass
        try:
            self.request.disconnect(self._worker.execute)
        except (RuntimeError, TypeError):
            pass
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.quit()
        return self._thread.wait(3_000)

    @classmethod
    def close_all(cls, controllers: Iterable[DeviceController], timeout_ms: int = 3_000) -> bool:
        """Shut down multiple device controllers concurrently instead of sequentially."""
        controllers_list = list(controllers)
        for controller in controllers_list:
            controller._run_access.begin_shutdown()
        active = [c for c in controllers_list if c._thread.isRunning()]
        if not active:
            return all(c.close() for c in controllers_list)

        wait_loop = QEventLoop()
        remaining = set(active)

        def make_handler(ctrl: DeviceController) -> Callable[[], None]:
            def _on_shutdown() -> None:
                remaining.discard(ctrl)
                if not remaining:
                    wait_loop.quit()
            return _on_shutdown

        handlers: dict[DeviceController, Callable[[], None]] = {}
        for c in active:
            handler = make_handler(c)
            handlers[c] = handler
            c._worker.shutdown_complete.connect(handler)
            QMetaObject.invokeMethod(
                c._worker,
                "shutdown",
                Qt.ConnectionType.QueuedConnection,
            )

        QTimer.singleShot(timeout_ms, wait_loop.quit)
        if remaining:
            wait_loop.exec()

        for c, handler in handlers.items():
            try:
                c._worker.shutdown_complete.disconnect(handler)
            except (RuntimeError, TypeError):
                pass

        all_stopped = True
        for c in controllers_list:
            try:
                self_request = getattr(c, "request", None)
                if self_request is not None:
                    self_request.disconnect(c._worker.execute)
            except (RuntimeError, TypeError):
                pass
            c._thread.finished.connect(c._worker.deleteLater)
            c._thread.quit()
            if not c._thread.wait(2_000):
                all_stopped = False

        return all_stopped
