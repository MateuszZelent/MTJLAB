"""Extract the sweep audit's simulator-only test evidence, without instrument I/O."""

import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import h5py

from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.recipes import parse_recipe_text
from tests.test_sweep_audit_contracts import audit_settings


def main():
    source_path = Path(sys.argv[1])
    destination = Path("docs/audits/2026-10-04-sweeps")
    with h5py.File(source_path, "r") as file:
        source = file["run/recipe_yaml"].asstr()[()]
    settings = audit_settings(destination / "synthetic-profile")
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    evidence = {
        "scope": "simulator only; waits replaced by recorder in Cartesian test",
        "source_head": "c6697e2cee78277cd9125e8cc2a9e63c64ccc4a4 plus pre-existing working tree changes",
        "plan_actions": len(plan.actions), "points": plan.total_points,
        "spectra": plan.total_spectra, "estimate": asdict(PlanEstimator(settings).estimate(plan)),
        "archive_bytes": source_path.stat().st_size,
    }
    with h5py.File(source_path, "r") as file:
        assert json.loads(file["run/simulation_json"].asstr()[()])["enabled"] is True
        evidence["status"] = file["run"].attrs["status"]
        evidence["public_schema"] = {name: str(value) for name, value in file.attrs.items() if "schema" in name or "thatec" in name}
        evidence["checkpoint_samples"] = []
        for index in (0, 1, 9, 17):
            point = file[f"points/{index}"]
            evidence["checkpoint_samples"].append({
                "index": index,
                **{key: json.loads(point[key].asstr()[()]) for key in ("setpoints_json", "measurements_json", "metadata_json", "device_states_json")},
                "spectrum_attributes": {key: str(value) for key, value in file[f"spectra/{index}"].attrs.items()},
                "raw_source_metadata": json.loads(file[f"recipe_raw_sweeps_v1/{index}"].attrs["metadata_json"]),
            })
        evidence["events"] = len(file["events/name"])
        evidence["safe_resume_boundaries"] = sum(name == "safe_resume_boundary" for name in file["events/name"].asstr()[:])
        evidence["first_spectrum_operation_order"] = []
        for name, raw in zip(file["events/name"].asstr()[:], file["events/message"].asstr()[:], strict=True):
            if name == "action_started":
                payload = json.loads(raw)
                evidence["first_spectrum_operation_order"].append(payload["kind"])
                if payload["kind"] == "acquire_spectrum":
                    break
        evidence["public_axes"] = []
        for row in file["scan_definition"]:
            if not row.startswith("row_"):
                continue
            table = dict(file[f"scan_definition/{row}"].asstr()[()])
            if table.get("function") == "scalar control":
                evidence["public_axes"].append({"definition": table, "values": file[f"measurement/{row}/data"][:].tolist()})
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    (destination / "scenario-simulation-only.yml").write_text(
        "# Synthetic audit scenario. NOT a hardware-ready recipe.\n"
        "# Frequency, compliance and range settings are test fixtures.\n"
        "# 2 x 3 x 3 points; user has not specified real axis steps.\n" + source, encoding="utf-8")
    target = destination / "cartesian-simulation.h5"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source_path.read_bytes()).digest():
            raise FileExistsError("Existing audit archive has different contents: " + str(target))
    else:
        shutil.copyfile(source_path, target)
    print(json.dumps({key: evidence[key] for key in ("plan_actions", "points", "spectra", "status", "archive_bytes", "events", "safe_resume_boundaries")}, indent=2))


if __name__ == "__main__":
    main()
