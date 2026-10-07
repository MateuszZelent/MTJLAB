"""Slow OFF proof never freezes the card or enables after cancellation/stale input."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QThread, QTimer

from tests.test_source_review_characterization_gaps import card as card, qt_application as qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("ending", ["success", "stop", "close", "changed_config", "changed_settings", "on", "failed"])
def test_slow_output_readback_is_cancellable_and_revalidated(card, qt_application, monkeypatch, ending):
    entered, release = threading.Event(), threading.Event()
    ticks = []
    config = object()
    monkeypatch.setattr(card, "_build_config", lambda **_: config)
    continuation = Mock()
    monkeypatch.setattr(card, "_continue_single_start", continuation)

    def read():
        assert QThread.currentThread() != qt_application.thread()
        entered.set()
        assert release.wait(5)
        if ending == "failed":
            raise TimeoutError("readback timeout")
        return SimpleNamespace(channels=(SimpleNamespace(channel="A", output_enabled=ending == "on"),))

    proxy = SimpleNamespace(connected=True, read_configuration=read, compliance_policy=Mock(return_value="stop"))
    card.resize(1366, 768)
    card.show()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        timer.start(5)
        card._request_output_proof(proxy, "A", config)
        wait_until(qt_application, lambda: entered.is_set() and len(ticks) >= 3)
        assert not card.start_button.isEnabled()
        if ending == "stop":
            card._on_stop_clicked()
        elif ending == "close":
            assert not card.close()
            assert card.isVisible()
        elif ending == "changed_config":
            monkeypatch.setattr(card, "_build_config", lambda **_: object())
        elif ending == "changed_settings":
            card._settings.execution["retry_count"] = 5
        release.set()
        wait_until(qt_application, lambda: card._output_proof_worker is None)
        assert continuation.call_count == (1 if ending == "success" else 0)
        if ending in {"stop", "close", "on", "failed"}:
            proxy.compliance_policy.assert_not_called()
        assert card.start_button.isEnabled()
        assert card.prepare_application_shutdown()
    finally:
        release.set()
        timer.stop()
        wait_until(qt_application, lambda: card._output_proof_worker is None)


@pytest.mark.parametrize("confirmed", [True, False])
def test_retry_restore_requires_positive_off_proof_outside_gui(card, qt_application, monkeypatch, confirmed):
    def confirm(channel):
        assert QThread.currentThread() != qt_application.thread()
        assert channel == "B"
        return confirmed

    proxy = SimpleNamespace(connected=True, confirm_output_off=confirm)
    card._controller.adapter_for_run.return_value = proxy
    card._temporary_policy_phase = "restore_failed"
    card._temporary_policy_channel = "B"
    card._temporary_policy_original = "stop"
    restored = Mock()
    monkeypatch.setattr(card, "_begin_policy_restore", restored)
    try:
        card._on_policy_retry_clicked()
        wait_until(qt_application, lambda: card._output_proof_worker is None)
        assert restored.call_count == (1 if confirmed else 0)
        if not confirmed:
            assert "not confirmed" in card.banner.last_message
    finally:
        card._temporary_policy_phase = "idle"


@pytest.mark.parametrize("ending", ["success", "stop", "close", "changed", "failed_b"])
def test_field_preflight_reads_both_channels_off_gui_before_review(card, qt_application, monkeypatch, ending):
    entered, release = threading.Event(), threading.Event()
    proof_channels, ticks = [], []
    config = object()
    monkeypatch.setattr(card, "_build_field_start_config", lambda: config)
    monkeypatch.setattr(card, "_selected_channel", lambda: "A")
    continuation = Mock()
    monkeypatch.setattr(card, "_continue_field_start", continuation)

    def policy(channel):
        assert QThread.currentThread() != qt_application.thread()
        if channel == "A":
            entered.set()
            assert release.wait(5)
        return "warn_clamp" if channel == "A" else "skip"

    def confirm(channel):
        assert QThread.currentThread() != qt_application.thread()
        proof_channels.append(channel)
        return not (channel == "B" and ending == "failed_b")

    proxy = SimpleNamespace(connected=True, compliance_policy=policy, confirm_output_off=confirm)
    card.show()
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    try:
        timer.start(5)
        card._request_output_proof(proxy, "A", config, field_series=True)
        wait_until(qt_application, lambda: entered.is_set() and len(ticks) >= 3)
        assert not card.start_button.isEnabled()
        if ending == "stop":
            card._on_stop_clicked()
        elif ending == "close":
            assert not card.close()
        elif ending == "changed":
            monkeypatch.setattr(card, "_build_field_start_config", lambda: object())
        release.set()
        wait_until(qt_application, lambda: card._output_proof_worker is None)
        if ending == "success":
            continuation.assert_called_once_with(config, {"A": "warn_clamp", "B": "skip"})
        else:
            continuation.assert_not_called()
        assert proof_channels == ([] if ending in {"stop", "close"} else ["A", "B"])
        assert card.start_button.isEnabled()
    finally:
        release.set()
        timer.stop()
        wait_until(qt_application, lambda: card._output_proof_worker is None)
