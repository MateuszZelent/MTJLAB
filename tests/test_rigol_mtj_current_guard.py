"""MTJ current guard rejects unsafe DC before instrument traffic, for either LOAD."""

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import SafetyViolation
from app.settings.repository import SettingsRepository
from tests.helpers import simulation_settings


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("load,multiplier", [("HIGHZ", 1), (50.0, 2)])
def test_4_ma_guard_survives_reload_and_checks_dc_before_traffic(
    tmp_path, channel, sign, load, multiplier
):
    raw = simulation_settings().model_dump(mode="python")
    for item in raw["devices"]["rigol"]["safety"]["channels"].values():
        item["enabled"] = True
        item["lab_limits"]["combined_voltage_limit"] = "600 mV"
        item["lab_limits"]["estimated_load_current"].update(
            min="-4 mA", max="4 mA", max_abs="4 mA", enabled=True
        )
    repository = SettingsRepository(tmp_path / "settings.yml")
    repository.save_raw(raw)
    settings = repository.load().settings
    session = RigolSimulator()
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    for open_v in (.18, .2):
        level_v = sign * open_v / multiplier
        estimate = adapter.configure_channel(
            RigolChannelConfig(channel, "DC", 1.0, level_v, level_v, output_load=load)
        )
        assert estimate.peak_absolute_current_a <= .004
        assert not session.output[channel]
    for open_v in (.2002, .6):
        level_v = sign * open_v / multiplier
        before = tuple(session.commands)
        with pytest.raises(SafetyViolation, match="Estimated Rigol load current"):
            adapter.configure_channel(
                RigolChannelConfig(channel, "DC", 1.0, level_v, level_v, output_load=load)
            )
        assert tuple(session.commands) == before
        assert not session.output[channel]
