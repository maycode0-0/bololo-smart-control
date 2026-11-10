"""Route device requests through LAN first, with bounded cloud fallback.

The LAN implementation owns discovery, connection/authentication, framing and
timeouts. Its errors must follow lan_errors.py, particularly the distinction
between pre-command failure and an uncertain physical command result.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import Any, Literal, Protocol

from .api import BololoError
from .lan_errors import LanError, LanUnavailableError
from .schema import device_id

TransportMode = Literal["auto", "cloud", "lan"]
Device = dict[str, Any]
INVENTORY_TTL = 300.0
INVENTORY_RETRY_INTERVAL = 30.0


class DeviceClient(Protocol):
    """Shared cloud and LAN request interface."""

    async def async_get_status(self, device: Device) -> dict[str, Any]: ...

    async def async_control(self, device: Device, attrs: dict[str, Any]) -> None: ...


class CloudClient(DeviceClient, Protocol):
    async def async_get_devices(self) -> list[Device]: ...


class LanClient(DeviceClient, Protocol):
    """LAN reads wrap transport failures as LanError subclasses.

    A failed write may be replayed over cloud only when it raised
    LanUnavailableError, meaning no physical control bytes could be sent.
    """

    async def async_close(self) -> None: ...


class HybridClient:
    """Use cached inventory to keep LAN independent of cloud availability.

    Supplied device metadata is kept in memory only. The integration owns any
    persistent cache, secret redaction and fresh-account bootstrap. An
    authoritative successful cloud inventory replaces the previous inventory,
    including an empty response after all devices have been removed.
    """

    def __init__(
        self,
        cloud: CloudClient | None,
        lan: LanClient | None,
        mode: TransportMode = "auto",
        cached_devices: list[Device] | None = None,
    ) -> None:
        if mode not in {"auto", "cloud", "lan"}:
            raise ValueError("Unsupported transport mode")
        self.cloud = cloud
        self.lan = lan
        self.mode = mode
        self._cached_devices = self._validate_inventory(
            [] if cached_devices is None else cached_devices
        )
        self._have_inventory = cached_devices is not None
        self._inventory_refresh_at = (
            time.monotonic() + INVENTORY_TTL if self._have_inventory else 0.0
        )
        self._inventory_lock = asyncio.Lock()
        self.last_transport: dict[str, str] = {}
        self.inventory_error: BololoError | None = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise BololoError("Device transport is closed")

    @staticmethod
    def _validate_inventory(devices: list[Device]) -> list[Device]:
        if not isinstance(devices, list) or any(not isinstance(item, dict) for item in devices):
            raise BololoError("Invalid device inventory")
        return deepcopy(devices)

    @property
    def cached_devices(self) -> list[Device]:
        """Return an independent copy; callers cannot mutate future requests."""
        return deepcopy(self._cached_devices)

    async def async_get_devices(self) -> list[Device]:
        """Refresh cloud metadata at most once per five minutes."""
        return await self._async_get_devices(force=False)

    async def async_refresh_devices(self) -> list[Device]:
        """Explicitly refresh inventory; LAN-only never contacts the cloud."""
        return await self._async_get_devices(force=True)

    async def _async_get_devices(self, *, force: bool) -> list[Device]:
        self._ensure_open()
        if self.mode == "lan":
            if not self._have_inventory:
                raise LanUnavailableError("LAN mode requires known device metadata")
            return self.cached_devices
        async with self._inventory_lock:
            if not force and self._have_inventory and time.monotonic() < self._inventory_refresh_at:
                return self.cached_devices
            try:
                if self.cloud is None:
                    raise BololoError("Cloud client is unavailable")
                devices = self._validate_inventory(await self.cloud.async_get_devices())
            except BololoError as err:
                self.inventory_error = err
                # Retain identities during an outage so reachable LAN devices
                # remain usable. Retry only inventory refresh after a backoff.
                if not self._have_inventory:
                    raise
                self._inventory_refresh_at = time.monotonic() + INVENTORY_RETRY_INTERVAL
            else:
                self._cached_devices = devices
                self._have_inventory = True
                self.inventory_error = None
                self._inventory_refresh_at = time.monotonic() + INVENTORY_TTL
                known_ids = {
                    device_id(item)
                    for item in devices
                    if isinstance(item.get("productKey"), str) and isinstance(item.get("mac"), str)
                }
                self.last_transport = {
                    key: source for key, source in self.last_transport.items() if key in known_ids
                }
            return self.cached_devices

    async def async_get_status(self, device: Device) -> dict[str, Any]:
        """Prefer a direct read, then use cloud if a LAN read fails."""
        self._ensure_open()
        if self.mode != "cloud":
            try:
                if self.lan is None:
                    raise LanUnavailableError("LAN client is unavailable")
                status = await self.lan.async_get_status(device)
            except LanError:
                if self.mode == "lan":
                    raise
            else:
                self.last_transport[device_id(device)] = "lan"
                return status
        self._ensure_open()
        if self.cloud is None:
            raise BololoError("Cloud client is unavailable")
        status = await self.cloud.async_get_status(device)
        self.last_transport[device_id(device)] = "cloud"
        return status

    async def async_control(self, device: Device, attrs: dict[str, Any]) -> None:
        """Fall back only before sending a physical LAN control command."""
        self._ensure_open()
        if self.mode != "cloud":
            try:
                if self.lan is None:
                    raise LanUnavailableError("LAN client is unavailable")
                await self.lan.async_control(device, attrs)
            except LanUnavailableError:
                if self.mode == "lan":
                    raise
            else:
                self.last_transport[device_id(device)] = "lan"
                return
        self._ensure_open()
        if self.cloud is None:
            raise BololoError("Cloud client is unavailable")
        await self.cloud.async_control(device, attrs)
        self.last_transport[device_id(device)] = "cloud"

    async def async_close(self) -> None:
        """Release LAN resources; the caller owns the cloud's HTTP session."""
        self._closed = True
        if self.lan is not None:
            await self.lan.async_close()
