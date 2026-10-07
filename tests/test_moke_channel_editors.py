"""Editing a recipe must not remap a physical output channel."""

import pytest
from PySide6.QtWidgets import QApplication

from app.devices.moke_box.protocol import MokeFrame, set_vout
from app.devices.moke_box.sweep_provider import voltage_channel
from app.domain.errors import ConfigurationError
from app.recipes import RecipeNode
from app.ui.recipes.common_dialogs import ActionNodeEditorDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("channel", range(8))
@pytest.mark.parametrize("node_type", ["configure_moke_box", "update_moke_voltage", "sequence"])
def test_moke_action_preserves_all_eight_zero_based_channels(app, channel, node_type):
    fields = {"channel": channel}
    if node_type == "sequence":
        fields["device_module"] = "moke_box"
    dialog = ActionNodeEditorDialog(RecipeNode("moke", node_type, fields))
    try:
        dialog.show()
        app.processEvents()
        selector = dialog._editors["channel"][0]
        assert selector.isVisible() and selector.width() > 0
        assert [selector.itemText(i) for i in range(selector.count())] == [f"VOUT {i}" for i in range(8)]
        assert dialog.node_fields() == fields
        assert dialog.review.table.counts["changed"] == 0
        dialog._validate_fields(dialog.node_fields())
        for selected in range(8):
            selector.setCurrentIndex(selected)
            stored_channel = dialog.node_fields()["channel"]
            assert type(stored_channel) is int and stored_channel == selected
            target = f"moke_box.vout{stored_channel}.voltage"
            assert voltage_channel(target) == selected
            assert MokeFrame.decode(set_vout(stored_channel, 0.01)).channel == selected
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize("channel", [-1, 8, "0", True])
def test_invalid_moke_channel_is_not_silently_replaced(app, channel):
    dialog = ActionNodeEditorDialog(RecipeNode("moke", "update_moke_voltage", {"channel": channel}))
    try:
        fields = dialog.node_fields()
        assert fields["channel"] == channel and type(fields["channel"]) is type(channel)
        with pytest.raises(ConfigurationError, match="Invalid channel"):
            dialog._validate_fields(fields)
    finally:
        dialog.close()


@pytest.mark.parametrize("node_type,channel,choices", [
    ("configure_rigol", 1, [1, 2]),
    ("configure_rigol", 2, [1, 2]),
    ("configure_keithley", "A", ["A", "B"]),
    ("configure_keithley", "B", ["A", "B"]),
])
def test_other_instruments_keep_their_own_channel_numbering(app, node_type, channel, choices):
    dialog = ActionNodeEditorDialog(RecipeNode("device", node_type, {"channel": channel}))
    try:
        selector = dialog._editors["channel"][0]
        assert [selector.itemData(i) for i in range(selector.count())] == choices
        assert dialog.node_fields()["channel"] == channel
    finally:
        dialog.close()


def test_channel_expression_is_preserved(app):
    dialog = ActionNodeEditorDialog(RecipeNode("moke", "update_moke_voltage", {"channel": "${output}"}))
    try:
        assert dialog.node_fields()["channel"] == "${output}"
    finally:
        dialog.close()
