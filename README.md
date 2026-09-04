# MTJLAB

<a id="readme-top"></a>

<div align="center">
  <h2>Automated spintronics and magnetic tunnel junction measurement station with PySide6 Fluent UI, real-time PyThat/HDF5 acquisition, and multi-instrument VISA safety runtime</h2>

  <p>
    <strong>Safe hardware orchestration · Rigol, Keithley, Anritsu &amp; Lake Shore · interactive recipe builder · live real-time analysis · strict scientific provenance</strong>
  </p>

  <p>
    <a href="#quick-start"><strong>Quick start</strong></a>
    ·
    <a href="#apparatus-and-supported-instruments">Apparatus &amp; devices</a>
    ·
    <a href="#execution-and-safety-model">Execution &amp; safety</a>
    ·
    <a href="#data-persistence-and-pythat">Data persistence</a>
    ·
    <a href="#version-dashboard">Version dashboard</a>
    ·
    <a href="#citation-and-authors">Citation &amp; authors</a>
  </p>
</div>

## Overview

**MTJLAB** (`lab-control`) is production-grade laboratory software for automated electrical, magnetic, and high-frequency characterization of **Magnetic Tunnel Junctions (MTJs)**, spin-torque nano-oscillators (STNOs), and spintronic nanodevices.

The platform orchestrates multi-instrument measurement campaigns by combining a **declarative YAML recipe engine**, a **PySide6 Fluent Design desktop shell**, **conservative hardware safety interlocks**, and **PyThat / thaTEC:OS compatible scientific data persistence**.

Measurements can be executed interactively via the visual workspace, dispatched as complex nested sweep batches, or monitored live with hardware-accelerated time-series, spectrum, and heatmap visualizations.

> [!IMPORTANT]
> **Safety-first laboratory control:** Hardware output configuration, setpoint transitions, and energization strictly adhere to laboratory safety profiles. The application enforces hardware compliance limits, software clamps, automated ramp-to-zero routines, and an immediate hardware-level emergency stop (**E-STOP** via `Ctrl+Shift+E`).
>
> A complete **simulation mode** (`--simulation`) allows authoring recipes, inspecting device panels, testing preflight compilations, and verifying execution plans offline with zero hardware risk.

<a id="apparatus-and-supported-instruments"></a>

## Apparatus and supported instruments

Each supported device family is integrated via an isolated device module containing its physical domain model, validation logic, VISA driver adapter, and Fluent control cards:

| Instrument | Role in station | Interface / Protocol | Key safety &amp; measurement capabilities |
|---|---|---|---|
| **Keithley 2600 / 2400 / 2450 Series** | Source Measure Unit (SMU) | VISA (GPIB / USB / VXI-11 TCP/IP) | Bipolar current/voltage sourcing; 4-wire remote sensing; hardware compliance enforcement; safe ramp-to-zero before output disconnection |
| **Rigol DG1000Z Series (DG1032Z / DG1062Z)** | Arbitrary Waveform &amp; Function Generator | VISA (USB-TMC / TCP/IP) | RF excitation, continuous waveforms, and high-speed pulsing; conservative DUT load current modeling for 50&nbsp;Ω output impedances; post-transaction readback verification |
| **Anritsu MS2830A Series** | Signal &amp; Microwave Spectrum Analyzer | VISA (GPIB / VXI-11 TCP/IP) | PSD trace acquisition; streaming live spectrum window; resolution bandwidth (RBW) and attenuation interlocks; RF input overload safeguarding |
| **Lake Shore 425 / 475 Series** | Hall Effect Gaussmeter &amp; Electromagnet | VISA / Serial / USB | Real-time magnetic field sensing (T, G); Hall probe temperature compensation; field sweep control with ramp rate saturation limits |
| **MOKE Optical Box** | Magneto-Optical Kerr Effect Optical Detector | Serial / Analog I/O | Magnetic domain wall and magnetization reversal tracking; synchronized optical hysteresis loops |

<a id="execution-and-safety-model"></a>

## Execution and safety model

```text
Interactive Recipe Editor (YAML / Visual Tree) or Quick Controls
                                │
                                ▼
                   Preflight Static AST Compiler
             (syntax, types, units, and safety bounds)
                                │
                                ▼
                   Validated Execution Plan
           (SHA-256 hash, duration and disk-space bounds)
                                │
                                ▼
                     Run Engine Supervisor
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
     Keithley SMU           Rigol AWG          Anritsu Spectrum
  (Current/Voltage)       (RF Pulsing)          (Microwave PSD)
          └─────────────────────┬─────────────────────┘
                                ▼
             PyThat / thaTEC:OS Compatible HDF5
         + Checkpoint CSV Stream + Structured Audit Log
```

### Safety interlocks and fail-safe operation

1. **Hardware compliance vs. DUT bounds**: Every active source enforces both instrument hardware limits and per-sample Device-Under-Test (DUT) damage thresholds.
2. **Conservative load impedance modeling**: For non-SMU voltage sources (such as the Rigol DG1000Z), estimated DUT load currents are calculated from equivalent circuit models before enabling outputs.
3. **Explicit profile authorization**: An unverified configuration file permits only reading status and setting parameters at `OUTPUT OFF`. Energizing hardware requires explicit operator authorization.
4. **Emergency Stop (E-STOP)**: Triggerable instantly via the UI banner, title bar, or global shortcut `Ctrl+Shift+E`. Immediately commands all active sources to disconnect outputs and execute zero-ramps without animation delays.
5. **Fail-safe shutdown**: Unsaved recipe edits prompt the operator, active preflight cancellation is confirmed, and background measurement workers are cleanly halted before window disposal.

<a id="sweep-recipe-engine"></a>

## Declarative sweep recipe engine

Measurements are defined as structured YAML documents or visually through the interactive Recipe Builder tree:

- **Multi-dimensional nested loops**: Seamlessly sweep current $\times$ magnetic field $\times$ RF power $\times$ frequency.
- **Dynamic parameter substitution**: Express dependent setpoints using expressions such as `${keithley.B.current}` or `${rigol.1.high_level}`.
- **Preflight compilation**: Generates an immutable, content-hashed `ExecutionPlan` detailing the exact number of checkpoints, expected nominal duration, worst-case duration, and estimated disk footprint.

<details>
<summary><strong>Show example sweep recipe (Keithley current × Rigol voltage × Anritsu spectrum)</strong></summary>

```yaml
schema_version: 1
name: Keithley B current × Rigol CH1 high level × Anritsu spectrum

root:
  id: sequence-main
  type: sequence
  children:
  - id: anritsu-spectrum-setup
    type: configure_anritsu
    start_frequency: 1 MHz
    stop_frequency: 10 MHz
    reference_level: 0 dBm
    points: 1001

  - id: keithley-current-sweep
    type: sweep
    target: keithley.B.current
    start: 1 mA
    stop: 10 mA
    points: 100
    spacing: linear
    children:
    - id: keithley-b-config
      type: configure_keithley
      channel: B
      mode: current
      level: ${keithley.B.current}
      compliance: 100 mV
      nplc: 1.0

    - id: rigol-high-level-sweep
      type: sweep
      target: rigol.1.high_level
      start: 1 mV
      stop: 3 mV
      points: 20
      spacing: linear
      children:
      - id: rigol-ch1-config
        type: configure_rigol
        channel: 1
        waveform: SQU
        frequency: 1 kHz
        high_level: ${rigol.1.high_level}
        low_level: -1 mV
        output_load: HIGHZ
        dut_min_impedance: 50 ohm

      - id: acquire-spectrum
        type: acquire_spectrum
        trace: TRAC1

      - id: measure-keithley-b
        type: measure_keithley
        channel: B

      - id: wait-for-settle
        type: wait
        duration: 50 ms

finally:
- id: rigol-ch1-off-finally
  type: set_rigol_output
  channel: 1
  enabled: false
- id: keithley-b-ramp-zero-finally
  type: ramp_keithley_to_zero
  channel: B
  deadline: 10 s
- id: keithley-b-off-finally
  type: set_keithley_output
  channel: B
  enabled: false
```

</details>

<a id="data-persistence-and-pythat"></a>

## Data persistence and PyThat compatibility

All experimental datasets are written following strict scientific reproducibility principles:

- **HDF5 Datasets**: Formatted for direct interoperability with **PyThat** and **thaTEC:OS**. Numerical arrays are stored alongside unit attributes, scan axes, instrument IDs, and sensor calibration factors.
- **Crash resilience**: Measurements stream to disk with atomic checkpoints; interrupted runs remain valid, readable HDF5 files containing complete diagnostic headers.
- **Session recovery**: Unsaved recipe drafts are auto-saved to localized `.recovery` journals.
- **Electronic Lab Notebook (eLabFTW)**: Native integration for uploading closed run artifacts, YAML manifests, and summary plots directly to an eLabFTW experiment via API tokens.

<a id="quick-start"></a>

## Quick start

### 1. Installation

Clone the repository and install the station package in development mode:

```bash
git clone https://github.com/MateuszZelent/MTJLAB.git
cd MTJLAB
python -m pip install -e ".[dev]"
```

### 2. Launching in simulation mode (offline / no hardware)

To explore the user interface, recipe builder, and plots without physical instruments:

```bash
lab-control --simulate
```

Or invoke the Python module directly:

```bash
python -m app.main --simulation
```

### 3. Launching in production mode with hardware

Ensure VISA instruments are connected via USB, GPIB, or Ethernet, then launch:

```bash
lab-control --settings .config/settings.yml
```

### 4. Running automated test suite

```bash
pytest
```

<a id="repository-map"></a>

## Repository map

| Directory | Core responsibility |
|---|---|
| [`app/domain/`](app/domain/) | Physical quantities, unit normalization, device models, safety limits, and quick-control contracts |
| [`app/devices/`](app/devices/) | Hardware adapters, SCPI protocols, and Fluent UI pages (Rigol, Keithley, Anritsu, Lake Shore, MOKE) |
| [`app/recipes/`](app/recipes/) | YAML parser, AST nodes, static preflight validator, and execution plan compiler |
| [`app/execution/`](app/execution/) | Run engine, worker threads, safe ramp controllers, hardware watchdogs, and E-STOP coordination |
| [`app/storage/`](app/storage/) | HDF5 persistence, PyThat/thaTEC:OS manifest writer, CSV index generator, and eLabFTW integration |
| [`app/ui/`](app/ui/) | PySide6 Fluent application shell, floating quick controls, real-time PyQtGraph plots, and theme manager |
| [`tests/`](tests/) | Comprehensive automated test suite (safety interlocks, parser validation, storage, UI responsiveness) |
| [`tools/`](tools/) | Version dashboard synchronizer, qualification helpers, and environment verification scripts |

<!-- mtjlab-version-dashboard:start -->
<a id="version-dashboard"></a>

## Version and compatibility dashboard

The badges below reflect repository manifests. They distinguish exact pins from supported dependency ranges.

<p align="center">
  <strong>Continuous verification &amp; quality</strong><br />
  <a href="https://github.com/MateuszZelent/MTJLAB/actions"><img alt="CI test suite" src="https://img.shields.io/badge/tests-passing-brightgreen?style=for-the-badge&amp;logo=pytest&amp;logoColor=white" /></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff code style" src="https://img.shields.io/badge/code%20style-ruff-000000?style=for-the-badge&amp;logo=ruff&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="License: Open Source" src="https://img.shields.io/badge/license-Open%20Source-2563EB?style=for-the-badge" /></a>
</p>

<p align="center">
  <strong>Core runtime &amp; UI platform</strong><br />
  <a href="pyproject.toml"><img alt="MTJLAB v0.1.0" src="https://img.shields.io/badge/MTJLAB-v0.1.0-2563EB?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Python >=3.11" src="https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PySide6 >=6.7" src="https://img.shields.io/badge/PySide6-%3E%3D6.7-41CD52?style=for-the-badge&amp;logo=qt&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PySide6 Fluent Widgets 1.11.2" src="https://img.shields.io/badge/Fluent%20Widgets-1.11.2-0078D4?style=for-the-badge&amp;logo=windows11&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="PyQtGraph >=0.13.7,<0.14" src="https://img.shields.io/badge/PyQtGraph-%3E%3D0.13.7%2C%3C0.14-1793D1?style=for-the-badge&amp;logo=python&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="NumPy >=1.26" src="https://img.shields.io/badge/NumPy-%3E%3D1.26-013243?style=for-the-badge&amp;logo=numpy&amp;logoColor=white" /></a>
</p>

<p align="center">
  <strong>Measurement, hardware &amp; persistence stack</strong><br />
  <a href="pyproject.toml"><img alt="PyThat 0.2.14" src="https://img.shields.io/badge/PyThat-0.2.14-7C3AED?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="PyVISA >=1.14" src="https://img.shields.io/badge/PyVISA-%3E%3D1.14-00599C?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Lake Shore 1.10.0" src="https://img.shields.io/badge/Lake%20Shore-1.10.0-D97706?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="h5py >=3.11" src="https://img.shields.io/badge/h5py-%3E%3D3.11-2E7D32?style=for-the-badge" /></a>
  <a href="pyproject.toml"><img alt="Pydantic >=2.7,<3" src="https://img.shields.io/badge/Pydantic-%3E%3D2.7%2C%3C3-E92063?style=for-the-badge&amp;logo=pydantic&amp;logoColor=white" /></a>
  <a href="pyproject.toml"><img alt="ruamel.yaml >=0.18" src="https://img.shields.io/badge/ruamel.yaml-%3E%3D0.18-5B6EC4?style=for-the-badge" /></a>
</p>

<details>
<summary><strong>Version policy and sources of truth</strong></summary>

| Layer / Component | Declared manifest value | Source of truth | Compatibility &amp; maintenance policy |
|---|---|---|---|
| **MTJLAB Station Shell** | `v0.1.0` | `pyproject.toml` | Application semantic version |
| **Python Toolchain** | `>=3.11` | `pyproject.toml` | Tested on Python 3.11, 3.12, and 3.14 |
| **Desktop UI Platform** | PySide6 `>=6.7` | `pyproject.toml` | Qt 6.7+ multimedia &amp; modern graphics pipeline |
| **Design Language** | QFluent `1.11.2` | `pyproject.toml` | 100% open-source PySide6-Fluent-Widgets |
| **Real-Time Plotting** | PyQtGraph `>=0.13.7,<0.14` | `pyproject.toml` | High-throughput GPU/CPU trace and heatmap rendering |
| **Numerical Array Engine** | NumPy `>=1.26` | `pyproject.toml` | Vectorized acquisition arrays and calibration math |
| **Persistent Data Schema** | PyThat `0.2.14` / HDF5 `>=3.11` | `pyproject.toml` | thaTEC:OS / PyThat compatible dataset hierarchies |
| **Instrument Communication** | PyVISA `>=1.14` / pyvisa-py | `pyproject.toml` | Standard SCPI over USB, GPIB, and TCP/IP (VXI-11) |
| **Gaussmeter &amp; Magnet** | Lake Shore `1.10.0` | `pyproject.toml` | Official Lake Shore Hall probe &amp; field controller driver |
| **Recipe AST &amp; Validation** | Pydantic `>=2.7,<3` / ruamel.yaml `>=0.18` | `pyproject.toml` | Declarative schema validation and round-trip preservation |

Regenerate after updating dependencies:

```bash
python tools/update_readme_version_dashboard.py --write
```

</details>
<!-- mtjlab-version-dashboard:end -->

## Contributing

Before contributing or modifying the codebase, please review [`AGENTS.md`](AGENTS.md). All modifications to user interfaces, device communication, recipes, or safety systems must satisfy the repository contracts:

- **Fluent-first architecture**: Maintain strict QFluent design tokens without embedding legacy Qt shells.
- **Instrument safety**: Do not bypass output compliance checks, hardware clamping, or ramp-to-zero safety guarantees.
- **Scientific data integrity**: Preserve HDF5 schemas, unit dimensions, and reproducible PyThat metadata.

<a id="citation-and-authors"></a>

## Citation and authors

Until a formal release with a persistent DOI is published, please cite the repository and specific commit:

> M. Zelent, *MTJLAB: automated measurement station and safety runtime for spintronics and magnetic tunnel junctions*, research software, 2026. Repository: [github.com/MateuszZelent/MTJLAB](https://github.com/MateuszZelent/MTJLAB).

<details>
<summary><strong>BibTeX</strong></summary>

```bibtex
@software{mtjlab_2026,
  author = {Zelent, Mateusz},
  title  = {MTJLAB: Automated Measurement Station and Safety Runtime for
            Spintronics and Magnetic Tunnel Junctions},
  year   = {2026},
  url    = {https://github.com/MateuszZelent/MTJLAB},
  note   = {Research software; cite the exact release or commit used}
}
```

</details>

| Author | Affiliation |
|---|---|
| **Dr Mateusz Zelent** | Fachbereich Physik and Landesforschungszentrum OPTIMAS, RPTU Kaiserslautern-Landau, Germany |

Project coordination: **Mateusz Zelent, RPTU Kaiserslautern-Landau**.

## License

This project is 100% open source under the terms of the project repository license.

## Funding

Mateusz Zelent acknowledges funding from the European Union's Horizon Europe programme under HORIZON-MSCA-2024-PF-01, Marie Skłodowska-Curie Grant Agreement No. **101208951 (CNMA)**.
