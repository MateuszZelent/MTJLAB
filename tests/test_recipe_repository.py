from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.recipes import RecipeRepository


SOURCE_A = """\
schema_version: 1
name: version-a
root:
  id: wait
  type: wait
  duration: "1 ms"
"""

SOURCE_B = SOURCE_A.replace("version-a", "version-b").replace('"1 ms"', '"2 ms"')


class RecipeRepositoryTests(unittest.TestCase):
    def test_save_is_atomic_and_preserves_changed_previous_version(self) -> None:
        repository = RecipeRepository()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.yml"
            first = repository.save(path, SOURCE_A)
            second = repository.save(path, SOURCE_B)

            self.assertIsNone(first.backup_path)
            self.assertIsNotNone(second.backup_path)
            assert second.backup_path is not None
            self.assertEqual(second.backup_path.read_text(encoding="utf-8"), SOURCE_A)
            self.assertEqual(path.read_text(encoding="utf-8"), SOURCE_B)
            self.assertEqual(repository.versions(path), (second.backup_path,))
            self.assertNotEqual(first.sha256, second.sha256)

    def test_autosave_can_preserve_invalid_work_and_is_cleared_after_valid_save(self) -> None:
        repository = RecipeRepository()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.yml"
            repository.save(path, SOURCE_A)
            recovery = repository.autosave(path, "unfinished: [")

            self.assertTrue(recovery.is_file())
            self.assertTrue(repository.has_newer_recovery(path))
            self.assertEqual(repository.load_recovery(path), "unfinished: [")

            repository.save(path, SOURCE_B)
            self.assertFalse(recovery.exists())
            self.assertIsNone(repository.load_recovery(path))

    def test_invalid_recipe_never_replaces_the_saved_file(self) -> None:
        repository = RecipeRepository()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.yml"
            repository.save(path, SOURCE_A)
            with self.assertRaises(Exception):
                repository.save(path, "schema_version: 1\n")
            self.assertEqual(path.read_text(encoding="utf-8"), SOURCE_A)


if __name__ == "__main__":
    unittest.main()
