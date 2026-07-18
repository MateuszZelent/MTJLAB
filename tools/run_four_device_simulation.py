"""Generate a deterministic four-device synthetic sweep artefact.

This is an acceptance utility: it does not contact laboratory hardware.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.devices.anritsu import AnritsuAdapter
from app.devices.anritsu.adapter import SpectrumConfig
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.moke_box import MokeBoxAdapter, MokeBoxConfig
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.rigol import RigolAdapter
from app.devices.rigol.adapter import RigolChannelConfig
from app.devices.simulation import SimulationContext
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.runner import RecipeRunner
from app.storage import Hdf5RunWriter
from app.settings import SettingsRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Target HDF5 file; it must not exist.")
    parser.add_argument("--seed", type=int, default=2_026_0718)
    args = parser.parse_args()

    settings_template = Path(__file__).resolve().parents[1] / "app" / "resources" / "settings.template.yml"
    settings = simulated_station_settings(SettingsRepository(settings_template).load().settings)
    context = SimulationContext(seed=args.seed)
    rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol", context=context))
    keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley", context=context))
    anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu", context=context))
    moke = MokeBoxAdapter(
        MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM"),
        SimulatedMokeBoxTransport(context),
    )
    devices = (rigol, keithley, anritsu, moke)
    for device in devices:
        device.connect()
    plan = ExecutionPlan(
        recipe_name="four-device-simulation",
        actions=(
            PlanAction("rigol", "configure_rigol", {"config": RigolChannelConfig(1, "SIN", 1_000.0, 0.001, -0.001, dut_min_impedance_ohm=50)}, {}),
            PlanAction("keithley", "configure_keithley", {"request": KeithleySourceRequest("B", "current", 0.001, 0.067)}, {}),
            PlanAction("anritsu", "configure_anritsu", {"config": SpectrumConfig(1e6, 2e6, 0.0, 101)}, {}),
            PlanAction("moke", "measure_moke_hall", {"checkpoint": False}, {}),
            PlanAction("spectrum", "acquire_spectrum", {"trace": "TRAC1", "average_count": 1}, {}),
        ),
        total_points=1,
        sha256="four-device-simulation",
        recipe_source="schema_version: 1\nname: four-device-simulation\n",
        required_devices=frozenset({"rigol", "keithley", "anritsu", "moke_box"}),
    )
    writer = Hdf5RunWriter(
        args.output,
        recipe_source=plan.recipe_source,
        settings_source="simulation: true\n",
        plan_hash=plan.sha256,
        device_idn={name: "SIM" for name in ("rigol", "keithley", "anritsu", "moke_box")},
        simulation_metadata=context.metadata(("rigol", "keithley", "anritsu", "moke_box")),
    )
    try:
        result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, moke_box=moke, writer=writer).run(plan)
        if result.error:
            raise SystemExit(result.error)
        print(args.output.resolve())
    finally:
        for device in devices:
            device.disconnect()


if __name__ == "__main__":
    main()
