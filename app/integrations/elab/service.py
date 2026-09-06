"""Application service that turns an immutable local run into an eLab record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable

from app.integrations.elab.client import ElabApiClient, ElabApiError
from app.integrations.elab.config import ElabCredentials, ElabIntegrationProfile
from app.integrations.elab.ledger import ElabUploadLedger, ElabUploadRecord, sha256_file
from app.storage.hdf5_reader import RunSummary, Hdf5RunReader


@dataclass(frozen=True, slots=True)
class ElabUploadRequest:
    path: Path
    credentials: ElabCredentials
    profile: ElabIntegrationProfile
    ledger_path: Path
    timeout_s: float = 30.0
    force: bool = False


@dataclass(frozen=True, slots=True)
class ElabUploadResult:
    record: ElabUploadRecord
    skipped_existing: bool = False


def _experiment_body(summary: RunSummary, path: Path, file_sha256: str) -> str:
    """Keep the eLab entry readable while the attached HDF5 remains authoritative."""

    created = summary.created_at_utc or "unknown"
    sample_section = ""
    if summary.sample_id:
        sample_section = (
            "<h2>Sample &amp; Coordinate Inventory</h2>"
            "<table>"
            f"<tr><th>Sample ID</th><td><code>{escape(summary.sample_id)}</code></td></tr>"
            f"<tr><th>Sample Name</th><td>{escape(summary.sample_name or summary.sample_id)}</td></tr>"
            f"<tr><th>Row</th><td>{escape(summary.sample_row or '-')}</td></tr>"
            f"<tr><th>Column</th><td>{escape(summary.sample_col or '-')}</td></tr>"
            f"<tr><th>Device Label</th><td><strong>{escape(summary.sample_coordinate_label or '-')}</strong></td></tr>"
            "</table>"
        )
    return (
        "<h1>PyLab measurement result</h1>"
        "<p>The attached HDF5 file is the immutable station result and contains the "
        "recipe, settings snapshot, device identity, checkpoints and audit events.</p>"
        f"{sample_section}"
        "<table>"
        f"<tr><th>Local file</th><td><code>{escape(path.name)}</code></td></tr>"
        f"<tr><th>Run status</th><td>{escape(summary.status)}</td></tr>"
        f"<tr><th>Created (UTC)</th><td>{escape(created)}</td></tr>"
        f"<tr><th>Committed points</th><td>{summary.point_count}</td></tr>"
        f"<tr><th>Spectra</th><td>{summary.spectrum_count}</td></tr>"
        f"<tr><th>Plan SHA-256</th><td><code>{escape(summary.plan_sha256 or 'unknown')}</code></td></tr>"
        f"<tr><th>HDF5 SHA-256</th><td><code>{escape(file_sha256)}</code></td></tr>"
        f"<tr><th>Application</th><td>{escape(summary.application_version or 'unknown')}</td></tr>"
        "</table>"
        "<p>Numeric values retain their unit-bearing keys and original HDF5 representation. "
        "The optional CSV attachment is a rebuildable index, not the scientific authority.</p>"
    )


def _record_with(record: ElabUploadRecord, **changes: object) -> ElabUploadRecord:
    values = {
        "run_path": record.run_path,
        "run_sha256": record.run_sha256,
        "template_id": record.template_id,
        "template_name": record.template_name,
        "status": record.status,
        "created_at_utc": record.created_at_utc,
        "experiment_id": record.experiment_id,
        "experiment_url": record.experiment_url,
        "uploaded_files": record.uploaded_files,
        "warnings": record.warnings,
        "error": record.error,
    }
    values.update(changes)
    return ElabUploadRecord(**values)


def upload_result(
    request: ElabUploadRequest,
    *,
    progress: Callable[[str], None] | None = None,
) -> ElabUploadResult:
    """Create an experiment from the selected template and attach the run files.

    A pending/failed record with a remote experiment ID is resumed in place. A
    local result is never deleted or modified when the remote operation fails.
    """

    target = Path(request.path).expanduser()
    if target.suffix.lower() not in {".h5", ".hdf5"}:
        raise ElabApiError("Choose an HDF5 result file for eLab upload.")
    if not target.is_file():
        raise ElabApiError(f"Result file not found: {target}")
    if request.profile.template_id is None:
        raise ElabApiError("Select an eLab experiment template before uploading.")
    request.profile.validate()
    try:
        summary = Hdf5RunReader.summary(target)
    except Exception as exc:
        raise ElabApiError(f"Cannot validate the local result before upload: {exc}") from exc
    normalized_status = str(summary.status).strip().casefold()
    if normalized_status not in {"completed", "aborted", "faulted", "thatec"}:
        raise ElabApiError(
            f"The result is not closed for upload (status: {summary.status}). "
            "Wait for the run to reach a terminal state."
        )

    file_sha256 = sha256_file(target)
    attachments: list[Path] = []
    if request.profile.upload_hdf5:
        attachments.append(target)
    csv_target = target.with_suffix(".csv")
    warnings: list[str] = []
    if request.profile.upload_csv:
        if csv_target.is_file():
            attachments.append(csv_target)
        else:
            warnings.append("CSV summary was selected but no adjacent CSV file exists.")
    if not attachments:
        raise ElabApiError("No result attachment is selected or available.")

    ledger = ElabUploadLedger(request.ledger_path)
    existing = ledger.find(target, file_sha256, request.profile.template_id)
    if existing is not None and existing.status == "uploaded" and not request.force:
        requested_files = {attachment.name for attachment in attachments}
        if requested_files.issubset(existing.uploaded_files):
            return ElabUploadResult(existing, skipped_existing=True)

    record = existing or ElabUploadRecord(
        run_path=ledger.normalize_run_path(target),
        run_sha256=file_sha256,
        template_id=request.profile.template_id,
        template_name=request.profile.template_name,
        status="pending",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        warnings=tuple(warnings),
    )
    if warnings:
        record = _record_with(record, warnings=tuple(dict.fromkeys((*record.warnings, *warnings))))
    record = _record_with(record, status="pending", error=None)
    ledger.save(record)
    report = progress or (lambda _message: None)

    try:
        client = ElabApiClient(request.credentials, timeout_s=request.timeout_s)
        if record.experiment_id is None:
            report("Creating an eLab experiment from the selected template...")
            title = request.profile.render_title(
                run_name=target.stem,
                status=summary.status,
                created_at=summary.created_at_utc or "unknown",
            )
            experiment_id, experiment_url = client.create_experiment(
                template_id=request.profile.template_id,
                title=title,
                body=_experiment_body(summary, target, file_sha256),
            )
            record = _record_with(
                record,
                experiment_id=experiment_id,
                experiment_url=experiment_url,
            )
            ledger.save(record)
            tags_to_add = list(request.profile.tags)
            if summary.sample_id:
                tags_to_add.append(f"sample:{summary.sample_id}")
                if summary.sample_coordinate_label:
                    tags_to_add.append(f"device:{summary.sample_coordinate_label}")
                elif summary.sample_row and summary.sample_col:
                    tags_to_add.append(f"coord:R{summary.sample_row}C{summary.sample_col}")
            for tag in tags_to_add:
                try:
                    client.add_tag(experiment_id=experiment_id, tag=tag)
                except ElabApiError as exc:
                    warning = f"Tag {tag!r} was not added: {exc}"
                    record = _record_with(
                        record,
                        warnings=tuple(dict.fromkeys((*record.warnings, warning))),
                    )
                    ledger.save(record)

        uploaded = set(record.uploaded_files)
        sample_info = ""
        if summary.sample_id:
            coord_desc = f"R{summary.sample_row or '?'}C{summary.sample_col or '?'}"
            if summary.sample_coordinate_label:
                coord_desc += f" ({summary.sample_coordinate_label})"
            sample_info = f"; Sample: {summary.sample_id} [{coord_desc}]"

        for attachment in attachments:
            attachment_name = attachment.name
            if attachment_name in uploaded:
                continue
            report(f"Uploading {attachment_name} to eLab...")
            client.upload_file(
                experiment_id=int(record.experiment_id),
                path=attachment,
                comment=(
                    f"Uploaded by PyLab; local SHA-256 {file_sha256}; source run {target.name}{sample_info}."
                ),
            )
            uploaded.add(attachment_name)
            record = _record_with(record, uploaded_files=tuple(sorted(uploaded)))
            ledger.save(record)
        record = _record_with(record, status="uploaded", error=None)
        ledger.save(record)
        report("eLab upload completed.")
        return ElabUploadResult(record)
    except Exception as exc:
        record = _record_with(record, status="failed", error=str(exc))
        ledger.save(record)
        if isinstance(exc, ElabApiError):
            raise
        raise ElabApiError(f"eLab upload failed: {exc}") from exc
