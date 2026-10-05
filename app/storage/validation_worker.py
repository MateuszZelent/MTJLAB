"""Isolate native PyThat/HDF5 validation from the Qt acquisition process."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory


def validate_archive_isolated(path, *, timeout_s=120):
    from .thatec_validator import CompatibilityIssue, ThatecCompatibilityReport

    target = Path(path).resolve()
    try:
        with TemporaryDirectory(prefix="mtjlab-validation-") as directory:
            result_path = Path(directory) / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.storage.validation_worker",
                    str(target),
                    str(result_path),
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=timeout_s,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"Validation process failed ({result.returncode}): {detail}")
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if Path(data["path"]) != target:
                raise ValueError("Validation report path does not match the archive.")
            return ThatecCompatibilityReport(
                target,
                data["manifest_version"],
                tuple(CompatibilityIssue(**item) for item in data["errors"]),
                tuple(CompatibilityIssue(**item) for item in data["warnings"]),
                data["pythat_version"],
                tuple(tuple(item) for item in data["dimensions"]),
                tuple(data["data_variables"]),
            )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        return ThatecCompatibilityReport(
            target, 1, (CompatibilityIssue("validation process", str(exc)),), ()
        )


def main():
    from .thatec_validator import ThatecCompatibilityValidator

    target, result_path = map(Path, sys.argv[1:])
    report = ThatecCompatibilityValidator().validate(target, require_pythat=True)
    data = asdict(report)
    data["path"] = str(target.resolve())
    result_path.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
