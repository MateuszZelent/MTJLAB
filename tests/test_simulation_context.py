from __future__ import annotations

import unittest


class SimulationContextTests(unittest.TestCase):
    def test_device_streams_are_reproducible_and_independent(self) -> None:
        from app.devices.simulation import SimulationContext

        first = SimulationContext(seed=7)
        second = SimulationContext(seed=7)

        self.assertEqual(
            first.random_stream("anritsu", "trace").random(),
            second.random_stream("anritsu", "trace").random(),
        )
        self.assertNotEqual(
            first.random_stream("anritsu", "trace").random(),
            first.random_stream("keithley", "measurement").random(),
        )

    def test_moke_module_connects_without_tcp_in_simulation(self) -> None:
        from app.devices.simulation import SimulationContext
        from app.devices.moke_box.adapter import MokeBoxAdapter
        from app.devices.moke_box.models import MokeBoxConfig
        from app.devices.moke_box.simulator import SimulatedMokeBoxTransport

        adapter = MokeBoxAdapter(
            MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM"),
            SimulatedMokeBoxTransport(SimulationContext(seed=7)),
        )

        self.assertTrue(adapter.connect().idn.startswith("MOKE"))
        adapter.disconnect()

    def test_anritsu_trace_uses_the_saved_seed(self) -> None:
        from app.devices.simulation import SimulationContext
        from app.devices.simulators import SimulatedVisaFactory

        def trace(seed: int) -> str:
            session = SimulatedVisaFactory(
                "anritsu", context=SimulationContext(seed=seed)
            ).open("SIM::ANRITSU", "@sim", 1_000)
            session.write("SWE:POIN 5")
            return session.query("TRAC? TRACE1")

        self.assertEqual(trace(17), trace(17))
        self.assertNotEqual(trace(17), trace(18))


if __name__ == "__main__":
    unittest.main()
