"""Thread-safe SQLite storage for samples, grid coordinates, attachments, and measurement runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import uuid

from app.inventory.models import (
    ActiveSampleTarget,
    Sample,
    SampleAttachment,
    SampleRunRecord,
)


class InventoryStore:
    """SQLite-backed persistent store for sample inventory and measurement tracking."""

    def __init__(
        self,
        db_path: str | Path = "measurements/inventory.db",
        attachments_dir: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if attachments_dir is not None:
            self.attachments_dir = Path(attachments_dir).resolve()
        else:
            self.attachments_dir = self.db_path.parent / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit mode
        )
        self._connection.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _init_db(self) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    sample_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    rows_json TEXT NOT NULL DEFAULT '[]',
                    row_labels_json TEXT NOT NULL DEFAULT '{}',
                    cols_json TEXT NOT NULL DEFAULT '[]',
                    col_labels_json TEXT NOT NULL DEFAULT '{}',
                    device_labels_json TEXT NOT NULL DEFAULT '{}',
                    device_states_json TEXT NOT NULL DEFAULT '{}',
                    device_notes_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sample_attachments (
                    id TEXT PRIMARY KEY,
                    sample_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    uploaded_at_utc TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (sample_id) REFERENCES samples (sample_id) ON DELETE CASCADE
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_attachments_sample_id
                ON sample_attachments (sample_id);
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sample_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL,
                    sample_name TEXT NOT NULL DEFAULT '',
                    row TEXT NOT NULL,
                    col TEXT NOT NULL,
                    device_label TEXT NOT NULL,
                    run_path TEXT NOT NULL,
                    run_sha256 TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    point_count INTEGER NOT NULL DEFAULT 0,
                    spectrum_count INTEGER NOT NULL DEFAULT 0,
                    recipe_name TEXT NOT NULL DEFAULT '',
                    elab_experiment_id INTEGER,
                    elab_url TEXT,
                    elab_status TEXT NOT NULL DEFAULT 'not_uploaded',
                    notes TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (sample_id) REFERENCES samples (sample_id) ON DELETE CASCADE
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_sample_cell
                ON sample_runs (sample_id, row, col);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_run_path
                ON sample_runs (run_path);
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS active_target (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    sample_id TEXT,
                    sample_name TEXT,
                    row TEXT,
                    col TEXT,
                    device_label TEXT,
                    notes TEXT
                );
                """
            )
            cursor.execute(
                "INSERT OR IGNORE INTO active_target (id, sample_id) VALUES (1, NULL);"
            )

    # -------------------------------------------------------------------------
    # Sample CRUD
    # -------------------------------------------------------------------------

    def save_sample(self, sample: Sample) -> Sample:
        """Create or update a sample definition."""
        now = datetime.now(timezone.utc).isoformat()
        updated_sample = Sample(
            sample_id=sample.sample_id.strip(),
            name=sample.name.strip() or sample.sample_id.strip(),
            description=sample.description,
            created_at_utc=sample.created_at_utc or now,
            updated_at_utc=now,
            tags=sample.tags,
            rows=sample.rows,
            row_labels=sample.row_labels,
            cols=sample.cols,
            col_labels=sample.col_labels,
            device_labels=sample.device_labels,
            device_states=sample.device_states,
            device_notes=sample.device_notes,
            attachments=sample.attachments,
        )

        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT INTO samples (
                    sample_id, name, description, created_at_utc, updated_at_utc,
                    tags_json, rows_json, row_labels_json, cols_json, col_labels_json,
                    device_labels_json, device_states_json, device_notes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    updated_at_utc=excluded.updated_at_utc,
                    tags_json=excluded.tags_json,
                    rows_json=excluded.rows_json,
                    row_labels_json=excluded.row_labels_json,
                    cols_json=excluded.cols_json,
                    col_labels_json=excluded.col_labels_json,
                    device_labels_json=excluded.device_labels_json,
                    device_states_json=excluded.device_states_json,
                    device_notes_json=excluded.device_notes_json;
                """,
                (
                    updated_sample.sample_id,
                    updated_sample.name,
                    updated_sample.description,
                    updated_sample.created_at_utc,
                    updated_sample.updated_at_utc,
                    json.dumps(list(updated_sample.tags)),
                    json.dumps(list(updated_sample.rows)),
                    json.dumps(updated_sample.row_labels),
                    json.dumps(list(updated_sample.cols)),
                    json.dumps(updated_sample.col_labels),
                    json.dumps(updated_sample.device_labels),
                    json.dumps(updated_sample.device_states),
                    json.dumps(updated_sample.device_notes),
                ),
            )
        return updated_sample

    def get_sample(self, sample_id: str) -> Sample | None:
        """Fetch sample by ID, including its attachments."""
        clean_id = str(sample_id or "").strip()
        if not clean_id:
            return None

        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT * FROM samples WHERE sample_id = ?;", (clean_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None

            attachments = self._list_attachments_for_sample(clean_id)
            return Sample(
                sample_id=row["sample_id"],
                name=row["name"],
                description=row["description"],
                created_at_utc=row["created_at_utc"],
                updated_at_utc=row["updated_at_utc"],
                tags=tuple(json.loads(row["tags_json"] or "[]")),
                rows=tuple(json.loads(row["rows_json"] or "[]")),
                row_labels=json.loads(row["row_labels_json"] or "{}"),
                cols=tuple(json.loads(row["cols_json"] or "[]")),
                col_labels=json.loads(row["col_labels_json"] or "{}"),
                device_labels=json.loads(row["device_labels_json"] or "{}"),
                device_states=json.loads(row["device_states_json"] or "{}"),
                device_notes=json.loads(row["device_notes_json"] or "{}"),
                attachments=attachments,
            )

    def list_samples(self) -> tuple[Sample, ...]:
        """Fetch all samples ordered by latest modification."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT sample_id FROM samples ORDER BY updated_at_utc DESC;")
            rows = cursor.fetchall()

        results: list[Sample] = []
        for r in rows:
            sample = self.get_sample(r["sample_id"])
            if sample is not None:
                results.append(sample)
        return tuple(results)

    def delete_sample(self, sample_id: str) -> bool:
        """Delete sample, its attached files on disk, and cascade DB records."""
        clean_id = str(sample_id or "").strip()
        if not clean_id:
            return False

        with self._lock:
            sample_dir = self.attachments_dir / clean_id
            if sample_dir.is_dir():
                shutil.rmtree(sample_dir, ignore_errors=True)

            cursor = self._connection.cursor()
            cursor.execute("DELETE FROM samples WHERE sample_id = ?;", (clean_id,))
            deleted = cursor.rowcount > 0

            # If this sample was active target, clear it
            active = self.get_active_target()
            if active.sample_id == clean_id:
                self.clear_active_target()

            return deleted

    # -------------------------------------------------------------------------
    # Attachments
    # -------------------------------------------------------------------------

    def _list_attachments_for_sample(self, sample_id: str) -> tuple[SampleAttachment, ...]:
        cursor = self._connection.cursor()
        cursor.execute(
            """
            SELECT * FROM sample_attachments
            WHERE sample_id = ?
            ORDER BY uploaded_at_utc ASC;
            """,
            (sample_id,),
        )
        rows = cursor.fetchall()
        return tuple(
            SampleAttachment(
                id=row["id"],
                sample_id=row["sample_id"],
                filename=row["filename"],
                rel_path=row["rel_path"],
                file_type=row["file_type"],
                size_bytes=row["size_bytes"],
                uploaded_at_utc=row["uploaded_at_utc"],
                caption=row["caption"],
            )
            for row in rows
        )

    def add_attachment(
        self,
        sample_id: str,
        source_path: str | Path,
        caption: str = "",
    ) -> SampleAttachment:
        """Copy an image or PDF attachment into durable sample storage and index it."""
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Attachment source file does not exist: {source}")

        clean_sample_id = str(sample_id or "").strip()
        if not clean_sample_id:
            raise ValueError("Sample ID cannot be empty.")

        ext = source.suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}:
            file_type = "image"
        elif ext == ".pdf":
            file_type = "pdf"
        elif ext in {".txt", ".md", ".csv", ".doc", ".docx"}:
            file_type = "document"
        else:
            file_type = "other"

        attachment_id = uuid.uuid4().hex[:12]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.name)
        storage_filename = f"{attachment_id}_{safe_name}"

        sample_storage_dir = self.attachments_dir / clean_sample_id
        sample_storage_dir.mkdir(parents=True, exist_ok=True)
        dest_path = sample_storage_dir / storage_filename

        shutil.copy2(source, dest_path)
        size_bytes = dest_path.stat().st_size
        uploaded_at = datetime.now(timezone.utc).isoformat()
        rel_path = f"{clean_sample_id}/{storage_filename}"

        attachment = SampleAttachment(
            id=attachment_id,
            sample_id=clean_sample_id,
            filename=source.name,
            rel_path=rel_path,
            file_type=file_type,
            size_bytes=size_bytes,
            uploaded_at_utc=uploaded_at,
            caption=caption.strip(),
        )

        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT INTO sample_attachments (
                    id, sample_id, filename, rel_path, file_type,
                    size_bytes, uploaded_at_utc, caption
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    attachment.id,
                    attachment.sample_id,
                    attachment.filename,
                    attachment.rel_path,
                    attachment.file_type,
                    attachment.size_bytes,
                    attachment.uploaded_at_utc,
                    attachment.caption,
                ),
            )
            # Update sample updated_at_utc timestamp
            cursor.execute(
                "UPDATE samples SET updated_at_utc = ? WHERE sample_id = ?;",
                (uploaded_at, clean_sample_id),
            )

        return attachment

    def delete_attachment(self, attachment_id: str) -> bool:
        """Remove attachment file from disk and database."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                "SELECT * FROM sample_attachments WHERE id = ?;", (attachment_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return False

            sample_id = row["sample_id"]
            rel_path = row["rel_path"]
            file_path = self.attachments_dir / rel_path
            if file_path.is_file():
                file_path.unlink(missing_ok=True)

            cursor.execute("DELETE FROM sample_attachments WHERE id = ?;", (attachment_id,))
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "UPDATE samples SET updated_at_utc = ? WHERE sample_id = ?;",
                (now, sample_id),
            )
            return True

    def get_attachment_path(self, attachment: SampleAttachment) -> Path:
        """Resolve absolute path on disk for an attachment."""
        return self.attachments_dir / attachment.rel_path

    # -------------------------------------------------------------------------
    # Measurement Runs Association
    # -------------------------------------------------------------------------

    def record_run(self, record: SampleRunRecord) -> SampleRunRecord:
        """Log a measurement sweep executed against a sample coordinate."""
        now = record.created_at_utc or datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT INTO sample_runs (
                    sample_id, sample_name, row, col, device_label,
                    run_path, run_sha256, created_at_utc, status,
                    point_count, spectrum_count, recipe_name,
                    elab_experiment_id, elab_url, elab_status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.sample_id,
                    record.sample_name,
                    record.row,
                    record.col,
                    record.device_label,
                    str(record.run_path),
                    record.run_sha256,
                    now,
                    record.status,
                    record.point_count,
                    record.spectrum_count,
                    record.recipe_name,
                    record.elab_experiment_id,
                    record.elab_url,
                    record.elab_status,
                    record.notes,
                ),
            )
            inserted_id = cursor.lastrowid

            # Automatically mark the cell state as 'measured' if it was 'untested'
            sample = self.get_sample(record.sample_id)
            if sample is not None:
                current_state = sample.cell_state(record.row, record.col)
                if current_state in {"untested", ""}:
                    updated = sample.with_cell_update(
                        record.row, record.col, state="measured"
                    )
                    self.save_sample(updated)

            return SampleRunRecord(
                id=inserted_id,
                sample_id=record.sample_id,
                sample_name=record.sample_name,
                row=record.row,
                col=record.col,
                device_label=record.device_label,
                run_path=record.run_path,
                run_sha256=record.run_sha256,
                created_at_utc=now,
                status=record.status,
                point_count=record.point_count,
                spectrum_count=record.spectrum_count,
                recipe_name=record.recipe_name,
                elab_experiment_id=record.elab_experiment_id,
                elab_url=record.elab_url,
                elab_status=record.elab_status,
                notes=record.notes,
            )

    def list_runs_for_sample(self, sample_id: str) -> tuple[SampleRunRecord, ...]:
        """List all sweep runs recorded for this sample, latest first."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT * FROM sample_runs
                WHERE sample_id = ?
                ORDER BY created_at_utc DESC;
                """,
                (sample_id,),
            )
            rows = cursor.fetchall()
            return tuple(SampleRunRecord.from_dict(dict(r)) for r in rows)

    def list_runs_for_cell(
        self, sample_id: str, row: str, col: str
    ) -> tuple[SampleRunRecord, ...]:
        """List all sweep runs recorded for a specific cell coordinate."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                SELECT * FROM sample_runs
                WHERE sample_id = ? AND row = ? AND col = ?
                ORDER BY created_at_utc DESC;
                """,
                (sample_id, str(row), str(col)),
            )
            rows = cursor.fetchall()
            return tuple(SampleRunRecord.from_dict(dict(r)) for r in rows)

    def update_run_elab_status(
        self,
        run_path: str | Path,
        *,
        elab_experiment_id: int | None,
        elab_url: str | None,
        elab_status: str,
    ) -> None:
        """Update eLab upload status for a run file."""
        norm_path = str(Path(run_path).resolve())
        with self._lock:
            cursor = self._connection.cursor()
            # Match both normalized full path or exact stored string or filename
            cursor.execute(
                """
                UPDATE sample_runs
                SET elab_experiment_id = ?, elab_url = ?, elab_status = ?
                WHERE run_path = ? OR run_path = ? OR run_path LIKE ?;
                """,
                (
                    elab_experiment_id,
                    elab_url,
                    elab_status,
                    str(run_path),
                    norm_path,
                    f"%{Path(run_path).name}",
                ),
            )

    # -------------------------------------------------------------------------
    # Active Measurement Target
    # -------------------------------------------------------------------------

    def get_active_target(self) -> ActiveSampleTarget:
        """Get the currently selected sample and device target."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT * FROM active_target WHERE id = 1;")
            row = cursor.fetchone()
            if row is None or not row["sample_id"]:
                return ActiveSampleTarget()
            return ActiveSampleTarget(
                sample_id=row["sample_id"],
                sample_name=row["sample_name"],
                row=row["row"],
                col=row["col"],
                device_label=row["device_label"],
                notes=row["notes"],
            )

    def set_active_target(self, target: ActiveSampleTarget) -> None:
        """Update the active measurement target."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE active_target
                SET sample_id = ?, sample_name = ?, row = ?, col = ?,
                    device_label = ?, notes = ?
                WHERE id = 1;
                """,
                (
                    target.sample_id,
                    target.sample_name,
                    target.row,
                    target.col,
                    target.device_label,
                    target.notes,
                ),
            )

    def clear_active_target(self) -> None:
        """Clear active sample target."""
        self.set_active_target(ActiveSampleTarget())
