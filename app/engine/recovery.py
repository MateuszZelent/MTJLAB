"""Recovery of interrupted plans from explicitly recorded safe boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.domain.errors import ExecutionError
from app.engine.compiler import ExecutionPlan, PlanAction


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    path: Path
    stored_points: int
    next_action_index: int
    committed_points_found: int
    previous_status: str
    prelude_actions: tuple[PlanAction, ...]


class RunRecoveryManager:
    """Inspect an artefact without mutating it and select its last safe boundary."""

    def inspect(self, path: str | Path, plan: ExecutionPlan) -> RecoveryCheckpoint:
        import h5py

        target = Path(path)
        try:
            h5 = h5py.File(target, "r")
        except OSError as exc:
            raise ExecutionError(f"Cannot open recovery file: {target}") from exc
        with h5:
            run = h5.get("run")
            if run is None:
                raise ExecutionError("Recovery requires private /run metadata.")
            if str(run.attrs.get("plan_sha256", "")) != plan.sha256:
                raise ExecutionError("Recovery plan hash does not match the stored run.")
            status = str(run.attrs.get("status", "incomplete"))
            if status == "completed":
                raise ExecutionError("A completed run cannot be resumed.")
            committed = self._committed_points(h5)
            boundary = self._latest_boundary(h5, plan, len(committed))
            if boundary is None:
                if committed:
                    raise ExecutionError(
                        "The run contains points but no confirmed safe resume boundary."
                    )
                stored_points, next_action_index = 0, 0
            else:
                stored_points, next_action_index = boundary
            for index in range(stored_points):
                if not bool(h5[f"points/{index}"].attrs.get("complete", False)):
                    raise ExecutionError(
                        f"Checkpoint {index} before the recovery boundary is incomplete."
                    )
        return RecoveryCheckpoint(
            path=target,
            stored_points=stored_points,
            next_action_index=next_action_index,
            committed_points_found=len(committed),
            previous_status=status,
            prelude_actions=self._configuration_prelude(plan, next_action_index),
        )

    @staticmethod
    def _committed_points(h5: Any) -> tuple[int, ...]:
        if "points" not in h5:
            raise ExecutionError("Recovery file has no private /points group.")
        indices = tuple(sorted(int(name) for name in h5["points"] if str(name).isdigit()))
        if indices != tuple(range(len(indices))):
            raise ExecutionError("Recovery checkpoints are not contiguous from zero.")
        return indices

    @staticmethod
    def _latest_boundary(
        h5: Any,
        plan: ExecutionPlan,
        committed_count: int,
    ) -> tuple[int, int] | None:
        if "events/name" not in h5 or "events/message" not in h5:
            return None
        names_ds = h5["events/name"]
        messages_ds = h5["events/message"]
        total = len(names_ds)
        if total != len(messages_ds):
            raise ExecutionError("Recovery event arrays have different lengths.")
        if total == 0:
            return None

        # Scan backwards in bounded chunks (500 events) to prevent huge memory spikes (GUI-02)
        chunk_size = 500
        for end in range(total, 0, -chunk_size):
            start = max(0, end - chunk_size)
            chunk_names = tuple(str(value) for value in names_ds.asstr()[start:end])
            chunk_messages = tuple(str(value) for value in messages_ds.asstr()[start:end])
            for name, message in reversed(tuple(zip(chunk_names, chunk_messages))):
                if name != "safe_resume_boundary":
                    continue
                try:
                    payload = json.loads(message)
                    stored_points = int(payload["stored_points"])
                    next_action_index = int(payload["next_action_index"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ExecutionError("Malformed safe resume boundary event.") from exc
                if payload.get("plan_sha256") != plan.sha256:
                    continue
                if not 0 <= stored_points <= committed_count:
                    raise ExecutionError("Safe boundary point count exceeds committed data.")
                if not 0 <= next_action_index <= len(plan.actions):
                    raise ExecutionError("Safe boundary action index is outside the plan.")
                acquired = sum(
                    action.kind in {"acquire_spectrum", "checkpoint"}
                    for action in plan.actions[:next_action_index]
                )
                if acquired != stored_points:
                    raise ExecutionError(
                        "Safe boundary point count does not match completed acquisition actions."
                    )
                return stored_points, next_action_index
        return None

    @staticmethod
    def _configuration_prelude(
        plan: ExecutionPlan,
        next_action_index: int,
    ) -> tuple[PlanAction, ...]:
        latest: dict[tuple[str, object], tuple[int, PlanAction]] = {}
        for index, action in enumerate(plan.actions[:next_action_index]):
            if action.kind == "configure_rigol":
                key = ("rigol", action.payload["config"].channel)
            elif action.kind == "configure_keithley":
                key = ("keithley", action.payload["request"].channel)
            elif action.kind == "configure_anritsu":
                key = ("anritsu", "spectrum")
            elif action.kind == "configure_anritsu_advanced":
                key = ("anritsu", "advanced")
            else:
                continue
            latest[key] = (index, action)
        return tuple(action for _index, action in sorted(latest.values(), key=lambda item: item[0]))
