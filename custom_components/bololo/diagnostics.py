"""Export protocol coverage without account or device identifiers."""

from .const import DOMAIN
from .schema import device_id


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "integration_version": "0.1.1",
        "connection_mode": getattr(coordinator.client, "mode", "cloud"),
        "last_update_success": coordinator.last_update_success,
        "inventory_refresh_error": (
            type(coordinator.client.inventory_error).__name__
            if getattr(coordinator.client, "inventory_error", None)
            else None
        ),
        "unsupported_product_keys": coordinator.unsupported,
        "devices": [
            {
                "product_key": state.product.key,
                "model": state.product.name,
                "product_transport": state.product.transport,
                "active_transport": state.transport,
                "read_success": state.available,
                "last_error": state.error_kind,
                "lan_last_error": getattr(
                    getattr(coordinator.client, "lan", None), "last_errors", {}
                ).get(device_id(state.device)),
                "reported_datapoint_names": sorted(state.attrs),
                "supported_datapoint_names": [point.key for point in state.product.datapoints],
            }
            for state in (coordinator.data or {}).values()
        ],
    }
