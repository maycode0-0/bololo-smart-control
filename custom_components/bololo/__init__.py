"""BOLOLO smart home integration with LAN-first and cloud transports."""

import asyncio
import hashlib
import json
from copy import deepcopy

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import json as json_util
from homeassistant.util.file import WriteError

from .api import BololoAuthError, BololoClient, BololoConnectionError, BololoError
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

_AUTH_FIELDS = ("token", "refresh_token", "created_at", "expired_at", "uid")


class _AuthStore(Store):
    """Require an actual disk write before a rotated session can be used."""

    async def async_save(self, data):
        if self._read_only or self.hass.state is CoreState.stopping:
            raise OSError("Authentication storage is not writable")
        await super().async_save(data)

    async def _async_write_data(self, path, data):
        try:
            await super()._async_write_data(path, data)
        except (json_util.SerializationError, WriteError) as err:
            # Store normally logs and suppresses these errors. Authentication
            # rotation must fail before the API commits a non-persisted token.
            raise OSError("Authentication session write failed") from err


def _session_fingerprint(entry):
    """Bind rotations to one account and the exact bootstrap credentials."""
    identity = {
        "unique_id": getattr(entry, "unique_id", None),
        "username": entry.data.get(CONF_USERNAME, "").strip().casefold(),
        "country_code": entry.data.get(CONF_COUNTRY_CODE, "86").lstrip("+"),
        "auth_method": entry.data.get("auth_method", "password"),
        "mobile_country_code": entry.data.get("mobile_country_code", "CN"),
        "password": entry.data.get(CONF_PASSWORD),
        "auth": entry.data.get("auth"),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _minimal_auth(value):
    if not isinstance(value, dict) or not isinstance(value.get("token"), str) or not value["token"]:
        return None
    return {
        key: value[key]
        for key in _AUTH_FIELDS
        if key in value
        and not isinstance(value[key], bool)
        and (value[key] is None or isinstance(value[key], (str, int, float)))
    }


class _AuthSession:
    """Persist rotations without letting an old runtime overwrite a reauth session."""

    def __init__(self, entry, store):
        self.entry = entry
        self.store = store
        self.fingerprint = _session_fingerprint(entry)
        self._active = True
        self._lock = asyncio.Lock()
        bootstrap = _minimal_auth(entry.data.get("auth"))
        self._uid = getattr(entry, "unique_id", None)
        if self._uid is None and bootstrap and bootstrap.get("uid") is not None:
            self._uid = str(bootstrap["uid"])

    def _same_account(self, auth):
        return auth.get("uid") is None or self._uid is None or str(auth["uid"]) == self._uid

    async def async_load(self):
        stored = await self.store.async_load()
        if isinstance(stored, dict) and stored.get("session_fingerprint") == self.fingerprint:
            auth = _minimal_auth(stored.get("auth"))
            if auth and self._same_account(auth):
                return deepcopy(auth)
        auth = _minimal_auth(self.entry.data.get("auth"))
        if auth and not self._same_account(auth):
            raise BololoAuthError("BOLOLO session belongs to a different account")
        return deepcopy(auth)

    async def async_save(self, value):
        auth = _minimal_auth(value)
        if not auth:
            raise BololoAuthError("Invalid BOLOLO authentication session")
        async with self._lock:
            if not self._active or _session_fingerprint(self.entry) != self.fingerprint:
                raise BololoConnectionError("BOLOLO authentication session was replaced or stopped")
            if not self._same_account(auth):
                raise BololoAuthError("BOLOLO session belongs to a different account")
            try:
                await self.store.async_save(
                    {
                        "session_fingerprint": self.fingerprint,
                        "auth": deepcopy(auth),
                    }
                )
            except OSError as err:
                raise BololoConnectionError(
                    "Unable to persist BOLOLO authentication session"
                ) from err

    async def async_close(self):
        # Deactivate before awaiting: a callback waiting on this lock must exit.
        self._active = False
        async with self._lock:
            pass


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
    auth_session = None
    client = None
    coordinator = None
    try:
        if mode != "lan":
            auth_session = _AuthSession(
                entry, _AuthStore(hass, 1, f"{DOMAIN}.{entry.entry_id}.auth", private=True)
            )
            try:
                auth = await auth_session.async_load()
            except BololoAuthError as err:
                raise ConfigEntryAuthFailed(
                    "BOLOLO session belongs to a different account"
                ) from err
            cloud = BololoClient(
                async_get_clientsession(hass),
                username=entry.data.get(CONF_USERNAME, ""),
                password=entry.data.get(CONF_PASSWORD, ""),
                country_code=entry.data.get(CONF_COUNTRY_CODE, "86"),
                auth=auth,
                on_auth_update=auth_session.async_save,
            )
            if mode == "cloud" or cached is None:
                try:
                    if auth:
                        await cloud.async_ensure_authenticated()
                    else:
                        await cloud.async_login()
                except BololoAuthError as err:
                    raise ConfigEntryAuthFailed("BOLOLO authentication expired") from err
                except BololoError as err:
                    raise ConfigEntryNotReady("Unable to connect to BOLOLO") from err
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
            auth_session=auth_session,
        )
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if auth_session is not None:
            await auth_session.async_close()
        if coordinator is not None:
            await coordinator.async_shutdown()
        if client is not None:
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
