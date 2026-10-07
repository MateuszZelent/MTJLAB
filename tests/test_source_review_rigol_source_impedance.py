"""A configured resistor cannot weaken the hardware-only current bound."""

import pytest
from pydantic import ValidationError

from app.domain.errors import SafetyViolation
from app.safety.rigol_current import estimate_rigol_current
from app.settings.models import StationSettings
from tests.helpers import simulation_settings


@pytest.mark.parametrize("resistance", ["0 ohm", "49 ohm", "51 ohm", "5000 ohm"])
def test_settings_reject_nonphysical_internal_impedance(resistance):
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["fixed_source_resistance"] = resistance
    with pytest.raises(ValidationError, match="must equal"):
        StationSettings.model_validate(raw)


@pytest.mark.parametrize("load", ["HIGHZ", 50.])
@pytest.mark.parametrize("resistance", [49., 51., 5000.])
def test_direct_estimator_cannot_bypass_impedance_constraint(load, resistance):
    with pytest.raises(SafetyViolation, match="fixed hardware"):
        estimate_rigol_current(high_level=.1, low_level=-.1,
                               output_load=load, source_resistance=resistance)


@pytest.mark.parametrize("load,current,power", [("HIGHZ", .002, .00005), (50., .004, .0002)])
def test_current_and_power_bounds_use_physical_source_with_load_scaling(load, current, power):
    result = estimate_rigol_current(high_level=.1, low_level=-.1,
                                    output_load=load, source_resistance="0.05 kohm")
    assert result.source_resistance_ohm == 50.
    assert result.peak_absolute_current_a == pytest.approx(current)
    assert result.peak_estimated_dut_power_w == pytest.approx(power)
