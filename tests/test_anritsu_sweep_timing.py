"""Spectrum timing must confirm the receiver state before it is used for acquisition."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.sweep_timing import SpectrumTimingSettings
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.spectrum_correction import SweepEvidence
from app.settings.models import StationSettings
from app.storage.reference_store import ReferenceHdf5Store
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings


@pytest.fixture
def receiver():
    raw = simulation_settings().model_dump()
    raw["devices"]["anritsu"]["advanced_spectrum"].update(
        control_protocol="standard_scpi", qualified_firmware=("sim-1.0",),
    )
    station = StationSettings.model_validate(raw)
    session = AnritsuSimulator()
    session.remote_language = "NAT"
    adapter = AnritsuAdapter(station, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    try:
        yield adapter, session, station
    finally:
        adapter.disconnect()


def test_sweep_priority_is_separate_from_effective_method(receiver):
    adapter, session, _ = receiver
    before = adapter.read_sweep_timing()
    actual = adapter.configure_sweep_timing(replace(before.settings, sweep_rule="PFFT", fft_width_hz=2_000_000))
    assert actual.settings.sweep_rule == "PFFT"
    assert actual.effective_method == "FFT"
    assert actual.actual_fft_width_hz == 2_000_000
    assert actual.continuous == before.continuous
    assert not any(command.startswith(("OUTP ", "POW:GAIN ", "POW:ATT ", "INST ")) for command in session.commands)


def test_gate_is_enabled_before_writing_timing_and_forces_sweep(receiver):
    adapter, session, _ = receiver
    before = adapter.read_sweep_timing()
    result = adapter.configure_sweep_timing(replace(
        before.settings, sweep_rule="OSW", gate_enabled=True, gate_source="FRAM", gate_length_s=0.005,
    ))
    assert result.settings.gate_enabled and result.settings.gate_length_s == 0.005
    assert result.effective_method == "SWE"
    assert session.commands.index("SWE:EGAT ON") < session.commands.index("SWE:EGAT:LENG 0.005S")
    assert adapter.read_acquisition_configuration()[1].timing_context == result.context()


def test_configuration_rejects_unqualified_filter_and_invalid_timing_without_writes(receiver):
    adapter, session, _ = receiver
    for requested in (
        SpectrumTimingSettings(rbw_filter="CISP"),
        SpectrumTimingSettings(gate_enabled=True, gate_source="FRAM", sweep_rule="PFFT"),
        SpectrumTimingSettings(gate_enabled=True, gate_source="FRAM", sweep_rule="OSW", gate_delay_s=float("nan")),
        SpectrumTimingSettings(gate_enabled=True, sweep_rule="OSW"),
        SpectrumTimingSettings(gate_enabled=True, gate_source="FRAM", sweep_rule="OSW", trigger_source="EXT"),
    ):
        session.commands.clear()
        with pytest.raises(SafetyViolation):
            adapter.configure_sweep_timing(requested)
        assert all("?" in command for command in session.commands)


def test_gate_rejection_restores_gate_details_before_disabling_it(receiver, monkeypatch):
    adapter, session, _ = receiver
    before = adapter.read_sweep_timing()
    write = session.write

    def reject_length(command):
        if command == "SWE:EGAT:LENG 0.007S":
            session.error_queue.append('-221,"Settings conflict"')
            return
        write(command)

    monkeypatch.setattr(session, "write", reject_length)
    with pytest.raises(DeviceError, match="Settings conflict"):
        adapter.configure_sweep_timing(replace(
            before.settings, sweep_rule="OSW", gate_enabled=True, gate_source="FRAM",
            gate_delay_s=0.0002, gate_length_s=0.007,
        ))
    assert adapter.read_sweep_timing() == before


def test_switching_between_gate_and_trigger_obeys_firmware_order(receiver, monkeypatch):
    adapter, session, _ = receiver
    gate = SpectrumTimingSettings(sweep_rule="OSW", gate_enabled=True, gate_source="FRAM", gate_length_s=0.005)
    adapter.configure_sweep_timing(gate)
    write = session._write

    def write_with_exclusive_sources(command):
        if command.startswith("SWE:EGAT ") and session.sweep_timing.trigger_source != "IMM":
            session.error_queue.append('-221,"Gate conflicts with Trigger"')
            return
        if command == "TRIG:SOUR EXT" and session.sweep_timing.gate_enabled:
            session.error_queue.append('-221,"Trigger conflicts with Gate"')
            return
        write(command)
        if command == "SWE:EGAT OFF":
            session.sweep_timing = replace(session.sweep_timing, gate_source="IMM")

    monkeypatch.setattr(session, "_write", write_with_exclusive_sources)
    session.commands.clear()
    result = adapter.configure_sweep_timing(replace(gate, gate_enabled=False, trigger_source="EXT"))
    assert not result.settings.gate_enabled and result.settings.trigger_source == "EXT"
    assert session.commands.index("SWE:EGAT OFF") < session.commands.index("TRIG:SOUR EXT")
    session.commands.clear()
    assert adapter.configure_sweep_timing(gate).settings.gate_enabled
    assert session.commands.index("TRIG:SOUR IMM") < session.commands.index("SWE:EGAT ON")


def test_external_trigger_wait_and_stop_do_not_publish_a_stale_trace(receiver, monkeypatch):
    adapter, session, _ = receiver
    adapter.configure_sweep_timing(SpectrumTimingSettings(trigger_source="EXT"))
    assert adapter.start_live(True, allow_synchronized=True).synchronized
    query = session._query
    def waiting_for_pulse(command):
        latest_commands = session.commands[::-1]
        if (command == "INIT:SWP?" and "INIT:MODE:SING" in latest_commands
                and latest_commands.index("INIT:MODE:SING") < latest_commands.index("ABOR")):
            return "1"
        return query(command)

    monkeypatch.setattr(session, "_query", waiting_for_pulse)
    assert adapter.poll_synchronized_live() is None
    assert adapter.poll_synchronized_live() is None
    assert "*WAI" not in session.commands and "TRAC? TRAC1" not in session.commands
    adapter.stop_live()
    assert session.commands.count("INIT:MODE:SING") == 1
    assert session.commands[-2:] == ["INIT:CONT?", "SYST:ERR?"]


def test_frame_trigger_publishes_only_completed_sweeps(receiver):
    adapter, session, _ = receiver
    adapter.configure_sweep_timing(SpectrumTimingSettings(trigger_source="FRAM"))
    adapter.start_live(True, allow_synchronized=True)
    assert adapter.poll_synchronized_live() is None
    trace = adapter.poll_synchronized_live()
    assert trace.sweep_evidence == SweepEvidence.QUALIFIED_SINGLE_SWEEP
    assert len(trace.powers_dbm) == session.points


def test_timing_context_distinguishes_triggered_spectra_and_keeps_legacy_defaults(receiver):
    adapter, _, _ = receiver
    full, advanced = adapter.read_acquisition_configuration()
    baseline = spectrum_configuration_fingerprint(full, advanced, adapter.identity.idn)
    assert baseline == spectrum_configuration_fingerprint(
        replace(full, timing_context=None), replace(advanced, timing_context=None), adapter.identity.idn,
    )
    adapter.configure_sweep_timing(SpectrumTimingSettings(trigger_source="FRAM"))
    full, advanced = adapter.read_acquisition_configuration()
    assert spectrum_configuration_fingerprint(full, advanced, adapter.identity.idn) != baseline


@pytest.mark.parametrize("size,theme", [((680, 590), "light"), ((490, 440), "dark")])
def test_anritsu_timing_dialog_renders_and_applies_gate(receiver, size, theme, tmp_path):
    adapter, _, station = receiver
    application = QApplication.instance() or QApplication([])
    apply_application_theme(application, theme)
    controller = MagicMock()
    page = AnritsuPage(controller, station, single_sweep_available=True)
    dialog = page._sweep_timing_dialog
    try:
        page.resize(1440, 900)
        page.show()
        page.set_capabilities(adapter.capabilities)
        page._result("connect", adapter.identity)
        page.toggle_acquisition_controls.click()
        application.processEvents()
        assert page.sweep_timing_button.isVisible() and page.sweep_timing_button.isEnabled()
        page.sweep_timing_button.click()
        assert controller.call.call_args.args == ("read_sweep_timing",)
        page._result("read_sweep_timing", MODULE.dispatch(adapter, "read_sweep_timing", None))
        dialog.resize(*size)
        application.processEvents()
        assert "Sweep" in dialog.method.text()
        assert not dialog._editors["rbw_filter"].isEnabled()
        assert dialog._editors["rbw_filter"].findData("CISP") < 0
        assert dialog.grab().save(str(tmp_path / f"sweep-{theme}.png"))
        dialog.tabs.setCurrentIndex(2)
        navigation = dialog.tabs.navigation
        assert navigation.widget(navigation.currentRouteKey()).text() == "Gate"
        check = dialog._editors["gate_enabled"]
        assert check.isVisible() and check.height() > 0
        check.setChecked(True)
        source = dialog._editors["gate_source"]
        source.setCurrentIndex(source.findData("FRAM"))
        length = dialog._editors["gate_length_s"]
        length.setText("5 ms")
        application.processEvents()
        assert not dialog._editors["gate_level_dbm"].isVisible()
        assert length.isVisible() and length.height() > 0
        assert dialog.rect().contains(length.mapTo(dialog, QPoint(0, 0)))
        assert dialog.rect().contains(dialog.apply_button.mapTo(dialog, QPoint(0, 0)))
        assert dialog.grab().save(str(tmp_path / f"gate-{theme}.png"))
        dialog.apply_button.click()
        operation, config = controller.call.call_args.args
        assert operation == "configure_sweep_timing"
        assert config.sweep_rule == "OSW" and config.gate_length_s == 0.005
        page._result(operation, MODULE.dispatch(adapter, operation, config))
        assert "verified" in dialog.status.text()

        page.toggle_live()
        page._result("start_live", MODULE.dispatch(adapter, "start_live", True))
        page.fetch_live()
        assert controller.call.call_args.args == ("poll_synchronized_live", "TRAC1")
        page._result("poll_synchronized_live", adapter.poll_synchronized_live())
        assert page._live_frame_count == 0 and not page._fetch_pending
        page.fetch_live()
        page._result("poll_synchronized_live", adapter.poll_synchronized_live())
        assert page._live_frame_count == 1
        page.toggle_live()
        adapter.stop_live()
        page._result("stop_live", None)

        reference = page._build_reference(page._latest_trace, kind="single", count=1)
        target = tmp_path / "reference.h5"
        ReferenceHdf5Store.save(target, reference)
        assert ReferenceHdf5Store.load(target).timing_context == reference.timing_context
        assert "gate_length_s" in reference.timing_context
    finally:
        page.shutdown_analysis()
        dialog.close()
        page.close()
        page.deleteLater()
        application.processEvents()
