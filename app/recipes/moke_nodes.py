"""Canonical authored MOKE device blocks shared by every visual entry point."""
from app.domain.errors import ConfigurationError


def moke_device_node(node_id, channel, children):
    if type(channel) is not int or channel not in range(8):
        raise ConfigurationError("MOKE output channel must be an integer in 0..7.")
    return {"id": node_id, "type": "sequence", "device_module": "moke_box",
            "channel": channel, "children": children}


def moke_fixed_node(node_id, operation_id, channel, voltage):
    return moke_device_node(node_id, channel, [{"id": operation_id,
        "type": "set_moke_voltage", "channel": channel, "voltage": voltage}])
