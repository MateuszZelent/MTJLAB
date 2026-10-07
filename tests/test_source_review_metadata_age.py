from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from app.devices.moke_box.models import MokeHallVoltageReading
from app.devices.moke_box.ui.page import MokeBoxPage
from app.devices.lakeshore_475.ui.page import LakeShore475Page
from app.devices.lakeshore_475.models import GaussmeterReading, GaussmeterSnapshot, MeasurementMode, FieldUnit
from app.domain.manual_metadata import ManualMetadataValue
from app.storage import ManualSpectrumArchive, Hdf5RunReader
from tests.helpers import loaded_settings
from tests.test_source_review_manual_provenance import trace


def test_metadata_timestamp_requires_timezone_and_is_persisted(tmp_path):
    stamp = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    value = ManualMetadataValue("v", "meter", "Voltage", "voltage", "V", .1, recorded_at_utc=stamp)
    assert value.recorded_at_utc.hour == 10
    with pytest.raises(ValueError, match="timezone"):
        ManualMetadataValue("v", "meter", "Voltage", "voltage", "V", .1, recorded_at_utc=datetime(2026, 1, 1))
    archive = ManualSpectrumArchive()
    result = archive.save(trace(), destination=tmp_path / "dated.h5", mode="timestamped", metadata_values=(value,))
    point, = Hdf5RunReader.points(result.path)
    assert point.metadata["metadata_descriptors"][0]["recorded_at_utc"] == "2026-01-01T10:00:00+00:00"


@pytest.mark.parametrize("state", ["disconnected", "fault", "unknown"])
def test_moke_metadata_does_not_survive_unusable_connection(state):
    app = QApplication.instance() or QApplication([])
    page = MokeBoxPage(Mock(), loaded_settings())
    try:
        stamp = datetime.now(timezone.utc)
        page._show_confirmed_voltage(0, .02)
        page._show_hall_reading(MokeHallVoltageReading(.1, .001, 1, (0x800000,), stamp))
        values = page.manual_metadata_values()
        assert len(values) == 3 and all(value.recorded_at_utc is not None for value in values)
        page.field_workflow._connected = False
        page._state_changed(state)
        assert page.manual_metadata_values() == ()
        assert len(page._history) == 1
        page._state_changed("verified")
        assert page.manual_metadata_values() == ()
        page._show_confirmed_voltage(0, .03)
        assert [value.value_si for value in page.manual_metadata_values()] == [.03]
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_lakeshore_reconnect_requires_new_metadata_reading():
    app = QApplication.instance() or QApplication([])
    page = LakeShore475Page(Mock(), loaded_settings())
    try:
        stamp = datetime.now(timezone.utc)
        snapshot = GaussmeterSnapshot("1", MeasurementMode.DC, "2", FieldUnit.TESLA, "0", True, "40", stamp)
        reading = GaussmeterReading(mode=MeasurementMode.DC, unit=FieldUnit.TESLA, snapshot=snapshot, timestamp_utc=stamp, field_t=.01)
        page._show_reading(reading)
        assert page.manual_metadata_values()[0].recorded_at_utc == stamp
        page._state("DISCONNECTED")
        page._state("verified")
        assert page.manual_metadata_values() == ()
        assert len(page._history) == 1
        page._show_reading(reading)
        assert page.manual_metadata_values()[0].value_si == .01
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
