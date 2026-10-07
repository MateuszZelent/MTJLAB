"""Prepare single-run raw artifacts and analysis without using GUI or devices."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path

from PySide6.QtCore import QThread

from .analyzer import KeithleyCharacterizationAnalyzer
from .export import KeithleyDataExporter
from .models import ExtractedScientificParameters
from .report_paths import create_run_directory, report_path


@dataclass(frozen=True)
class PreparedArtifacts:
    csv_path: Path | None
    csv_sha256: str | None
    pdf_path: Path
    parameters: ExtractedScientificParameters | None
    errors: tuple[str, ...]


class SingleArtifactsWorker(QThread):
    def __init__(self, dataset, directory, inventory_target, parent=None):
        super().__init__(parent)
        self.dataset = deepcopy(dataset)
        self.directory = Path(directory)
        self.inventory_target = inventory_target
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self._prepare()
        except Exception as exc:
            self.error = str(exc)

    def _prepare(self):
        from .rigol_report import export_rigol_equivalence

        errors = []
        csv_path = digest = parameters = None
        directory = create_run_directory(self.directory)
        # Preserve raw data even when derived analysis cannot be calculated.
        try:
            csv_path = KeithleyDataExporter.export_csv(self.dataset, directory / "characterization.csv")
        except Exception as exc:
            errors.append(f"CSV: {exc}")
        if csv_path is not None:
            try:
                hasher = hashlib.sha256()
                with csv_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            except Exception as exc:
                errors.append(f"CSV hash: {exc}")
        try:
            export_rigol_equivalence(self.dataset, directory / "rigol_equivalence.csv")
        except Exception as exc:
            errors.append(f"Rigol equivalence CSV: {exc}")
        try:
            parameters = KeithleyCharacterizationAnalyzer.analyze(self.dataset)
        except Exception as exc:
            errors.append(f"Scientific analysis: {exc}")
        return PreparedArtifacts(csv_path, digest, report_path(directory), parameters, tuple(errors))
