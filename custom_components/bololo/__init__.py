"""BOLOLO smart home integration with LAN-first and cloud transports."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import BololoAuthError, BololoClient, BololoError
from .const import (
    CONF_CONNECTION_MODE,
    CONF_COUNTRY_CODE,
    CONF_DEVICES,
    CONF_LAN_HOSTS,
    CONF_LAN_PASSCODES,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import BololoCoordinator
from .inventory import minimal_inventory
from .schema import load_products
from .transport import HybridClient


def _create_lan_client(products, options):
    # A cloud-only setup does not need LAN sockets or background tasks.
    from .lan import BololoLanClient

    return BololoLanClient(
        products,
        hosts=options.get(CONF_LAN_HOSTS, {}),
        passcodes=options.get(CONF_LAN_PASSCODES, {}),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mode = entry.options.get(
        CONF_CONNECTION_MODE, entry.data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    )
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.devices", private=True)
    stored = await store.async_load()
    # An existing empty Store is authoritative after unbinding all devices.
    has_stored_inventory = isinstance(stored, dict) and isinstance(stored.get(CONF_DEVICES), list)
    if has_stored_inventory:
        cached = minimal_inventory(stored[CONF_DEVICES])
    elif isinstance(entry.data.get(CONF_DEVICES), list):
        cached = minimal_inventory(entry.data[CONF_DEVICES])
    else:
        cached = None
    cloud = None
    if mode != "lan":
        cloud = BololoClient(
            async_get_clientsession(hass),
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            country_code=entry.data.get(CONF_COUNTRY_CODE, "86"),
        )
        if mode == "cloud" or cached is None:
            try:
                await cloud.async_login()
            except BololoAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except BololoError as err:
                raise ConfigEntryNotReady(str(err)) from err
    products = await hass.async_add_executor_job(load_products)
    lan = _create_lan_client(products, entry.options) if mode != "cloud" else None
    client = HybridClient(cloud, lan, mode=mode, cached_devices=cached)
    coordinator = BololoCoordinator(
        hass,
        entry,
        client,
        products,
        inventory_store=store,
        saved_inventory=cached if has_stored_inventory else None,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await coordinator.async_shutdown()
        await client.async_close()
        raise
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        await client.async_close()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        await coordinator.client.async_close()
    return unloaded
