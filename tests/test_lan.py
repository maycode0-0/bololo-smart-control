"""Use real loopback UDP/TCP peers with fixed native-protocol wire fixtures."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from custom_components.bololo import lan
from custom_components.bololo.lan_errors import LanCommandUncertainError, LanUnavailableError
from custom_components.bololo.model_overrides import P30
from custom_components.bololo.schema import device_id, load_products
from custom_components.bololo.transport import HybridClient

DEVICE = {"productKey": P30, "mac": "aabbccddeeff", "did": "device-1"}
IDENTIFIER = device_id(DEVICE)


def peer_frame(command, payload=b"", sequence=None):
    body = b"\x00" + command.to_bytes(2, "big")
    if sequence is not None:
        body += sequence.to_bytes(4, "big")
    body += payload
    assert len(body) < 128
    return b"\x00\x00\x00\x03" + bytes([len(body)]) + body


async def peer_read(reader):
    assert await reader.readexactly(4) == b"\x00\x00\x00\x03"
    length = (await reader.readexactly(1))[0]
    assert length < 128
    return await reader.readexactly(length)


def peer_identity(mac="aabbccddeeff"):
    fields = [b"device-1", mac.encode(), b"1.0", P30.encode()]
    return peer_frame(4, b"".join(len(f).to_bytes(2, "big") + f for f in fields) + bytes(8))


@asynccontextmanager
async def local_device(monkeypatch, *, action="read", login_ok=True, mac="aabbccddeeff"):
    errors = []
    requests = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            # These expected bytes were derived independently from native code,
            # rather than calling the codec used by the client.
            requests.append(await peer_read(reader))
            assert requests[-1] == bytes.fromhex("000006")
            writer.write(peer_frame(7, b"\x00\x04test"))
            await writer.drain()
            requests.append(await peer_read(reader))
            assert requests[-1] == bytes.fromhex("0000080004") + b"test"
            writer.write(peer_frame(9, b"\x00" if login_ok else b"\x01"))
            await writer.drain()
            if not login_ok:
                assert await reader.read() == b""
                return
            requests.append(await peer_read(reader))
            if action == "read":
                assert requests[-1] == bytes.fromhex("0000930000000112ffffffff")
                response = peer_frame(148, bytes.fromhex("1300018001012d2a"), 1)
                # Exercise TCP fragmentation across magic, length and body.
                for fragment in (response[:2], response[2:7], response[7:]):
                    writer.write(fragment)
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                assert requests[-1] == bytes.fromhex("0000930000000111000080002d")
                if action == "write":
                    writer.write(peer_frame(148, sequence=1))
                    await writer.drain()
                elif action == "report":
                    writer.write(peer_frame(147, bytes.fromhex("13000080002d"), 1))
                    await writer.drain()
                elif action == "wrong_sequence":
                    writer.write(peer_frame(148, sequence=99))
                    await writer.drain()
            assert await reader.read() == b""
        except (AssertionError, Exception) as err:
            errors.append(err)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    class DiscoveryPeer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, address):
            if data == bytes.fromhex("0000000303000003"):
                self.transport.sendto(peer_identity(mac), address)

    udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        DiscoveryPeer, local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(lan, "TCP_PORT", port)
    monkeypatch.setattr(lan, "DISCOVERY_PORT", udp.get_extra_info("sockname")[1])
    monkeypatch.setattr(lan, "REPORT_PORT", 0)
    monkeypatch.setattr(lan, "DISCOVERY_TIMEOUT", 0.04)
    monkeypatch.setattr(lan, "REQUEST_TIMEOUT", 0.08)
    client = lan.BololoLanClient(load_products(), hosts={IDENTIFIER: "127.0.0.1"})
    try:
        yield client, requests
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()
        udp.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(tasks)), 1)
        assert not errors


async def test_udp_identity_tcp_auth_and_fragmented_status(monkeypatch):
    async with local_device(monkeypatch) as (client, requests):
        assert await client.async_get_status(DEVICE) == {
            "switch": True,
            "temp_set": 45,
            "temp_current": 42,
        }
        assert len(requests) == 3
        assert client.last_errors == {}


@pytest.mark.parametrize("action", ["write", "report"])
async def test_real_socket_write_ack_avoids_cloud(monkeypatch, action):
    async with local_device(monkeypatch, action=action) as (client, _):
        cloud = AsyncMock()
        hybrid = HybridClient(cloud, client, cached_devices=[DEVICE])
        await hybrid.async_control(DEVICE, {"temp_set": 45})
        cloud.async_control.assert_not_called()
        assert hybrid.last_transport[IDENTIFIER] == "lan"


@pytest.mark.parametrize("action", ["silent", "wrong_sequence"])
async def test_sent_write_timeout_is_never_replayed_in_cloud(monkeypatch, action):
    async with local_device(monkeypatch, action=action) as (client, requests):
        cloud = AsyncMock()
        hybrid = HybridClient(cloud, client, cached_devices=[DEVICE])
        with pytest.raises(LanCommandUncertainError):
            await hybrid.async_control(DEVICE, {"temp_set": 45})
        assert len(requests) == 3
        cloud.async_control.assert_not_called()


async def test_rejected_login_falls_back_before_physical_write(monkeypatch):
    async with local_device(monkeypatch, login_ok=False) as (client, requests):
        cloud = AsyncMock()
        await HybridClient(cloud, client).async_control(DEVICE, {"temp_set": 45})
        assert len(requests) == 2
        cloud.async_control.assert_awaited_once_with(DEVICE, {"temp_set": 45})


async def test_wrong_device_at_manual_ip_is_never_connected(monkeypatch):
    async with local_device(monkeypatch, mac="112233445566") as (client, requests):
        with pytest.raises(LanUnavailableError):
            await client.async_control(DEVICE, {"temp_set": 45})
        assert requests == []


async def test_closed_client_cannot_send(monkeypatch):
    async with local_device(monkeypatch) as (client, requests):
        await client.async_close()
        with pytest.raises(LanUnavailableError):
            await client.async_control(DEVICE, {"temp_set": 45})
        assert requests == []
