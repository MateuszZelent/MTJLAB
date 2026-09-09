"""Associate saved field curves with their original inventory target offline."""

import hashlib

from app.devices.keithley_2600.characterization.field_reader import load_field_series
from app.devices.keithley_2600.characterization.report_paths import report_path as resolve_report_path
from app.domain.quantities import format_quantity_auto
from app.inventory.models import SampleRunRecord


def register_field_series(store, directory) -> tuple[SampleRunRecord, ...]:
    series = load_field_series(directory)
    target = series.manifest["provenance"].get("inventory_target")
    if target is None:
        return ()  # Older/manual data never inherits today's selected sample.
    sample_id = target["sample_id"]
    sample = store.get_sample(sample_id)
    if sample is None:
        raise ValueError(f"Original sample {sample_id!r} is no longer in the inventory.")
    records = []
    for curve in series.curves:
        if curve.dataset is None:
            continue  # Skipped targets without a curve remain in the series manifest.
        csv_path = curve.directory / "characterization.csv"
        report_path = resolve_report_path(curve.directory, field_curve=True, existing=True)
        record = SampleRunRecord(
            sample_id=sample_id, sample_name=target["sample_name"],
            row=target["row"], col=target["col"], device_label=target["device_label"],
            run_path=str(csv_path), run_sha256=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            created_at_utc=curve.dataset.completed_at_iso,
            status=curve.status, point_count=len(curve.dataset.points), spectrum_count=0,
            recipe_name=f"Keithley field characterization · #{curve.index + 1} · B {format_quantity_auto(curve.current_a, 'current')}",
            notes=f"Series: {series.directory.name}; history segment {curve.history_segment}. {curve.detail}",
            csv_path=str(csv_path), report_path=str(report_path) if report_path.is_file() else "",
        )
        records.append(store.register_run_artifacts(record))
    return tuple(records)
def summary_artifacts_for_run(run):
    """Resolve summary links only through a verified parent series membership."""
    from pathlib import Path
    from app.devices.keithley_2600.characterization.field_reader import load_field_series
    path = Path(run.csv_path or run.run_path).resolve()
    directory = path.parent.parent
    if path.name != "characterization.csv" or not (directory / "series.json").is_file():
        return ()
    try:
        series = load_field_series(directory)
    except (ValueError, OSError, KeyError, TypeError):
        return ()
    if not any(curve.directory == path.parent and curve.dataset is not None for curve in series.curves):
        return ()
    results = []
    for label, filename in (("Open series summary PDF", "field_series_report.pdf"),
                            ("Open series summary CSV", "field_series_summary.csv")):
        artifact = directory / filename
        if filename == "field_series_report.pdf":
            artifact = resolve_report_path(directory, summary=True, existing=True)
        if artifact.is_file() and artifact.resolve().parent == directory:
            results.append((label, str(artifact)))
    return tuple(results)
