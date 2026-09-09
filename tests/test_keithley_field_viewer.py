"""Switch saved field curves and plot dimensions without instrument operations."""

from unittest.mock import Mock
from dataclasses import replace
import math

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics

from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
from tests.test_keithley_field_worker import make_worker


def test_large_overlay_groups_keep_original_indices(tmp_path, monkeypatch):
    from app.devices.keithley_2600.characterization import field_reader
    app = QApplication.instance() or QApplication([])
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = field_reader.load_field_series(worker.directory)
    first = series.curves[0]
    curves = tuple(replace(first, index=i, current_a=i * 1e-6) for i in range(17))
    monkeypatch.setattr(field_reader, "load_field_series", lambda _: replace(series, curves=curves))
    controller = Mock()
    card = KeithleyCharacterizationCard(controller, worker.settings)
    try:
        card.open_field_series(worker.directory)
        card.field_overlay_check.setChecked(True)
        assert card.field_overlay_group.count() == 3
        card.show()
        for button in (card.policy_retry_button, card.field_summary_pdf_button,
                       card.field_summary_csv_button):
            button.show()
        for width in (1400, 1000):
            card.resize(width, 900)
            app.processEvents()
            assert card.width() == width
            assert card._workspace_splitter.orientation() == (
                Qt.Orientation.Horizontal if width >= 1360 else Qt.Orientation.Vertical
            )
            assert card.plot_widget.isVisible()
            assert card.plot_widget.width() > 300
            assert card.plot_widget.height() > 150
            assert card.field_overlay_group.isVisible()
            assert card.field_overlay_group.width() > 80
            for widget in (card.start_button, card.status_label, card.field_overlay_check):
                metrics = QFontMetrics(widget.font())
                assert all(metrics.inFont(char) for char in "ABCxyz012"), widget.font().toString()
            legend = card._field_overlay_legend
            assert legend.columnCount == 2
            assert card.plot_widget.sceneBoundingRect().contains(legend.sceneBoundingRect())
            for button in (card.start_button, card.stop_button, card.policy_retry_button,
                           card.field_summary_pdf_button, card.field_summary_csv_button,
                           card.pdf_button, card.csv_button):
                assert button.isVisible()
                assert button.parentWidget().rect().contains(button.geometry())
            assert card.grab().save(str(tmp_path / f"overlay_groups_{width}.png"))
        for group, count, first_label in ((0, 8, "#1"), (1, 8, "#9"), (2, 1, "#17")):
            card.field_overlay_group.setCurrentIndex(group)
            app.processEvents()
            assert len(card._field_overlay_items) == count
            assert card._field_overlay_items[0].name().startswith(first_label)
        controller.call.assert_not_called()
    finally:
        card.close()
        app.processEvents()


def test_saved_series_selection_and_resistance_are_offline(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker, _ = make_worker(tmp_path)
    worker.run()
    controller = Mock()
    card = KeithleyCharacterizationCard(controller, worker.settings)
    try:
        card.resize(1400, 900)
        card.show()
        card.open_field_series(worker.directory)
        app.processEvents()
        assert card.field_curve_combo.count() == 4
        assert card.field_curve_combo.currentIndex() == 3
        np.testing.assert_allclose(card.curve_r_true.getData()[1], [1000, 1000, 1000])
        for index in (0, 2, 3, 0):
            card.field_curve_combo.setCurrentIndex(index)
            card.plot_view_nav.setCurrentItem("res")
            app.processEvents()
            assert card.curve_r_true.isVisible()
            assert not card.curve_iv.isVisible()
            assert card._current_dataset.field_sequence_index == index
            np.testing.assert_allclose(card.curve_r_true.getData()[1], [1000, 1000, 1000])
            card.plot_view_nav.setCurrentItem("iv")
            assert card.curve_iv.isVisible()
        card.field_curve_combo.setCurrentIndex(1)
        assert card._current_dataset is None
        assert not card.csv_button.isEnabled()
        assert not card.pdf_button.isEnabled()
        assert "skipped_field_compliance" in card.status_label.text()
        card.field_curve_combo.setCurrentIndex(2)
        card.plot_view_nav.setCurrentItem("res")
        app.processEvents()
        low, high = card.plot_widget.viewRange()[1]
        assert high - low >= 20
        assert card.grab().save(str(tmp_path / "saved_field_resistance.png"))
        card.field_overlay_check.setChecked(True)
        app.processEvents()
        assert len(card._field_overlay_items) == 3
        assert len(card._field_overlay_legend.items) == 3
        assert not card.curve_r_true.isVisible()
        assert all(len(item.getData()[0]) == 3 for item in card._field_overlay_items)
        card.plot_view_nav.setCurrentItem("iv")
        assert len(card._field_overlay_items) == 3
        np.testing.assert_allclose(card._field_overlay_items[0].getData()[1], [.001, .002, .003])
        card.plot_view_nav.setCurrentItem("res")
        app.processEvents()
        QTest.qWait(250)
        assert card.grab().save(str(tmp_path / "field_overlay.png"))
        card.field_overlay_check.setChecked(False)
        assert card.curve_r_true.isVisible()
        assert card._field_overlay_items == []
        assert not card._field_overlay_legend.isVisible()
        controller.assert_not_called()
        assert controller.method_calls == []
    finally:
        card.close()


def test_overlay_keeps_repeated_fields_separate_and_preserves_invalid_gaps(tmp_path):
    from app.devices.keithley_2600.characterization.field_reader import load_field_series
    from app.devices.keithley_2600.characterization.field_plot_data import field_overlay_curves
    worker, _ = make_worker(tmp_path)
    worker.run()
    series = load_field_series(worker.directory)
    first = series.curves[0]
    points = list(first.dataset.points)
    points[1] = replace(points[1], valid=False)
    series = replace(series, curves=(replace(first, dataset=replace(first.dataset, points=tuple(points))), *series.curves[1:]))
    curves = field_overlay_curves(series, resistance=True)
    assert [curve.index for curve in curves] == [0, 2, 3]
    assert "B 0 A" in curves[0].label and "B 0 A" in curves[1].label
    assert curves[0].label != curves[1].label
    assert "h0" in curves[0].label and "h1" in curves[1].label
    assert math.isnan(curves[0].y[1])
    assert len(curves[0].x) == len(first.dataset.points)
