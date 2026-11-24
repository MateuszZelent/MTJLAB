from __future__ import annotations

from datetime import UTC, datetime
from math import isnan

import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.spectrum.display_model import SpectrumDisplayState, build_display_state


def _trace(name: str, values: tuple[float, ...]) -> SpectrumTrace:
    return SpectrumTrace(
        frequencies_hz=(1.0, 2.0, 3.0),
        powers_dbm=values,
        acquired_at_utc=datetime(2026, 8, 1, tzinfo=UTC),
        trace_name=name,
    )


def test_display_state_derives_processed_trace_and_selects_it_as_primary() -> None:
    raw = _trace("TRAC1", (-30.0, -20.0, -10.0))
    reference = _trace("reference", (-40.0, -30.0, -20.0))

    state = build_display_state(
        raw=raw,
        averaged=None,
        reference=reference,
        reference_operation="difference_db",
        visible={"raw": True, "reference": True, "processed": True},
        frame_id=7,
    )

    assert state.primary_key == "processed"
    assert state.selected_key == "processed"
    processed = state.by_key["processed"]
    assert processed.values == (10.0, 10.0, 10.0)
    assert processed.unit == "dB"
    assert processed.frame_id == 7
    assert processed.provenance == ("raw", "reference", "difference_db")


def test_selected_trace_is_retained_when_available_and_falls_back_when_hidden() -> None:
    raw = _trace("TRAC1", (-30.0, -20.0, -10.0))
    averaged = _trace("average", (-29.0, -19.0, -9.0))

    first = build_display_state(
        raw=raw,
        averaged=averaged,
        reference=None,
        reference_operation="none",
        visible={"raw": True, "averaged": True},
        frame_id=1,
        preferred_key="averaged",
    )
    assert first.selected_key == "averaged"

    retained = build_display_state(
        raw=raw,
        averaged=averaged,
        reference=None,
        reference_operation="none",
        visible={"raw": True, "averaged": True},
        frame_id=2,
        preferred_key=first.selected_key,
    )
    assert retained.selected_key == "averaged"

    fallback = build_display_state(
        raw=raw,
        averaged=averaged,
        reference=None,
        reference_operation="none",
        visible={"raw": True, "averaged": False},
        frame_id=3,
        preferred_key="averaged",
    )
    assert fallback.selected_key == "raw"
    assert fallback.primary_key == "raw"


def test_state_rejects_unknown_selection_and_keeps_units_explicit() -> None:
    raw = _trace("TRAC1", (-30.0, -20.0, -10.0))
    state = build_display_state(
        raw=raw,
        averaged=None,
        reference=None,
        reference_operation="none",
        visible={"raw": True},
        frame_id=9,
    )

    with pytest.raises(KeyError):
        state.select("missing")
    assert isinstance(state, SpectrumDisplayState)
    assert state.selected.unit == "dBm"


def test_live_frame_uses_same_reference_operation_and_keeps_undefined_power_gaps() -> None:
    raw = _trace("TRAC1", (-70., -60., -50.))
    average = _trace("Live average", (-60., -60., -60.))
    reference = _trace("reference", (-65., -65., -65.))

    state = build_display_state(
        raw=raw,
        averaged=average,
        reference=reference,
        reference_operation="subtract_power",
        use_average_for_reference=True,
        show_live_frame=True,
        visible={"raw": False, "averaged": False, "reference": False},
        frame_id=10,
    )

    assert state.primary_key == state.selected_key == "processed"
    curve = state.by_key["live_frame"]
    assert curve.unit == state.by_key["processed"].unit == "dBm"
    assert isnan(curve.values[0])
    assert curve.values[1] == state.by_key["processed"].values[1]
    assert curve.values[2] > state.by_key["processed"].values[2]
