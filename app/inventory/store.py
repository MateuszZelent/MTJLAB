"""Thread-safe SQLite storage for samples, grid coordinates, attachments, and measurement runs."""

from __future__ import annotations

from collections.abc import Mapping
import csv
from datetime import datetime, timezone
import json
import logging
import re
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

from app.inventory.models import (
    ActiveSampleTarget,
    Sample,
    SampleAttachment,
    SampleRunRecord,
)

logger = logging.getLogger(__name__)


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
                    device_notes_json TEXT NOT NULL DEFAULT '{}',
                    measurement_directory TEXT NOT NULL DEFAULT '',
                    folder_name TEXT NOT NULL DEFAULT ''
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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
                    csv_path TEXT NOT NULL DEFAULT '',
                    report_path TEXT NOT NULL DEFAULT '',
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
                    notes TEXT,
                    row_label TEXT,
                    col_label TEXT,
                    description TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            cursor.execute(
                "INSERT OR IGNORE INTO active_target (id, sample_id) VALUES (1, NULL);"
            )
            cursor.execute("PRAGMA table_info(samples);")
            sample_cols = {r["name"] for r in cursor.fetchall()}
            if "measurement_directory" not in sample_cols:
                cursor.execute(
                    "ALTER TABLE samples ADD COLUMN measurement_directory TEXT NOT NULL DEFAULT '';"
                )
            if "folder_name" not in sample_cols:
                cursor.execute(
                    "ALTER TABLE samples ADD COLUMN folder_name TEXT NOT NULL DEFAULT '';"
                )
            cursor.execute("PRAGMA table_info(sample_runs);")
            run_cols = {r["name"] for r in cursor.fetchall()}
            for col_name in ("csv_path", "report_path"):
                if col_name not in run_cols:
                    cursor.execute(
                        f"ALTER TABLE sample_runs ADD COLUMN {col_name} TEXT NOT NULL DEFAULT '';"
                    )
            cursor.execute("PRAGMA table_info(active_target);")
            existing_cols = {r["name"] for r in cursor.fetchall()}
            for col_name, col_type in (
                ("row_label", "TEXT"),
                ("col_label", "TEXT"),
                ("description", "TEXT"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if col_name not in existing_cols:
                    cursor.execute(f"ALTER TABLE active_target ADD COLUMN {col_name} {col_type};")

    # -------------------------------------------------------------------------
    # Sample catalogue layout
    # -------------------------------------------------------------------------

    @property
    def catalogue_root(self) -> Path:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM inventory_settings WHERE key = 'catalogue_root';"
            ).fetchone()
        configured = str(row["value"] if row is not None else "").strip()
        return (
            Path(configured).expanduser().resolve()
            if configured
            else (self.db_path.parent / "catalogue").resolve()
        )

    def set_catalogue_root(self, root: str | Path) -> Path:
        """Set the one application catalogue root and materialize every sample tree."""
        target = Path(root).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        old_root = self.catalogue_root
        samples = self.list_samples()
        for sample in samples:
            if not sample.folder_name:
                continue
            existing_sample_dir = (old_root / sample.folder_name).resolve()
            try:
                target.relative_to(existing_sample_dir)
            except ValueError:
                continue
            raise ValueError(
                "The catalogue root cannot be placed inside an existing sample folder."
            )

        with self._lock:
            self._connection.execute(
                """
                INSERT INTO inventory_settings (key, value) VALUES ('catalogue_root', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                """,
                (str(target),),
            )

        for sample in samples:
            if not sample.folder_name:
                sample = self.save_sample(sample)
            destination = target / sample.folder_name
            sources: list[tuple[Path, Path]] = []
            if sample.folder_name:
                sources.append((old_root / sample.folder_name, destination))
            sources.append(
                (
                    self.db_path.parent / "samples" / self._safe_component(sample.sample_id),
                    destination,
                )
            )
            legacy_row = self._connection.execute(
                "SELECT measurement_directory FROM samples WHERE sample_id = ?",
                (sample.sample_id,),
            ).fetchone()
            legacy_measurements = str(legacy_row["measurement_directory"] or "").strip()
            if legacy_measurements:
                sources.append((Path(legacy_measurements).expanduser(), destination / "measurements"))

            for source, mapped_destination in sources:
                if source.resolve() == mapped_destination.resolve() or not source.is_dir():
                    continue
                shutil.copytree(source, mapped_destination, dirs_exist_ok=True)
                self._rewrite_run_paths(source, mapped_destination, sample.sample_id)

            self.ensure_sample_structure(sample.sample_id)
            self._migrate_legacy_attachments(sample.sample_id)
            self.ensure_sample_structure(sample.sample_id)
            if legacy_measurements:
                with self._lock:
                    self._connection.execute(
                        "UPDATE samples SET measurement_directory = '' WHERE sample_id = ?",
                        (sample.sample_id,),
                    )
        return target

    @staticmethod
    def _safe_component(value: str, fallback: str = "sample") -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip(" ._")
        return cleaned or fallback

    def _normalize_or_allocate_folder_name(
        self, requested: str, display_name: str, *, current_sample_id: str
    ) -> str:
        if requested.strip():
            normalized = self._safe_component(requested)[:64]
            with self._lock:
                duplicate = self._connection.execute(
                    "SELECT 1 FROM samples WHERE folder_name = ? AND sample_id != ?",
                    (normalized, current_sample_id),
                ).fetchone()
            if duplicate is not None:
                raise ValueError(f"Sample folder name is already in use: {normalized}")
            return normalized
        words = re.findall(r"[A-Za-z0-9]+", display_name)
        camel = "".join(word[:1].upper() + word[1:] for word in words)[:32] or "Sample"
        with self._lock:
            rows = self._connection.execute(
                "SELECT sample_id, folder_name FROM samples"
            ).fetchall()
        used = {
            str(row["folder_name"])
            for row in rows
            if row["sample_id"] != current_sample_id and row["folder_name"]
        }
        prefix = self._safe_component(current_sample_id)[:24]
        candidate = f"{prefix}_{camel}"
        suffix = 2
        while candidate in used:
            candidate = f"{prefix}_{camel}_{suffix}"
            suffix += 1
        return candidate

    def sample_directory(self, sample_id: str) -> Path:
        sample = self.get_sample(sample_id)
        if sample is None:
            raise KeyError(f"Sample not found: {sample_id}")
        folder = sample.folder_name or self._safe_component(sample.sample_id)
        return self.catalogue_root / folder

    def measurement_directory_for(
        self, sample_id: str, device_name: str, measurement_type: str = ""
    ) -> Path:
        path = self.sample_directory(sample_id) / "measurements" / self._safe_component(device_name)
        if measurement_type:
            path /= self._safe_component(measurement_type)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_sample_structure(self, sample_id: str) -> Path:
        sample = self.get_sample(sample_id)
        if sample is None:
            raise KeyError(f"Sample not found: {sample_id}")
        root = self.catalogue_root / sample.folder_name
        (root / "attachments").mkdir(parents=True, exist_ok=True)
        (root / "measurements" / "sweeps").mkdir(parents=True, exist_ok=True)
        self._write_sample_info(sample, root / "info.csv")
        return root

    def _write_sample_info(self, sample: Sample, path: Path) -> None:
        with self._lock:
            attachments = self._connection.execute(
                "SELECT * FROM sample_attachments WHERE sample_id = ? ORDER BY uploaded_at_utc",
                (sample.sample_id,),
            ).fetchall()
            runs = self._connection.execute(
                "SELECT * FROM sample_runs WHERE sample_id = ? ORDER BY created_at_utc",
                (sample.sample_id,),
            ).fetchall()
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["field", "value"])
            writer.writerow(["sample_id", sample.sample_id])
            writer.writerow(["name", sample.name])
            writer.writerow(["description", sample.description])
            writer.writerow(["folder_name", sample.folder_name])
            writer.writerow(["created_at_utc", sample.created_at_utc])
            writer.writerow(["updated_at_utc", sample.updated_at_utc])
            writer.writerow(["tags", ";".join(sample.tags)])
            writer.writerow([])
            writer.writerow(["row", "column", "label", "state", "notes"])
            for row in sample.rows:
                for col in sample.cols:
                    writer.writerow(
                        [row, col, sample.cell_label(row, col), sample.cell_state(row, col), sample.cell_notes(row, col)]
                    )
            writer.writerow([])
            writer.writerow(["attachments"])
            writer.writerow(["filename", "type", "relative_path", "uploaded_at_utc", "caption"])
            for attachment in attachments:
                writer.writerow(
                    [
                        attachment["filename"], attachment["file_type"],
                        attachment["rel_path"], attachment["uploaded_at_utc"],
                        attachment["caption"],
                    ]
                )
            writer.writerow([])
            writer.writerow(["measurements"])
            writer.writerow(
                ["created_at_utc", "device", "type", "status", "points", "data_path", "report_path"]
            )
            for run in runs:
                writer.writerow(
                    [
                        run["created_at_utc"], run["device_label"], run["recipe_name"],
                        run["status"], run["point_count"], run["run_path"],
                        run["report_path"],
                    ]
                )
            stream.flush()
        temporary.replace(path)

    def _rename_sample_directory(self, old_name: str, new_name: str) -> None:
        if not old_name or old_name == new_name:
            return
        old_path = self.catalogue_root / old_name
        new_path = self.catalogue_root / new_name
        if old_path.is_dir() and not new_path.exists():
            try:
                old_path.rename(new_path)
            except (PermissionError, OSError) as exc:
                logger.warning(
                    "Could not rename sample directory from %s to %s (file locked?): %s",
                    old_path,
                    new_path,
                    exc,
                )
                return
        if new_path.is_dir():
            self._rewrite_run_paths(old_path, new_path, None)
            with self._lock:
                rows = self._connection.execute(
                    "SELECT id, rel_path FROM sample_attachments"
                ).fetchall()
                for row in rows:
                    rel = Path(str(row["rel_path"]))
                    if rel.parts and rel.parts[0] == old_name:
                        self._connection.execute(
                            "UPDATE sample_attachments SET rel_path = ? WHERE id = ?",
                            (str(Path(new_name, *rel.parts[1:])), row["id"]),
                        )

    def _rewrite_run_paths(
        self, source: Path, destination: Path, sample_id: str | None
    ) -> None:
        with self._lock:
            if sample_id is None:
                rows = self._connection.execute("SELECT * FROM sample_runs").fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM sample_runs WHERE sample_id = ?", (sample_id,)
                ).fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in ("run_path", "csv_path", "report_path"):
                    raw = str(row[column] or "")
                    if not raw:
                        continue
                    path = Path(raw)
                    try:
                        relative = path.resolve().relative_to(source.resolve())
                    except ValueError:
                        continue
                    updates[column] = str(destination / relative)
                if updates:
                    assignments = ", ".join(f"{key} = ?" for key in updates)
                    self._connection.execute(
                        f"UPDATE sample_runs SET {assignments} WHERE id = ?",
                        (*updates.values(), row["id"]),
                    )

    def _migrate_legacy_attachments(self, sample_id: str) -> None:
        sample_root = self.sample_directory(sample_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, rel_path FROM sample_attachments WHERE sample_id = ?",
                (sample_id,),
            ).fetchall()
            for row in rows:
                old_rel = str(row["rel_path"])
                old_path = self.attachments_dir / old_rel
                filename = Path(old_rel).name
                destination = sample_root / "attachments" / filename
                if old_path.is_file() and old_path.resolve() != destination.resolve():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old_path, destination)
                if destination.is_file():
                    new_rel = str(destination.relative_to(self.catalogue_root))
                    self._connection.execute(
                        "UPDATE sample_attachments SET rel_path = ? WHERE id = ?",
                        (new_rel, row["id"]),
                    )

    # -------------------------------------------------------------------------
    # Sample CRUD
    # -------------------------------------------------------------------------

    def save_sample(self, sample: Sample) -> Sample:
        """Create or update a sample definition."""
        now = datetime.now(timezone.utc).isoformat()
        previous = self.get_sample(sample.sample_id)
        folder_name = self._normalize_or_allocate_folder_name(
            sample.folder_name,
            sample.name or sample.sample_id,
            current_sample_id=sample.sample_id,
        )
        updated_sample = Sample(
            sample_id=sample.sample_id.strip(),
            name=sample.name.strip() or sample.sample_id.strip(),
            description=sample.description,
            folder_name=folder_name,
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
                    device_labels_json, device_states_json, device_notes_json,
                    measurement_directory, folder_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    device_notes_json=excluded.device_notes_json,
                    folder_name=excluded.folder_name;
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
                    "",
                    updated_sample.folder_name,
                ),
            )
        if previous is not None and previous.folder_name != updated_sample.folder_name:
            self._rename_sample_directory(previous.folder_name, updated_sample.folder_name)
        self.ensure_sample_structure(updated_sample.sample_id)
        return updated_sample

    def remap_sample_rows(self, sample_id: str, row_mapping: Mapping[str, str]) -> Sample:
        """Remap row numbers for a sample in SQLite and update historical measurement links."""
        with self._lock:
            sample = self.get_sample(sample_id)
            if sample is None:
                raise KeyError(f"Sample not found: {sample_id}")

            updated = sample.remap_rows(row_mapping)
            self.save_sample(updated)

            # Update historical runs
            cursor = self._connection.cursor()
            for old_r, new_r in row_mapping.items():
                if old_r != new_r:
                    cursor.execute(
                        "UPDATE sample_runs SET row = ? WHERE sample_id = ? AND row = ?",
                        (new_r, sample_id, old_r),
                    )

            # Update active target if it pointed to this sample and remapped row
            active = self.get_active_target()
            if active.is_active and active.sample_id == sample_id and active.row in row_mapping:
                new_row = row_mapping[active.row]
                self.set_active_target(
                    sample_id=sample_id,
                    row=new_row,
                    col=active.col,
                    device_label=active.device_label,
                    notes=active.notes,
                )
            return updated

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
                folder_name=row["folder_name"],
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

        sample = self.get_sample(clean_sample_id)
        if sample is None:
            raise KeyError(f"Sample not found: {clean_sample_id}")
        sample_storage_dir = self.ensure_sample_structure(clean_sample_id) / "attachments"
        sample_storage_dir.mkdir(parents=True, exist_ok=True)
        dest_path = sample_storage_dir / storage_filename

        temporary = dest_path.with_name(f".{dest_path.name}.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(dest_path)
        size_bytes = dest_path.stat().st_size
        uploaded_at = datetime.now(timezone.utc).isoformat()
        rel_path = str(dest_path.relative_to(self.catalogue_root))

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

        refreshed = self.get_sample(clean_sample_id)
        if refreshed is not None:
            self._write_sample_info(refreshed, self.sample_directory(clean_sample_id) / "info.csv")
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
            file_path = self.get_attachment_path(
                SampleAttachment(
                    id=row["id"], sample_id=row["sample_id"], filename=row["filename"],
                    rel_path=row["rel_path"], file_type=row["file_type"],
                    size_bytes=row["size_bytes"], uploaded_at_utc=row["uploaded_at_utc"],
                    caption=row["caption"],
                )
            )
            if file_path.is_file():
                file_path.unlink(missing_ok=True)

            cursor.execute("DELETE FROM sample_attachments WHERE id = ?;", (attachment_id,))
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "UPDATE samples SET updated_at_utc = ? WHERE sample_id = ?;",
                (now, sample_id),
            )
            refreshed = self.get_sample(sample_id)
            if refreshed is not None:
                self._write_sample_info(
                    refreshed, self.sample_directory(sample_id) / "info.csv"
                )
            return True

    def get_attachment_path(self, attachment: SampleAttachment) -> Path:
        """Resolve absolute path on disk for an attachment."""
        rel = Path(attachment.rel_path)
        if rel.is_absolute():
            return rel
        catalogue_path = self.catalogue_root / rel
        if catalogue_path.exists():
            return catalogue_path
        return self.attachments_dir / rel

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
                    elab_experiment_id, elab_url, elab_status, notes,
                    csv_path, report_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                    record.csv_path,
                    record.report_path,
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

            refreshed = self.get_sample(record.sample_id)
            if refreshed is not None:
                self._write_sample_info(
                    refreshed, self.sample_directory(record.sample_id) / "info.csv"
                )

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
                csv_path=record.csv_path,
                report_path=record.report_path,
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
            row_keys = row.keys() if hasattr(row, "keys") else ()
            tags_json = row["tags_json"] if "tags_json" in row_keys else "[]"
            try:
                tags = tuple(json.loads(tags_json or "[]"))
            except Exception:
                tags = ()
            return ActiveSampleTarget(
                sample_id=row["sample_id"],
                sample_name=row["sample_name"],
                row=row["row"],
                col=row["col"],
                device_label=row["device_label"],
                notes=row["notes"],
                row_label=row["row_label"] if "row_label" in row_keys else None,
                col_label=row["col_label"] if "col_label" in row_keys else None,
                description=row["description"] if "description" in row_keys else None,
                tags=tags,
            )

    def set_active_target(self, target: ActiveSampleTarget) -> None:
        """Update the active measurement target."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                UPDATE active_target
                SET sample_id = ?, sample_name = ?, row = ?, col = ?,
                    device_label = ?, notes = ?, row_label = ?, col_label = ?,
                    description = ?, tags_json = ?
                WHERE id = 1;
                """,
                (
                    target.sample_id,
                    target.sample_name,
                    target.row,
                    target.col,
                    target.device_label,
                    target.notes,
                    target.row_label,
                    target.col_label,
                    target.description,
                    json.dumps(list(target.tags)),
                ),
            )

    def clear_active_target(self) -> None:
        """Clear active sample target."""
        self.set_active_target(ActiveSampleTarget())
