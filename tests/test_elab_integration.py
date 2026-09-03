from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from app.devices.anritsu_ms2830a.ui.manual_save import ManualSpectrumSaveDialog
from app.storage import ManualSpectrumSaveMode
from app.integrations.elab.client import ElabApiClient, ElabTemplate
from app.integrations.elab.config import (
    ElabCredentials,
    ElabIntegrationProfile,
    ElabTemplateReference,
    load_credentials,
    resolve_env_path,
    save_credentials,
)
from app.integrations.elab.ledger import ElabUploadLedger
from app.integrations.elab.service import ElabUploadRequest, upload_result
from app.storage.hdf5_reader import RunSummary
from app.ui.elab import ElabFavoritesDialog, ElabPage, SearchableComboBox
from app.settings import SettingsRepository


class _FakeResponse:
    def __init__(
        self, payload: object, *, status: int = 200, headers: dict[str, str] | None = None
    ):
        self._payload = payload
        self._status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")

    def getcode(self) -> int:
        return self._status


class ElabConfigurationTests(unittest.TestCase):
    def test_credentials_read_utf8_bom_and_explicit_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_path = root / ".env"
            env_path.write_bytes(
                "ELAB_HOST=https://elab.example.org\nELAB_API=test-key\n".encode("utf-8-sig")
            )
            loaded = load_credentials(env_path, environ={})

            self.assertEqual(loaded.host, "https://elab.example.org")
            self.assertEqual(loaded.api_key, "test-key")
            self.assertEqual(resolve_env_path(env_path), env_path)

    def test_relative_env_path_falls_back_to_repository_file(self) -> None:
        repository_env_path = (Path(__file__).resolve().parents[1] / ".env").resolve()
        with TemporaryDirectory() as temporary:
            with patch("app.integrations.elab.config.Path.cwd", return_value=Path(temporary)):
                resolved = resolve_env_path(".env")
        self.assertEqual(resolved, repository_env_path)

    def test_credentials_round_trip_preserves_other_dotenv_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("OTHER=value\nELAB_HOST=https://old.example\n", encoding="utf-8")
            save_credentials(path, host="https://elab.rptu.de", api_key="test-key")

            loaded = load_credentials(path, environ={})
            self.assertEqual(loaded.host, "https://elab.rptu.de")
            self.assertEqual(loaded.api_key, "test-key")
            text = path.read_text(encoding="utf-8")
            self.assertIn("OTHER=value", text)
            self.assertIn('ELAB_HOST="https://elab.rptu.de"', text)
            self.assertIn('ELAB_API="test-key"', text)

    def test_title_pattern_rejects_unknown_placeholders(self) -> None:
        with self.assertRaisesRegex(ValueError, r"only \{run_name\}"):
            ElabIntegrationProfile(title_pattern="{unknown}").validate()

    def test_recent_template_shortcuts_round_trip_without_credentials(self) -> None:
        profile = ElabIntegrationProfile().remember_template(42, "MTJ setup")
        profile = profile.remember_template(99, "Cryostat sweep")

        restored = ElabIntegrationProfile.from_application({"elab": profile.to_raw()})

        self.assertEqual(
            [(item.id, item.title) for item in restored.recent_templates],
            [(99, "Cryostat sweep"), (42, "MTJ setup")],
        )

    def test_favorite_templates_round_trip_and_helpers(self) -> None:
        profile = ElabIntegrationProfile()
        self.assertFalse(profile.is_favorite(42))
        profile = profile.with_favorite_template(42, "MTJ setup")
        profile = profile.with_favorite_template(99, "Cryostat sweep")
        self.assertTrue(profile.is_favorite(42))
        self.assertTrue(profile.is_favorite(99))
        self.assertFalse(profile.is_favorite(7))

        restored = ElabIntegrationProfile.from_application({"elab": profile.to_raw()})
        self.assertEqual(
            [(item.id, item.title) for item in restored.favorite_templates],
            [(42, "MTJ setup"), (99, "Cryostat sweep")],
        )

        toggled = restored.toggle_favorite(42, "MTJ setup")
        self.assertFalse(toggled.is_favorite(42))
        self.assertTrue(toggled.is_favorite(99))
        retoggled = toggled.toggle_favorite(42, "MTJ setup")
        self.assertTrue(retoggled.is_favorite(42))


class ElabClientTests(unittest.TestCase):
    def test_client_uses_v2_endpoints_and_authorization(self) -> None:
        requests: list[object] = []

        def opener(request, *, timeout):
            requests.append(request)
            if request.full_url.endswith(
                "/experiments_templates?limit=100&offset=0&order=title&sort=asc&state=1"
            ):
                return _FakeResponse([{"id": 42, "title": "MTJ setup"}])
            if request.full_url.endswith("/experiments"):
                return _FakeResponse(
                    None,
                    status=201,
                    headers={"Location": "https://elab.rptu.de/api/v2/experiments/99"},
                )
            if request.full_url.endswith("/experiments/99/tags"):
                return _FakeResponse(None, status=201)
            if request.full_url.endswith("/experiments/99/uploads"):
                return _FakeResponse(
                    None,
                    status=201,
                    headers={"Location": "https://elab.rptu.de/api/v2/experiments/99/uploads/1"},
                )
            raise AssertionError(request.full_url)

        client = ElabApiClient(
            ElabCredentials.from_values("https://elab.rptu.de", "secret"),
            opener=opener,
        )
        self.assertEqual(client.list_experiment_templates()[0].title, "MTJ setup")
        experiment_id, experiment_url = client.create_experiment(
            template_id=42,
            title="Run",
            body="<p>Result</p>",
        )
        self.assertEqual((experiment_id, experiment_url.rsplit("/", 1)[-1]), (99, "99"))
        client.add_tag(experiment_id=99, tag="pylab")
        with TemporaryDirectory() as temporary:
            result = Path(temporary) / "run.h5"
            result.write_bytes(b"hdf5")
            client.upload_file(experiment_id=99, path=result, comment="attachment")
        self.assertEqual(
            requests[0].get_header("Authorization"),
            "secret",
        )
        self.assertEqual(json.loads(requests[1].data.decode("utf-8"))["template"], 42)
        self.assertIn(b'filename="run.h5"', requests[3].data)


class ElabUploadServiceTests(unittest.TestCase):
    def test_upload_is_idempotent_and_records_terminal_state(self) -> None:
        calls: list[tuple[str, object]] = []

        class FakeClient:
            def __init__(self, _credentials, *, timeout_s):
                calls.append(("client", timeout_s))

            def create_experiment(self, *, template_id, title, body):
                calls.append(("create", (template_id, title, body)))
                return 99, "https://elab.rptu.de/experiments/99"

            def add_tag(self, *, experiment_id, tag):
                calls.append(("tag", (experiment_id, tag)))

            def upload_file(self, *, experiment_id, path, comment):
                calls.append(("upload", (experiment_id, Path(path).name, comment)))
                return ""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "20260903T120000Z_mtj.h5"
            result_path.write_bytes(b"immutable result")
            result_path.with_suffix(".csv").write_text("index,value\n0,1\n", encoding="utf-8")
            request = ElabUploadRequest(
                path=result_path,
                credentials=ElabCredentials.from_values("https://elab.rptu.de", "secret"),
                profile=ElabIntegrationProfile(
                    template_id=42,
                    template_name="MTJ setup",
                    tags=("pylab",),
                ),
                ledger_path=root / "elab_uploads.json",
            )
            summary = RunSummary(
                path=result_path,
                created_at_utc="2026-09-03T12:00:00+00:00",
                status="completed",
                point_count=4,
                spectrum_count=4,
                plan_sha256="plan-hash",
                application_version="test",
            )
            with (
                patch("app.integrations.elab.service.ElabApiClient", FakeClient),
                patch("app.integrations.elab.service.Hdf5RunReader.summary", return_value=summary),
            ):
                first = upload_result(request)
                second = upload_result(request)

            self.assertEqual(first.record.status, "uploaded")
            self.assertFalse(first.skipped_existing)
            self.assertTrue(second.skipped_existing)
            self.assertEqual([name for name, _value in calls].count("create"), 1)
            self.assertEqual([name for name, _value in calls].count("upload"), 2)
            records = ElabUploadLedger(request.ledger_path).records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].experiment_id, 99)
            self.assertEqual(
                set(records[0].uploaded_files),
                {result_path.name, result_path.with_suffix(".csv").name},
            )


class ElabPageRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_page_has_visible_fluent_workspace_and_safe_simulation_state(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = SettingsRepository(Path(temporary) / "settings.yml")
            repository.ensure_exists()
            env_path = Path(temporary) / ".env"
            env_path.write_text(
                "ELAB_HOST=https://elab.example.org\nELAB_API=first-key\n",
                encoding="utf-8",
            )
            page = ElabPage(
                repository,
                env_path=env_path,
                simulation=True,
            )
            try:
                page.resize(1280, 900)
                page.show()
                self.application.processEvents()
                self.assertTrue(page.isVisible())
                self.assertGreater(page.connection_card.width(), 0)
                self.assertGreater(page.history_table.height(), 0)
                self.assertTrue(page.simulation_notice.isVisible())
                self.assertFalse(page.upload_button.isEnabled())
                self.assertEqual(page.api_key_edit.echoMode(), QLineEdit.EchoMode.Password)
                self.assertTrue(page.reload_credentials_button.isEnabled())
                self.assertTrue(page._credentials.configured)
                page._templates_loaded(
                    (ElabTemplate(42, "MTJ setup"), ElabTemplate(99, "Cryostat sweep"))
                )
                page.template_combo.setCurrentIndex(1)
                self.assertEqual(page.template_combo.currentData(), 99)
                page._save_policy()
                stored_elab = repository.load().settings.application["elab"]
                self.assertEqual(stored_elab["template_id"], 99)
                self.assertEqual(
                    stored_elab["recent_templates"][0],
                    {"id": 99, "title": "Cryostat sweep"},
                )
                page._profile = page._profile.remember_template(99, "Cryostat sweep")
                page._populate_profile_controls()
                page.recent_template_combo.setCurrentIndex(0)
                self.assertEqual(page.template_combo.currentData(), 99)
                env_path.write_text(
                    "ELAB_HOST=https://new.example.org\nELAB_API=second-key\n",
                    encoding="utf-8",
                )
                page._reload_credentials()
                self.assertEqual(page._credentials.host, "https://new.example.org")
                self.assertEqual(page._credentials.api_key, "second-key")
                page.resize(820, 620)
                self.application.processEvents()
                self.assertGreater(page.connection_card.width(), 0)
                self.assertGreater(page.history_table.height(), 0)
            finally:
                page.close()

    def test_explicit_run_upload_overrides_disabled_automatic_default(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = SettingsRepository(Path(temporary) / "settings.yml")
            repository.ensure_exists()
            env_path = Path(temporary) / ".env"
            env_path.write_text(
                "ELAB_HOST=https://elab.example.org\nELAB_API=first-key\n",
                encoding="utf-8",
            )
            page = ElabPage(repository, env_path=env_path, simulation=False)
            try:
                page._profile = ElabIntegrationProfile(
                    enabled=False,
                    template_id=42,
                    template_name="MTJ setup",
                )
                with patch.object(page, "_start_upload") as start_upload:
                    page.queue_automatic_upload(
                        Path(temporary) / "run.h5",
                        run_state="safe",
                        requested=True,
                    )
                    start_upload.assert_called_once()
                    start_upload.reset_mock()
                    page.queue_automatic_upload(
                        Path(temporary) / "run.h5",
                        run_state="safe",
                        requested=None,
                    )
                    start_upload.assert_not_called()
            finally:
                page.close()

    def test_manual_save_dialog_exposes_closed_file_upload_choice(self) -> None:
        dialog = ManualSpectrumSaveDialog(
            None,
            trace_choices=(("raw", "Raw"),),
            metadata_values=(),
            default_destination="manual.h5",
            default_mode=ManualSpectrumSaveMode.APPEND,
            default_upload_to_elab=True,
            elab_upload_available=True,
            elab_upload_hint="Uses the configured research template.",
        )
        try:
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.upload_to_elab.isChecked())
            self.assertEqual(
                dialog.mode.currentData(), ManualSpectrumSaveMode.TIMESTAMPED.value
            )
            self.assertTrue(dialog.options().upload_to_elab)
        finally:
            dialog.close()

    def test_page_template_selection_persists_default_and_toggles_favorites(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = SettingsRepository(Path(temporary) / "settings.yml")
            repository.ensure_exists()
            env_path = Path(temporary) / ".env"
            env_path.write_text(
                "ELAB_HOST=https://elab.example.org\nELAB_API=first-key\n",
                encoding="utf-8",
            )
            page = ElabPage(repository, env_path=env_path, simulation=True)
            try:
                page.show()
                self.application.processEvents()
                self.assertFalse(page.favorite_toggle_button.isEnabled())

                # Load templates
                page._templates_loaded(
                    (
                        ElabTemplate(42, "MTJ setup"),
                        ElabTemplate(99, "Cryostat sweep"),
                        ElabTemplate(105, "Anritsu RF test"),
                    )
                )
                self.assertEqual(page.template_combo.count(), 3)

                # Select template 99
                page.template_combo.setCurrentIndex(1)
                self.assertEqual(page.template_combo.currentData(), 99)
                self.assertTrue(page.favorite_toggle_button.isEnabled())

                # Verify that template 99 was automatically persisted as default/last option
                stored_elab = repository.load().settings.application["elab"]
                self.assertEqual(stored_elab["template_id"], 99)
                self.assertEqual(stored_elab["template_name"], "Cryostat sweep")

                # Toggle favorite on template 99
                self.assertFalse(page._profile.is_favorite(99))
                page.favorite_toggle_button.click()
                self.assertTrue(page._profile.is_favorite(99))
                stored_elab = repository.load().settings.application["elab"]
                self.assertEqual(len(stored_elab["favorite_templates"]), 1)
                self.assertEqual(stored_elab["favorite_templates"][0]["id"], 99)

                # Check available_templates includes favorites
                templates = page.available_templates()
                self.assertIn((99, "Cryostat sweep"), templates)

                # Re-toggle favorite to remove
                page.favorite_toggle_button.click()
                self.assertFalse(page._profile.is_favorite(99))
                stored_elab = repository.load().settings.application["elab"]
                self.assertEqual(len(stored_elab["favorite_templates"]), 0)
            finally:
                page.close()


class SearchableComboBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_searchable_combo_menu_filters_items_live(self) -> None:
        cb = SearchableComboBox()
        cb.addItem("42: MTJ Setup", userData=42)
        cb.addItem("99: Cryostat Sweep", userData=99)
        cb.addItem("105: Room Temp RF", userData=105)
        try:
            cb.show()
            cb._showComboMenu()
            menu = cb.dropMenu
            self.assertIsNotNone(menu)
            self.assertEqual(menu.view.count(), 3)

            # Type 'cryo' into search
            menu.search_edit.setText("cryo")
            self.assertTrue(menu.view.item(0).isHidden())
            self.assertFalse(menu.view.item(1).isHidden())
            self.assertTrue(menu.view.item(2).isHidden())

            # Press return to pick the filtered item
            menu._on_return_pressed()
            self.assertEqual(cb.currentData(), 99)
            self.assertEqual(cb.currentText(), "99: Cryostat Sweep")

            # Re-open and clear filter
            cb._showComboMenu()
            menu = cb.dropMenu
            menu.search_edit.setText("")
            self.assertFalse(menu.view.item(0).isHidden())
            self.assertFalse(menu.view.item(1).isHidden())
            self.assertFalse(menu.view.item(2).isHidden())
            cb._closeComboMenu()
        finally:
            cb.close()


class ElabFavoritesDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_favorites_dialog_preselects_current_and_allows_selection(self) -> None:
        favorites = (
            ElabTemplateReference(42, "MTJ setup"),
            ElabTemplateReference(99, "Cryostat sweep"),
        )
        dialog = ElabFavoritesDialog(favorites, selected_template_id=99)
        try:
            dialog.show()
            self.application.processEvents()
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.table.currentRow(), 1)
            self.assertEqual(dialog._current_selection(), (99, "Cryostat sweep"))

            dialog.select_button.click()
            self.assertEqual(dialog.selected_template(), (99, "Cryostat sweep"))
        finally:
            dialog.close()

    def test_favorites_dialog_filters_and_removes(self) -> None:
        favorites = (
            ElabTemplateReference(42, "MTJ setup"),
            ElabTemplateReference(99, "Cryostat sweep"),
            ElabTemplateReference(105, "Anritsu RF"),
        )
        dialog = ElabFavoritesDialog(favorites, selected_template_id=42)
        changed_emits: list[tuple[ElabTemplateReference, ...]] = []
        dialog.favorites_changed.connect(changed_emits.append)
        try:
            dialog.show()
            self.application.processEvents()

            # Filter for 'anritsu'
            dialog.search_edit.setText("anritsu")
            self.assertTrue(dialog.table.isRowHidden(0))
            self.assertTrue(dialog.table.isRowHidden(1))
            self.assertFalse(dialog.table.isRowHidden(2))

            # Remove item 105
            dialog.remove_button.click()
            self.assertEqual(len(changed_emits), 1)
            self.assertEqual([ref.id for ref in changed_emits[0]], [42, 99])
        finally:
            dialog.close()

    def test_favorites_dialog_empty_state(self) -> None:
        dialog = ElabFavoritesDialog(())
        try:
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.empty_label.isVisible())
            self.assertFalse(dialog.table.isVisible())
            self.assertFalse(dialog.select_button.isEnabled())
            self.assertFalse(dialog.remove_button.isEnabled())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()

