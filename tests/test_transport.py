"""Test routing decisions independently from either wire implementation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bololo.api import BololoAuthError, BololoConnectionError, BololoError
from custom_components.bololo.lan_errors import (
    LanCommandUncertainError,
    LanProtocolError,
    LanUnavailableError,
)
from custom_components.bololo.schema import device_id
from custom_components.bololo.transport import HybridClient

DEVICE = {"productKey": "product", "mac": "AA:BB:CC", "name": "Example"}


@pytest.fixture
def clients():
    cloud = SimpleNamespace(
        async_get_devices=AsyncMock(return_value=[DEVICE]),
        async_get_status=AsyncMock(return_value={"switch": False}),
        async_control=AsyncMock(),
    )
    lan = SimpleNamespace(
        async_get_status=AsyncMock(return_value={"switch": True}),
        async_control=AsyncMock(),
        async_close=AsyncMock(),
    )
    return cloud, lan


async def test_auto_reads_lan_first_without_cloud_auth_dependency(clients):
    cloud, lan = clients
    cloud.async_get_status.side_effect = BololoAuthError("expired")
    client = HybridClient(cloud, lan, cached_devices=[DEVICE])
    assert await client.async_get_devices() == [DEVICE]
    assert await client.async_get_status(DEVICE) == {"switch": True}
    cloud.async_get_devices.assert_not_awaited()
    cloud.async_get_status.assert_not_awaited()
    assert client.last_transport[device_id(DEVICE)] == "lan"


@pytest.mark.parametrize("failure", [LanUnavailableError("offline"), LanProtocolError("bad frame")])
async def test_failed_lan_read_falls_back_to_cloud(clients, failure):
    cloud, lan = clients
    lan.async_get_status.side_effect = failure
    client = HybridClient(cloud, lan)
    assert await client.async_get_status(DEVICE) == {"switch": False}
    cloud.async_get_status.assert_awaited_once_with(DEVICE)
    assert client.last_transport[device_id(DEVICE)] == "cloud"


async def test_cloud_only_avoids_all_lan_calls(clients):
    cloud, lan = clients
    client = HybridClient(cloud, lan, "cloud")
    await client.async_get_devices()
    await client.async_get_status(DEVICE)
    await client.async_control(DEVICE, {"switch": True})
    lan.async_get_status.assert_not_awaited()
    lan.async_control.assert_not_awaited()
    cloud.async_control.assert_awaited_once_with(DEVICE, {"switch": True})


async def test_lan_only_never_refreshes_cloud_even_when_forced(clients):
    cloud, lan = clients
    client = HybridClient(cloud, lan, "lan", [DEVICE])
    assert await client.async_refresh_devices() == [DEVICE]
    assert await client.async_get_status(DEVICE) == {"switch": True}
    await client.async_control(DEVICE, {"switch": True})
    cloud.async_get_devices.assert_not_awaited()
    cloud.async_get_status.assert_not_awaited()
    cloud.async_control.assert_not_awaited()


async def test_lan_only_without_metadata_fails_without_using_cloud(clients):
    cloud, lan = clients
    client = HybridClient(cloud, lan, "lan")
    with pytest.raises(LanUnavailableError):
        await client.async_get_devices()
    cloud.async_get_devices.assert_not_awaited()


async def test_lan_only_failure_does_not_fall_back(clients):
    cloud, lan = clients
    lan.async_get_status.side_effect = LanProtocolError("bad response")
    lan.async_control.side_effect = LanUnavailableError("offline")
    client = HybridClient(cloud, lan, "lan", [DEVICE])
    with pytest.raises(LanProtocolError):
        await client.async_get_status(DEVICE)
    with pytest.raises(LanUnavailableError):
        await client.async_control(DEVICE, {"switch": True})
    cloud.async_get_status.assert_not_awaited()
    cloud.async_control.assert_not_awaited()


async def test_auto_command_uses_lan_when_available(clients):
    cloud, lan = clients
    client = HybridClient(cloud, lan)
    await client.async_control(DEVICE, {"switch": False})
    lan.async_control.assert_awaited_once_with(DEVICE, {"switch": False})
    cloud.async_control.assert_not_awaited()
    assert client.last_transport[device_id(DEVICE)] == "lan"


async def test_auto_command_can_fall_back_before_any_lan_control_bytes(clients):
    cloud, lan = clients
    lan.async_control.side_effect = LanUnavailableError("connection failed")
    client = HybridClient(cloud, lan)
    await client.async_control(DEVICE, {"switch": False})
    cloud.async_control.assert_awaited_once_with(DEVICE, {"switch": False})
    assert client.last_transport[device_id(DEVICE)] == "cloud"


@pytest.mark.parametrize(
    "failure", [LanCommandUncertainError("ack timed out"), LanProtocolError("rejected")]
)
async def test_ambiguous_or_rejected_commands_never_replayed_over_cloud(clients, failure):
    cloud, lan = clients
    lan.async_control.side_effect = failure
    client = HybridClient(cloud, lan)
    with pytest.raises(type(failure)):
        await client.async_control(DEVICE, {"boiling": True})
    cloud.async_control.assert_not_awaited()


async def test_programming_errors_are_not_hidden_as_transport_fallback(clients):
    cloud, lan = clients
    lan.async_get_status.side_effect = TypeError("bad internal argument")
    with pytest.raises(TypeError):
        await HybridClient(cloud, lan).async_get_status(DEVICE)
    cloud.async_get_status.assert_not_awaited()


async def test_inventory_ttl_reduces_cloud_calls_and_serializes_refresh(clients):
    cloud, lan = clients
    with patch("custom_components.bololo.transport.time.monotonic", return_value=100):
        client = HybridClient(cloud, lan)
        results = await asyncio.gather(*(client.async_get_devices() for _ in range(4)))
        assert results == [[DEVICE]] * 4
        cloud.async_get_devices.assert_awaited_once()
    with patch("custom_components.bololo.transport.time.monotonic", return_value=401):
        await client.async_get_devices()
        assert cloud.async_get_devices.await_count == 2


@pytest.mark.parametrize("failure", [BololoConnectionError("offline"), BololoAuthError("expired")])
async def test_inventory_outage_keeps_cached_devices_for_lan(clients, failure):
    cloud, lan = clients
    cloud.async_get_devices.side_effect = failure
    client = HybridClient(cloud, lan, cached_devices=[DEVICE])
    assert await client.async_refresh_devices() == [DEVICE]
    assert client.inventory_error is failure
    assert await client.async_get_status(DEVICE) == {"switch": True}
    # Retry is delayed rather than hitting cloud on every device/poll call.
    assert await client.async_get_devices() == [DEVICE]
    cloud.async_get_devices.assert_awaited_once()


async def test_initial_inventory_failure_does_not_invent_devices(clients):
    cloud, lan = clients
    cloud.async_get_devices.side_effect = BololoAuthError("invalid account")
    with pytest.raises(BololoAuthError):
        await HybridClient(cloud, lan).async_get_devices()


async def test_empty_authoritative_inventory_removes_previous_devices(clients):
    cloud, lan = clients
    cloud.async_get_devices.return_value = []
    client = HybridClient(cloud, lan, cached_devices=[DEVICE])
    client.last_transport[device_id(DEVICE)] = "lan"
    assert await client.async_refresh_devices() == []
    assert client.cached_devices == []
    assert not client.last_transport


async def test_cached_metadata_is_independent_of_caller_mutation(clients):
    cloud, lan = clients
    supplied = [{**DEVICE, "lan": {"host": "192.0.2.1"}}]
    client = HybridClient(cloud, lan, cached_devices=supplied)
    supplied[0]["lan"]["host"] = "192.0.2.2"
    view = await client.async_get_devices()
    view[0]["lan"]["host"] = "192.0.2.3"
    assert client.cached_devices[0]["lan"]["host"] == "192.0.2.1"


async def test_missing_lan_client_uses_cloud_in_auto(clients):
    cloud, _ = clients
    client = HybridClient(cloud, None)
    assert await client.async_get_status(DEVICE) == {"switch": False}
    await client.async_control(DEVICE, {"switch": True})
    cloud.async_control.assert_awaited_once()


async def test_close_owns_only_lan_resources(clients):
    cloud, lan = clients
    await HybridClient(cloud, lan).async_close()
    lan.async_close.assert_awaited_once()


@pytest.mark.parametrize("operation", ["async_get_status", "async_control"])
async def test_unload_during_lan_failure_does_not_start_cloud_fallback(clients, operation):
    cloud, lan = clients
    client = HybridClient(cloud, lan)

    async def interrupted(*args):
        await client.async_close()
        raise LanUnavailableError("Socket closed during unload")

    getattr(lan, operation).side_effect = interrupted
    arguments = (DEVICE,) if operation == "async_get_status" else (DEVICE, {"switch": True})
    with pytest.raises(BololoError, match="closed"):
        await getattr(client, operation)(*arguments)
    getattr(cloud, operation).assert_not_awaited()


def test_invalid_mode_is_rejected(clients):
    cloud, lan = clients
    with pytest.raises(ValueError):
        HybridClient(cloud, lan, "random")


async def test_invalid_inventory_response_does_not_replace_cached_metadata(clients):
    cloud, lan = clients
    cloud.async_get_devices.return_value = {"devices": [DEVICE]}
    client = HybridClient(cloud, lan, cached_devices=[DEVICE])
    assert await client.async_refresh_devices() == [DEVICE]
    assert isinstance(client.inventory_error, BololoError)
