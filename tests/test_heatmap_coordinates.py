from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

import numpy as np

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.ui.results.heatmap_coordinates import (
    HeatmapRequest,
    build_heatmap_coordinates,
    read_heatmap_matrix,
)


def _write_current_sweep(path: Path) -> None:
    writer = Hdf5RunWriter(
        path,
        recipe_source="name: current sweep\n",
        settings_source="schema_version: 1\n",
        plan_hash="current-sweep",
        device_idn={},
        expected_points=3,
    )
    for index, current_a in enumerate((0.0, 0.001, 0.002)):
        writer.append(
            MeasurementPoint(
                index=index,
                setpoints={"keithley.B.current": current_a},
                measurements={},
            ),
            SpectrumTrace(
                (1e6, 2e6, 3e6),
                (-60.0 - index, -50.0 - index, -55.0 - index),
                datetime.now(timezone.utc),
                "TRAC1",
            ),
        )
    writer.close("completed")


def _write_two_axis_sweep(path: Path) -> None:
    writer = Hdf5RunWriter(
        path,
        recipe_source="name: two-axis sweep\n",
        settings_source="schema_version: 1\n",
        plan_hash="two-axis-sweep",
        device_idn={},
        expected_points=4,
    )
    for index, (current_a, generator_hz) in enumerate(
        ((0.0, 10.0), (0.0, 20.0), (0.001, 10.0), (0.001, 20.0))
    ):
        writer.append(
            MeasurementPoint(
                index=index,
                setpoints={
                    "keithley.B.current": current_a,
                    "rigol.CH1.frequency": generator_hz,
                },
                measurements={},
            ),
            SpectrumTrace(
                (1e6, 2e6),
                (-100.0 - index, -90.0 - index),
                datetime.now(timezone.utc),
                "TRAC1",
            ),
        )
    writer.close("completed")


def test_coordinates_use_stored_current_as_the_physical_sweep_axis() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "current-sweep.h5"
        _write_current_sweep(path)
        run = ThatecRunReader.describe(path)
        row = next(candidate for candidate in run.rows.values() if len(candidate.shape) == 2)
        coordinates = build_heatmap_coordinates(path, run, row, Hdf5RunReader.points(path))

        assert [dimension.id for dimension in coordinates.dimensions] == [
            "frequency",
            "keithley.B.current",
        ]
        current = coordinates.dimension("keithley.B.current")
        assert current.label == "Keithley B current"
        assert current.unit == "A"
        assert current.values == (0.0, 0.001, 0.002)

        matrix = read_heatmap_matrix(
            path,
            row,
            coordinates,
            HeatmapRequest("frequency", "keithley.B.current", {}),
        )
        assert matrix.values.shape == (3, 3)
        assert np.allclose(matrix.y_values, (0.0, 0.001, 0.002))
        assert matrix.cell_checkpoints[:, 0].tolist() == [0, 1, 2]


def test_coordinates_can_swap_frequency_to_the_y_axis() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "duplicate-current.h5"
        _write_current_sweep(path)
        run = ThatecRunReader.describe(path)
        row = next(candidate for candidate in run.rows.values() if len(candidate.shape) == 2)
        coordinates = build_heatmap_coordinates(path, run, row, Hdf5RunReader.points(path))

        matrix = read_heatmap_matrix(
            path,
            row,
            coordinates,
            HeatmapRequest("keithley.B.current", "frequency", {}),
        )
        assert matrix.values.shape == (3, 3)
        assert np.allclose(matrix.x_values, (0.0, 0.001, 0.002))
        assert matrix.cell_checkpoints[0, :].tolist() == [0, 1, 2]


def test_coordinates_filter_other_sweeps_and_build_a_non_frequency_plane() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "two-axis-sweep.h5"
        _write_two_axis_sweep(path)
        run = ThatecRunReader.describe(path)
        row = next(candidate for candidate in run.rows.values() if len(candidate.shape) == 2)
        coordinates = build_heatmap_coordinates(path, run, row, Hdf5RunReader.points(path))

        frequency_plane = read_heatmap_matrix(
            path,
            row,
            coordinates,
            HeatmapRequest(
                "frequency",
                "keithley.B.current",
                {"rigol.CH1.frequency": 20.0},
            ),
        )
        assert np.allclose(frequency_plane.values[:, 0], (-101.0, -103.0))
        assert frequency_plane.cell_checkpoints[:, 0].tolist() == [1, 3]

        sweep_plane = read_heatmap_matrix(
            path,
            row,
            coordinates,
            HeatmapRequest(
                "rigol.CH1.frequency",
                "keithley.B.current",
                {"frequency": 2e6},
            ),
        )
        assert np.allclose(sweep_plane.x_values, (10.0, 20.0))
        assert np.allclose(sweep_plane.y_values, (0.0, 0.001))
        assert np.allclose(sweep_plane.values, ((-90.0, -91.0), (-92.0, -93.0)))
        assert sweep_plane.cell_checkpoints.tolist() == [[0, 1], [2, 3]]
