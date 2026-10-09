"""Recovery of interrupted plans from explicitly recorded safe boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields as dataclass_fields, replace
from datetime import datetime
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.domain.errors import ExecutionError
from app.engine.compiler import ExecutionPlan, PlanAction, action_produces_checkpoint, controlled_output_endpoints

if TYPE_CHECKING:
    from app.devices.anritsu_ms2830a.adapter import SpectrumTrace


@dataclass(frozen=True, slots=True)
class RecoveredReference:
    trace: SpectrumTrace
    index: int
    fingerprint: str
    origin: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    path: Path
    stored_points: int
    next_action_index: int
    committed_points_found: int
    previous_status: str
    prelude_actions: tuple[PlanAction, ...]
    reference: RecoveredReference | None = None


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
                confirmed_states = {}
                reference = None
            else:
                stored_points, next_action_index, confirmed_states, reference_state = boundary
                reference = self._read_reference(h5, reference_state)
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
            prelude_actions=self._configuration_prelude(plan, next_action_index, confirmed_states),
            reference=reference,
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
    ) -> tuple[int, int, dict[str, Any], dict[str, Any]] | None:
        if "events/name" not in h5 or "events/message" not in h5:
            return None
        names_ds = h5["events/name"]
        messages_ds = h5["events/message"]
        from app.storage.event_log import committed_event_count
        try:
            total = committed_event_count(h5["events"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ExecutionError(f"Invalid recovery event log: {exc}") from exc
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
                    stored_points = payload["stored_points"]
                    next_action_index = payload["next_action_index"]
                    if type(stored_points) is not int or type(next_action_index) is not int:
                        raise ValueError("boundary counts must be exact integers")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ExecutionError("Malformed safe resume boundary event.") from exc
                if payload.get("plan_sha256") != plan.sha256:
                    continue
                expected_scope = controlled_output_endpoints(plan.actions)
                scope = payload.get("confirmed_output_scope", [])
                output_status = payload.get("output_status", {})
                if (not isinstance(scope, list) or not all(isinstance(endpoint, str) for endpoint in scope)
                        or not expected_scope <= set(scope) or not isinstance(output_status, dict)
                        or any(output_status.get(endpoint) != "off" for endpoint in expected_scope)):
                    raise ExecutionError("Recovery boundary lacks confirmed OFF proofs for its instrument outputs.")
                if not 0 <= stored_points <= committed_count:
                    raise ExecutionError("Safe boundary point count exceeds committed data.")
                if not 0 <= next_action_index <= len(plan.actions):
                    raise ExecutionError("Safe boundary action index is outside the plan.")
                acquired = sum(action_produces_checkpoint(action) for action in plan.actions[:next_action_index])
                if acquired != stored_points:
                    raise ExecutionError(
                        "Safe boundary point count does not match completed acquisition actions."
                    )
                snapshot = payload.get("state_snapshot", {}).get("device_states", {})
                if not isinstance(snapshot, dict):
                    raise ExecutionError("Safe boundary configuration snapshot is malformed.")
                reference = payload.get("state_snapshot", {}).get("reference", {})
                if not isinstance(reference, dict):
                    raise ExecutionError("Safe boundary reference snapshot is malformed.")
                return stored_points, next_action_index, snapshot, reference
        return None

    @staticmethod
    def _read_reference(h5: Any, state: dict[str, Any]) -> RecoveredReference | None:
        if not state or state.get("index") is None:
            return None
        from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
        from app.storage.hdf5_writer import Hdf5RunWriter

        index = state["index"]
        fingerprint = state.get("fingerprint")
        if type(index) is not int or index < 0 or not isinstance(fingerprint, str) or not fingerprint:
            raise ExecutionError("Recovery reference lacks a qualified identity.")
        key = f"references/{index}"
        if key not in h5:
            raise ExecutionError("Recovery reference is missing from the archive.")
        group = h5[key]
        from app.storage.reference_transaction import require_committed_reference

        require_committed_reference(group)
        try:
            trace = SpectrumTrace(tuple(group["frequency_hz"][:]), tuple(group["power_dbm"][:]),
                datetime.fromisoformat(str(group.attrs["acquired_at_utc"])), str(group.attrs["trace_name"]))
            Hdf5RunWriter._validate_trace(trace)
            origin = state.get("origin", {})
            if not isinstance(origin, dict):
                raise ValueError("origin must be a mapping")
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("Recovery reference is malformed.") from exc
        return RecoveredReference(trace, index, fingerprint, origin)

    @staticmethod
    def _configuration_prelude(
        plan: ExecutionPlan,
        next_action_index: int,
        confirmed_states: dict[str, Any] | None = None,
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
            elif action.kind == "configure_rigol_output":
                key = ("rigol_output", action.payload["config"].channel)
            elif action.kind == "configure_anritsu_sg":
                key = ("anritsu", "sg")
            elif action.kind in {"update_keithley_level", "update_keithley_compliance"}:
                key = ("keithley", action.payload["channel"])
                if key not in latest:
                    raise ExecutionError("Recovery source update lacks an explicit baseline.")
                old_index, baseline = latest[key]
                field = "level_si" if action.kind == "update_keithley_level" else "compliance_si"
                request = replace(baseline.payload["request"], **{field: action.payload[field]})
                latest[key] = (old_index, replace(baseline, payload={"request": request}))
                continue
            elif action.kind in {"update_rigol_frequency", "update_rigol_levels", "update_anritsu_sg"}:
                key = ("anritsu", "sg") if action.kind == "update_anritsu_sg" else ("rigol", action.payload["channel"])
                if key not in latest:
                    raise ExecutionError("Recovery carrier update lacks an explicit baseline.")
                old_index, baseline = latest[key]
                config = baseline.payload["config"]
                if action.kind == "update_anritsu_sg":
                    update = action.payload["config"]
                    fields = update.changed_fields or ("frequency_hz", "power_dbm")
                    config = replace(config, **{field: getattr(update, field) for field in fields}, changed_fields=None)
                else:
                    fields = ("frequency_hz",) if action.kind == "update_rigol_frequency" else ("high_level_v", "low_level_v")
                    config = replace(config, **{field: action.payload[field] for field in fields})
                latest[key] = (old_index, replace(baseline, payload={"config": config}))
                continue
            else:
                continue
            payload_key = "request" if action.kind == "configure_keithley" else "config"
            update = action.payload[payload_key]
            previous = latest.get(key)
            changed_fields = getattr(update, "changed_fields", None)
            if changed_fields is not None:
                if previous is None:
                    # An authored initial configuration may itself be a patch.
                    # Retain its mask until the durable actual snapshot resolves
                    # every omitted field; never substitute request defaults.
                    is_initial_keithley = action.kind == "configure_keithley" and "mode" in changed_fields
                    is_initial_rigol = action.kind == "configure_rigol" and {"waveform", "frequency_hz", "high_level_v", "low_level_v"} <= set(changed_fields)
                    is_initial_anritsu = action.kind == "configure_anritsu" and {"start_hz", "stop_hz", "reference_level_dbm", "points"} <= set(changed_fields)
                    is_initial_output_path = action.kind == "configure_rigol_output"
                    if not (is_initial_keithley or is_initial_rigol or is_initial_anritsu or is_initial_output_path):
                        raise ExecutionError("Recovery selected-field operation lacks its baseline.")
                else:
                    baseline = previous[1].payload[payload_key]
                    old_mask = getattr(baseline, "changed_fields", None)
                    mask = None if old_mask is None else tuple(dict.fromkeys((*old_mask, *changed_fields)))
                    update = replace(baseline, **{
                        field: getattr(update, field) for field in changed_fields
                    }, changed_fields=mask)
            elif action.kind == "configure_anritsu_advanced" and previous is not None:
                update = replace(previous[1].payload[payload_key], **{
                    field: value for field, value in asdict(update).items() if value is not None
                })
            action = replace(action, payload={**action.payload, payload_key: update})
            latest[key] = (index, action)
        result = []
        for key, (_index, action) in sorted(latest.items(), key=lambda item: item[1][0]):
            payload_key = "request" if action.kind == "configure_keithley" else "config"
            request = action.payload[payload_key]
            if action.kind == "configure_keithley":
                actual = (confirmed_states or {}).get("keithley", {}).get(f"channel_{key[1]}", {}).get("actual", {})
                if actual:
                    values = {}
                    for field in dataclass_fields(request):
                        name = field.name
                        if name in {"channel", "changed_fields"}:
                            continue
                        source_name = "source_level_si" if name == "level_si" else name
                        if source_name not in actual:
                            raise ExecutionError(f"Recovery Keithley snapshot lacks {source_name}.")
                        values[name] = actual[source_name]
                    request = replace(request, **values, changed_fields=None)
                elif request.changed_fields is not None:
                    raise ExecutionError("Recovery requires the confirmed Keithley configuration snapshot; request defaults are insufficient.")
                # Recovery must use the same verified-delta path as a sweep,
                # not the manual full-reset path (which also sets offmode).
                source_only = {"level_si", "compliance_si", "source_autorange", "source_range_si"}
                request = replace(request, changed_fields=tuple(
                    field.name for field in dataclass_fields(request)
                    if field.name not in {"channel", "changed_fields"}
                    and getattr(request, field.name) is not None
                    and not (request.mode == "measure_only" and field.name in source_only)
                ))
            elif action.kind == "configure_rigol" and request.changed_fields is not None:
                actual = (confirmed_states or {}).get("rigol", {}).get(f"channel_{key[1]}", {}).get("actual", {})
                names = {field.name for field in dataclass_fields(request)} - {"channel", "changed_fields"}
                if not names <= actual.keys():
                    raise ExecutionError("Recovery requires the confirmed Rigol carrier snapshot; request defaults are insufficient.")
                # Restore known physical fields, preserving the existing basic
                # mode rather than issuing the manual full-reset transaction.
                request = replace(request, **{name: actual[name] for name in names},
                    changed_fields=tuple(name for name in names if actual[name] is not None))
            elif action.kind == "configure_rigol_output" and request.changed_fields is not None:
                actual = (confirmed_states or {}).get("rigol", {}).get(f"channel_{key[1]}", {}).get("actual", {}).get("output_path", {})
                names = {field.name for field in dataclass_fields(request)} - {"channel", "changed_fields"}
                if not names <= actual.keys():
                    raise ExecutionError("Recovery requires the confirmed Rigol output-path snapshot; request defaults are insufficient.")
                request = replace(request, **{name: actual[name] for name in names}, changed_fields=tuple(sorted(names)))
            result.append(replace(action, payload={**action.payload, payload_key: request}))
        advanced = (confirmed_states or {}).get("anritsu", {}).get("advanced_spectrum", {}).get("actual")
        if isinstance(advanced, dict) and ("anritsu", "spectrum") in latest:
            from app.devices.anritsu_ms2830a.adapter import AdvancedSpectrumConfig

            names = {field.name for field in dataclass_fields(AdvancedSpectrumConfig)}
            if not names <= advanced.keys():
                raise ExecutionError("Recovery analyzer snapshot lacks qualified advanced settings.")
            if advanced["vbw_filter_mode"] not in {"VID", "POW"}:
                raise ExecutionError("Recovery requires a confirmed analyzer Video/Power mode.")
            config = AdvancedSpectrumConfig(**{name: advanced[name] for name in names})
            # Automatic modes own their numeric readbacks. Writing a cached
            # manual value would silently disable AUTO on some instruments.
            config = replace(config,
                rbw_hz=None if config.rbw_auto else config.rbw_hz,
                vbw_hz=None if config.vbw_mode != "manual" else config.vbw_hz,
                attenuation_db=None if config.attenuation_auto else config.attenuation_db,
                sweep_time_s=None if config.sweep_time_auto else config.sweep_time_s)
            result = [action for action in result if action.kind != "configure_anritsu_advanced"]
            spectrum_index = next(index for index, action in enumerate(result) if action.kind == "configure_anritsu")
            result.insert(spectrum_index + 1, PlanAction("recovery.anritsu.advanced", "configure_anritsu_advanced", {"config": config}, {}))
        return tuple(result)
