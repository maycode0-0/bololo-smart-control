"""Restrict persisted cloud inventory to identifiers and LAN bootstrap metadata."""

from typing import Any

from .schema import device_id

_INVENTORY_FIELDS = frozenset({"productKey", "mac", "did", "sno", "name", "passcode"})


def minimal_inventory(value: Any) -> list[dict[str, Any]]:
    """Exclude datapoints, account records, tokens and unrelated cloud fields."""
    if not isinstance(value, list):
        return []
    devices = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("productKey"), str) or not item["productKey"]:
            continue
        if not isinstance(item.get("mac"), str) or not item["mac"]:
            continue
        device = {
            key: val
            for key, val in item.items()
            if key in _INVENTORY_FIELDS
            and isinstance(val, (str, int))
            and not isinstance(val, bool)
        }
        devices[device_id(device)] = device
    return list(devices.values())
