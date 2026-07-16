"""Graficzny panel sterowania generatorem Rigol DG1032Z."""

from __future__ import annotations

import argparse
import math
import queue
import random
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

from dg1032z import (
    DEFAULT_ADDRESS,
    MODULATION_TYPES,
    WAVEFORMS,
    DG1032Z,
)


BG = "#10151d"
PANEL = "#18212c"
PANEL_ALT = "#202b38"
TEXT = "#e8edf3"
MUTED = "#91a0b2"
ACCENT = "#4cc2ff"
GREEN = "#38d996"
RED = "#ff657a"
YELLOW = "#ffcc66"


def display_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "0"
    if abs(number) >= 1e6 or abs(number) < 1e-3:
        return f"{number:.9g}"
    return f"{number:g}"


@dataclass
class Task:
    label: str
    operation: Callable[[DG1032Z], Any]
    success: Callable[[Any], None] | None
    error: Callable[[Exception], None] | None


class VisaWorker:
    """Wykonuje wszystkie operacje VISA sekwencyjnie poza watkiem GUI."""

    def __init__(self) -> None:
        self.tasks: queue.Queue[Task | None] = queue.Queue()
        self.results: queue.Queue[tuple[Task, Any, Exception | None]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, task: Task) -> None:
        self.tasks.put(task)

    def _run(self) -> None:
        instrument = DG1032Z()
        while True:
            task = self.tasks.get()
            if task is None:
                instrument.disconnect()
                return
            try:
                result = task.operation(instrument)
                self.results.put((task, result, None))
            except Exception as exc:  # blad jest prezentowany w glownym watku
                self.results.put((task, None, exc))

    def close(self) -> None:
        self.tasks.put(None)


def labeled_entry(
    parent: tk.Misc,
    row: int,
    label: str,
    variable: tk.Variable,
    unit: str = "",
    width: int = 15,
) -> ttk.Entry:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
    entry = ttk.Entry(parent, textvariable=variable, width=width)
    entry.grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=5)
    ttk.Label(parent, text=unit, style="Muted.TLabel").grid(
        row=row, column=2, sticky="w", padx=(2, 8), pady=5
    )
    return entry


def labeled_combo(
    parent: tk.Misc,
    row: int,
    label: str,
    variable: tk.Variable,
    values: tuple[str, ...],
    width: int = 13,
) -> ttk.Combobox:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
    combo = ttk.Combobox(
        parent, textvariable=variable, values=values, state="readonly", width=width
    )
    combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
    return combo


class ChannelPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "GeneratorApp", channel: int) -> None:
        super().__init__(parent, padding=14)
        self.app = app
        self.channel = channel
        self.vars: dict[str, tk.Variable] = {
            "waveform": tk.StringVar(value="SIN"),
            "frequency": tk.StringVar(value="10000"),
            "phase": tk.StringVar(value="0"),
            "high_level": tk.StringVar(value="2.5"),
            "low_level": tk.StringVar(value="-2.5"),
            "dc_level": tk.StringVar(value="0"),
            "square_duty": tk.StringVar(value="50"),
            "ramp_symmetry": tk.StringVar(value="50"),
            "pulse_width": tk.StringVar(value="0.0005"),
            "pulse_leading": tk.StringVar(value="0.00000002"),
            "pulse_trailing": tk.StringVar(value="0.00000002"),
            "safe_output_off": tk.BooleanVar(value=True),
            "output": tk.BooleanVar(value=False),
            "load": tk.StringVar(value="50"),
            "polarity": tk.StringVar(value="NORM"),
            "output_mode": tk.StringVar(value="NORM"),
            "gate_polarity": tk.StringVar(value="POS"),
            "sync": tk.BooleanVar(value=True),
            "sync_polarity": tk.StringVar(value="POS"),
            "sync_delay": tk.StringVar(value="0"),
        }
        self.shape_widgets: dict[str, list[ttk.Entry]] = {}
        self.basic_widgets: dict[str, ttk.Entry] = {}
        self._build()
        self.vars["waveform"].trace_add("write", self._waveform_changed)
        self.vars["output"].trace_add("write", lambda *_: self._update_output_button())
        self._waveform_changed()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Label(toolbar, text=f"Kanał {self.channel}", style="Title.TLabel").pack(
            side="left"
        )
        ttk.Button(toolbar, text="Odczytaj wszystko", command=self.read).pack(
            side="right", padx=(8, 0)
        )
        self.output_button = ttk.Button(toolbar, command=self.toggle_output)
        self.output_button.pack(side="right")
        self._update_output_button()

        content = ttk.Frame(self)
        content.pack(fill="both", expand=True)
        content.columnconfigure((0, 1, 2), weight=1, uniform="channel")

        basic = ttk.LabelFrame(content, text="Przebieg", padding=8)
        shape = ttk.LabelFrame(content, text="Parametry kształtu", padding=8)
        output = ttk.LabelFrame(content, text="Wyjście i Sync", padding=8)
        basic.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        shape.grid(row=0, column=1, sticky="nsew", padx=6)
        output.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        for frame in (basic, shape, output):
            frame.columnconfigure(1, weight=1)

        labeled_combo(basic, 0, "Kształt", self.vars["waveform"], WAVEFORMS)
        self.basic_widgets["frequency"] = labeled_entry(
            basic, 1, "Częstotliwość", self.vars["frequency"], "Hz"
        )
        self.basic_widgets["phase"] = labeled_entry(
            basic, 2, "Faza", self.vars["phase"], "°"
        )
        self.basic_widgets["high_level"] = labeled_entry(
            basic, 3, "HighLevel", self.vars["high_level"], "V"
        )
        self.basic_widgets["low_level"] = labeled_entry(
            basic, 4, "LowLevel", self.vars["low_level"], "V"
        )
        self.basic_widgets["dc_level"] = labeled_entry(
            basic, 5, "Poziom DC", self.vars["dc_level"], "V"
        )
        ttk.Checkbutton(
            basic,
            text="Wyłącz wyjście przed zmianą",
            variable=self.vars["safe_output_off"],
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=7)
        ttk.Button(basic, text="Zastosuj przebieg", command=self.apply_waveform).grid(
            row=7, column=0, columnspan=3, sticky="ew", padx=8, pady=(10, 6)
        )

        duty = labeled_entry(shape, 0, "Duty cycle", self.vars["square_duty"], "%")
        symmetry = labeled_entry(
            shape, 1, "Symetria ramp", self.vars["ramp_symmetry"], "%"
        )
        width = labeled_entry(
            shape, 2, "Szerokość impulsu", self.vars["pulse_width"], "s"
        )
        leading = labeled_entry(
            shape, 3, "Zbocze narastające", self.vars["pulse_leading"], "s"
        )
        trailing = labeled_entry(
            shape, 4, "Zbocze opadające", self.vars["pulse_trailing"], "s"
        )
        self.shape_widgets = {
            "SQU": [duty],
            "RAMP": [symmetry],
            "PULS": [width, leading, trailing],
        }
        ttk.Label(
            shape,
            text="Aktywne są tylko pola właściwe\ndla wybranego kształtu.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=12)

        ttk.Checkbutton(output, text="Wyjście CH", variable=self.vars["output"]).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=5
        )
        labeled_entry(output, 1, "Obciążenie", self.vars["load"], "Ω / HIGHZ")
        labeled_combo(
            output, 2, "Polaryzacja", self.vars["polarity"], ("NORM", "INV")
        )
        labeled_combo(
            output, 3, "Tryb wyjścia", self.vars["output_mode"], ("NORM", "GAT")
        )
        labeled_combo(
            output, 4, "Pol. bramki", self.vars["gate_polarity"], ("POS", "NEG")
        )
        ttk.Separator(output).grid(row=5, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Checkbutton(output, text="Wyjście Sync", variable=self.vars["sync"]).grid(
            row=6, column=0, columnspan=3, sticky="w", padx=8, pady=5
        )
        labeled_combo(
            output,
            7,
            "Pol. Sync",
            self.vars["sync_polarity"],
            ("POS", "NEG"),
        )
        labeled_entry(output, 8, "Opóźnienie Sync", self.vars["sync_delay"], "s")
        ttk.Button(output, text="Zastosuj wyjście", command=self.apply_output).grid(
            row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=(10, 6)
        )

        self.preview = tk.Canvas(
            self,
            height=150,
            bg="#0a0f15",
            highlightthickness=1,
            highlightbackground="#2f4052",
        )
        self.preview.pack(fill="x", pady=(14, 0))
        self.preview.bind("<Configure>", lambda _event: self.draw_preview())

    def _waveform_changed(self, *_args: Any) -> None:
        waveform = str(self.vars["waveform"].get())
        for widgets in self.shape_widgets.values():
            for widget in widgets:
                widget.configure(state="disabled")
        for widget in self.shape_widgets.get(waveform, []):
            widget.configure(state="normal")

        is_dc = waveform == "DC"
        is_noise = waveform == "NOIS"
        self.basic_widgets["frequency"].configure(
            state="disabled" if is_dc or is_noise else "normal"
        )
        self.basic_widgets["phase"].configure(
            state="disabled" if is_dc or is_noise else "normal"
        )
        for name in ("high_level", "low_level"):
            self.basic_widgets[name].configure(state="disabled" if is_dc else "normal")
        self.basic_widgets["dc_level"].configure(state="normal" if is_dc else "disabled")
        self.draw_preview()

    def draw_preview(self) -> None:
        canvas = self.preview
        canvas.delete("all")
        width = max(canvas.winfo_width(), 500)
        height = max(canvas.winfo_height(), 150)
        mid = height / 2
        canvas.create_line(0, mid, width, mid, fill="#314154", dash=(4, 6))
        waveform = str(self.vars["waveform"].get())
        points: list[float] = []
        count = 240
        for index in range(count):
            x = index / (count - 1)
            phase = x * math.tau * 3
            if waveform == "SIN":
                y = math.sin(phase)
            elif waveform == "SQU":
                y = 1 if math.sin(phase) >= 0 else -1
            elif waveform == "RAMP":
                y = 2 * ((x * 3) % 1) - 1
            elif waveform == "PULS":
                y = 1 if (x * 3) % 1 < 0.2 else -1
            elif waveform == "NOIS":
                random.seed(index)
                y = random.uniform(-1, 1)
            elif waveform == "DC":
                y = 0.25
            else:
                y = 0.7 * math.sin(phase) + 0.2 * math.sin(phase * 3)
            points.extend((x * width, mid - y * (height * 0.33)))
        canvas.create_line(points, fill=ACCENT, width=2, smooth=waveform == "SIN")
        canvas.create_text(
            12,
            12,
            anchor="nw",
            text=f"Podgląd: {waveform}",
            fill=MUTED,
            font=("Segoe UI", 9),
        )

    def _collect_waveform(self) -> dict[str, Any]:
        names = (
            "waveform",
            "frequency",
            "phase",
            "high_level",
            "low_level",
            "dc_level",
            "square_duty",
            "ramp_symmetry",
            "pulse_width",
            "pulse_leading",
            "pulse_trailing",
            "safe_output_off",
        )
        return {name: self.vars[name].get() for name in names}

    def _collect_output(self) -> dict[str, Any]:
        names = (
            "output",
            "load",
            "polarity",
            "output_mode",
            "gate_polarity",
            "sync",
            "sync_polarity",
            "sync_delay",
        )
        return {name: self.vars[name].get() for name in names}

    def _set_state(self, state: dict[str, Any]) -> None:
        for name, value in state.items():
            if name in self.vars:
                self.vars[name].set(
                    value if isinstance(value, bool) else display_number(value)
                )
        self._waveform_changed()

    def read(self) -> None:
        self.app.run_task(
            f"Odczyt CH{self.channel}",
            lambda device: device.read_channel(self.channel),
            self._set_state,
        )

    def apply_waveform(self) -> None:
        settings = self._collect_waveform()

        def done(_result: Any) -> None:
            self.app.log(f"CH{self.channel}: zastosowano przebieg")
            self.read()

        self.app.run_task(
            f"Ustawianie przebiegu CH{self.channel}",
            lambda device: device.apply_waveform(self.channel, settings),
            done,
        )

    def apply_output(self) -> None:
        settings = self._collect_output()

        def done(_result: Any) -> None:
            self.app.log(f"CH{self.channel}: zastosowano ustawienia wyjścia")
            self.read()

        self.app.run_task(
            f"Ustawianie wyjścia CH{self.channel}",
            lambda device: device.apply_output(self.channel, settings),
            done,
        )

    def toggle_output(self) -> None:
        enabled = not bool(self.vars["output"].get())
        if enabled and not messagebox.askyesno(
            "Włączenie wyjścia",
            f"Czy na pewno włączyć fizyczne wyjście CH{self.channel}?",
            parent=self,
        ):
            return
        self.app.run_task(
            f"{'Włączanie' if enabled else 'Wyłączanie'} CH{self.channel}",
            lambda device: device.set_output(self.channel, enabled),
            lambda actual: self.vars["output"].set(actual),
        )

    def _update_output_button(self) -> None:
        enabled = bool(self.vars["output"].get())
        self.output_button.configure(
            text=f"CH{self.channel}: {'ON' if enabled else 'OFF'}",
            style="OutputOn.TButton" if enabled else "OutputOff.TButton",
        )


class ModulationPanel(ttk.Frame):
    PARAMETER_LABELS = {
        "AM": "Głębokość [%]",
        "FM": "Dewiacja [Hz]",
        "PM": "Dewiacja [°]",
        "ASK": "Amplituda ASK [Vpp]",
        "FSK": "Częst. skoku [Hz]",
        "PSK": "Faza PSK [°]",
        "PWM": "Dewiacja duty [%]",
    }

    def __init__(self, parent: tk.Misc, app: "GeneratorApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.vars: dict[str, tk.Variable] = {
            "channel": tk.StringVar(value="1"),
            "enabled": tk.BooleanVar(value=False),
            "type": tk.StringVar(value="AM"),
            "source": tk.StringVar(value="INT"),
            "internal_shape": tk.StringVar(value="SIN"),
            "rate": tk.StringVar(value="100"),
            "parameter": tk.StringVar(value="50"),
            "polarity": tk.StringVar(value="POS"),
        }
        self._build()
        self.vars["type"].trace_add("write", self._type_changed)
        self._type_changed()

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Modulacja", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="Włączenie modulacji automatycznie wyłącza Sweep i Burst na tym kanale.",
            style="Muted.TLabel",
        ).pack(side="right")

        form = ttk.LabelFrame(self, text="Parametry", padding=14)
        form.pack(fill="x", anchor="n")
        form.columnconfigure(1, weight=1)
        labeled_combo(form, 0, "Kanał", self.vars["channel"], ("1", "2"))
        ttk.Checkbutton(form, text="Modulacja włączona", variable=self.vars["enabled"]).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=6
        )
        labeled_combo(form, 2, "Typ", self.vars["type"], MODULATION_TYPES)
        labeled_combo(form, 3, "Źródło", self.vars["source"], ("INT", "EXT"))
        self.shape_combo = labeled_combo(
            form,
            4,
            "Przebieg mod.",
            self.vars["internal_shape"],
            ("SIN", "SQU", "TRI", "NRAM", "USER"),
        )
        self.rate_entry = labeled_entry(
            form, 5, "Częst./rate", self.vars["rate"], "Hz"
        )
        self.parameter_label = ttk.Label(form, text="Parametr")
        self.parameter_label.grid(row=6, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(form, textvariable=self.vars["parameter"]).grid(
            row=6, column=1, columnspan=2, sticky="ew", padx=8, pady=5
        )
        self.polarity_combo = labeled_combo(
            form, 7, "Polaryzacja", self.vars["polarity"], ("POS", "NEG")
        )
        buttons = ttk.Frame(form)
        buttons.grid(row=8, column=0, columnspan=3, sticky="ew", padx=8, pady=(16, 4))
        ttk.Button(buttons, text="Odczytaj", command=self.read).pack(side="left")
        ttk.Button(buttons, text="Zastosuj modulację", command=self.apply).pack(
            side="right"
        )

    def _type_changed(self, *_args: Any) -> None:
        kind = str(self.vars["type"].get())
        self.parameter_label.configure(text=self.PARAMETER_LABELS.get(kind, "Parametr"))
        analog = kind in {"AM", "FM", "PM", "PWM"}
        self.shape_combo.configure(state="readonly" if analog else "disabled")
        self.polarity_combo.configure(state="disabled" if analog else "readonly")

    def _settings(self) -> dict[str, Any]:
        return {
            name: variable.get()
            for name, variable in self.vars.items()
            if name != "channel"
        }

    def _set(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            if name in self.vars:
                self.vars[name].set(value if isinstance(value, bool) else display_number(value))
        self._type_changed()

    def read(self) -> None:
        channel = int(self.vars["channel"].get())
        self.app.run_task(
            f"Odczyt modulacji CH{channel}",
            lambda device: device.read_modulation(channel),
            self._set,
        )

    def apply(self) -> None:
        channel = int(self.vars["channel"].get())
        settings = self._settings()
        self.app.run_task(
            f"Ustawianie modulacji CH{channel}",
            lambda device: device.apply_modulation(channel, settings),
            lambda _result: self.read(),
        )


class SweepPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "GeneratorApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.vars: dict[str, tk.Variable] = {
            "channel": tk.StringVar(value="1"),
            "enabled": tk.BooleanVar(value=False),
            "start": tk.StringVar(value="100"),
            "stop": tk.StringVar(value="10000"),
            "time": tk.StringVar(value="1"),
            "spacing": tk.StringVar(value="LIN"),
            "steps": tk.StringVar(value="100"),
            "start_hold": tk.StringVar(value="0"),
            "stop_hold": tk.StringVar(value="0"),
            "return_time": tk.StringVar(value="0"),
            "trigger_source": tk.StringVar(value="INT"),
            "trigger_slope": tk.StringVar(value="POS"),
            "trigger_out": tk.StringVar(value="OFF"),
        }
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Sweep częstotliwości", style="Title.TLabel").pack(
            anchor="w", pady=(0, 14)
        )
        columns = ttk.Frame(self)
        columns.pack(fill="x")
        columns.columnconfigure((0, 1), weight=1, uniform="sweep")
        sweep = ttk.LabelFrame(columns, text="Zakres i czas", padding=12)
        trigger = ttk.LabelFrame(columns, text="Wyzwalanie", padding=12)
        sweep.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        trigger.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        for frame in (sweep, trigger):
            frame.columnconfigure(1, weight=1)

        labeled_combo(sweep, 0, "Kanał", self.vars["channel"], ("1", "2"))
        ttk.Checkbutton(sweep, text="Sweep włączony", variable=self.vars["enabled"]).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=5
        )
        labeled_entry(sweep, 2, "Start", self.vars["start"], "Hz")
        labeled_entry(sweep, 3, "Stop", self.vars["stop"], "Hz")
        labeled_entry(sweep, 4, "Czas sweep", self.vars["time"], "s")
        labeled_combo(sweep, 5, "Skala", self.vars["spacing"], ("LIN", "LOG", "STEP"))
        labeled_entry(sweep, 6, "Liczba kroków", self.vars["steps"])
        labeled_entry(sweep, 7, "Start hold", self.vars["start_hold"], "s")
        labeled_entry(sweep, 8, "Stop hold", self.vars["stop_hold"], "s")
        labeled_entry(sweep, 9, "Return time", self.vars["return_time"], "s")

        labeled_combo(
            trigger,
            0,
            "Źródło",
            self.vars["trigger_source"],
            ("INT", "EXT", "MAN"),
        )
        labeled_combo(
            trigger, 1, "Zbocze", self.vars["trigger_slope"], ("POS", "NEG")
        )
        labeled_combo(
            trigger,
            2,
            "Trigger Out",
            self.vars["trigger_out"],
            ("OFF", "POS", "NEG"),
        )
        ttk.Button(trigger, text="Wyzwól teraz", command=self.trigger).grid(
            row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(15, 5)
        )
        ttk.Label(
            trigger,
            text="Manualne wyzwolenie wymaga źródła MAN.\nSweep obsługuje SIN, SQU, RAMP i USER.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=10)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=16)
        ttk.Button(buttons, text="Odczytaj", command=self.read).pack(side="left")
        ttk.Button(buttons, text="Zastosuj Sweep", command=self.apply).pack(side="right")

    def _settings(self) -> dict[str, Any]:
        return {name: var.get() for name, var in self.vars.items() if name != "channel"}

    def _set(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            self.vars[name].set(value if isinstance(value, bool) else display_number(value))

    def read(self) -> None:
        channel = int(self.vars["channel"].get())
        self.app.run_task(
            f"Odczyt Sweep CH{channel}",
            lambda device: device.read_sweep(channel),
            self._set,
        )

    def apply(self) -> None:
        channel = int(self.vars["channel"].get())
        settings = self._settings()
        self.app.run_task(
            f"Ustawianie Sweep CH{channel}",
            lambda device: device.apply_sweep(channel, settings),
            lambda _result: self.read(),
        )

    def trigger(self) -> None:
        channel = int(self.vars["channel"].get())
        self.app.run_task(
            f"Wyzwalanie Sweep CH{channel}",
            lambda device: device.trigger_sweep(channel),
        )


class BurstPanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "GeneratorApp") -> None:
        super().__init__(parent, padding=18)
        self.app = app
        self.vars: dict[str, tk.Variable] = {
            "channel": tk.StringVar(value="1"),
            "enabled": tk.BooleanVar(value=False),
            "mode": tk.StringVar(value="TRIG"),
            "cycles": tk.StringVar(value="1"),
            "phase": tk.StringVar(value="0"),
            "period": tk.StringVar(value="0.01"),
            "delay": tk.StringVar(value="0"),
            "trigger_source": tk.StringVar(value="INT"),
            "trigger_slope": tk.StringVar(value="POS"),
            "trigger_out": tk.StringVar(value="OFF"),
            "gate_polarity": tk.StringVar(value="POS"),
            "idle": tk.StringVar(value="FPT"),
        }
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Burst", style="Title.TLabel").pack(anchor="w", pady=(0, 14))
        columns = ttk.Frame(self)
        columns.pack(fill="x")
        columns.columnconfigure((0, 1), weight=1, uniform="burst")
        burst = ttk.LabelFrame(columns, text="Sekwencja", padding=12)
        trigger = ttk.LabelFrame(columns, text="Wyzwalanie i bramka", padding=12)
        burst.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        trigger.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        for frame in (burst, trigger):
            frame.columnconfigure(1, weight=1)

        labeled_combo(burst, 0, "Kanał", self.vars["channel"], ("1", "2"))
        ttk.Checkbutton(burst, text="Burst włączony", variable=self.vars["enabled"]).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=5
        )
        labeled_combo(burst, 2, "Tryb", self.vars["mode"], ("TRIG", "INF", "GAT"))
        labeled_entry(burst, 3, "Liczba cykli", self.vars["cycles"])
        labeled_entry(burst, 4, "Faza początkowa", self.vars["phase"], "°")
        labeled_entry(burst, 5, "Okres wewnętrzny", self.vars["period"], "s")
        labeled_entry(burst, 6, "Opóźnienie", self.vars["delay"], "s")
        labeled_combo(
            burst,
            7,
            "Poziom idle",
            self.vars["idle"],
            ("FPT", "TOP", "CENTER", "BOTTOM"),
        )

        labeled_combo(
            trigger,
            0,
            "Źródło",
            self.vars["trigger_source"],
            ("INT", "EXT", "MAN"),
        )
        labeled_combo(
            trigger, 1, "Zbocze", self.vars["trigger_slope"], ("POS", "NEG")
        )
        labeled_combo(
            trigger,
            2,
            "Trigger Out",
            self.vars["trigger_out"],
            ("OFF", "POS", "NEG"),
        )
        labeled_combo(
            trigger,
            3,
            "Pol. bramki",
            self.vars["gate_polarity"],
            ("POS", "NEG"),
        )
        ttk.Button(trigger, text="Wyzwól teraz", command=self.trigger).grid(
            row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(15, 5)
        )
        ttk.Label(
            trigger,
            text="Manualne wyzwolenie wymaga źródła MAN\ni włączonego wyjścia kanału.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=10)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=16)
        ttk.Button(buttons, text="Odczytaj", command=self.read).pack(side="left")
        ttk.Button(buttons, text="Zastosuj Burst", command=self.apply).pack(side="right")

    def _settings(self) -> dict[str, Any]:
        return {name: var.get() for name, var in self.vars.items() if name != "channel"}

    def _set(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            self.vars[name].set(value if isinstance(value, bool) else display_number(value))

    def read(self) -> None:
        channel = int(self.vars["channel"].get())
        self.app.run_task(
            f"Odczyt Burst CH{channel}",
            lambda device: device.read_burst(channel),
            self._set,
        )

    def apply(self) -> None:
        channel = int(self.vars["channel"].get())
        settings = self._settings()
        self.app.run_task(
            f"Ustawianie Burst CH{channel}",
            lambda device: device.apply_burst(channel, settings),
            lambda _result: self.read(),
        )

    def trigger(self) -> None:
        channel = int(self.vars["channel"].get())
        self.app.run_task(
            f"Wyzwalanie Burst CH{channel}",
            lambda device: device.trigger_burst(channel),
        )


class ConsolePanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: "GeneratorApp") -> None:
        super().__init__(parent, padding=16)
        self.app = app
        self.command = tk.StringVar(value="*IDN?")
        self._build()

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Konsola SCPI", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="Pełny dostęp do pozostałych komend DG1000Z",
            style="Muted.TLabel",
        ).pack(side="right")

        self.history = tk.Text(
            self,
            height=20,
            bg="#0a0f15",
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#31546c",
            relief="flat",
            padx=12,
            pady=10,
            font=("Cascadia Mono", 10),
            state="disabled",
        )
        self.history.pack(fill="both", expand=True)
        self.history.tag_configure("command", foreground=ACCENT)
        self.history.tag_configure("response", foreground=GREEN)
        self.history.tag_configure("error", foreground=RED)
        self.history.tag_configure("info", foreground=MUTED)

        command_bar = ttk.Frame(self)
        command_bar.pack(fill="x", pady=(10, 6))
        self.entry = ttk.Entry(command_bar, textvariable=self.command, font=("Cascadia Mono", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _event: self.send())
        ttk.Button(command_bar, text="Wyślij", command=self.send).pack(side="left", padx=(8, 0))

        shortcuts = ttk.Frame(self)
        shortcuts.pack(fill="x")
        for label, command in (
            ("Identyfikacja", "*IDN?"),
            ("Błędy", ":SYST:ERR?"),
            ("CH1 Apply?", ":SOUR1:APPL?"),
            ("CH2 Apply?", ":SOUR2:APPL?"),
            ("Wyczyść status", "*CLS"),
        ):
            ttk.Button(
                shortcuts,
                text=label,
                command=lambda value=command: self._send_value(value),
            ).pack(side="left", padx=(0, 6))
        ttk.Button(shortcuts, text="Wyczyść ekran", command=self.clear).pack(side="right")

        device = ttk.LabelFrame(self, text="Operacje urządzenia", padding=10)
        device.pack(fill="x", pady=(12, 0))
        ttk.Button(device, text="Synchronizuj fazy CH1/CH2", command=self.sync_phases).pack(
            side="left"
        )
        ttk.Button(device, text="Reset fabryczny (*RST)", command=self.reset).pack(
            side="right"
        )

    def append(self, text: str, tag: str = "info") -> None:
        self.history.configure(state="normal")
        self.history.insert("end", text + "\n", tag)
        self.history.see("end")
        self.history.configure(state="disabled")

    def clear(self) -> None:
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.configure(state="disabled")

    def _send_value(self, value: str) -> None:
        self.command.set(value)
        self.send()

    def send(self) -> None:
        command = self.command.get().strip()
        if not command:
            return
        if any(token in command.upper() for token in ("*RST", ":SYST:PRES")):
            if not messagebox.askyesno(
                "Potwierdzenie",
                "Ta komenda może skasować bieżące ustawienia generatora. Kontynuować?",
                parent=self,
            ):
                return
        self.append(f"> {command}", "command")

        def done(response: str | None) -> None:
            self.append("OK" if response is None else f"< {response}", "response")

        def failed(exc: Exception) -> None:
            self.append(f"! {exc}", "error")

        self.app.run_task(
            f"SCPI: {command}",
            lambda device: device.raw(command),
            done,
            failed,
        )

    def sync_phases(self) -> None:
        self.app.run_task(
            "Synchronizacja faz",
            lambda device: device.synchronize_phases(),
            lambda _result: self.append("Fazy zsynchronizowane", "response"),
        )

    def reset(self) -> None:
        if not messagebox.askyesno(
            "Reset generatora",
            "Przywrócić ustawienia fabryczne? Wyjścia mogą zmienić stan.",
            parent=self,
        ):
            return
        self.app.run_task(
            "Reset generatora",
            lambda device: device.reset(),
            lambda _result: self.append("Reset zakończony", "response"),
        )


class GeneratorApp(tk.Tk):
    def __init__(self, address: str) -> None:
        super().__init__()
        self.title("Rigol DG1032Z Control")
        self.geometry("1180x790")
        self.minsize(1000, 680)
        self.configure(bg=BG)
        self.worker = VisaWorker()
        self.connected = False
        self.busy_count = 0
        self.address = tk.StringVar(value=address)
        self.connection_text = tk.StringVar(value="Rozłączono")
        self.status_text = tk.StringVar(value="Gotowy")
        self._configure_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(50, self._poll_results)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 17), foreground=TEXT)
        style.configure("Status.TLabel", foreground=MUTED, padding=(8, 5))
        style.configure("TLabelFrame", background=BG, foreground=ACCENT)
        style.configure(
            "TLabelFrame.Label", background=BG, foreground=ACCENT, font=("Segoe UI Semibold", 10)
        )
        style.configure(
            "TEntry",
            fieldbackground=PANEL_ALT,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor="#344659",
            lightcolor="#344659",
            darkcolor="#344659",
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground=PANEL_ALT,
            background=PANEL_ALT,
            foreground=TEXT,
            arrowcolor=TEXT,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL_ALT), ("disabled", PANEL)],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
        )
        style.configure(
            "TButton",
            background="#26384a",
            foreground=TEXT,
            borderwidth=0,
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map("TButton", background=[("active", "#34516a"), ("disabled", PANEL)])
        style.configure("OutputOn.TButton", background="#176b50", foreground="#ecfff8")
        style.map("OutputOn.TButton", background=[("active", "#218c69")])
        style.configure("OutputOff.TButton", background="#5b2731", foreground="#ffeef1")
        style.map("OutputOff.TButton", background=[("active", "#7b3441")])
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=PANEL,
            foreground=MUTED,
            padding=(18, 10),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL_ALT), ("active", "#263545")],
            foreground=[("selected", TEXT), ("active", TEXT)],
        )
        style.configure("Horizontal.TSeparator", background="#2d3b4a")

    def _build(self) -> None:
        connection = ttk.Frame(self, padding=(16, 14))
        connection.pack(fill="x")
        ttk.Label(connection, text="DG1032Z", style="Title.TLabel").pack(side="left")
        ttk.Label(connection, text="  VISA:", style="Muted.TLabel").pack(side="left")
        ttk.Entry(connection, textvariable=self.address, width=54).pack(
            side="left", padx=(6, 8), fill="x", expand=True
        )
        self.connect_button = ttk.Button(connection, text="Połącz", command=self.connect)
        self.connect_button.pack(side="left", padx=(0, 6))
        self.disconnect_button = ttk.Button(
            connection, text="Rozłącz", command=self.disconnect, state="disabled"
        )
        self.disconnect_button.pack(side="left")
        self.connection_label = ttk.Label(
            connection, textvariable=self.connection_text, style="Muted.TLabel"
        )
        self.connection_label.pack(side="left", padx=(12, 0))

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        channels_tab = ttk.Frame(notebook)
        channels = ttk.Notebook(channels_tab)
        channels.pack(fill="both", expand=True)
        self.channel_panels = [
            ChannelPanel(channels, self, 1),
            ChannelPanel(channels, self, 2),
        ]
        channels.add(self.channel_panels[0], text="CH1")
        channels.add(self.channel_panels[1], text="CH2")
        notebook.add(channels_tab, text="Kanały")

        self.modulation_panel = ModulationPanel(notebook, self)
        self.sweep_panel = SweepPanel(notebook, self)
        self.burst_panel = BurstPanel(notebook, self)
        self.console_panel = ConsolePanel(notebook, self)
        notebook.add(self.modulation_panel, text="Modulacja")
        notebook.add(self.sweep_panel, text="Sweep")
        notebook.add(self.burst_panel, text="Burst")
        notebook.add(self.console_panel, text="SCPI / Zaawansowane")

        status = ttk.Frame(self, padding=(10, 2))
        status.pack(fill="x")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=110)
        self.progress.pack(side="right", padx=(8, 0))
        ttk.Label(status, textvariable=self.status_text, style="Status.TLabel").pack(
            side="left", fill="x", expand=True
        )

    def log(self, message: str, tag: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, "console_panel"):
            self.console_panel.append(f"[{stamp}] {message}", tag)

    def connect(self) -> None:
        address = self.address.get().strip()
        self.run_task(
            "Łączenie z generatorem",
            lambda device: device.connect(address),
            self._connected,
            require_connection=False,
        )

    def _connected(self, identity: str) -> None:
        self.connected = True
        self.connection_text.set(identity)
        self.connect_button.configure(state="disabled")
        self.disconnect_button.configure(state="normal")
        self.log(f"Połączono: {identity}", "response")
        for panel in self.channel_panels:
            panel.read()

    def disconnect(self) -> None:
        def done(_result: Any) -> None:
            self.connected = False
            self.connection_text.set("Rozłączono")
            self.connect_button.configure(state="normal")
            self.disconnect_button.configure(state="disabled")
            self.log("Rozłączono")

        self.run_task(
            "Rozłączanie",
            lambda device: device.disconnect(),
            done,
            require_connection=False,
        )

    def run_task(
        self,
        label: str,
        operation: Callable[[DG1032Z], Any],
        success: Callable[[Any], None] | None = None,
        error: Callable[[Exception], None] | None = None,
        require_connection: bool = True,
    ) -> None:
        if require_connection and not self.connected:
            messagebox.showwarning(
                "Brak połączenia", "Najpierw połącz się z generatorem.", parent=self
            )
            return
        self.busy_count += 1
        if self.busy_count == 1:
            self.progress.start(10)
        self.status_text.set(label + "…")
        self.worker.submit(Task(label, operation, success, error))

    def _poll_results(self) -> None:
        try:
            while True:
                task, result, error = self.worker.results.get_nowait()
                self.busy_count = max(0, self.busy_count - 1)
                if error is None:
                    self.status_text.set(f"{task.label}: OK")
                    if task.success is not None:
                        task.success(result)
                else:
                    self.status_text.set(f"{task.label}: błąd")
                    self.log(f"{task.label}: {error}", "error")
                    if task.error is not None:
                        task.error(error)
                    else:
                        messagebox.showerror("Błąd", str(error), parent=self)
                if self.busy_count == 0:
                    self.progress.stop()
        except queue.Empty:
            pass
        self.after(50, self._poll_results)

    def close(self) -> None:
        self.worker.close()
        self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="adres zasobu VISA")
    parser.add_argument(
        "--unsafe-legacy",
        action="store_true",
        help="uruchom historyczny prototyp Tkinter bez produkcyjnych blokad bezpieczenstwa",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.unsafe_legacy:
        print(
            "Ten Tkinter GUI jest historycznym prototypem. "
            "Uruchom produkcyjna aplikacje: lab-control. "
            "Do celow serwisowych wymagajacych prototypu podaj --unsafe-legacy."
        )
        return
    if not args.address:
        raise SystemExit("Podaj --address albo ustaw RIGOL_VISA_RESOURCE.")
    app = GeneratorApp(args.address)
    app.mainloop()


if __name__ == "__main__":
    main()
