"""Fluent workspace for configuring and sending results to eLabFTW."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit as QtLineEdit,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    ToolButton,
)

from app.integrations.elab import (
    ElabConfigurationError,
    ElabCredentials,
    ElabIntegrationProfile,
    ElabTemplate,
    ElabUploadLedger,
    ElabUploadRequest,
    ElabUploadResult,
    load_credentials,
    resolve_env_path,
    save_credentials,
)
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.ui.dialogs import StationFileDialog as QFileDialog
from app.ui.elab.favorites_dialog import ElabFavoritesDialog
from app.ui.elab.searchable_combo import SearchableComboBox
from app.ui.elab.workers import ElabTemplatesWorker, ElabUploadWorker


class ElabPage(QWidget):
    """Configure eLabFTW and upload closed, immutable station results."""

    settings_saved = Signal(object)
    configuration_changed = Signal()
    status = Signal(str)

    def __init__(
        self,
        repository: SettingsRepository,
        parent: QWidget | None = None,
        *,
        settings: StationSettings | None = None,
        env_path: str | Path = ".env",
        simulation: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("elabPage")
        self.setProperty("stationSurface", "page")
        self._repository = repository
        self._env_path = resolve_env_path(env_path)
        self._simulation = simulation
        self._settings = settings or repository.load().settings
        self._profile = ElabIntegrationProfile.from_application(self._settings.application)
        self._credentials = load_credentials(self._env_path)
        self._selected_result: Path | None = None
        self._templates_worker: ElabTemplatesWorker | None = None
        self._upload_worker: ElabUploadWorker | None = None
        self._template_request_is_test = False
        self._upload_is_automatic = False
        self._last_experiment_url: str | None = None
        # qfluentwidgets.ComboBox stores one userData value per item and does
        # not implement Qt's role-aware itemData(index, role) overload.  Keep
        # the display name in a small page-owned map while the combo's userData
        # remains the stable numeric template ID.
        self._template_names_by_id: dict[str, str] = {}
        self._ledger = ElabUploadLedger(self._repository.path.with_name("elab_uploads.json"))
        self._compact_layout: bool | None = None
        self._build()
        self._populate_profile_controls()
        self._refresh_credentials_state()
        self._refresh_history()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = SubtitleLabel("eLabFTW", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = BodyLabel(
            "Turn a closed PyLab measurement into a traceable eLab experiment with the "
            "selected research template and immutable result attachments.",
            self,
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.simulation_notice = CardWidget(self)
        notice_layout = QVBoxLayout(self.simulation_notice)
        notice_layout.setContentsMargins(16, 12, 16, 12)
        notice_layout.addWidget(StrongBodyLabel("Simulation mode", self.simulation_notice))
        notice_text = CaptionLabel(
            "External eLab writes are disabled while the station is simulating. "
            "Credentials and upload policy can still be prepared here.",
            self.simulation_notice,
        )
        notice_text.setWordWrap(True)
        notice_layout.addWidget(notice_text)
        layout.addWidget(self.simulation_notice)

        self.connection_card = CardWidget(self)
        connection_layout = QVBoxLayout(self.connection_card)
        connection_layout.setContentsMargins(16, 14, 16, 16)
        connection_layout.setSpacing(8)
        connection_layout.addWidget(StrongBodyLabel("Connection", self.connection_card))
        connection_hint = CaptionLabel(
            "The key is kept in the local .env file and is never written to settings.yml or the upload ledger.",
            self.connection_card,
        )
        connection_hint.setWordWrap(True)
        connection_layout.addWidget(connection_hint)
        connection_form = QFormLayout()
        connection_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        connection_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        self.host_edit = LineEdit(self.connection_card)
        self.host_edit.setPlaceholderText("https://elab.example.org")
        self.host_edit.setAccessibleName("eLab host")
        connection_form.addRow("eLab host", self.host_edit)
        self.api_key_edit = LineEdit(self.connection_card)
        self.api_key_edit.setEchoMode(QtLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            "Enter a new API key, or leave blank to keep the current key"
        )
        self.api_key_edit.setClearButtonEnabled(True)
        self.api_key_edit.setAccessibleName("eLab API key")
        connection_form.addRow("API key", self.api_key_edit)
        connection_layout.addLayout(connection_form)
        connection_actions = QHBoxLayout()
        connection_actions.setSpacing(8)
        self.reload_credentials_button = PushButton("Reload from .env", self.connection_card)
        self.save_credentials_button = PrimaryPushButton("Save credentials", self.connection_card)
        self.test_connection_button = PushButton("Test connection", self.connection_card)
        self.refresh_templates_button = PushButton("Refresh templates", self.connection_card)
        connection_actions.addWidget(self.reload_credentials_button)
        connection_actions.addWidget(self.save_credentials_button)
        connection_actions.addWidget(self.test_connection_button)
        connection_actions.addStretch(1)
        connection_layout.addLayout(connection_actions)
        self.credentials_state = BodyLabel(self.connection_card)
        self.credentials_state.setObjectName("elabStatus")
        self.credentials_state.setWordWrap(True)
        connection_layout.addWidget(self.credentials_state)
        layout.addWidget(self.connection_card)

        self.policy_card = CardWidget(self)
        policy_layout = QVBoxLayout(self.policy_card)
        policy_layout.setContentsMargins(16, 14, 16, 16)
        policy_layout.setSpacing(8)
        policy_layout.addWidget(
            StrongBodyLabel("Research template and automation", self.policy_card)
        )
        policy_hint = CaptionLabel(
            "One eLab experiment is created per local result. Automatic upload starts only after a run closes safely.",
            self.policy_card,
        )
        policy_hint.setWordWrap(True)
        policy_layout.addWidget(policy_hint)
        policy_form = QFormLayout()
        policy_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        policy_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        template_row = QHBoxLayout()
        template_row.setSpacing(8)
        self.template_combo = SearchableComboBox(self.policy_card)
        self.template_combo.setPlaceholderText("Load templates from eLab")
        self.template_combo.setAccessibleName("eLab experiment template")
        self.favorite_toggle_button = ToolButton(FIF.HEART, self.policy_card)
        self.favorite_toggle_button.setToolTip("Add this template to favorites")
        self.favorite_toggle_button.setAccessibleName("Toggle favorite template")
        self.favorites_dialog_button = PushButton(FIF.HEART, "Favorites...", self.policy_card)
        self.favorites_dialog_button.setToolTip("Choose from favorite templates")
        self.favorites_dialog_button.setAccessibleName("Choose from favorites")
        template_row.addWidget(self.template_combo, 1)
        template_row.addWidget(self.favorite_toggle_button)
        template_row.addWidget(self.favorites_dialog_button)
        template_row.addWidget(self.refresh_templates_button)
        policy_form.addRow("Experiment template", template_row)
        self.recent_template_combo = ComboBox(self.policy_card)
        self.recent_template_combo.setPlaceholderText("No saved template shortcuts")
        self.recent_template_combo.setAccessibleName("Recently used eLab template")
        policy_form.addRow("Recent templates", self.recent_template_combo)
        self.template_info = CaptionLabel(self.policy_card)
        self.template_info.setWordWrap(True)
        policy_form.addRow("Template", self.template_info)
        self.title_pattern_edit = LineEdit(self.policy_card)
        self.title_pattern_edit.setPlaceholderText("PyLab measurement {run_name}")
        self.title_pattern_edit.setAccessibleName("eLab experiment title pattern")
        policy_form.addRow("Experiment title", self.title_pattern_edit)
        self.tags_edit = LineEdit(self.policy_card)
        self.tags_edit.setPlaceholderText("pylab, measurement")
        self.tags_edit.setAccessibleName("eLab experiment tags")
        policy_form.addRow("Tags", self.tags_edit)
        policy_layout.addLayout(policy_form)
        checks = QHBoxLayout()
        checks.setSpacing(18)
        self.auto_upload_check = CheckBox("Upload safe runs automatically", self.policy_card)
        self.upload_hdf5_check = CheckBox("Attach HDF5", self.policy_card)
        self.upload_csv_check = CheckBox("Attach CSV when available", self.policy_card)
        checks.addWidget(self.auto_upload_check)
        checks.addWidget(self.upload_hdf5_check)
        checks.addWidget(self.upload_csv_check)
        checks.addStretch(1)
        policy_layout.addLayout(checks)
        policy_actions = QHBoxLayout()
        policy_actions.setSpacing(8)
        self.save_policy_button = PrimaryPushButton("Save automation policy", self.policy_card)
        policy_actions.addWidget(self.save_policy_button)
        policy_actions.addStretch(1)
        policy_layout.addLayout(policy_actions)
        self.policy_status = BodyLabel(self.policy_card)
        self.policy_status.setObjectName("elabStatus")
        self.policy_status.setWordWrap(True)
        policy_layout.addWidget(self.policy_status)
        layout.addWidget(self.policy_card)

        self.upload_card = CardWidget(self)
        upload_layout = QVBoxLayout(self.upload_card)
        upload_layout.setContentsMargins(16, 14, 16, 16)
        upload_layout.setSpacing(8)
        upload_layout.addWidget(StrongBodyLabel("Upload a selected result", self.upload_card))
        upload_hint = CaptionLabel(
            "Only a terminal HDF5 result can be sent. The local file is never moved or deleted.",
            self.upload_card,
        )
        upload_hint.setWordWrap(True)
        upload_layout.addWidget(upload_hint)
        selected_row = QHBoxLayout()
        selected_row.setSpacing(8)
        self.selected_result_edit = LineEdit(self.upload_card)
        self.selected_result_edit.setReadOnly(True)
        self.selected_result_edit.setPlaceholderText("Choose an HDF5 result...")
        self.selected_result_edit.setAccessibleName("Selected result file")
        self.browse_result_button = PushButton("Choose result...", self.upload_card)
        self.upload_button = PrimaryPushButton("Upload selected result", self.upload_card)
        self.upload_button.setEnabled(False)
        selected_row.addWidget(self.selected_result_edit, 1)
        selected_row.addWidget(self.browse_result_button)
        selected_row.addWidget(self.upload_button)
        upload_layout.addLayout(selected_row)
        self.upload_status = BodyLabel(self.upload_card)
        self.upload_status.setObjectName("elabStatus")
        self.upload_status.setWordWrap(True)
        upload_layout.addWidget(self.upload_status)
        self.open_experiment_button = PushButton("Open last eLab experiment", self.upload_card)
        self.open_experiment_button.setEnabled(False)
        self.open_experiment_button.clicked.connect(self._open_last_experiment)
        upload_layout.addWidget(self.open_experiment_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.upload_card)

        self.history_card = CardWidget(self)
        history_layout = QVBoxLayout(self.history_card)
        history_layout.setContentsMargins(16, 14, 16, 16)
        history_layout.setSpacing(8)
        history_header = QHBoxLayout()
        history_header.addWidget(StrongBodyLabel("Upload history", self.history_card))
        history_header.addStretch(1)
        self.refresh_history_button = PushButton("Refresh", self.history_card)
        history_header.addWidget(self.refresh_history_button)
        history_layout.addLayout(history_header)
        self.history_table = TableWidget(self.history_card)
        self.history_table.setObjectName("elabHistoryTable")
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels(
            ["Local result", "Status", "Experiment", "Template", "Files", "Created (UTC)"]
        )
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setMinimumHeight(150)
        self.history_table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        history_layout.addWidget(self.history_table)
        layout.addWidget(self.history_card)

        layout.addStretch(1)

        self.simulation_notice.setVisible(self._simulation)
        self.reload_credentials_button.clicked.connect(self._reload_credentials)
        self.save_credentials_button.clicked.connect(self._save_credentials)
        self.test_connection_button.clicked.connect(self._test_connection)
        self.refresh_templates_button.clicked.connect(self._refresh_templates)
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        self.recent_template_combo.currentIndexChanged.connect(self._recent_template_changed)
        self.favorite_toggle_button.clicked.connect(self._toggle_favorite_clicked)
        self.favorites_dialog_button.clicked.connect(self._open_favorites_dialog)
        self.save_policy_button.clicked.connect(self._save_policy)
        self.browse_result_button.clicked.connect(self._browse_result)
        self.upload_button.clicked.connect(self._upload_selected)
        self.refresh_history_button.clicked.connect(self._refresh_history)

    def _populate_profile_controls(self) -> None:
        self.auto_upload_check.setChecked(self._profile.enabled)
        self.title_pattern_edit.setText(self._profile.title_pattern)
        self.tags_edit.setText(", ".join(self._profile.tags))
        self.upload_hdf5_check.setChecked(self._profile.upload_hdf5)
        self.upload_csv_check.setChecked(self._profile.upload_csv)
        self.template_combo.clear()
        self._template_names_by_id.clear()
        self.recent_template_combo.blockSignals(True)
        self.recent_template_combo.clear()
        for reference in self._profile.recent_templates:
            self.recent_template_combo.addItem(
                f"{reference.title}  ·  #{reference.id}", userData=reference.id
            )
        self.recent_template_combo.setCurrentIndex(-1)
        self.recent_template_combo.blockSignals(False)
        if self._profile.template_id is not None:
            template_id = str(self._profile.template_id)
            is_fav = self._profile.is_favorite(self._profile.template_id)
            self.template_combo.addItem(
                self._profile.template_name or f"Configured template #{self._profile.template_id}",
                icon=FIF.HEART if is_fav else None,
                userData=self._profile.template_id,
            )
            self._template_names_by_id[template_id] = self._profile.template_name
            self.template_combo.setCurrentIndex(0)
        else:
            self.template_combo.setCurrentIndex(-1)
            self.template_info.setText(
                "No template selected. Refresh templates after saving credentials."
            )
        self._template_changed(self.template_combo.currentIndex())
        self._update_favorite_controls()
        self._update_upload_button()

    def _refresh_credentials_state(self) -> None:
        if self._credentials.configured:
            self.host_edit.setText(self._credentials.host)
            self.api_key_edit.setPlaceholderText("Key loaded from .env; leave blank to keep it")
            self.credentials_state.setText(
                f"Credentials loaded from {self._env_path}. The API key is masked in this page."
            )
            self.credentials_state.setProperty("elabStatusState", "success")
        else:
            if self._credentials.host:
                self.host_edit.setText(self._credentials.host)
            self.api_key_edit.setPlaceholderText("Enter an eLab API key")
            self.credentials_state.setText(
                f"No complete credentials found. Add ELAB_API and ELAB_HOST to {self._env_path}."
            )
            self.credentials_state.setProperty("elabStatusState", "caution")
        self._repolish(self.credentials_state)

    def _credentials_for_form(self) -> ElabCredentials:
        key = self.api_key_edit.text().strip() or self._credentials.api_key
        return ElabCredentials.from_values(self.host_edit.text().strip(), key)

    def _reload_credentials(self) -> None:
        """Reload the file-backed credentials without exposing the API key."""

        try:
            self._credentials = load_credentials(self._env_path)
        except ElabConfigurationError as exc:
            self._set_status(self.credentials_state, str(exc), "danger")
            return
        self.api_key_edit.clear()
        self._refresh_credentials_state()
        self._set_status(
            self.credentials_state,
            "Credentials reloaded from .env."
            if self._credentials.configured
            else f"No complete credentials found in {self._env_path}.",
            "success" if self._credentials.configured else "caution",
        )
        self._update_upload_button()
        self.configuration_changed.emit()

    def _save_credentials(self) -> None:
        try:
            credentials = self._credentials_for_form()
            save_credentials(self._env_path, host=credentials.host, api_key=credentials.api_key)
        except (ElabConfigurationError, OSError) as exc:
            self._show_error("eLab credentials not saved", str(exc))
            return
        self._credentials = credentials
        os.environ["ELAB_HOST"] = credentials.host
        os.environ["ELAB_API"] = credentials.api_key
        self.api_key_edit.clear()
        self._refresh_credentials_state()
        self._set_status(
            self.credentials_state, "Credentials saved. The API key remains masked.", "success"
        )
        self.status.emit(f"ELAB credentials saved to {self._env_path}")
        self.configuration_changed.emit()

    def _test_connection(self) -> None:
        self._start_templates_request(test_only=True)

    def _refresh_templates(self) -> None:
        self._start_templates_request(test_only=False)

    def _start_templates_request(self, *, test_only: bool) -> None:
        if self._templates_worker is not None and self._templates_worker.isRunning():
            return
        try:
            credentials = self._credentials_for_form()
        except ElabConfigurationError as exc:
            self._set_status(self.credentials_state, str(exc), "caution")
            return
        self._template_request_is_test = test_only
        self._set_status(
            self.credentials_state,
            "Testing eLab connection..." if test_only else "Loading experiment templates...",
            "neutral",
        )
        self.test_connection_button.setEnabled(False)
        self.refresh_templates_button.setEnabled(False)
        worker = ElabTemplatesWorker(credentials, self)
        self._templates_worker = worker
        worker.loaded.connect(self._templates_loaded)
        worker.failed.connect(self._templates_failed)
        worker.finished.connect(self._templates_thread_finished)
        worker.start()

    def _templates_loaded(self, templates: object) -> None:
        if not isinstance(templates, tuple) or not all(
            isinstance(template, ElabTemplate) for template in templates
        ):
            self._templates_failed("eLab returned an invalid template list.")
            return
        selected_id = self._profile.template_id
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self._template_names_by_id.clear()
        for template in templates:
            is_fav = self._profile.is_favorite(template.id)
            self.template_combo.addItem(
                f"{template.title}  ·  #{template.id}",
                icon=FIF.HEART if is_fav else None,
                userData=template.id,
            )
            self._template_names_by_id[str(template.id)] = template.title
        if selected_id is not None and self.template_combo.findData(selected_id) < 0:
            is_fav = self._profile.is_favorite(selected_id)
            self.template_combo.addItem(
                f"Configured template  ·  #{selected_id}",
                icon=FIF.HEART if is_fav else None,
                userData=selected_id,
            )
            self._template_names_by_id[str(selected_id)] = (
                self._profile.template_name or f"Template #{selected_id}"
            )
        if self.template_combo.count():
            self.template_combo.setCurrentIndex(
                max(0, self.template_combo.findData(selected_id)) if selected_id is not None else 0
            )
        else:
            self.template_combo.setCurrentIndex(-1)
        self.template_combo.blockSignals(False)
        self._template_changed(self.template_combo.currentIndex())
        if self._template_request_is_test:
            self._set_status(
                self.credentials_state,
                f"Connection successful. {len(templates)} experiment template(s) are accessible.",
                "success",
            )
        else:
            self._set_status(
                self.credentials_state,
                f"Loaded {len(templates)} accessible experiment template(s).",
                "success",
            )

    def _templates_failed(self, message: str) -> None:
        self._set_status(self.credentials_state, f"eLab connection failed: {message}", "danger")
        self.status.emit(f"ELAB connection failed: {message}")

    def _templates_thread_finished(self) -> None:
        worker = self._templates_worker
        self._templates_worker = None
        self.test_connection_button.setEnabled(True)
        self.refresh_templates_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def _select_template(self, template_id: int, title: str) -> None:
        index = self.template_combo.findData(template_id)
        if index < 0:
            display_title = title.strip() or f"Template #{template_id}"
            is_fav = self._profile.is_favorite(template_id)
            self.template_combo.addItem(
                f"{display_title}  ·  #{template_id}",
                icon=FIF.HEART if is_fav else None,
                userData=template_id,
            )
            self._template_names_by_id[str(template_id)] = display_title
            index = self.template_combo.count() - 1
        elif title.strip() and not self._template_names_by_id.get(str(template_id)):
            self._template_names_by_id[str(template_id)] = title.strip()
        self.template_combo.setCurrentIndex(index)

    def _recent_template_changed(self, index: int) -> None:
        if not 0 <= index < len(self._profile.recent_templates):
            return
        reference = self._profile.recent_templates[index]
        self._select_template(reference.id, reference.title)

    def _template_changed(self, _index: int) -> None:
        template_id = self.template_combo.currentData()
        name = self._template_names_by_id.get(str(template_id), "")
        if template_id in (None, ""):
            self.template_info.setText("No template selected.")
        else:
            self.template_info.setText(
                f"Selected eLab experiment template #{template_id}: {name or 'unnamed'}"
            )
            recent_index = self.recent_template_combo.findData(template_id)
            self.recent_template_combo.blockSignals(True)
            self.recent_template_combo.setCurrentIndex(recent_index)
            self.recent_template_combo.blockSignals(False)
            try:
                tid = int(template_id)
                if tid != self._profile.template_id or (name and name != self._profile.template_name):
                    self._profile = self._profile.with_overrides(
                        template_id=tid,
                        template_name=name,
                    ).remember_template(tid, name)
                    self._persist_profile_settings()
                    self.configuration_changed.emit()
            except (ValueError, TypeError):
                pass
        self._update_favorite_controls()
        self._update_upload_button()

    def _persist_profile_settings(self) -> None:
        def transform(raw: dict[str, Any], _settings: StationSettings) -> dict[str, Any]:
            application = raw.setdefault("application", {})
            if not isinstance(application, dict):
                raise ElabConfigurationError("The application settings section is not a mapping.")
            application["elab"] = self._profile.to_raw()
            return raw

        try:
            settings, _raw = self._repository.update_raw(transform)
            self._settings = settings
        except Exception:
            pass

    def _toggle_favorite_clicked(self) -> None:
        template_id = self.template_combo.currentData()
        if template_id in (None, ""):
            return
        tid = int(template_id)
        name = self._template_names_by_id.get(str(tid), f"Template #{tid}")
        self._profile = self._profile.toggle_favorite(tid, name)
        self._persist_profile_settings()
        self._update_favorite_controls()
        self._refresh_template_icons()
        self.configuration_changed.emit()

    def _open_favorites_dialog(self) -> None:
        dialog = ElabFavoritesDialog(
            self._profile.favorite_templates,
            selected_template_id=self._profile.template_id,
            parent=self.window(),
        )
        dialog.favorites_changed.connect(self._on_favorites_changed)
        if dialog.exec():
            chosen = dialog.selected_template()
            if chosen is not None:
                tid, title = chosen
                self._select_template(tid, title)
        updated_favs = dialog.updated_favorites()
        if updated_favs != self._profile.favorite_templates:
            self._on_favorites_changed(updated_favs)

    def _on_favorites_changed(self, favorites: tuple[Any, ...]) -> None:
        self._profile = replace(self._profile, favorite_templates=favorites)
        self._persist_profile_settings()
        self._update_favorite_controls()
        self._refresh_template_icons()
        self.configuration_changed.emit()

    def _update_favorite_controls(self) -> None:
        template_id = self.template_combo.currentData()
        if template_id in (None, ""):
            self.favorite_toggle_button.setEnabled(False)
            self.favorite_toggle_button.setToolTip("Select a template to mark as favorite")
            return
        self.favorite_toggle_button.setEnabled(True)
        is_fav = self._profile.is_favorite(template_id)
        if is_fav:
            self.favorite_toggle_button.setToolTip("Remove this template from favorites")
        else:
            self.favorite_toggle_button.setToolTip("Add this template to favorites")

    def _refresh_template_icons(self) -> None:
        for i in range(self.template_combo.count()):
            tid = self.template_combo.itemData(i)
            if tid is not None:
                is_fav = self._profile.is_favorite(tid)
                self.template_combo.setItemIcon(i, FIF.HEART if is_fav else QIcon())

    def _profile_from_widgets(self) -> ElabIntegrationProfile:
        template_id = self.template_combo.currentData()
        if template_id in (None, ""):
            selected_template_id = None
        else:
            try:
                selected_template_id = int(template_id)
            except (TypeError, ValueError) as exc:
                raise ElabConfigurationError("The selected eLab template ID is invalid.") from exc
        template_name = self._template_names_by_id.get(str(template_id), "").strip()
        tags = tuple(item.strip() for item in self.tags_edit.text().split(",") if item.strip())
        profile = ElabIntegrationProfile(
            enabled=self.auto_upload_check.isChecked(),
            template_id=selected_template_id,
            template_name=template_name,
            title_pattern=self.title_pattern_edit.text().strip(),
            tags=tuple(dict.fromkeys(tags)),
            upload_hdf5=self.upload_hdf5_check.isChecked(),
            upload_csv=self.upload_csv_check.isChecked(),
            recent_templates=self._profile.recent_templates,
            favorite_templates=self._profile.favorite_templates,
        )
        profile.validate()
        if profile.enabled and profile.template_id is None:
            raise ElabConfigurationError("Automatic upload requires an eLab experiment template.")
        if profile.enabled:
            self._credentials_for_form()
        return profile

    def _save_policy(self) -> None:
        try:
            profile = self._profile_from_widgets()
            if profile.template_id is not None:
                profile = profile.remember_template(profile.template_id, profile.template_name)
                profile.validate()
        except ElabConfigurationError as exc:
            self._set_status(self.policy_status, str(exc), "danger")
            return

        def transform(raw: dict[str, Any], _settings: StationSettings) -> dict[str, Any]:
            application = raw.setdefault("application", {})
            if not isinstance(application, dict):
                raise ElabConfigurationError("The application settings section is not a mapping.")
            application["elab"] = profile.to_raw()
            return raw

        try:
            settings, _raw = self._repository.update_raw(transform)
        except Exception as exc:
            self._set_status(self.policy_status, f"Policy was not saved: {exc}", "danger")
            return
        self._settings = settings
        self._profile = profile
        self.settings_saved.emit(settings)
        self._set_status(self.policy_status, "Automation policy saved.", "success")
        self.status.emit("ELAB automation policy saved")
        self._update_upload_button()
        self.configuration_changed.emit()

    def set_settings(self, settings: StationSettings) -> None:
        """Refresh the non-secret profile after another page saves settings."""

        self._settings = settings
        self._profile = ElabIntegrationProfile.from_application(settings.application)
        self._populate_profile_controls()
        self.configuration_changed.emit()

    def upload_configuration(self) -> tuple[bool, bool, str]:
        """Return availability, default choice and a user-facing explanation."""

        if self._simulation:
            return (
                False,
                False,
                "Simulation mode disables external eLab writes.",
            )
        if not self._credentials.configured:
            return (
                False,
                False,
                f"Add ELAB_API and ELAB_HOST to {self._env_path} first.",
            )
        if self._profile.template_id is None:
            return (
                False,
                False,
                "Select and save an experiment template in the eLabFTW tab first.",
            )
        return (
            True,
            self._profile.enabled,
            f"Uses {self._profile.template_name or f'Template #{self._profile.template_id}'} from the eLabFTW policy.",
        )

    def current_profile(self) -> ElabIntegrationProfile:
        return self._profile

    def current_credentials(self) -> ElabCredentials:
        return self._credentials

    def available_templates(self) -> list[tuple[int, str]]:
        """Return all known template choices from live cache, history, and profile."""

        templates: list[tuple[int, str]] = []
        seen_ids: set[int] = set()

        for raw_id, name in self._template_names_by_id.items():
            try:
                tid = int(raw_id)
            except (ValueError, TypeError):
                continue
            if tid > 0 and tid not in seen_ids:
                seen_ids.add(tid)
                templates.append((tid, name or f"Template #{tid}"))

        for reference in self._profile.favorite_templates:
            if reference.id > 0 and reference.id not in seen_ids:
                seen_ids.add(reference.id)
                templates.append((reference.id, reference.title))

        for reference in self._profile.recent_templates:
            if reference.id > 0 and reference.id not in seen_ids:
                seen_ids.add(reference.id)
                templates.append((reference.id, reference.title))

        if self._profile.template_id is not None and self._profile.template_id not in seen_ids:
            seen_ids.add(self._profile.template_id)
            templates.append(
                (
                    self._profile.template_id,
                    self._profile.template_name or f"Template #{self._profile.template_id}",
                )
            )

        return templates

    def refresh_templates(self) -> None:
        """Trigger an asynchronous template refresh."""
        self._refresh_templates()

    def manual_upload_configuration(self) -> tuple[bool, bool, str]:
        """Configuration provider used by individual device save dialogs."""

        return self.upload_configuration()

    def queue_manual_upload(self, path: str | Path) -> None:
        """Upload a closed, timestamped manual result selected at save time."""

        available, _default_enabled, hint = self.upload_configuration()
        if not available:
            self.status.emit(f"ELAB manual upload skipped: {hint}")
            return
        target = Path(path).expanduser()
        try:
            credentials = ElabCredentials.from_values(
                self._credentials.host,
                self._credentials.api_key,
            )
            profile = self._profile
            profile.validate()
        except ElabConfigurationError as exc:
            self.status.emit(f"ELAB manual upload skipped: {exc}")
            return
        if self._upload_worker is not None and self._upload_worker.isRunning():
            self.status.emit("ELAB manual upload skipped: another upload is already running")
            return
        self._start_upload(target, credentials, profile, automatic=False)

    def set_selected_result(self, path: object) -> None:
        if path is None:
            self._selected_result = None
            self.selected_result_edit.clear()
        else:
            candidate = Path(str(path)).expanduser()
            self._selected_result = (
                candidate
                if candidate.is_file() and candidate.suffix.lower() in {".h5", ".hdf5"}
                else None
            )
            self.selected_result_edit.setText(str(candidate))
            if self._selected_result is None:
                self._set_status(
                    self.upload_status,
                    "Select a native HDF5 result (.h5 or .hdf5) for eLab upload.",
                    "caution",
                )
        self._update_upload_button()

    def _browse_result(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose HDF5 result for eLab",
            str(self._repository.path.parent),
            "HDF5 results (*.h5 *.hdf5);;All files (*)",
        )
        if selected:
            self.set_selected_result(Path(selected))

    def _update_upload_button(self) -> None:
        ready = (
            self._selected_result is not None
            and self._selected_result.is_file()
            and self.template_combo.currentData() not in (None, "")
            and bool(self.host_edit.text().strip())
            and bool(self.api_key_edit.text().strip() or self._credentials.api_key.strip())
            and not self._simulation
            and not (self._upload_worker is not None and self._upload_worker.isRunning())
        )
        self.upload_button.setEnabled(ready)

    def _upload_selected(self) -> None:
        if self._selected_result is None:
            self._set_status(self.upload_status, "Choose an HDF5 result first.", "caution")
            return
        if self._simulation:
            self._set_status(
                self.upload_status,
                "External eLab writes are disabled in simulation mode.",
                "caution",
            )
            return
        try:
            credentials = self._credentials_for_form()
            profile = self._profile_from_widgets()
        except ElabConfigurationError as exc:
            self._set_status(self.upload_status, str(exc), "danger")
            return
        self._start_upload(self._selected_result, credentials, profile, automatic=False)

    def queue_automatic_upload(
        self,
        path: str | Path,
        *,
        run_state: str,
        error: object | None = None,
        requested: bool | None = None,
        profile_override: ElabIntegrationProfile | None = None,
    ) -> None:
        """Queue a successful terminal run without blocking the instrument shell."""

        # ``requested=True`` is an explicit per-run opt-in from the sweep
        # page or recipe tree.  It must be able to override the central default
        # policy; otherwise the checkbox or recipe block would appear actionable
        # but silently do nothing whenever automatic upload is disabled in the eLab tab.
        effective_profile = profile_override or self._profile
        if self._simulation:
            return
        if requested is False:
            return
        if requested is None and not effective_profile.enabled:
            return
        if str(run_state).casefold() != "safe" or error:
            return
        target = Path(path).expanduser()
        try:
            credentials = ElabCredentials.from_values(
                self._credentials.host,
                self._credentials.api_key,
            )
            profile = effective_profile
            profile.validate()
            if profile.template_id is None:
                raise ElabConfigurationError(
                    "Automatic upload requires an eLab experiment template."
                )
        except ElabConfigurationError as exc:
            self.status.emit(f"ELAB automatic upload skipped: {exc}")
            return
        if self._upload_worker is not None and self._upload_worker.isRunning():
            self.status.emit("ELAB automatic upload skipped: another upload is already running")
            return
        self._start_upload(target, credentials, profile, automatic=True)

    def _start_upload(
        self,
        path: Path,
        credentials: ElabCredentials,
        profile: ElabIntegrationProfile,
        *,
        automatic: bool,
    ) -> None:
        if self._upload_worker is not None and self._upload_worker.isRunning():
            self._set_status(self.upload_status, "An eLab upload is already running.", "neutral")
            return
        self._upload_is_automatic = automatic
        request = ElabUploadRequest(
            path=Path(path),
            credentials=credentials,
            profile=profile,
            ledger_path=self._ledger.path,
        )
        self._set_status(
            self.upload_status,
            "Automatic upload started..." if automatic else "Upload started...",
            "neutral",
        )
        self.upload_button.setEnabled(False)
        self.browse_result_button.setEnabled(False)
        worker = ElabUploadWorker(request, self)
        self._upload_worker = worker
        worker.progress.connect(
            lambda message: self._set_status(self.upload_status, message, "neutral")
        )
        worker.completed.connect(self._upload_completed)
        worker.failed.connect(self._upload_failed)
        worker.finished.connect(self._upload_thread_finished)
        worker.start()

    def _upload_completed(self, result: object) -> None:
        if not isinstance(result, ElabUploadResult):
            self._set_status(
                self.upload_status, "eLab returned an invalid upload result.", "danger"
            )
            return
        record = result.record
        self._last_experiment_url = record.experiment_url
        self.open_experiment_button.setEnabled(bool(record.experiment_url))
        if result.skipped_existing:
            message = "This result was already uploaded; no duplicate experiment was created."
        else:
            message = f"Uploaded to eLab experiment #{record.experiment_id}."
        if record.warnings:
            message += " " + " ".join(record.warnings)
        self._set_status(self.upload_status, message, "success")
        self._refresh_history()
        self.status.emit(f"ELAB upload completed: {record.run_path} -> #{record.experiment_id}")

    def _upload_failed(self, message: str) -> None:
        self._set_status(self.upload_status, f"eLab upload failed: {message}", "danger")
        self._refresh_history()
        self.status.emit(f"ELAB upload failed: {message}")

    def _upload_thread_finished(self) -> None:
        worker = self._upload_worker
        self._upload_worker = None
        self.browse_result_button.setEnabled(True)
        self._upload_is_automatic = False
        if worker is not None:
            worker.deleteLater()
        self._update_upload_button()

    def _refresh_history(self) -> None:
        try:
            records = self._ledger.records()
        except Exception as exc:
            self.history_table.setRowCount(0)
            self._set_status(self.upload_status, f"Upload history is unavailable: {exc}", "danger")
            return
        visible = records[:20]
        self.history_table.setRowCount(len(visible))
        for row, record in enumerate(visible):
            values = (
                Path(record.run_path).name,
                record.status,
                f"#{record.experiment_id}" if record.experiment_id is not None else "—",
                record.template_name or f"#{record.template_id}",
                ", ".join(record.uploaded_files) or "—",
                record.created_at_utc,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(record.error or "")
                self.history_table.setItem(row, column, item)

    def _open_last_experiment(self) -> None:
        if self._last_experiment_url:
            QDesktopServices.openUrl(QUrl(self._last_experiment_url))

    def shutdown(self, *, timeout_ms: int = 5_000) -> bool:
        """Wait for network workers before the shell tears down Qt objects."""

        workers = (self._templates_worker, self._upload_worker)
        for worker in workers:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(timeout_ms):
                    return False
        return True

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    @classmethod
    def _set_status(cls, widget: QLabel, text: str, state: str) -> None:
        widget.setText(text)
        widget.setProperty("elabStatusState", state)
        cls._repolish(widget)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 900
        if compact == self._compact_layout:
            return
        self._compact_layout = compact
        self.upload_button.setText("Upload" if compact else "Upload selected result")
