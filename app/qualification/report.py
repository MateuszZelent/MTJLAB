"""Tamper-evident qualification evidence and authorization gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from app.domain.errors import ConfigurationError, SafetyViolation
from app.security import AccessPolicy, Permission
from app.settings.models import StationSettings


class RiskLevel(StrEnum):
    PASSIVE = "passive"
    ENERGIZED = "energized"


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


@dataclass(slots=True)
class CaseResult:
    case_id: str
    name: str
    risk: RiskLevel
    status: CaseStatus
    started_at_utc: str
    finished_at_utc: str
    duration_s: float
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_document(self) -> dict[str, Any]:
        result = asdict(self)
        result["risk"] = self.risk.value
        result["status"] = self.status.value
        return result


@dataclass(slots=True)
class QualificationReport:
    qualification_id: str
    started_at_utc: str
    settings_path: str
    settings_sha256: str
    profile_id: str
    profile_state: str
    operator: dict[str, Any]
    simulation: bool
    cases: list[CaseResult]
    finished_at_utc: str | None = None
    overall_status: str | None = None
    evidence_sha256: str | None = None

    def _document_without_digest(self) -> dict[str, Any]:
        if self.finished_at_utc is None or self.overall_status is None:
            raise ConfigurationError("Qualification report must be finished before serialization.")
        return {
            "schema_version": 1,
            "qualification_id": self.qualification_id,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "settings_path": self.settings_path,
            "settings_sha256": self.settings_sha256,
            "profile_id": self.profile_id,
            "profile_state": self.profile_state,
            "operator": self.operator,
            "simulation": self.simulation,
            "overall_status": self.overall_status,
            "cases": [case.as_document() for case in self.cases],
        }

    def finish(self) -> "QualificationReport":
        from datetime import datetime, timezone

        self.finished_at_utc = datetime.now(timezone.utc).isoformat()
        statuses = {case.status for case in self.cases}
        if CaseStatus.BLOCKED in statuses:
            self.overall_status = "blocked"
        elif CaseStatus.FAILED in statuses:
            self.overall_status = "failed"
        elif CaseStatus.INCOMPLETE in statuses:
            self.overall_status = "incomplete"
        elif self.simulation:
            self.overall_status = "simulation_passed"
        else:
            self.overall_status = "passed"
        payload = self._document_without_digest()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.evidence_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return self

    def write_atomic(self, path: str | Path) -> Path:
        if self.evidence_sha256 is None:
            self.finish()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        document = self._document_without_digest()
        document["evidence_sha256"] = self.evidence_sha256
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @classmethod
    def verify_file(cls, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read qualification evidence {source}: {exc}") from exc
        if not isinstance(document, dict):
            raise ConfigurationError("Qualification evidence must contain a JSON object.")
        digest = document.get("evidence_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ConfigurationError("Qualification evidence is missing its SHA-256 digest.")
        unsigned = dict(document)
        unsigned.pop("evidence_sha256", None)
        encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ConfigurationError("Qualification evidence digest does not match the document.")
        return document


@dataclass(frozen=True, slots=True)
class EnergizedAuthorization:
    """Independent gates required before a qualification may energize hardware."""

    allow_energized: bool
    dummy_load_id: str
    interlock_confirmed: bool
    confirmation: str

    def validate(
        self,
        settings: StationSettings,
        policy: AccessPolicy,
        *,
        simulation: bool,
        environment: Mapping[str, str],
        profile_state: str | None = None,
    ) -> None:
        del settings
        if simulation:
            raise SafetyViolation("Energized qualification is unavailable in simulation mode.")
        policy.require(Permission.SERVICE_DIAGNOSTICS, action="service_diagnostics")
        if profile_state is not None and profile_state.casefold() != "approved":
            raise SafetyViolation("Energized qualification requires an approved station profile.")
        if not self.allow_energized:
            raise SafetyViolation("Energized qualification requires --allow-energized.")
        if not self.dummy_load_id.strip():
            raise SafetyViolation("Energized qualification requires a traceable dummy-load ID.")
        if not self.interlock_confirmed:
            raise SafetyViolation("The physical interlock must be confirmed.")
        if self.confirmation != "I CONFIRM DUMMY LOAD AND PHYSICAL INTERLOCK":
            raise SafetyViolation("The energized qualification confirmation phrase is invalid.")
        if environment.get("LAB_CONTROL_ENABLE_ENERGIZED_HIL") != "YES":
            raise SafetyViolation(
                "LAB_CONTROL_ENABLE_ENERGIZED_HIL must equal exactly YES for energized qualification."
            )
