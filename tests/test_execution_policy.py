from __future__ import annotations

from contextlib import contextmanager
import time
import unittest
from uuid import UUID

from app.devices.anritsu import SpectrumConfig
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.policy import ExecutionPolicy
from app.engine.runner import RecipeRunner
from tests.helpers import loaded_settings


class _MemoryWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object], str]] = []
        self.closed: str | None = None
        self.points: list[tuple[object, object]] = []

    def append_event(self, name, payload, *, severity="info") -> None:
        self.events.append((name, payload, severity))

    def append(self, point, trace=None) -> None:
        self.points.append((point, trace))

    def close(self, status) -> None:
        self.closed = status


class _PassiveAdapter:
    def __init__(self) -> None:
        self.state = DeviceState.OUTPUT_OFF

    @contextmanager
    def io_timeout(self, timeout_s):
        self.last_timeout_s = timeout_s
        yield

    def emergency_off(self) -> None:
        self.state = DeviceState.OUTPUT_OFF


class _RetryingAnritsu(_PassiveAdapter):
    def __init__(self, *, failures: int = 0, delay_s: float = 0.0) -> None:
        super().__init__()
        self.failures = failures
        self.delay_s = delay_s
        self.configure_calls = 0

    def configure_spectrum(self, config) -> None:
        del config
        self.configure_calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.configure_calls <= self.failures:
            raise DeviceError("temporary simulated error")


class _FailingKeithley(_PassiveAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.output_calls = 0

    def set_output(self, channel, enabled) -> None:
        del channel, enabled
        self.output_calls += 1
        raise DeviceError("ambiguous OUTPUT ON failure")


def _plan(action: PlanAction, required: str) -> ExecutionPlan:
    return ExecutionPlan(
        recipe_name="policy-test",
        actions=(action,),
        total_points=0,
        sha256="policy-test",
        recipe_source="schema_version: 1\n",
        required_devices=frozenset({required}),
    )


class ExecutionPolicyTests(unittest.TestCase):
    def test_policy_is_loaded_from_station_settings(self) -> None:
        policy = ExecutionPolicy.from_settings(loaded_settings())
        self.assertEqual(policy.command_timeout_s, 5.0)
        self.assertEqual(policy.retry_count, 1)
        self.assertEqual(policy.retry_backoff_s, 0.25)
        self.assertGreater(policy.acquisition_timeout_s, policy.command_timeout_s)

    def test_passive_configuration_retries_and_records_attempt(self) -> None:
        writer = _MemoryWriter()
        anritsu = _RetryingAnritsu(failures=1)
        passive = _PassiveAdapter()
        action = PlanAction(
            "configure",
            "configure_anritsu",
            {"config": SpectrumConfig(1e6, 2e6, 0.0, 101)},
            {},
        )
        result = RecipeRunner(
            rigol=passive,
            keithley=passive,
            anritsu=anritsu,
            writer=writer,
            policy=ExecutionPolicy(retry_count=1, retry_backoff_s=0.0),
        ).run(_plan(action, "anritsu"))

        self.assertIsNone(result.error)
        self.assertEqual(anritsu.configure_calls, 2)
        self.assertIn("action_retry", [event[0] for event in writer.events])
        self.assertEqual(writer.closed, "completed")

    def test_every_run_event_carries_unique_correlation_cancellation_and_state_snapshot(self) -> None:
        writer = _MemoryWriter()
        passive = _PassiveAdapter()
        action = PlanAction("checkpoint", "checkpoint", {"label": "state"}, {})
        runner = RecipeRunner(
            rigol=passive,
            keithley=passive,
            anritsu=passive,
            writer=writer,
        )

        first = runner.run(_plan(action, "anritsu"))

        self.assertIsNone(first.error)
        correlation_ids = {str(payload["correlation_id"]) for _, payload, _ in writer.events}
        self.assertEqual(len(correlation_ids), 1)
        correlation_id = correlation_ids.pop()
        UUID(correlation_id)
        for _name, payload, _severity in writer.events:
            self.assertEqual(payload["cancellation_token_id"], correlation_id)
            self.assertIn("cancellation_requested", payload)
            snapshot = payload["state_snapshot"]
            self.assertIn("application", snapshot)
            self.assertEqual(set(snapshot["devices"]), {"rigol", "keithley", "anritsu"})
        started = next(payload for name, payload, _ in writer.events if name == "action_started")
        self.assertGreater(started["deadline_s"], 0)

        second_writer = _MemoryWriter()
        RecipeRunner(
            rigol=passive,
            keithley=passive,
            anritsu=passive,
            writer=second_writer,
        ).run(_plan(action, "anritsu"))
        second_id = next(
            payload["correlation_id"]
            for name, payload, _ in second_writer.events
            if name == "run_started"
        )
        self.assertNotEqual(second_id, correlation_id)

    def test_output_on_is_never_retried_after_an_ambiguous_failure(self) -> None:
        writer = _MemoryWriter()
        keithley = _FailingKeithley()
        passive = _PassiveAdapter()
        action = PlanAction(
            "output-on",
            "set_keithley_output",
            {"channel": "B", "enabled": True},
            {},
        )
        result = RecipeRunner(
            rigol=passive,
            keithley=keithley,
            anritsu=passive,
            writer=writer,
            policy=ExecutionPolicy(retry_count=5, retry_backoff_s=0.0),
        ).run(_plan(action, "keithley"))

        self.assertIsNotNone(result.error)
        self.assertEqual(keithley.output_calls, 1)
        self.assertNotIn("action_retry", [event[0] for event in writer.events])
        self.assertEqual(writer.closed, "faulted")

    def test_watchdog_deadline_faults_run_and_emits_out_of_band_event(self) -> None:
        writer = _MemoryWriter()
        telemetry: list[tuple[str, dict[str, object]]] = []
        anritsu = _RetryingAnritsu(delay_s=0.08)
        passive = _PassiveAdapter()
        action = PlanAction(
            "slow-configure",
            "configure_anritsu",
            {"config": SpectrumConfig(1e6, 2e6, 0.0, 101)},
            {},
        )
        result = RecipeRunner(
            rigol=passive,
            keithley=passive,
            anritsu=anritsu,
            writer=writer,
            on_telemetry=lambda name, data: telemetry.append((name, data)),
            policy=ExecutionPolicy(
                command_timeout_s=0.02,
                retry_count=0,
                heartbeat_interval_s=0.005,
                watchdog_grace_s=0.0,
            ),
        ).run(_plan(action, "anritsu"))

        self.assertIsNotNone(result.error)
        self.assertEqual(writer.closed, "faulted")
        names = [name for name, _data in telemetry]
        self.assertIn("runner_heartbeat", names)
        self.assertEqual(names.count("watchdog_timeout"), 1)

    def test_invalid_policy_values_are_rejected(self) -> None:
        for kwargs in (
            {"command_timeout_s": 0.0},
            {"retry_count": 6},
            {"retry_backoff_s": -1.0},
            {"heartbeat_interval_s": float("nan")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                ExecutionPolicy(**kwargs)

    def test_explicit_checkpoint_stores_scalar_state_without_a_spectrum(self) -> None:
        writer = _MemoryWriter()
        passive = _PassiveAdapter()
        action = PlanAction(
            "checkpoint",
            "checkpoint",
            {"label": "after measurement"},
            {"repeat.sample.index": 2.0},
        )
        plan = ExecutionPlan(
            recipe_name="checkpoint",
            actions=(action,),
            total_points=1,
            sha256="checkpoint",
            recipe_source="schema_version: 1\n",
        )
        result = RecipeRunner(
            rigol=passive,
            keithley=passive,
            anritsu=passive,
            writer=writer,
        ).run(plan)

        self.assertEqual(result.stored_points, 1)
        self.assertEqual(len(writer.points), 1)
        point, trace = writer.points[0]
        self.assertIsNone(trace)
        self.assertEqual(point.metadata["checkpoint_label"], "after measurement")


if __name__ == "__main__":
    unittest.main()
