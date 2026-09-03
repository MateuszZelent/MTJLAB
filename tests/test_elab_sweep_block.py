from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.devices.registry import built_in_device_registry
from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.integrations.elab.config import ElabCredentials, ElabIntegrationProfile
from app.recipes.models import RecipeNode, parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from tests.helpers import simulation_settings
from app.ui.measurement_tree.model import MeasurementTreeModel
from app.ui.recipes.elab_dialog import ElabUploadEditorDialog
from app.ui.recipes.page import RecipePage


class TestElabSweepBlockModelsAndCompiler(unittest.TestCase):
    def test_parse_valid_upload_to_elab(self) -> None:
        yaml = """schema_version: 1
name: "eLab Upload Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_to_elab"
      template_id: 42
      template_name: "Cryostat IV Sweep"
      title_pattern: "MTJ Sample {run_name}"
      tags: ["cryo", "sweep"]
      attach_hdf5: true
      attach_csv: true
"""
        recipe = parse_recipe_text(yaml)
        node = recipe.root.children[0]
        self.assertEqual(node.type, "upload_to_elab")
        self.assertEqual(node.data["template_id"], 42)
        self.assertEqual(node.data["template_name"], "Cryostat IV Sweep")
        self.assertEqual(node.data["title_pattern"], "MTJ Sample {run_name}")
        self.assertEqual(node.data["tags"], ["cryo", "sweep"])
        self.assertTrue(node.data["attach_hdf5"])
        self.assertTrue(node.data["attach_csv"])

    def test_parse_alias_upload_elab(self) -> None:
        yaml = """schema_version: 1
name: "eLab Alias Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_elab"
      template_id: 15
"""
        recipe = parse_recipe_text(yaml)
        self.assertEqual(recipe.root.children[0].type, "upload_elab")
        self.assertEqual(recipe.root.children[0].data["template_id"], 15)

    def test_reject_invalid_template_id(self) -> None:
        yaml = """schema_version: 1
name: "Invalid Template Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_to_elab"
      template_id: -5
"""
        with self.assertRaises(ConfigurationError) as ctx:
            parse_recipe_text(yaml)
        self.assertIn("positive integer", str(ctx.exception))

    def test_reject_invalid_title_pattern(self) -> None:
        yaml = """schema_version: 1
name: "Invalid Title Pattern Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_to_elab"
      title_pattern: "Result for {unknown_token}"
"""
        with self.assertRaises(ConfigurationError) as ctx:
            parse_recipe_text(yaml)
        self.assertIn("title_pattern may use only", str(ctx.exception))

    def test_reject_no_attachments(self) -> None:
        yaml = """schema_version: 1
name: "No Attachments Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_to_elab"
      attach_hdf5: false
      attach_csv: false
"""
        with self.assertRaises(ConfigurationError) as ctx:
            parse_recipe_text(yaml)
        self.assertIn("at least one of attach_hdf5 or attach_csv", str(ctx.exception))

    def test_semantic_tree_and_model_presentation(self) -> None:
        _app = QApplication.instance() or QApplication([])
        yaml = """schema_version: 1
name: "Semantic eLab Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-elab"
      type: "upload_to_elab"
      template_id: 7
      template_name: "Magnetoresistance Profile"
"""
        recipe = parse_recipe_text(yaml)
        tree = normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
        node = tree.by_id["step-elab"]
        self.assertEqual(node.label, "Upload to eLab · Magnetoresistance Profile")

        model = MeasurementTreeModel(tree)

        root_idx = model.index(0, 0)
        elab_idx_col0 = model.index(0, 0, root_idx)
        elab_idx_col1 = model.index(0, 1, root_idx)

        # Label in Col 0
        self.assertEqual(
            model.data(elab_idx_col0, Qt.ItemDataRole.DisplayRole),
            "Upload to eLab · Magnetoresistance Profile",
        )
        # Formatted template value in Col 1
        self.assertEqual(
            model.data(elab_idx_col1, Qt.ItemDataRole.DisplayRole),
            "Magnetoresistance Profile (#7)",
        )
        # Check icon name
        self.assertEqual(model._icon_name(node), "elab")

    def test_compiler_compiles_upload_to_elab(self) -> None:
        settings = simulation_settings()
        compiler = RecipeCompiler(settings)
        yaml = """schema_version: 1
name: "Compiler Test"
root:
  id: "root-seq"
  type: "sequence"
  children:
    - id: "step-wait"
      type: "wait"
      duration: "10 ms"
    - id: "step-elab"
      type: "upload_to_elab"
      template_id: 99
      template_name: "Cryo Run"
      title_pattern: "Test {run_name}"
      tags: ["mtj", "test"]
      attach_hdf5: true
      attach_csv: false
"""
        recipe = parse_recipe_text(yaml)
        plan = compiler.compile(recipe)
        self.assertIsNotNone(plan.elab_upload_config)
        self.assertEqual(plan.elab_upload_config["template_id"], 99)
        self.assertEqual(plan.elab_upload_config["template_name"], "Cryo Run")
        self.assertEqual(plan.elab_upload_config["title_pattern"], "Test {run_name}")
        self.assertEqual(plan.elab_upload_config["tags"], ["mtj", "test"])
        self.assertTrue(plan.elab_upload_config["attach_hdf5"])
        self.assertFalse(plan.elab_upload_config["attach_csv"])

        elab_action = next(a for a in plan.actions if a.kind == "upload_to_elab")
        self.assertEqual(elab_action.node_id, "step-elab")


class TestElabIntegrationProfileOverrides(unittest.TestCase):
    def test_with_overrides(self) -> None:
        profile = ElabIntegrationProfile(
            enabled=False,
            template_id=1,
            template_name="Default Template",
            title_pattern="Default {run_name}",
            tags=("default",),
            upload_hdf5=True,
            upload_csv=True,
        )
        overridden = profile.with_overrides(
            enabled=True,
            template_id=42,
            template_name="Overridden Template",
            title_pattern="Custom {run_name}",
            tags=["override1", "override2"],
            upload_hdf5=True,
            upload_csv=False,
        )
        self.assertTrue(overridden.enabled)
        self.assertEqual(overridden.template_id, 42)
        self.assertEqual(overridden.template_name, "Overridden Template")
        self.assertEqual(overridden.title_pattern, "Custom {run_name}")
        self.assertEqual(overridden.tags, ("override1", "override2"))
        self.assertTrue(overridden.upload_hdf5)
        self.assertFalse(overridden.upload_csv)


class TestElabUploadEditorDialogUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_population_and_interaction(self) -> None:
        profile = ElabIntegrationProfile(
            enabled=True,
            template_id=10,
            template_name="Default Lab Template",
            title_pattern="Lab {run_name}",
            tags=("standard",),
        )
        credentials = ElabCredentials(host="https://elab.lab.org", api_key="secret-key")
        templates = [(10, "Default Lab Template"), (25, "Specific Sweep Template")]

        node = RecipeNode(
            id="test-node",
            type="upload_to_elab",
            data={
                "template_id": 25,
                "template_name": "Specific Sweep Template",
                "title_pattern": "Run {run_name}",
                "tags": ["alpha", "beta"],
                "attach_hdf5": True,
                "attach_csv": False,
            },
        )

        refreshed = False

        def on_refresh():
            nonlocal refreshed
            refreshed = True

        navigated = False

        def on_navigate():
            nonlocal navigated
            navigated = True

        dialog = ElabUploadEditorDialog(
            node,
            profile_provider=lambda: profile,
            credentials_provider=lambda: credentials,
            available_templates_provider=lambda: templates,
            refresh_templates_callback=on_refresh,
            navigate_to_elab=on_navigate,
        )
        dialog.show()
        self.app.processEvents()

        # Visual quality bar: Verify rendered geometry after show() and event processing
        self.assertGreaterEqual(dialog.width(), 500)
        self.assertGreaterEqual(dialog.height(), 400)
        self.assertTrue(dialog.isVisible())

        # Check pre-populated fields
        self.assertEqual(dialog.template_combo.currentData(), 25)
        self.assertEqual(dialog.title_pattern_edit.text(), "Run {run_name}")
        self.assertEqual(dialog.tags_edit.text(), "alpha, beta")
        self.assertTrue(dialog.attach_hdf5_check.isChecked())
        self.assertFalse(dialog.attach_csv_check.isChecked())

        # Test refresh button
        dialog.refresh_button.click()
        self.assertTrue(refreshed)

        # Test modification
        dialog.title_pattern_edit.setText("Updated {run_name} {status}")
        dialog.tags_edit.setText("updated, tags")
        dialog.attach_csv_check.setChecked(True)

        # Test validation with invalid placeholder
        dialog.title_pattern_edit.setText("Bad {invalid_placeholder}")
        dialog._accept_if_valid()
        self.assertTrue(dialog.validation_error_label.isVisible())

        # Fix validation
        dialog.title_pattern_edit.setText("Valid {run_name}")
        dialog._accept_if_valid()
        self.assertFalse(dialog.validation_error_label.isVisible())

        res = dialog.result_data()
        self.assertEqual(res["template_id"], 25)
        self.assertEqual(res["template_name"], "Specific Sweep Template")
        self.assertEqual(res["title_pattern"], "Valid {run_name}")
        self.assertEqual(res["tags"], ["updated", "tags"])
        self.assertTrue(res["attach_hdf5"])
        self.assertTrue(res["attach_csv"])

        dialog.close()


class TestRecipePageElabBlockIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_recipe_page_library_and_add_elab(self) -> None:
        settings = simulation_settings()
        page = RecipePage(settings)
        try:
            page.new_recipe(confirm=False)
            page.show()
            self.app.processEvents()

            # Check library contains "Upload to eLab" action button
            elab_buttons = [
                b for b in page._library_action_buttons
                if "Upload to eLab" in b.text()
            ]
            self.assertEqual(len(elab_buttons), 1)
            btn = elab_buttons[0]
            self.assertEqual(btn.property("dragKind"), "integration:upload_to_elab")

            # Set elab context with a mock profile
            profile = ElabIntegrationProfile(
                enabled=True,
                template_id=88,
                template_name="Template Eighty-Eight",
                title_pattern="Sweep {run_name}",
                tags=("elab", "mtj"),
            )
            page.set_elab_context(
                profile_provider=lambda: profile,
                available_templates_provider=lambda: [(88, "Template Eighty-Eight")],
            )

            # Add upload_to_elab node via library method
            with patch("app.ui.recipes.page.QMessageBox.warning") as warning:
                page._library_add_elab_upload()
            warning.assert_not_called()
            self.app.processEvents()

            tree_source = page.editor.toPlainText()
            self.assertIn("upload_to_elab", tree_source)
            self.assertIn("template_id: 88", tree_source)
        finally:
            page._close_discard_confirmed = True
            page.close()




class TestMainWindowElabBlockExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_wires_elab_context_and_passes_recipe_override_on_run_finish(self) -> None:
        from app.ui.shell.main_window import MainWindow

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            # Verify context wiring between MainWindow, ElabPage and RecipePage
            self.assertIsNotNone(window.recipe_page._elab_profile_provider)
            self.assertIsNotNone(window.recipe_page._elab_credentials_provider)
            self.assertIsNotNone(window.recipe_page._elab_available_templates_provider)
            self.assertIsNotNone(window.recipe_page._elab_refresh_templates_callback)
            self.assertIsNotNone(window.recipe_page._elab_navigate_callback)

            # Test navigation callback switches to elab tab
            window._navigate_to("sweeps")
            self.app.processEvents()
            window.recipe_page._elab_navigate_callback()
            self.app.processEvents()
            self.assertIs(window.stackedWidget.currentWidget(), window.navigation_routes["elab"])

            # Test run finish with recipe upload config
            plan = MagicMock()
            plan.elab_upload_config = {
                "template_id": 77,
                "template_name": "Special Template",
                "title_pattern": "Custom Title {run_name}",
                "tags": ["alpha", "beta"],
                "attach_hdf5": True,
                "attach_csv": False,
            }

            has_recipe_elab = getattr(plan, "elab_upload_config", None) is not None
            window._run_upload_to_elab_requested = has_recipe_elab
            window._run_elab_upload_config = getattr(plan, "elab_upload_config", None)

            with patch.object(window.elab_page, "queue_automatic_upload") as mock_queue:
                result_path = Path("fake_run.h5")
                run_mock = MagicMock()
                run_mock.state.value = "completed"
                run_mock.error = None
                window._run_finished({"path": result_path, "result": run_mock})

                mock_queue.assert_called_once()
                args, kwargs = mock_queue.call_args
                self.assertEqual(args[0], result_path)
                self.assertEqual(kwargs["run_state"], "completed")
                self.assertTrue(kwargs["requested"])

                override: ElabIntegrationProfile = kwargs["profile_override"]
                self.assertIsNotNone(override)
                self.assertTrue(override.enabled)
                self.assertEqual(override.template_id, 77)
                self.assertEqual(override.template_name, "Special Template")
                self.assertEqual(override.title_pattern, "Custom Title {run_name}")
                self.assertEqual(override.tags, ("alpha", "beta"))
                self.assertTrue(override.upload_hdf5)
                self.assertFalse(override.upload_csv)

                # Check cleanup
                self.assertFalse(window._run_upload_to_elab_requested)
                self.assertIsNone(window._run_elab_upload_config)
        finally:
            window.close()

if __name__ == "__main__":
    unittest.main()
