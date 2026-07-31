"""Deterministic decimal quantisation used at instrument boundaries."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
import math

from app.domain.errors import SafetyViolation


def quantize_to_step(value: float, step: float, *, name: str) -> float:
    """Round a finite value to a positive hardware step.

    ``round(float, ...)`` is deliberately not used here: binary floating-point
    and Python's ties-to-even rule can produce a different wire value at an
    instrument boundary.  Decimal half-up rounding keeps the value sent to
    the device deterministic and auditable.
    """

    if not math.isfinite(value):
        raise SafetyViolation(f"{name} must be finite before quantisation.")
    if not math.isfinite(step) or step <= 0:
        raise SafetyViolation(f"{name} quantisation step must be finite and positive.")

    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    with localcontext() as context:
        # The context must accommodate the complete input decimal.  Sweep
        # interpolation can otherwise exceed the default 28-digit precision
        # before it is reduced to the instrument's resolution.
        context.prec = max(
            28,
            len(value_decimal.as_tuple().digits)
            + len(step_decimal.as_tuple().digits)
            + 16,
        )
        units = (value_decimal / step_decimal).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        quantized = units * step_decimal
    result = float(quantized)
    if not math.isfinite(result):
        raise SafetyViolation(f"{name} became non-finite after quantisation.")
    return result
