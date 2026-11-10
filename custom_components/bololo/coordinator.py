"""LAN-first state polling and validated, non-replayed device commands."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BololoAuthError, BololoError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .history import OperationHistory, history_kinds
from .inventory import minimal_inventory
from .model_overrides import P30, Z60, Z60_MODES
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
        auth_session=None,
        history_store=None,
        saved_history=None,
        cloud_history=None,
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
        self._auth_session = auth_session
        self.history = OperationHistory(saved_history)
        self._history_store = history_store
        self._history_dirty = bool(saved_history and saved_history != self.history.data)
        self.cloud_history = cloud_history
        self._history_tasks = {}
        self._history_refresh_at = {}
        self._history_stopping = False

    async def async_shutdown(self):
        self._history_stopping = True
        tasks = list(self._history_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._history_store is not None and self._history_dirty:
            try:
                await self._history_store.async_save({"devices": deepcopy(self.history.data)})
                self._history_dirty = False
            except OSError:
                _LOGGER.warning("Unable to persist BOLOLO operation history")
        if self._auth_session is not None:
            await self._auth_session.async_close()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, DeviceState]:
        try:
            devices = await self.client.async_get_devices()
            await self._async_save_inventory(devices)
            if isinstance(getattr(self.client, "inventory_error", None), BololoAuthError):
                self._async_request_reauth()
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
            self._schedule_cloud_history(states)
            if any(state.error_kind == "auth" for state in states):
                if not any(state.available for state in states):
                    raise BololoAuthError("BOLOLO authentication expired")
                # A cloud-only device must not disable other devices reachable
                # directly over LAN while the account requires reauthentication.
                self._async_request_reauth()
            return {device_id(state.device): state for state in states}
        except BololoAuthError as err:
            self._history_gap_all()
            raise ConfigEntryAuthFailed("BOLOLO authentication expired") from err
        except BololoError as err:
            self._history_gap_all()
            raise UpdateFailed(str(err)) from err

    def _history_gap_all(self):
        before = deepcopy(self.history.data)
        for identifier in self.data or {}:
            self.history.observation_gap(identifier)
        self._save_history_if_changed(before)

    async def _async_poll(self, device: dict[str, Any], product: Product) -> DeviceState:
        identifier = device_id(device)
        async with self._semaphore, self._device_locks.setdefault(identifier, asyncio.Lock()):
            try:
                attrs = await self.client.async_get_status(device)
                source = getattr(self.client, "last_transport", {}).get(identifier, "cloud")
                before = deepcopy(self.history.data)
                self.history.observe(identifier, product, attrs, source)
                self._save_history_if_changed(before)
                return DeviceState(device, product, attrs, True, source)
            except BololoAuthError:
                kind = "auth"
            except BololoError as err:
                kind = type(err).__name__
            before = deepcopy(self.history.data)
            self.history.observation_gap(identifier)
            self._save_history_if_changed(before)
            previous = (self.data or {}).get(identifier)
            return DeviceState(
                device,
                product,
                previous.attrs if previous else {},
                False,
                previous.transport if previous else None,
                kind,
            )

    def _save_history_if_changed(self, before):
        if self._history_store is not None and before != self.history.data:
            self._history_dirty = True
            self._history_store.async_delay_save(
                lambda: {"devices": deepcopy(self.history.data)}, 5
            )

    def _schedule_cloud_history(self, states):
        cloud = getattr(self.client, "cloud", None)
        if (
            self._history_stopping
            or self.cloud_history is None
            or getattr(self.client, "mode", "cloud") == "lan"
            or cloud is None
            or not callable(getattr(cloud, "async_get_water_history", None))
        ):
            return
        for state in states:
            identifier = device_id(state.device)
            if "water" not in history_kinds(state.product) or not state.device.get("sno"):
                continue
            if identifier in self._history_tasks or time.monotonic() < self._history_refresh_at.get(
                identifier, 0
            ):
                continue
            self._history_tasks[identifier] = self.hass.async_create_background_task(
                self._async_sync_history(state.device, cloud),
                f"BOLOLO history {identifier}",
                eager_start=False,
            )

    async def _async_sync_history(self, device, cloud):
        identifier = device_id(device)
        try:
            await self.cloud_history.async_sync(device, cloud)
            if self.cloud_history.summary(identifier).get("sync_error") == "BololoAuthError":
                self._async_request_reauth()
        finally:
            complete = self.cloud_history.summary(identifier).get("complete")
            self._history_refresh_at[identifier] = time.monotonic() + (300 if complete else 30)
            self._history_tasks.pop(identifier, None)
            if not self._history_stopping:
                self.async_update_listeners()

    async def async_bottle_command(self, identifier, command):
        """Only the explicit bottle wizard can send these workflow commands."""
        if command not in (
            {"bottle_tag": 0, "new_bottles": 0},
            {"bottle_tag": 1},
            {"bottle_tag": 2},
        ):
            raise HomeAssistantError("Invalid bottle workflow command")
        state = (self.data or {}).get(identifier)
        if (
            not state
            or state.product.key != P30
            or not state.available
            or not self.last_update_success
        ):
            raise HomeAssistantError("Bottle device is unavailable")
        try:
            async with self._device_locks.setdefault(identifier, asyncio.Lock()):
                await self.client.async_control(state.device, command)
        except BololoAuthError as err:
            self._async_request_reauth()
            raise HomeAssistantError("BOLOLO authentication expired") from err
        except BololoError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

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
