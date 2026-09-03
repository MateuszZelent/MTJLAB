"""Crash-tolerant local ledger for eLabFTW upload attempts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


class ElabLedgerError(RuntimeError):
    """Raised when the private upload ledger cannot be read or written."""


@dataclass(frozen=True, slots=True)
class ElabUploadRecord:
    run_path: str
    run_sha256: str
    template_id: int
    template_name: str
    status: str
    created_at_utc: str
    experiment_id: int | None = None
    experiment_url: str | None = None
    uploaded_files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> "ElabUploadRecord":
        if not isinstance(value, dict):
            raise ElabLedgerError("An eLab upload ledger entry must be an object.")
        try:
            return cls(
                run_path=str(value["run_path"]),
                run_sha256=str(value["run_sha256"]),
                template_id=int(value["template_id"]),
                template_name=str(value.get("template_name", "")),
                status=str(value["status"]),
                created_at_utc=str(value["created_at_utc"]),
                experiment_id=(
                    None
                    if value.get("experiment_id") in (None, "")
                    else int(value["experiment_id"])
                ),
                experiment_url=(
                    None
                    if value.get("experiment_url") in (None, "")
                    else str(value["experiment_url"])
                ),
                uploaded_files=tuple(str(item) for item in value.get("uploaded_files", ())),
                warnings=tuple(str(item) for item in value.get("warnings", ())),
                error=None if value.get("error") in (None, "") else str(value["error"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ElabLedgerError("An eLab upload ledger entry has an invalid shape.") from exc

    def as_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> tuple[str, str, int]:
        return self.run_path, self.run_sha256, self.template_id


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a result without changing or loading the complete file in memory."""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ElabLedgerError(f"Cannot hash result file {Path(path).name}: {exc}") from exc
    return digest.hexdigest()


class ElabUploadLedger:
    """Atomically persist private upload state independently of immutable HDF5."""

    _VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    @staticmethod
    def normalize_run_path(path: str | Path) -> str:
        return str(Path(path).expanduser().resolve(strict=False))

    def records(self) -> tuple[ElabUploadRecord, ...]:
        if not self.path.is_file():
            return ()
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ElabLedgerError(f"Cannot read eLab upload ledger {self.path}: {exc}") from exc
        if isinstance(decoded, list):
            rows = decoded
        elif isinstance(decoded, dict) and decoded.get("version") == self._VERSION:
            rows = decoded.get("records", [])
        else:
            raise ElabLedgerError("The eLab upload ledger has an unsupported schema.")
        if not isinstance(rows, list):
            raise ElabLedgerError("The eLab upload ledger records must be a list.")
        return tuple(ElabUploadRecord.from_json(row) for row in rows)

    def find(
        self,
        run_path: str | Path,
        run_sha256: str,
        template_id: int,
    ) -> ElabUploadRecord | None:
        key = (self.normalize_run_path(run_path), str(run_sha256), int(template_id))
        return next((record for record in self.records() if record.key == key), None)

    def save(self, record: ElabUploadRecord) -> None:
        records = [item for item in self.records() if item.key != record.key]
        records.append(record)
        records.sort(key=lambda item: item.created_at_utc, reverse=True)
        payload = {
            "version": self._VERSION,
            "records": [item.as_json() for item in records],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ElabLedgerError(f"Cannot save eLab upload ledger {self.path}: {exc}") from exc

    @staticmethod
    def now_utc() -> str:
        return datetime.now(timezone.utc).isoformat()
