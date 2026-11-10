"""LAN-first state polling and validated, non-replayed device commands."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BololoAuthError, BololoError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .inventory import minimal_inventory
from .model_overrides import Z60, Z60_MODES
from .schema import Datapoint, Product, device_id

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceState:
    device: dict[str, Any]
    product: Product
    attrs: dict[str, Any]
    available: bool
    transport: str | None = None
    error_kind: str | None = None


class BololoCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    """Discover supported Wi-Fi devices and isolate individual poll failures."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client,
        products: dict[str, Product],
        *,
        inventory_store=None,
        saved_inventory=None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.client = client
        self.products = products
        self.unsupported: dict[str, str] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(4)
        self._inventory_store = inventory_store
        self._saved_inventory = saved_inventory
        self._reauth_requested = False

    async def _async_update_data(self) -> dict[str, DeviceState]:
        try:
            devices = await self.client.async_get_devices()
            await self._async_save_inventory(devices)
            supported = []
            self.unsupported = {}
            for device in devices:
                key = device.get("productKey")
                if not isinstance(key, str):
                    continue
                product = self.products.get(key)
                if not product or product.transport != "wifi":
                    self.unsupported[device.get("productKey", "unknown")] = (
                        product.transport if product else "unknown"
                    )
                    continue
                if isinstance(device.get("mac"), str) and device["mac"]:
                    supported.append((device, product))
            states = await asyncio.gather(
                *(self._async_poll(device, product) for device, product in supported)
            )
            if any(state.error_kind == "auth" for state in states):
                if not any(state.available for state in states):
                    raise BololoAuthError("BOLOLO authentication expired")
                # A cloud-only device must not disable other devices reachable
                # directly over LAN while the account requires reauthentication.
                self._async_request_reauth()
            return {device_id(state.device): state for state in states}
        except BololoAuthError as err:
            raise ConfigEntryAuthFailed("BOLOLO authentication expired") from err
        except BololoError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_poll(self, device: dict[str, Any], product: Product) -> DeviceState:
        identifier = device_id(device)
        async with self._semaphore, self._device_locks.setdefault(identifier, asyncio.Lock()):
            try:
                attrs = await self.client.async_get_status(device)
                source = getattr(self.client, "last_transport", {}).get(identifier, "cloud")
                return DeviceState(device, product, attrs, True, source)
            except BololoAuthError:
                kind = "auth"
            except BololoError as err:
                kind = type(err).__name__
            previous = (self.data or {}).get(identifier)
            return DeviceState(
                device,
                product,
                previous.attrs if previous else {},
                False,
                previous.transport if previous else None,
                kind,
            )

    async def _async_save_inventory(self, devices):
        if self._inventory_store is None:
            return
        metadata = minimal_inventory(devices)
        if metadata == self._saved_inventory:
            return
        try:
            await self._inventory_store.async_save({"devices": metadata})
        except OSError:
            _LOGGER.warning("Unable to persist BOLOLO device inventory")
        else:
            self._saved_inventory = metadata

    def _async_request_reauth(self):
        if not self._reauth_requested:
            self._reauth_requested = True
            self.entry.async_start_reauth(self.hass)

    async def async_write(self, identifier: str, datapoint: Datapoint, value: Any) -> None:
        state = (self.data or {}).get(identifier)
        if not state or not state.available or not self.last_update_success:
            raise HomeAssistantError("Device state is unavailable")
        try:
            encoded = datapoint.encode(value)
            async with self._device_locks.setdefault(identifier, asyncio.Lock()):
                command = {datapoint.key: encoded}
                if state.product.key == Z60 and datapoint.key in Z60_MODES and encoded is True:
                    # Cloud cache can lag preceding commands or app changes.
                    command.update({key: False for key in Z60_MODES if key != datapoint.key})
                await self.client.async_control(state.device, command)
                state.transport = getattr(self.client, "last_transport", {}).get(
                    identifier, state.transport
                )
        except BololoAuthError as err:
            self._async_request_reauth()
            raise HomeAssistantError("BOLOLO authentication expired") from err
        except (BololoError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()
