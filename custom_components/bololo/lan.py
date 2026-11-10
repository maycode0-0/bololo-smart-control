"""Bounded Gizwits LAN discovery and short-lived authenticated connections.

Connections are deliberately closed after each operation, avoiding dependence on
unverified firmware heartbeat intervals. A physical write is never retried here.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path
from typing import Any

from . import lan_codec as codec
from .lan_errors import LanCommandUncertainError, LanProtocolError, LanUnavailableError
from .schema import Product, device_id

DISCOVERY_PORT = 12414
REPORT_PORT = 2415
TCP_PORT = 12416
REQUEST_TIMEOUT = 2.5
DISCOVERY_TIMEOUT = 2.0
DISCOVERY_INTERVAL = 30.0
ADDRESS_TTL = 180.0


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: BololoLanClient) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        try:
            device = codec.discovery(data)
            if device is not None:
                self.owner._addresses[device_id(device)] = (address[0], time.monotonic())
        except (LanProtocolError, ValueError, KeyError, UnicodeError):
            # Ignore malformed and unrelated broadcast traffic.
            return


class BololoLanClient:
    """Discover only on the local network; cloud identity remains authoritative."""

    def __init__(
        self,
        products: dict[str, Product],
        hosts: dict[str, str] | None = None,
        passcodes: dict[str, str] | None = None,
    ) -> None:
        self.products = products
        self._hosts = dict(hosts or {})
        self._passcodes = dict(passcodes or {})
        self._profiles: dict[str, dict] | None = None
        self._profile_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._addresses: dict[str, tuple[str, float]] = {}
        self._transports: list[asyncio.DatagramTransport] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._discovery_at = 0.0
        self._closed = False
        self.last_errors: dict[str, str] = {}

    async def _profile(self, device: dict[str, Any]) -> dict:
        if self._profiles is None:
            async with self._profile_lock:
                if self._profiles is None:
                    self._profiles = await asyncio.to_thread(
                        lambda: json.loads(
                            Path(__file__)
                            .with_name("lan_profiles.json")
                            .read_text(encoding="utf-8")
                        )
                    )
        profile = self._profiles.get(device["productKey"])
        if not profile or profile.get("protocolType") != "var_len":
            raise LanUnavailableError("This product has no supported LAN wire layout")
        return profile

    async def _discover(self) -> None:
        async with self._discovery_lock:
            if self._closed:
                raise LanUnavailableError("LAN client is closed")
            if time.monotonic() < self._discovery_at:
                return
            self._discovery_at = time.monotonic() + DISCOVERY_INTERVAL
            loop = asyncio.get_running_loop()
            if not self._transports:
                # Listen both for solicited responses to the ephemeral source
                # port and unsolicited state announcements to SDK report port.
                try:
                    listener, _ = await loop.create_datagram_endpoint(
                        lambda: _DiscoveryProtocol(self),
                        local_addr=("0.0.0.0", REPORT_PORT),
                        family=socket.AF_INET,
                    )
                except OSError:
                    pass
                else:
                    self._transports.append(listener)
                try:
                    sender, _ = await loop.create_datagram_endpoint(
                        lambda: _DiscoveryProtocol(self),
                        local_addr=("0.0.0.0", 0),
                        family=socket.AF_INET,
                        allow_broadcast=True,
                    )
                except OSError as err:
                    for transport in self._transports:
                        transport.close()
                    self._transports.clear()
                    raise LanUnavailableError("LAN discovery socket is unavailable") from err
                self._transports.append(sender)
            sender = self._transports[-1]
            packet = codec.frame(3)
            for host in {"255.255.255.255", *self._hosts.values()}:
                try:
                    sender.sendto(packet, (host, DISCOVERY_PORT))
                except (OSError, ValueError):
                    continue
            await asyncio.sleep(DISCOVERY_TIMEOUT)

    async def _host(self, device: dict[str, Any]) -> str:
        identifier = device_id(device)
        address = self._addresses.get(identifier)
        if address is None or time.monotonic() - address[1] > ADDRESS_TTL:
            await self._discover()
            address = self._addresses.get(identifier)
        if self._closed or address is None or time.monotonic() - address[1] > ADDRESS_TTL:
            raise LanUnavailableError("Device was not found on the local network")
        # A manual address is used for unicast discovery, never as a substitute
        # for matching product key and MAC before controlling a physical device.
        configured = self._hosts.get(identifier)
        if configured and configured != address[0]:
            raise LanUnavailableError("Configured address did not announce the expected device")
        return address[0]

    async def _receive(self, reader, commands: set[int], sn: int | None = None):
        async with asyncio.timeout(REQUEST_TIMEOUT):
            for _ in range(32):
                response = await codec.async_read_frame(reader)
                if response.cmd in commands and (sn is None or response.sn == sn):
                    return response
        raise LanProtocolError("Too many unrelated LAN responses")

    async def _connect(self, device):
        host = await self._host(device)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, TCP_PORT), REQUEST_TIMEOUT
        )
        self._writers.add(writer)
        try:
            if self._closed:
                raise LanUnavailableError("LAN client is closed")
            passcode = self._passcodes.get(device_id(device)) or device.get("passcode")
            if not passcode:
                writer.write(codec.frame(6))
                await writer.drain()
                response = await self._receive(reader, {7})
                passcode = codec.bind_passcode(response.payload)
            writer.write(codec.frame(8, codec.login_payload(passcode)))
            await writer.drain()
            response = await self._receive(reader, {9})
            if not codec.login_success(response.payload):
                raise LanUnavailableError("Device rejected LAN authentication")
            return reader, writer
        except BaseException:
            await self._close_writer(writer)
            raise

    async def _close_writer(self, writer):
        self._writers.discard(writer)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, TimeoutError):
            pass

    async def _operate(self, device, attrs=None):
        identifier = device_id(device)
        writer = None
        sent = False
        try:
            if self._closed:
                raise LanUnavailableError("LAN client is closed")
            profile = await self._profile(device)
            payload = (
                codec.encode_read(profile) if attrs is None else codec.encode_write(profile, attrs)
            )
            reader, writer = await self._connect(device)
            sn = 1
            packet = codec.frame(147, payload, sn=sn)
            # Set before write(): even a write/drain exception may follow a
            # partial send. Auto mode must not replay that physical command.
            sent = attrs is not None
            writer.write(packet)
            await writer.drain()
            response = await self._receive(reader, {148} if attrs is None else {147, 148}, sn=sn)
            if attrs is not None and response.cmd == 147:
                codec.decode_status(profile, response.payload)
            result = codec.decode_status(profile, response.payload) if attrs is None else None
            self.last_errors.pop(identifier, None)
            return result
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, EOFError, LanProtocolError, LanUnavailableError) as err:
            self.last_errors[identifier] = type(err).__name__
            if sent:
                raise LanCommandUncertainError(
                    "LAN command may have been sent; check device state before retrying"
                ) from err
            raise LanUnavailableError("LAN device could not complete the request") from err
        finally:
            if writer is not None:
                await self._close_writer(writer)

    async def async_get_status(self, device: dict[str, Any]) -> dict[str, Any]:
        return await self._operate(device)

    async def async_control(self, device: dict[str, Any], attrs: dict[str, Any]) -> None:
        await self._operate(device, attrs)

    async def async_close(self) -> None:
        self._closed = True
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        for writer in tuple(self._writers):
            await self._close_writer(writer)
