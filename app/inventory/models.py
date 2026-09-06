"""Domain models for sample and device inventory."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class SampleAttachment:
    """An attached visual or document artifact for a sample (microscope photo, PDF layout)."""

    id: str
    sample_id: str
    filename: str
    rel_path: str
    file_type: str  # "image", "pdf", "document", "other"
    size_bytes: int
    uploaded_at_utc: str
    caption: str = ""

    @property
    def is_image(self) -> bool:
        return self.file_type == "image"

    @property
    def is_pdf(self) -> bool:
        return self.file_type == "pdf"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sample_id": self.sample_id,
            "filename": self.filename,
            "rel_path": self.rel_path,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "uploaded_at_utc": self.uploaded_at_utc,
            "caption": self.caption,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SampleAttachment:
        return cls(
            id=str(data["id"]),
            sample_id=str(data["sample_id"]),
            filename=str(data["filename"]),
            rel_path=str(data["rel_path"]),
            file_type=str(data.get("file_type", "other")),
            size_bytes=int(data.get("size_bytes", 0)),
            uploaded_at_utc=str(data.get("uploaded_at_utc", "")),
            caption=str(data.get("caption", "")),
        )


@dataclass(frozen=True, slots=True)
class ActiveSampleTarget:
    """The globally active sample and coordinate set for upcoming measurements."""

    sample_id: str | None = None
    sample_name: str | None = None
    row: str | None = None
    col: str | None = None
    device_label: str | None = None
    notes: str | None = None

    @property
    def is_active(self) -> bool:
        return bool(self.sample_id and self.sample_id.strip())

    def display_text(self) -> str:
        if not self.is_active:
            return "No active sample target"
        name = self.sample_name or self.sample_id
        coord_parts = []
        if self.row is not None and self.col is not None:
            coord_parts.append(f"R{self.row}:C{self.col}")
        elif self.row is not None:
            coord_parts.append(f"R{self.row}")
        elif self.col is not None:
            coord_parts.append(f"C{self.col}")

        coord_str = ", ".join(coord_parts)
        label_str = f" [{self.device_label}]" if self.device_label else ""
        if coord_str:
            return f"{name} · {coord_str}{label_str}"
        return f"{name}{label_str}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "sample_name": self.sample_name,
            "row": self.row,
            "col": self.col,
            "device_label": self.device_label,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ActiveSampleTarget:
        if not data:
            return cls()
        return cls(
            sample_id=str(data.get("sample_id") or "") or None,
            sample_name=str(data.get("sample_name") or "") or None,
            row=str(data.get("row") or "") or None,
            col=str(data.get("col") or "") or None,
            device_label=str(data.get("device_label") or "") or None,
            notes=str(data.get("notes") or "") or None,
        )


@dataclass(frozen=True, slots=True)
class SampleRunRecord:
    """Historical association linking an executed HDF5 measurement to a sample coordinate."""

    sample_id: str
    row: str
    col: str
    device_label: str
    run_path: str
    run_sha256: str
    created_at_utc: str
    status: str
    point_count: int
    spectrum_count: int
    recipe_name: str
    id: int | None = None
    sample_name: str = ""
    elab_experiment_id: int | None = None
    elab_url: str | None = None
    elab_status: str = "not_uploaded"  # not_uploaded, pending, uploaded, failed
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sample_id": self.sample_id,
            "sample_name": self.sample_name,
            "row": self.row,
            "col": self.col,
            "device_label": self.device_label,
            "run_path": self.run_path,
            "run_sha256": self.run_sha256,
            "created_at_utc": self.created_at_utc,
            "status": self.status,
            "point_count": self.point_count,
            "spectrum_count": self.spectrum_count,
            "recipe_name": self.recipe_name,
            "elab_experiment_id": self.elab_experiment_id,
            "elab_url": self.elab_url,
            "elab_status": self.elab_status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SampleRunRecord:
        return cls(
            id=data.get("id"),
            sample_id=str(data["sample_id"]),
            sample_name=str(data.get("sample_name") or ""),
            row=str(data.get("row") or ""),
            col=str(data.get("col") or ""),
            device_label=str(data.get("device_label") or ""),
            run_path=str(data.get("run_path") or ""),
            run_sha256=str(data.get("run_sha256") or ""),
            created_at_utc=str(data.get("created_at_utc") or ""),
            status=str(data.get("status") or ""),
            point_count=int(data.get("point_count", 0)),
            spectrum_count=int(data.get("spectrum_count", 0)),
            recipe_name=str(data.get("recipe_name") or ""),
            elab_experiment_id=data.get("elab_experiment_id"),
            elab_url=data.get("elab_url"),
            elab_status=str(data.get("elab_status") or "not_uploaded"),
            notes=str(data.get("notes") or ""),
        )


@dataclass(frozen=True, slots=True)
class Sample:
    """Sample definition including its device grid matrix and metadata."""

    sample_id: str
    name: str
    description: str = ""
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    tags: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()  # e.g. ("1", "2", ... "10") or ("20", "21", ... "30")
    row_labels: dict[str, str] = field(default_factory=dict)  # e.g. {"23": "Central wedge"}
    cols: tuple[str, ...] = ()  # e.g. ("1", "2", ... "5")
    col_labels: dict[str, str] = field(default_factory=dict)  # e.g. {"3": "200 nm"}
    device_labels: dict[str, str] = field(default_factory=dict)  # key: "r,c" -> e.g. "200 nm"
    device_states: dict[str, str] = field(default_factory=dict)  # key: "r,c" -> "untested", "measured", "good", etc.
    device_notes: dict[str, str] = field(default_factory=dict)  # key: "r,c" -> str
    attachments: tuple[SampleAttachment, ...] = ()

    @staticmethod
    def coord_key(row: str | int, col: str | int) -> str:
        return f"{row},{col}"

    def cell_label(self, row: str | int, col: str | int) -> str:
        key = self.coord_key(row, col)
        if key in self.device_labels and self.device_labels[key].strip():
            return self.device_labels[key].strip()
        col_str = str(col)
        if col_str in self.col_labels and self.col_labels[col_str].strip():
            return self.col_labels[col_str].strip()
        return f"R{row}C{col}"

    def cell_state(self, row: str | int, col: str | int) -> str:
        return self.device_states.get(self.coord_key(row, col), "untested")

    def cell_notes(self, row: str | int, col: str | int) -> str:
        return self.device_notes.get(self.coord_key(row, col), "")

    def with_cell_update(
        self,
        row: str | int,
        col: str | int,
        *,
        label: str | None = None,
        state: str | None = None,
        notes: str | None = None,
    ) -> Sample:
        key = self.coord_key(row, col)
        new_labels = dict(self.device_labels)
        new_states = dict(self.device_states)
        new_notes = dict(self.device_notes)

        if label is not None:
            new_labels[key] = label
        if state is not None:
            new_states[key] = state
        if notes is not None:
            new_notes[key] = notes

        return replace(
            self,
            device_labels=new_labels,
            device_states=new_states,
            device_notes=new_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _split_coord_dict(d: Mapping[str, str]) -> dict[tuple[str, str], str]:
        result = {}
        for k, v in d.items():
            if "," in k:
                parts = k.split(",", 1)
                result[(parts[0], parts[1])] = v
        return result

    def with_row_label(self, row: str | int, label: str) -> Sample:
        new_row_labels = dict(self.row_labels)
        new_row_labels[str(row)] = label
        return replace(
            self,
            row_labels=new_row_labels,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_col_label(self, col: str | int, label: str) -> Sample:
        new_col_labels = dict(self.col_labels)
        new_col_labels[str(col)] = label
        return replace(
            self,
            col_labels=new_col_labels,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_renamed_row(
        self, old_row: str | int, new_row: str | int, new_label: str | None = None
    ) -> Sample:
        old_r = str(old_row)
        new_r = str(new_row)
        if old_r not in self.rows:
            return self
        new_rows = tuple(new_r if r == old_r else r for r in self.rows)
        new_row_labels = dict(self.row_labels)
        if old_r in new_row_labels:
            val = new_row_labels.pop(old_r)
            new_row_labels[new_r] = new_label if new_label is not None else val
        elif new_label is not None:
            new_row_labels[new_r] = new_label

        new_dev_labels = {}
        for (r, c), v in self._split_coord_dict(self.device_labels).items():
            new_dev_labels[f"{new_r if r == old_r else r},{c}"] = v

        new_dev_states = {}
        for (r, c), v in self._split_coord_dict(self.device_states).items():
            new_dev_states[f"{new_r if r == old_r else r},{c}"] = v

        new_dev_notes = {}
        for (r, c), v in self._split_coord_dict(self.device_notes).items():
            new_dev_notes[f"{new_r if r == old_r else r},{c}"] = v

        return replace(
            self,
            rows=new_rows,
            row_labels=new_row_labels,
            device_labels=new_dev_labels,
            device_states=new_dev_states,
            device_notes=new_dev_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_renamed_col(
        self, old_col: str | int, new_col: str | int, new_label: str | None = None
    ) -> Sample:
        old_c = str(old_col)
        new_c = str(new_col)
        if old_c not in self.cols:
            return self
        new_cols = tuple(new_c if c == old_c else c for c in self.cols)
        new_col_labels = dict(self.col_labels)
        if old_c in new_col_labels:
            val = new_col_labels.pop(old_c)
            new_col_labels[new_c] = new_label if new_label is not None else val
        elif new_label is not None:
            new_col_labels[new_c] = new_label

        new_dev_labels = {}
        for (r, c), v in self._split_coord_dict(self.device_labels).items():
            new_dev_labels[f"{r},{new_c if c == old_c else c}"] = v

        new_dev_states = {}
        for (r, c), v in self._split_coord_dict(self.device_states).items():
            new_dev_states[f"{r},{new_c if c == old_c else c}"] = v

        new_dev_notes = {}
        for (r, c), v in self._split_coord_dict(self.device_notes).items():
            new_dev_notes[f"{r},{new_c if c == old_c else c}"] = v

        return replace(
            self,
            cols=new_cols,
            col_labels=new_col_labels,
            device_labels=new_dev_labels,
            device_states=new_dev_states,
            device_notes=new_dev_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_added_row(
        self, row: str | int, label: str = "", after_row: str | int | None = None
    ) -> Sample:
        r_str = str(row)
        if r_str in self.rows:
            return self.with_row_label(r_str, label) if label else self
        rows_list = list(self.rows)
        if after_row is not None and str(after_row) in rows_list:
            idx = rows_list.index(str(after_row)) + 1
            rows_list.insert(idx, r_str)
        else:
            rows_list.append(r_str)
        new_row_labels = dict(self.row_labels)
        if label:
            new_row_labels[r_str] = label
        return replace(
            self,
            rows=tuple(rows_list),
            row_labels=new_row_labels,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_added_col(
        self, col: str | int, label: str = "", after_col: str | int | None = None
    ) -> Sample:
        c_str = str(col)
        if c_str in self.cols:
            return self.with_col_label(c_str, label) if label else self
        cols_list = list(self.cols)
        if after_col is not None and str(after_col) in cols_list:
            idx = cols_list.index(str(after_col)) + 1
            cols_list.insert(idx, c_str)
        else:
            cols_list.append(c_str)
        new_col_labels = dict(self.col_labels)
        if label:
            new_col_labels[c_str] = label
        return replace(
            self,
            cols=tuple(cols_list),
            col_labels=new_col_labels,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_deleted_row(self, row: str | int) -> Sample:
        r_str = str(row)
        if r_str not in self.rows:
            return self
        new_rows = tuple(r for r in self.rows if r != r_str)
        new_row_labels = {k: v for k, v in self.row_labels.items() if k != r_str}
        new_dev_labels = {
            k: v for k, v in self.device_labels.items() if not k.startswith(f"{r_str},")
        }
        new_dev_states = {
            k: v for k, v in self.device_states.items() if not k.startswith(f"{r_str},")
        }
        new_dev_notes = {
            k: v for k, v in self.device_notes.items() if not k.startswith(f"{r_str},")
        }
        return replace(
            self,
            rows=new_rows,
            row_labels=new_row_labels,
            device_labels=new_dev_labels,
            device_states=new_dev_states,
            device_notes=new_dev_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_deleted_col(self, col: str | int) -> Sample:
        c_str = str(col)
        if c_str not in self.cols:
            return self
        new_cols = tuple(c for c in self.cols if c != c_str)
        new_col_labels = {k: v for k, v in self.col_labels.items() if k != c_str}
        new_dev_labels = {
            k: v for k, v in self.device_labels.items() if not k.endswith(f",{c_str}")
        }
        new_dev_states = {
            k: v for k, v in self.device_states.items() if not k.endswith(f",{c_str}")
        }
        new_dev_notes = {
            k: v for k, v in self.device_notes.items() if not k.endswith(f",{c_str}")
        }
        return replace(
            self,
            cols=new_cols,
            col_labels=new_col_labels,
            device_labels=new_dev_labels,
            device_states=new_dev_states,
            device_notes=new_dev_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def remap_rows(self, row_mapping: Mapping[str, str]) -> Sample:
        """Remap row keys (e.g. {'1': '20', '2': '21', ...}), updating all cell data and labels."""
        new_rows = tuple(row_mapping.get(r, r) for r in self.rows)

        # Remap row labels
        new_row_labels: dict[str, str] = {}
        for r in self.rows:
            target_r = row_mapping.get(r, r)
            old_label = self.row_labels.get(r, f"Row {r}")
            if old_label in (r, f"Row {r}"):
                new_row_labels[target_r] = f"Row {target_r}"
            else:
                new_row_labels[target_r] = old_label

        # Remap device labels, states, notes
        new_device_labels: dict[str, str] = {}
        for (r, c), v in self._split_coord_dict(self.device_labels).items():
            new_r = row_mapping.get(r, r)
            new_device_labels[f"{new_r},{c}"] = v

        new_device_states: dict[str, str] = {}
        for (r, c), v in self._split_coord_dict(self.device_states).items():
            new_r = row_mapping.get(r, r)
            new_device_states[f"{new_r},{c}"] = v

        new_device_notes: dict[str, str] = {}
        for (r, c), v in self._split_coord_dict(self.device_notes).items():
            new_r = row_mapping.get(r, r)
            new_device_notes[f"{new_r},{c}"] = v

        return replace(
            self,
            rows=new_rows,
            row_labels=new_row_labels,
            device_labels=new_device_labels,
            device_states=new_device_states,
            device_notes=new_device_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_row_renumbering(
        self,
        start_row: int,
        count: int | None = None,
        row_prefix: str = "",
    ) -> Sample:
        """Renumber numeric rows sequentially starting from start_row (e.g. 20 to 30)."""
        if count is None:
            count = len(self.rows)
        new_row_keys = [str(start_row + i) for i in range(count)]

        # Map existing rows to new row keys positionally
        row_mapping: dict[str, str] = {}
        for idx, old_r in enumerate(self.rows):
            if idx < len(new_row_keys):
                row_mapping[old_r] = new_row_keys[idx]

        remapped = self.remap_rows(row_mapping)

        # Handle row count changes (extension or reduction)
        all_rows = tuple(new_row_keys)
        all_labels: dict[str, str] = {}
        for idx, r in enumerate(all_rows):
            if r in remapped.row_labels:
                all_labels[r] = remapped.row_labels[r]
            elif row_prefix:
                all_labels[r] = f"{row_prefix} {r}".strip()
            else:
                all_labels[r] = f"Row {r}"

        # Retain cell data for valid rows only
        valid_rows = set(all_rows)
        cleaned_labels = {
            k: v for k, v in remapped.device_labels.items()
            if k.split(",", 1)[0] in valid_rows
        }
        cleaned_states = {
            k: v for k, v in remapped.device_states.items()
            if k.split(",", 1)[0] in valid_rows
        }
        cleaned_notes = {
            k: v for k, v in remapped.device_notes.items()
            if k.split(",", 1)[0] in valid_rows
        }

        return replace(
            remapped,
            rows=all_rows,
            row_labels=all_labels,
            device_labels=cleaned_labels,
            device_states=cleaned_states,
            device_notes=cleaned_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_structure(
        self,
        rows: Sequence[str],
        row_labels: Mapping[str, str],
        cols: Sequence[str],
        col_labels: Mapping[str, str],
        row_mapping: Mapping[str, str] | None = None,
    ) -> Sample:
        """Update rows, cols and labels while retaining existing cell data for remaining keys."""
        current = self
        if row_mapping:
            current = current.remap_rows(row_mapping)
        elif len(rows) == len(self.rows) and set(rows) != set(self.rows):
            # Positional renumbering auto-detection
            auto_map = {old: new for old, new in zip(self.rows, rows, strict=False) if old != new}
            if auto_map:
                current = current.remap_rows(auto_map)

        valid_rows = set(rows)
        valid_cols = set(cols)

        retained_labels = {}
        for (r, c), v in current._split_coord_dict(current.device_labels).items():
            if r in valid_rows and c in valid_cols:
                retained_labels[f"{r},{c}"] = v

        retained_states = {}
        for (r, c), v in current._split_coord_dict(current.device_states).items():
            if r in valid_rows and c in valid_cols:
                retained_states[f"{r},{c}"] = v

        retained_notes = {}
        for (r, c), v in current._split_coord_dict(current.device_notes).items():
            if r in valid_rows and c in valid_cols:
                retained_notes[f"{r},{c}"] = v

        return replace(
            self,
            rows=tuple(rows),
            row_labels=dict(row_labels),
            cols=tuple(cols),
            col_labels=dict(col_labels),
            device_labels=retained_labels,
            device_states=retained_states,
            device_notes=retained_notes,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_cells_state(
        self, coords: Sequence[tuple[str | int, str | int]], state: str
    ) -> Sample:
        new_states = dict(self.device_states)
        for r, c in coords:
            key = self.coord_key(r, c)
            new_states[key] = state
        return replace(
            self,
            device_states=new_states,
            updated_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def with_row_state(self, row: str | int, state: str) -> Sample:
        r_str = str(row)
        coords = [(r_str, c) for c in self.cols]
        return self.with_cells_state(coords, state)

    def with_col_state(self, col: str | int, state: str) -> Sample:
        c_str = str(col)
        coords = [(r, c_str) for r in self.rows]
        return self.with_cells_state(coords, state)

    def total_cells(self) -> int:
        return len(self.rows) * len(self.cols)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "name": self.name,
            "description": self.description,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "tags": list(self.tags),
            "rows": list(self.rows),
            "row_labels": dict(self.row_labels),
            "cols": list(self.cols),
            "col_labels": dict(self.col_labels),
            "device_labels": dict(self.device_labels),
            "device_states": dict(self.device_states),
            "device_notes": dict(self.device_notes),
            "attachments": [att.to_dict() for att in self.attachments],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Sample:
        attachments_data = data.get("attachments") or []
        attachments = tuple(
            SampleAttachment.from_dict(att) if isinstance(att, Mapping) else att
            for att in attachments_data
        )
        return cls(
            sample_id=str(data["sample_id"]),
            name=str(data.get("name") or data["sample_id"]),
            description=str(data.get("description") or ""),
            created_at_utc=str(data.get("created_at_utc") or ""),
            updated_at_utc=str(data.get("updated_at_utc") or ""),
            tags=tuple(str(tag) for tag in (data.get("tags") or ())),
            rows=tuple(str(r) for r in (data.get("rows") or ())),
            row_labels={str(k): str(v) for k, v in (data.get("row_labels") or {}).items()},
            cols=tuple(str(c) for c in (data.get("cols") or ())),
            col_labels={str(k): str(v) for k, v in (data.get("col_labels") or {}).items()},
            device_labels={str(k): str(v) for k, v in (data.get("device_labels") or {}).items()},
            device_states={str(k): str(v) for k, v in (data.get("device_states") or {}).items()},
            device_notes={str(k): str(v) for k, v in (data.get("device_notes") or {}).items()},
            attachments=attachments,
        )
