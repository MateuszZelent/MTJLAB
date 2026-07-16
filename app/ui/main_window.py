"""Main PySide6 application window and manual-control pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.devices.anritsu import AnritsuAdapter, SpectrumConfig, SpectrumTrace
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import RigolAdapter, RigolChannelConfig
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.engine.compiler import RecipeCompiler
from app.recipes import load_recipe
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.ui.settings_page import SettingsPage
from app.ui.run_worker import RunController
from app.ui.workers import DeviceController


def _line(value: str, width: int = 14) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setMinimumWidth(width * 8)
    return edit


class DeviceCard(QFrame):
    connect_requested = Signal()
    disconnect_requested = Signal()

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        name = QLabel(title)
        name.setObjectName("cardTitle")
        self.state = QLabel("DISCONNECTED")
        self.state.setObjectName("stateDisconnected")
        self.identity = QLabel(resource or "Brak zasobu VISA")
        self.identity.setWordWrap(True)
        self.identity.setObjectName("muted")
        controls = QHBoxLayout()
        connect = QPushButton("Połącz")
        disconnect = QPushButton("Rozłącz")
        controls.addWidget(connect)
        controls.addWidget(disconnect)
        layout.addWidget(name)
        layout.addWidget(self.state)
        layout.addWidget(self.identity)
        layout.addStretch(1)
        layout.addLayout(controls)
        connect.clicked.connect(self.connect_requested)
        disconnect.clicked.connect(self.disconnect_requested)

    def update_state(self, state: str) -> None:
        self.state.setText(state.upper())
        self.state.setObjectName("state" + "".join(part.title() for part in state.split("_")))
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def update_identity(self, value: object) -> None:
        idn = getattr(value, "idn", None)
        if idn:
            self.identity.setText(str(idn))


class DashboardPage(QWidget):
    emergency_requested = Signal()

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Stanowisko pomiarowe")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Połącz urządzenia, sprawdź profil i dopiero potem przygotuj recepturę.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        grid = QGridLayout()
        self.cards = {
            "rigol": DeviceCard(settings.rigol.display_name, settings.rigol.connection.resource),
            "keithley": DeviceCard(settings.keithley.display_name, settings.keithley.connection.resource),
            "anritsu": DeviceCard(settings.anritsu.display_name, settings.anritsu.connection.resource),
        }
        for column, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, column)
        layout.addLayout(grid)
        self.checklist = QLabel()
        self.checklist.setObjectName("checklist")
        self.checklist.setWordWrap(True)
        layout.addWidget(self.checklist)
        emergency = QPushButton("E-STOP — wyłącz wszystkie wyjścia")
        emergency.setObjectName("emergencyButton")
        emergency.setMinimumHeight(44)
        layout.addWidget(emergency)
        layout.addStretch(1)
        emergency.clicked.connect(self.emergency_requested)
        self.update_settings(settings)

    def update_settings(self, settings: StationSettings) -> None:
        profile = "✓ zatwierdzony" if not settings.outputs_locked else "✕ niezaufany — wyjścia zablokowane"
        rigol_serial = "✓" if settings.rigol.identity.require_serial_match else "✕"
        anritsu = "✓" if settings.anritsu.safety.acquisition_allowed else "✕ wymaga limitu wejścia RF"
        self.checklist.setText(
            "Gotowość:\n"
            f"• Profil: {profile}\n"
            f"• Rigol przypięty do numeru seryjnego: {rigol_serial}\n"
            f"• Anritsu akwizycja: {anritsu}\n"
            "• Przed OUTPUT ON zadeklaruj DUT i sprawdź wszystkie limity."
        )


class RigolPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Rigol DG1032Z")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.channel = QComboBox()
        self.channel.addItems(["1", "2"])
        self.waveform = QComboBox()
        self.waveform.addItems(["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"])
        self.waveform.setCurrentText("SQU")
        self.frequency = _line("1 kHz")
        self.high_level = _line("1 mV")
        self.low_level = _line("-1 mV")
        self.load = _line("HIGHZ")
        self.dut_impedance = _line("50 ohm")
        self.phase = _line("0")
        self.duty = _line("50")
        for label, widget in (
            ("Kanał", self.channel),
            ("Przebieg", self.waveform),
            ("Częstotliwość", self.frequency),
            ("HighL", self.high_level),
            ("LowL", self.low_level),
            ("Obciążenie ustawione w generatorze", self.load),
            ("Minimalna impedancja DUT", self.dut_impedance),
            ("Faza [deg]", self.phase),
            ("Duty [%] — dla SQU", self.duty),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        configure = QPushButton("Waliduj i zastosuj przy OUTPUT OFF")
        output_on = QPushButton("ARM / OUTPUT ON")
        output_off = QPushButton("OUTPUT OFF")
        buttons.addWidget(configure)
        buttons.addWidget(output_on)
        buttons.addWidget(output_off)
        layout.addLayout(buttons)
        self.estimate = QLabel("Szacowany prąd: —")
        self.estimate.setObjectName("muted")
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        layout.addStretch(1)
        configure.clicked.connect(self.configure)
        output_on.clicked.connect(lambda: self._controller.call("set_output", (int(self.channel.currentText()), True)))
        output_off.clicked.connect(lambda: self._controller.call("set_output", (int(self.channel.currentText()), False)))
        controller.result.connect(self._result)
        controller.error.connect(self._error)

    def configure(self) -> None:
        try:
            config = RigolChannelConfig(
                channel=int(self.channel.currentText()),
                waveform=self.waveform.currentText(),
                frequency_hz=parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value,
                high_level_v=parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value,
                low_level_v=parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value,
                output_load=self.load.text().strip(),
                phase_deg=float(self.phase.text().replace(",", ".")),
                square_duty_percent=float(self.duty.text().replace(",", ".")) if self.waveform.currentText() == "SQU" else None,
                dut_min_impedance_ohm=parse_quantity(self.dut_impedance.text(), DIMENSION_RESISTANCE).si_value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Nieprawidłowe dane", str(exc))
            return
        self._controller.call("configure", config)

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure" and hasattr(result, "peak_absolute_current_a"):
            estimate = result
            self.estimate.setText(
                "Szacowany prąd obciążenia (nie pomiar): "
                f"{estimate.peak_absolute_current_a * 1e3:.6g} mA; "
                f"Vth High/Low: {estimate.open_circuit_high_v:.6g} / {estimate.open_circuit_low_v:.6g} V"
            )
            self.status.emit("Rigol skonfigurowany przy OUTPUT OFF")

    def _error(self, operation: str, error: str) -> None:
        if operation in {"configure", "set_output"}:
            QMessageBox.warning(self, "Rigol", error)


class KeithleyPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Keithley 2600 — SMU")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.channel = QComboBox()
        self.channel.addItems(["A", "B"])
        self.channel.setCurrentText("B")
        self.mode = QComboBox()
        self.mode.addItems(["current", "voltage", "measure_only"])
        self.level = _line("1 mA")
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle = _line("100 ms")
        for label, widget in (
            ("Kanał", self.channel),
            ("Tryb źródła", self.mode),
            ("Poziom", self.level),
            ("Compliance", self.compliance),
            ("NPLC", self.nplc),
            ("Czas ustalania", self.settle),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        configure = QPushButton("Ustaw źródło przy OUTPUT OFF")
        measure = QPushButton("Zmierz I / V")
        on = QPushButton("ARM / OUTPUT ON")
        off = QPushButton("Ramp to zero + OFF")
        for button in (configure, measure, on, off):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.readout = QLabel("I: —   V: —   P: —")
        self.readout.setObjectName("readout")
        layout.addWidget(self.readout)
        layout.addStretch(1)
        configure.clicked.connect(self.configure)
        measure.clicked.connect(lambda: self._controller.call("measure", self.channel.currentText()))
        on.clicked.connect(lambda: self._controller.call("set_output", (self.channel.currentText(), True)))
        off.clicked.connect(lambda: self._controller.call("ramp_to_zero", self.channel.currentText()))
        controller.result.connect(self._result)
        controller.error.connect(self._error)

    def configure(self) -> None:
        try:
            mode = self.mode.currentText()
            level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
            compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
            request = KeithleySourceRequest(
                channel=self.channel.currentText(),  # type: ignore[arg-type]
                mode=mode,  # type: ignore[arg-type]
                level_si=0.0 if mode == "measure_only" else parse_quantity(self.level.text(), level_dimension).si_value,
                compliance_si=0.0 if mode == "measure_only" else parse_quantity(self.compliance.text(), compliance_dimension).si_value,
                nplc=float(self.nplc.text().replace(",", ".")),
                settle_time_s=parse_quantity(self.settle.text(), "time").si_value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Nieprawidłowe dane", str(exc))
            return
        self._controller.call("configure", request)

    def _result(self, operation: str, result: object) -> None:
        if operation == "measure" and hasattr(result, "current_a"):
            measurement = result
            self.readout.setText(
                f"I: {measurement.current_a * 1e3:.8g} mA   "
                f"V: {measurement.voltage_v * 1e3:.8g} mV   P: {measurement.power_w * 1e6:.8g} µW"
            )
            self.status.emit("Odczyt Keithley zakończony")
        elif operation == "configure":
            self.status.emit("Keithley skonfigurowany przy OUTPUT OFF")

    def _error(self, operation: str, error: str) -> None:
        if operation in {"configure", "measure", "set_output", "ramp_to_zero"}:
            QMessageBox.warning(self, "Keithley", error)


class AnritsuPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._fetch_pending = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        title = QLabel("Anritsu MS2830A — Spectrum / Live")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = QSpinBox()
        self.points.setRange(101, 10001)
        self.points.setValue(1001)
        self.refresh = QSpinBox()
        self.refresh.setRange(100, 5000)
        self.refresh.setValue(500)
        self.refresh.setSuffix(" ms")
        for label, widget in (
            ("Start", self.start),
            ("Stop", self.stop),
            ("Reference level", self.reference),
            ("Punkty", self.points),
            ("Odświeżanie Live", self.refresh),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        controls = QHBoxLayout()
        configure = QPushButton("Zastosuj konfigurację")
        self.live = QPushButton("Start Live")
        abort = QPushButton("Abort")
        controls.addWidget(configure)
        controls.addWidget(self.live)
        controls.addWidget(abort)
        layout.addLayout(controls)
        self.series = QLineSeries()
        self.chart = QChart()
        self.chart.addSeries(self.series)
        self.chart.legend().hide()
        self.chart.setTitle("Aktualne widmo")
        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Częstotliwość [MHz]")
        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Moc [dBm]")
        self.chart.addAxis(self.axis_x, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignmentFlag.AlignLeft)
        self.series.attachAxis(self.axis_x)
        self.series.attachAxis(self.axis_y)
        view = QChartView(self.chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(300)
        layout.addWidget(view, 1)
        self.info = QLabel("Live zatrzymany. Każda klatka to pełny trace, nie strumień push.")
        self.info.setObjectName("muted")
        layout.addWidget(self.info)
        configure.clicked.connect(self.configure)
        self.live.clicked.connect(self.toggle_live)
        abort.clicked.connect(lambda: self._controller.call("emergency_off"))
        controller.result.connect(self._result)
        controller.error.connect(self._error)

    def configure(self) -> None:
        try:
            config = SpectrumConfig(
                start_hz=parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value,
                stop_hz=parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value,
                reference_level_dbm=parse_quantity(self.reference.text(), DIMENSION_DBM).si_value,
                points=self.points.value(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Nieprawidłowe dane", str(exc))
            return
        self._controller.call("configure", config)

    def toggle_live(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._controller.call("stop_live")
            self.live.setText("Start Live")
            self.info.setText("Live zatrzymany.")
            return
        self._timer.setInterval(self.refresh.value())
        self._controller.call("start_live")

    def fetch_live(self) -> None:
        if not self._fetch_pending:
            self._fetch_pending = True
            self._controller.call("fetch_trace", "TRAC1")

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure":
            self.status.emit("Anritsu skonfigurowany")
        elif operation == "start_live":
            self._timer.start()
            self.live.setText("Stop Live")
            self.status.emit("Anritsu Live uruchomiony")
        elif operation == "fetch_trace" and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
            self._show_trace(result)

    def _show_trace(self, trace: SpectrumTrace) -> None:
        limit = 1_000
        stride = max(1, len(trace.powers_dbm) // limit)
        points = [QPointF(trace.frequencies_hz[index] / 1e6, trace.powers_dbm[index]) for index in range(0, len(trace.powers_dbm), stride)]
        self.series.replace(points)
        self.axis_x.setRange(points[0].x(), points[-1].x())
        self.axis_y.setRange(min(point.y() for point in points) - 2, max(point.y() for point in points) + 2)
        self.info.setText(
            f"{len(trace.powers_dbm)} punktów • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm"
        )

    def _error(self, operation: str, error: str) -> None:
        if operation == "fetch_trace":
            self._fetch_pending = False
        if operation in {"configure", "start_live", "fetch_trace", "emergency_off"}:
            self._timer.stop()
            self.live.setText("Start Live")
            QMessageBox.warning(self, "Anritsu", error)


class RecipePage(QWidget):
    status = Signal(str)
    run_requested = Signal(object)

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._plan = None
        layout = QVBoxLayout(self)
        title = QLabel("Receptury pomiarowe")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        path_line = QHBoxLayout()
        self.path = _line("recipes/example_nested_sweep.yml", 42)
        compile_button = QPushButton("Wczytaj i skompiluj")
        self.run_button = QPushButton("Uruchom plan")
        self.run_button.setEnabled(False)
        path_line.addWidget(self.path, 1)
        path_line.addWidget(compile_button)
        path_line.addWidget(self.run_button)
        layout.addLayout(path_line)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Węzeł", "Typ / szczegóły"])
        layout.addWidget(self.tree, 1)
        self.summary = QLabel("Receptura nie została skompilowana.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        compile_button.clicked.connect(self.compile_recipe)
        self.run_button.clicked.connect(self.request_run)

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self._plan = None
        self.run_button.setEnabled(False)

    def compile_recipe(self) -> None:
        try:
            recipe = load_recipe(Path(self.path.text()))
            plan = RecipeCompiler(self._settings).compile(recipe)
        except Exception as exc:
            QMessageBox.warning(self, "Receptura", str(exc))
            return
        self.tree.clear()
        self._plan = plan
        self.run_button.setEnabled(True)
        for action in plan.actions:
            item = QTreeWidgetItem([action.node_id, action.kind])
            item.setToolTip(1, str(action.setpoints_si))
            self.tree.addTopLevelItem(item)
        self.summary.setText(
            f"Plan: {len(plan.actions)} akcji • {plan.total_points} widm • hash {plan.sha256}\n"
            "Kompilacja nie wysyła komend do urządzeń. Uruchomienie wymaga zatwierdzonego profilu i Run Engine."
        )
        self.status.emit("Receptura została skompilowana")

    def request_run(self) -> None:
        if self._plan is not None:
            self.run_requested.emit(self._plan)


class RunMonitorPage(QWidget):
    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Wykonanie receptury")
        title.setObjectName("pageTitle")
        self.state = QLabel("IDLE")
        self.state.setObjectName("readout")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        controls = QHBoxLayout()
        pause = QPushButton("Pauza po punkcie")
        resume = QPushButton("Wznów")
        stop = QPushButton("Stop safely")
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addWidget(stop)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        layout.addWidget(title)
        layout.addWidget(self.state)
        layout.addWidget(self.progress)
        layout.addLayout(controls)
        layout.addWidget(self.events, 1)
        pause.clicked.connect(self.pause_requested)
        resume.clicked.connect(self.resume_requested)
        stop.clicked.connect(self.stop_requested)

    def run_started(self, actions: int) -> None:
        self.state.setText("RUNNING")
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.events.clear()

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "action_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
        if name == "pause_pending":
            self.state.setText("PAUSED")
        elif name == "run_fault":
            self.state.setText("FAULT")
        self.events.appendPlainText(f"{name}: {data}")

    def complete(self, result: object) -> None:
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} punktów")
        self.events.appendPlainText(f"Plik: {result['path']}")

    def failed(self, error: str) -> None:
        self.state.setText("FAULT")
        self.events.appendPlainText(error)


class ResultsPage(QWidget):
    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        layout = QVBoxLayout(self)
        title = QLabel("Wyniki")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.listing = QPlainTextEdit()
        self.listing.setReadOnly(True)
        layout.addWidget(self.listing, 1)
        refresh = QPushButton("Odśwież listę plików")
        layout.addWidget(refresh)
        refresh.clicked.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        if not self._output_dir.exists():
            self.listing.setPlainText(f"Katalog wyników nie istnieje jeszcze: {self._output_dir}")
            return
        files = sorted(self._output_dir.glob("*.h5"), key=lambda item: item.stat().st_mtime, reverse=True)
        self.listing.setPlainText("\n".join(str(item) for item in files) or "Brak plików HDF5.")


class MainWindow(QMainWindow):
    """Local Qt client with manual control, live spectrum and safe settings."""

    def __init__(self, settings_path: str | Path = ".config/settings.yml") -> None:
        super().__init__()
        self._repository = SettingsRepository(settings_path)
        self._settings = self._repository.load().settings
        self.setWindowTitle("Lab Control — Rigol · Keithley · Anritsu")
        self.resize(1360, 880)
        self._controllers = {
            "rigol": DeviceController(RigolAdapter(self._settings), self),
            "keithley": DeviceController(KeithleyAdapter(self._settings), self),
            "anritsu": DeviceController(AnritsuAdapter(self._settings), self),
        }
        self._device_states = {"rigol": "disconnected", "keithley": "disconnected", "anritsu": "disconnected"}
        self._run_controller = RunController(self)
        self._build()
        self._connect_controllers()

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self._settings)
        self.rigol_page = RigolPage(self._controllers["rigol"])
        self.keithley_page = KeithleyPage(self._controllers["keithley"])
        self.anritsu_page = AnritsuPage(self._controllers["anritsu"])
        self.recipe_page = RecipePage(self._settings)
        self.run_monitor = RunMonitorPage()
        self.results_page = ResultsPage(str(self._settings.storage.get("output_directory", "./measurements")))
        self.settings_page = SettingsPage(self._repository)
        for widget, name in (
            (self.dashboard, "Dashboard"),
            (self.rigol_page, "Rigol"),
            (self.keithley_page, "Keithley"),
            (self.anritsu_page, "Anritsu"),
            (self.recipe_page, "Receptury"),
            (self.run_monitor, "Wykonanie"),
            (self.results_page, "Wyniki"),
            (self.settings_page, "Ustawienia"),
        ):
            self.tabs.addTab(widget, name)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(130)
        self.setStatusBar(self.statusBar())
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_dock())
        self.dashboard.emergency_requested.connect(self._emergency_off_all)
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.recipe_page.run_requested.connect(self._start_run)
        self.run_monitor.stop_requested.connect(self._run_controller.request_stop)
        self.run_monitor.pause_requested.connect(self._run_controller.request_pause)
        self.run_monitor.resume_requested.connect(self._run_controller.request_resume)
        self._run_controller.event.connect(self._run_event)
        self._run_controller.finished.connect(self._run_finished)
        self._run_controller.failed.connect(self._run_failed)
        for page in (self.rigol_page, self.keithley_page, self.anritsu_page, self.recipe_page, self.settings_page):
            page.status.connect(self._log)
        menu = self.menuBar().addMenu("Aplikacja")
        quit_action = QAction("Zamknij bezpiecznie", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    def _log_dock(self):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("Dziennik zdarzeń", self)
        dock.setWidget(self.log)
        return dock

    def _connect_controllers(self) -> None:
        for name, controller in self._controllers.items():
            card = self.dashboard.cards[name]
            card.connect_requested.connect(lambda current=controller: current.call("connect"))
            card.disconnect_requested.connect(lambda current=controller: current.call("disconnect"))
            controller.state_changed.connect(card.update_state)
            controller.state_changed.connect(lambda state, device=name: self._set_device_state(device, state))
            controller.result.connect(lambda operation, result, current=card: self._device_result(current, operation, result))
            controller.error.connect(lambda operation, error, device=name: self._device_error(device, operation, error))

    def _device_result(self, card: DeviceCard, operation: str, result: object) -> None:
        if operation == "connect":
            card.update_identity(result)
            self._log(f"Połączono: {getattr(result, 'idn', result)}")
        elif operation == "disconnect":
            self._log("Urządzenie rozłączone")

    def _device_error(self, device: str, operation: str, error: str) -> None:
        self._log(f"{device}/{operation}: {error}")

    def _set_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state

    def _start_run(self, plan: object) -> None:
        if self._settings.outputs_locked:
            QMessageBox.warning(
                self,
                "Profil niezaufany",
                "Przed uruchomieniem receptury zatwierdź profil w zakładce Ustawienia.",
            )
            return
        connected = [name for name, state in self._device_states.items() if state != "disconnected"]
        if connected:
            QMessageBox.warning(
                self,
                "Rozłącz sterowanie ręczne",
                "Run Engine otwiera własne sesje VISA. Najpierw rozłącz: " + ", ".join(connected) + ".",
            )
            return
        try:
            self._run_controller.start(self._settings, self._repository.path, plan)  # type: ignore[arg-type]
        except Exception as exc:
            QMessageBox.critical(self, "Nie uruchomiono", str(exc))
            return
        self.run_monitor.run_started(plan.actions)  # type: ignore[union-attr]
        self.tabs.setCurrentWidget(self.run_monitor)
        self._log("Uruchomiono Run Engine")

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        self.run_monitor.append_event(name, payload)

    def _run_finished(self, result: object) -> None:
        self.run_monitor.complete(result)
        self.results_page.refresh()
        self._log("Run Engine zakończył pomiar")

    def _run_failed(self, error: str) -> None:
        self.run_monitor.failed(error)
        self._log(f"Run Engine: {error}")
        QMessageBox.critical(self, "Run Engine", error)

    def _emergency_off_all(self) -> None:
        answer = QMessageBox.warning(
            self,
            "E-STOP",
            "Wyłączyć wyjścia wszystkich urządzeń i przerwać akwizycję?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            for controller in self._controllers.values():
                controller.call("emergency_off")
            self._log("Wysłano E-STOP do wszystkich urządzeń")

    def _settings_saved(self, settings: StationSettings) -> None:
        self._settings = settings
        self.dashboard.update_settings(settings)
        self.recipe_page.set_settings(settings)
        self._log("Zmieniono profil. Adaptery połączeń użyją nowych limitów po bezpiecznym restarcie aplikacji.")

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.statusBar().showMessage(message, 8_000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.anritsu_page._timer.stop()
        self._run_controller.close()
        for controller in self._controllers.values():
            controller.close()
        event.accept()
