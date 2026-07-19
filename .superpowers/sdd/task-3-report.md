# Task 3 report: Fluent, station QSS, motion, and PyQtGraph bridge

## Delivered

- Added `motion_enabled()` with an offscreen-platform safeguard and persisted reduced-motion preference.
- Added frozen `PlotTheme` and `AppliedTheme` value objects, plus the single Fluent/application QSS bridge.
- Exported the new public design-system interfaces without changing the Task 1–2 token or station-QSS interfaces.
- Replaced `app/main.py`'s legacy `STYLE` and `LIGHT_STYLE` strings with `apply_application_theme()` while retaining the existing theme signal and OS colour-scheme listener.
- Routed `SpectrumPlotWidget` through semantic plot tokens for the plot background, axes, crosshairs, markers, and implicit primary-trace colour. The widget does not invoke Fluent global APIs.

## Test-driven evidence

### RED

Command:

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py -q
```

Result: expected collection failure, `ImportError: cannot import name 'apply_application_theme' from 'app.ui.design_system'`. This demonstrated the missing bridge interface before production implementation.

### GREEN

Command:

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py -q
```

Result: `12 passed`.

## Verification

Command:

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py tests/test_main_window.py::MainWindowTests::test_theme_switch_supports_light_dark_and_system_persistence -q
```

Result: `13 passed`.

`git diff --check` also completed without whitespace errors. Pytest emitted one existing cache warning because `.pytest_cache` cannot create its cache-path hierarchy; it did not affect test collection or results.

## Test adjustment

No adjustment was necessary: the brief's PyQtGraph observable, `backgroundBrush().color().name()`, is available and the plot test asserts `#ffffff` after applying the light palette.

## Files changed

- `app/main.py`
- `app/ui/design_system/__init__.py`
- `app/ui/design_system/fluent_theme.py`
- `app/ui/design_system/motion.py`
- `app/ui/design_system/plot_theme.py`
- `app/ui/widgets/spectrum_plot.py`
- `tests/test_design_system.py`
- `tests/test_spectrum_plot.py`

## Self-review

- Confirmed the legacy global QSS constants are absent from `app/main.py`.
- Confirmed the application bridge resolves modes before selecting Fluent `Theme` and semantic station QSS.
- Confirmed plot styling uses tokens for background, axes/text, grid/crosshairs, reference markers, and automatic primary measurement traces.
- Confirmed no hardware logic was changed and the unrelated recovery files were left untouched.

## Concern

The focused test commands pass, with only the pre-existing `.pytest_cache` write warning described above. The full project test suite was not run because the task brief specifies the focused design-system, plot, and theme-persistence coverage.

## Review follow-up: token-owned primary curve retheming

### Root cause and fix

The initial implementation used the active measurement token only when it created an implicit primary curve. The `PlotDataItem` then retained that concrete pen, leaving `apply_theme()` no way to distinguish it from an explicitly coloured caller-owned curve.

`SpectrumPlotWidget` now records names of primary curves created without a supplied colour. On each theme change it updates only those pens to the current semantic measurement token. Providing an explicit colour removes that ownership, so explicitly caller-supplied curve colours are preserved. Clearing a trace removes its ownership record.

### Regression RED/GREEN

Added `test_apply_theme_rethemes_token_owned_plot_items`, which creates a default primary curve under dark mode, switches to light mode, and checks the primary pen, reference marker, both grid crosshairs, and axis/text pens against the light semantic palette. It also confirms an explicitly coloured reference curve remains `#123456`.

RED command:

```powershell
python -m pytest tests/test_spectrum_plot.py::SpectrumPlotTests::test_apply_theme_rethemes_token_owned_plot_items -q
```

Result: failed as expected: the existing primary pen was dark measurement `#60a5fa`, not light measurement `#0067c0`.

GREEN command: same command.

Result: `1 passed`.

### Follow-up verification

```powershell
python -m pytest tests/test_design_system.py tests/test_spectrum_plot.py tests/test_main_window.py::MainWindowTests::test_theme_switch_supports_light_dark_and_system_persistence -q
```

Result: `14 passed`. The same pre-existing `.pytest_cache` warning was emitted and did not affect results.
