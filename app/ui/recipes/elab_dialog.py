"""Fluent editor for configuring eLabFTW result upload blocks in recipes."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)

from app.integrations.elab.config import ElabCredentials, ElabIntegrationProfile
from app.recipes.models import RecipeNode
from app.ui.recipes.fluent_dialog import FluentRecipeDialog


class ElabUploadEditorDialog(FluentRecipeDialog):
    """Fluent configuration dialog for an eLabFTW upload step in a recipe."""

    def __init__(
        self,
        node: RecipeNode,
        parent: QWidget | None = None,
        *,
        profile_provider: Callable[[], ElabIntegrationProfile] | None = None,
        credentials_provider: Callable[[], ElabCredentials] | None = None,
        available_templates_provider: Callable[[], list[tuple[int, str]]] | None = None,
        refresh_templates_callback: Callable[[], None] | None = None,
        navigate_to_elab: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            resizable=True,
            modal_shell_outer_margins=(0, 0, 0, 0),
            modal_shell_backdrop_margins=(4, 4, 4, 4),
            modal_shell_surface_margins=(22, 18, 22, 18),
        )
        self.setObjectName("elabUploadEditorDialog")
        self.setProperty("stationSurface", "raised")
        self.setWindowTitle("eLabFTW Upload Configuration")
        self.setMinimumWidth(580)
        self.setMinimumHeight(560)
        self.resize(640, 620)

        self._node = node
        self._profile_provider = profile_provider
        self._credentials_provider = credentials_provider
        self._available_templates_provider = available_templates_provider
        self._refresh_templates_callback = refresh_templates_callback
        self._navigate_to_elab = navigate_to_elab

        self._active_profile: ElabIntegrationProfile | None = (
            profile_provider() if profile_provider is not None else None
        )
        self._credentials: ElabCredentials | None = (
            credentials_provider() if credentials_provider is not None else None
        )

        self._template_names_by_id: dict[int, str] = {}

        self._build_ui()
        self._populate_from_node()

    def _build_ui(self) -> None:
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=14)

        # ── 1. Clean Header (on surface, no extra card border) ───────
        header = QWidget(surface)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(2, 0, 2, 0)
        header_layout.setSpacing(4)

        title = SubtitleLabel("eLabFTW Result Upload", header)
        header_layout.addWidget(title)

        hint = CaptionLabel(
            "Configure automatic upload of the closed measurement result (HDF5 and CSV) "
            "to eLabFTW upon successful run completion according to the chosen template.",
            header,
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        header_layout.addWidget(hint)
        layout.addWidget(header)

        # ── 2. Connection Status Card ───────────────────────────────
        status_card = CardWidget(surface)
        status_card.setObjectName("elabConnectionCard")
        status_card.setProperty("stationSurface", "card")
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(16, 12, 16, 12)
        status_layout.setSpacing(12)

        info_col = QVBoxLayout()
        info_col.setSpacing(3)
        conn_title = StrongBodyLabel("Connection & Credentials", status_card)
        info_col.addWidget(conn_title)

        host_text = self._credentials.host if self._credentials and self._credentials.host else "Not configured"
        has_key = bool(self._credentials and self._credentials.api_key.strip())
        key_status = "Configured" if has_key else "Missing API key in .env"
        self.connection_info = CaptionLabel(
            f"Host: {host_text}  ·  Credentials: {key_status}", status_card
        )
        self.connection_info.setObjectName("muted")
        self.connection_info.setWordWrap(True)
        info_col.addWidget(self.connection_info)
        status_layout.addLayout(info_col, 1)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        if self._refresh_templates_callback is not None:
            self.refresh_button = PushButton("Refresh templates", status_card)
            self.refresh_button.clicked.connect(self._on_refresh_clicked)
            btn_layout.addWidget(self.refresh_button)
        if self._navigate_to_elab is not None:
            self.tab_button = PushButton("Open eLab tab", status_card)
            self.tab_button.clicked.connect(self._on_navigate_to_elab)
            btn_layout.addWidget(self.tab_button)
        status_layout.addLayout(btn_layout)
        layout.addWidget(status_card)

        # ── 3. Configuration Form Card ──────────────────────────────
        form_card = CardWidget(surface)
        form_card.setObjectName("elabFormCard")
        form_card.setProperty("stationSurface", "card")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(18, 16, 18, 16)
        form_layout.setSpacing(14)

        # 3a. Template group
        template_group = QVBoxLayout()
        template_group.setSpacing(5)
        template_label = StrongBodyLabel("Experiment template", form_card)
        template_group.addWidget(template_label)

        self.template_combo = ComboBox(form_card)
        self.template_combo.setAccessibleName("eLab experiment template")
        self.template_combo.setMinimumHeight(33)
        self.template_combo.currentIndexChanged.connect(self._on_template_selected)
        template_group.addWidget(self.template_combo)

        self.template_detail_label = CaptionLabel(form_card)
        self.template_detail_label.setObjectName("muted")
        self.template_detail_label.setWordWrap(True)
        template_group.addWidget(self.template_detail_label)
        form_layout.addLayout(template_group)

        # 3b. Title pattern group
        title_group = QVBoxLayout()
        title_group.setSpacing(5)
        title_label = StrongBodyLabel("Experiment title pattern", form_card)
        title_group.addWidget(title_label)

        self.title_pattern_edit = LineEdit(form_card)
        self.title_pattern_edit.setPlaceholderText("PyLab measurement {run_name}")
        self.title_pattern_edit.setAccessibleName("Experiment title pattern")
        self.title_pattern_edit.setClearButtonEnabled(True)
        title_group.addWidget(self.title_pattern_edit)

        title_hint = CaptionLabel(
            "Available tokens: {run_name} (stem), {status} (safe/completed), {created_at} (UTC timestamp)",
            form_card,
        )
        title_hint.setObjectName("muted")
        title_hint.setWordWrap(True)
        title_group.addWidget(title_hint)
        form_layout.addLayout(title_group)

        # 3c. Tags group
        tags_group = QVBoxLayout()
        tags_group.setSpacing(5)
        tags_label = StrongBodyLabel("Tags", form_card)
        tags_group.addWidget(tags_label)

        self.tags_edit = LineEdit(form_card)
        self.tags_edit.setPlaceholderText("pylab, measurement, mtj")
        self.tags_edit.setAccessibleName("eLab tags")
        self.tags_edit.setClearButtonEnabled(True)
        tags_group.addWidget(self.tags_edit)

        tags_hint = CaptionLabel(
            "Comma-separated tags attached to the experiment in eLabFTW.",
            form_card,
        )
        tags_hint.setObjectName("muted")
        tags_hint.setWordWrap(True)
        tags_group.addWidget(tags_hint)
        form_layout.addLayout(tags_group)

        # 3d. Attachments group
        attach_group = QVBoxLayout()
        attach_group.setSpacing(8)
        attach_label = StrongBodyLabel("Artifacts to attach", form_card)
        attach_group.addWidget(attach_label)

        checks_layout = QHBoxLayout()
        checks_layout.setSpacing(24)
        self.attach_hdf5_check = CheckBox("Attach HDF5 (complete scientific record)", form_card)
        self.attach_hdf5_check.setChecked(True)
        self.attach_csv_check = CheckBox("Attach CSV summary tables", form_card)
        self.attach_csv_check.setChecked(True)
        checks_layout.addWidget(self.attach_hdf5_check)
        checks_layout.addWidget(self.attach_csv_check)
        checks_layout.addStretch(1)
        attach_group.addLayout(checks_layout)
        form_layout.addLayout(attach_group)

        layout.addWidget(form_card)

        # ── 4. Validation error banner ──────────────────────────────
        self.validation_error_label = CaptionLabel(surface)
        self.validation_error_label.setObjectName("danger")
        self.validation_error_label.setWordWrap(True)
        self.validation_error_label.setVisible(False)
        layout.addWidget(self.validation_error_label)

        layout.addStretch(1)

        # ── 5. Footer actions ───────────────────────────────────────
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 4, 0, 0)
        footer.addStretch(1)
        cancel_btn = PushButton("Cancel", surface)
        cancel_btn.setMinimumWidth(100)
        cancel_btn.clicked.connect(self.reject)
        apply_btn = PrimaryPushButton("Apply configuration", surface)
        apply_btn.setMinimumWidth(140)
        apply_btn.clicked.connect(self._accept_if_valid)
        footer.addWidget(cancel_btn)
        footer.addWidget(apply_btn)
        layout.addLayout(footer)

    def _on_navigate_to_elab(self) -> None:
        self.reject()
        if self._navigate_to_elab is not None:
            self._navigate_to_elab()

    def _on_refresh_clicked(self) -> None:
        if self._refresh_templates_callback is not None:
            self._refresh_templates_callback()
        self._populate_templates(retain_selection=True)

    def _populate_templates(self, retain_selection: bool = True) -> None:
        selected_id = self.template_combo.currentData() if retain_selection else None

        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self._template_names_by_id.clear()

        active_id = self._active_profile.template_id if self._active_profile else None
        active_name = self._active_profile.template_name if self._active_profile else ""
        default_label = (
            f"Use active template from eLab tab ({active_name} · #{active_id})"
            if active_id is not None
            else "Use active template from eLab tab (None currently set)"
        )
        self.template_combo.addItem(default_label, userData=0)

        templates: list[tuple[int, str]] = []
        if self._available_templates_provider is not None:
            try:
                templates = self._available_templates_provider()
            except Exception:
                templates = []

        node_tid = self._node.data.get("template_id")
        node_tname = self._node.data.get("template_name", "")
        if (
            node_tid not in (None, 0, "0", "")
            and not any(t[0] == int(node_tid) for t in templates)
        ):
            templates.append((int(node_tid), str(node_tname or f"Template #{node_tid}")))

        for tid, tname in templates:
            self._template_names_by_id[tid] = tname
            self.template_combo.addItem(f"{tname}  ·  #{tid}", userData=tid)

        if selected_id is not None:
            idx = self.template_combo.findData(selected_id)
            if idx >= 0:
                self.template_combo.setCurrentIndex(idx)
            else:
                self.template_combo.setCurrentIndex(0)
        else:
            self.template_combo.setCurrentIndex(0)

        self.template_combo.blockSignals(False)
        self._on_template_selected(self.template_combo.currentIndex())

    def _on_template_selected(self, index: int) -> None:
        tid = self.template_combo.currentData()
        if tid == 0:
            active_id = self._active_profile.template_id if self._active_profile else None
            active_name = self._active_profile.template_name if self._active_profile else ""
            if active_id is not None:
                self.template_detail_label.setText(
                    f"Will use active eLab tab policy: {active_name} (#{active_id})"
                )
            else:
                self.template_detail_label.setText(
                    "No default template is configured in the eLabFTW tab. "
                    "Select a specific template above or configure one in the eLab tab."
                )
        else:
            name = self._template_names_by_id.get(tid, "")
            self.template_detail_label.setText(f"Selected template #{tid}: {name or 'unnamed'}")

    def _populate_from_node(self) -> None:
        self._populate_templates(retain_selection=False)

        data = self._node.data
        node_tid = data.get("template_id")
        if node_tid not in (None, 0, "0", ""):
            try:
                tid_int = int(node_tid)
                idx = self.template_combo.findData(tid_int)
                if idx >= 0:
                    self.template_combo.setCurrentIndex(idx)
            except (ValueError, TypeError):
                pass

        default_title = (
            self._active_profile.title_pattern
            if self._active_profile and self._active_profile.title_pattern
            else "PyLab measurement {run_name}"
        )
        self.title_pattern_edit.setText(str(data.get("title_pattern") or default_title))

        node_tags = data.get("tags")
        if node_tags:
            if isinstance(node_tags, str):
                self.tags_edit.setText(node_tags)
            elif isinstance(node_tags, (list, tuple)):
                self.tags_edit.setText(", ".join(str(t) for t in node_tags))
        elif self._active_profile and self._active_profile.tags:
            self.tags_edit.setText(", ".join(self._active_profile.tags))

        self.attach_hdf5_check.setChecked(bool(data.get("attach_hdf5", True)))
        self.attach_csv_check.setChecked(bool(data.get("attach_csv", True)))

    def _accept_if_valid(self) -> None:
        title_pattern = self.title_pattern_edit.text().strip()
        if not title_pattern:
            self._show_error("Experiment title pattern cannot be empty.")
            return

        try:
            title_pattern.format_map(
                {"run_name": "sample", "status": "safe", "created_at": "2026-09-03T10:00:00Z"}
            )
        except (KeyError, ValueError) as exc:
            self._show_error(
                f"Title pattern contains unsupported placeholder: {exc}. "
                "Only {run_name}, {status} and {created_at} are allowed."
            )
            return

        if not self.attach_hdf5_check.isChecked() and not self.attach_csv_check.isChecked():
            self._show_error("Select at least one result format (HDF5 or CSV) to attach.")
            return

        self.validation_error_label.setVisible(False)
        self.accept()

    def _show_error(self, message: str) -> None:
        self.validation_error_label.setText(message)
        self.validation_error_label.setVisible(True)

    def result_data(self) -> dict[str, Any]:
        """Return the dictionary to be stored in the RecipeNode.data."""
        tid = self.template_combo.currentData()
        template_id: int | None = int(tid) if tid and tid > 0 else None
        template_name = self._template_names_by_id.get(template_id, "") if template_id is not None else ""

        tags_raw = self.tags_edit.text()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        return {
            "template_id": template_id,
            "template_name": template_name,
            "title_pattern": self.title_pattern_edit.text().strip(),
            "tags": tags,
            "attach_hdf5": self.attach_hdf5_check.isChecked(),
            "attach_csv": self.attach_csv_check.isChecked(),
        }
