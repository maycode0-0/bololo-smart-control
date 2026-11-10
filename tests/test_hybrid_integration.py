"""Check LAN startup, persisted inventory and options against real HA classes."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.bololo import async_setup_entry, async_unload_entry
from custom_components.bololo.api import BololoAuthError, BololoConnectionError
from custom_components.bololo.config_flow import BololoOptionsFlow
from custom_components.bololo.const import DOMAIN
from custom_components.bololo.coordinator import BololoCoordinator, DeviceState
from custom_components.bololo.diagnostics import async_get_config_entry_diagnostics
from custom_components.bololo.inventory import minimal_inventory
from custom_components.bololo.schema import Datapoint, Product, device_id

PRODUCT = Product("wifi", "Test warmer", "wifi", (Datapoint("switch", "Power", "bool", True),))
DEVICE = {"productKey": "wifi", "mac": "AA:BB", "name": "Kitchen warmer"}
OTHER_DEVICE = {"productKey": "wifi", "mac": "CC:DD", "name": "Second warmer"}


class HybridSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(str(Path(__file__).parent))
        self.hass.config_entries = MagicMock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        self.entry = SimpleNamespace(
            entry_id="test-hybrid",
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            data={"username": "test", "password": "secret", "devices": [DEVICE]},
            options={},
            async_on_unload=Mock(),
            add_update_listener=Mock(),
        )
        self.lan = SimpleNamespace(
            async_get_status=AsyncMock(return_value={"switch": True}),
            async_control=AsyncMock(),
            async_close=AsyncMock(),
        )
        self.cloud = SimpleNamespace(
            async_login=AsyncMock(side_effect=BololoConnectionError("no internet")),
            async_get_devices=AsyncMock(side_effect=BololoConnectionError("no internet")),
            async_get_status=AsyncMock(side_effect=BololoConnectionError("no internet")),
        )
        self.store = SimpleNamespace(
            async_load=AsyncMock(return_value={"devices": [DEVICE]}), async_save=AsyncMock()
        )

    async def asyncTearDown(self):
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if coordinator is not None:
            await async_unload_entry(self.hass, self.entry)

    async def setup(self):
        with (
            patch("custom_components.bololo.Store", return_value=self.store),
            patch(
                "custom_components.bololo.BololoClient", return_value=self.cloud
            ) as cloud_factory,
            patch("custom_components.bololo.async_get_clientsession") as session_factory,
            patch("custom_components.bololo._create_lan_client", return_value=self.lan),
            patch("custom_components.bololo.load_products", return_value={"wifi": PRODUCT}),
        ):
            await async_setup_entry(self.hass, self.entry)
        return cloud_factory, session_factory

    async def test_auto_restarts_with_cached_inventory_without_cloud_login(self):
        await self.setup()
        self.cloud.async_login.assert_not_awaited()
        self.cloud.async_get_devices.assert_not_awaited()
        self.cloud.async_get_status.assert_not_awaited()
        coordinator = self.hass.data[DOMAIN][self.entry.entry_id]
        self.assertTrue(coordinator.data[device_id(DEVICE)].available)
        self.assertEqual(coordinator.data[device_id(DEVICE)].transport, "lan")
        self.store.async_save.assert_not_awaited()

    async def test_lan_only_never_constructs_cloud_client_or_session(self):
        self.entry.options = {"connection_mode": "lan"}
        cloud_factory, session_factory = await self.setup()
        cloud_factory.assert_not_called()
        session_factory.assert_not_called()
        self.lan.async_get_status.assert_awaited_once_with(DEVICE)
        await async_unload_entry(self.hass, self.entry)
        self.lan.async_close.assert_awaited_once()

    async def test_empty_store_does_not_resurrect_removed_bootstrap_devices(self):
        self.entry.options = {"connection_mode": "lan"}
        self.store.async_load.return_value = {"devices": []}
        await self.setup()
        self.assertEqual(self.hass.data[DOMAIN][self.entry.entry_id].data, {})
        self.lan.async_get_status.assert_not_awaited()

    async def test_first_start_persists_only_minimal_metadata(self):
        self.entry.options = {"connection_mode": "lan"}
        self.store.async_load.return_value = None
        self.entry.data["devices"] = [{**DEVICE, "token": "secret", "status": {"switch": True}}]
        await self.setup()
        self.store.async_save.assert_awaited_once_with({"devices": [DEVICE]})

    async def test_failed_platform_setup_closes_lan_and_removes_runtime(self):
        self.hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("platform")
        with self.assertRaisesRegex(RuntimeError, "platform"):
            await self.setup()
        self.lan.async_close.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])

    async def test_failed_first_refresh_closes_lan(self):
        with patch.object(
            BololoCoordinator,
            "async_config_entry_first_refresh",
            new=AsyncMock(side_effect=ConfigEntryNotReady("retry")),
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await self.setup()
        self.lan.async_close.assert_awaited_once()


class MixedCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_auth_failure_keeps_successful_lan_device_usable(self):
        hass = HomeAssistant(str(Path(__file__).parent))
        entry = SimpleNamespace(async_start_reauth=Mock(), async_on_unload=Mock())

        async def status(device):
            if device == OTHER_DEVICE:
                raise BololoAuthError("expired")
            return {"switch": True}

        client = SimpleNamespace(
            async_get_devices=AsyncMock(return_value=[DEVICE, OTHER_DEVICE]),
            async_get_status=AsyncMock(side_effect=status),
            last_transport={device_id(DEVICE): "lan"},
        )
        coordinator = BololoCoordinator(hass, entry, client, {"wifi": PRODUCT})
        try:
            for _ in range(2):
                coordinator.data = await coordinator._async_update_data()
            self.assertTrue(coordinator.data[device_id(DEVICE)].available)
            self.assertEqual(coordinator.data[device_id(DEVICE)].transport, "lan")
            self.assertFalse(coordinator.data[device_id(OTHER_DEVICE)].available)
            self.assertEqual(coordinator.data[device_id(OTHER_DEVICE)].error_kind, "auth")
            entry.async_start_reauth.assert_called_once_with(hass)
        finally:
            await coordinator.async_shutdown()

    async def test_diagnostics_excludes_network_and_account_secrets(self):
        entry = SimpleNamespace(entry_id="test")
        device = {
            **DEVICE,
            "host": "192.168.1.23",
            "passcode": "local-secret",
            "token": "token-secret",
        }
        coordinator = SimpleNamespace(
            client=SimpleNamespace(
                mode="auto",
                inventory_error=BololoAuthError("token-secret"),
                lan=SimpleNamespace(last_errors={device_id(device): "LanUnavailableError"}),
            ),
            last_update_success=True,
            unsupported={},
            data={device_id(device): DeviceState(device, PRODUCT, {"switch": True}, True, "lan")},
        )
        hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: coordinator}})
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual(diagnostics["devices"][0]["active_transport"], "lan")
        self.assertEqual(diagnostics["devices"][0]["lan_last_error"], "LanUnavailableError")
        self.assertEqual(diagnostics["inventory_refresh_error"], "BololoAuthError")
        text = json.dumps(diagnostics)
        for secret in ("192.168.1.23", "local-secret", "token-secret", "AA:BB", "Kitchen warmer"):
            self.assertNotIn(secret, text)


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, options=None):
        flow = BololoOptionsFlow()
        flow.hass = MagicMock()
        flow.hass.data = {}
        flow.handler = "test"
        flow.hass.config_entries.async_get_known_entry.return_value = SimpleNamespace(
            entry_id="test", data={"devices": [DEVICE]}, options=options or {}
        )
        return flow

    async def test_mode_change_does_not_require_network(self):
        flow = self.make_flow()
        result = await flow.async_step_init({"connection_mode": "cloud"})
        self.assertEqual(result["data"], {"connection_mode": "cloud"})

    async def test_saves_device_override_with_mode_and_preserves_other_device(self):
        other_id = device_id(OTHER_DEVICE)
        flow = self.make_flow({"lan_hosts": {other_id: "192.168.1.3"}})
        result = await flow.async_step_init(
            {"connection_mode": "auto", "device": device_id(DEVICE)}
        )
        self.assertEqual(result["step_id"], "device")
        self.assertEqual(result["description_placeholders"], {"device": "Kitchen warmer"})
        result = await flow.async_step_device({"host": "192.168.1.2", "passcode": "local-passcode"})
        self.assertEqual(result["data"]["connection_mode"], "auto")
        self.assertEqual(
            result["data"]["lan_hosts"], {device_id(DEVICE): "192.168.1.2", other_id: "192.168.1.3"}
        )
        self.assertEqual(result["data"]["lan_passcodes"], {device_id(DEVICE): "local-passcode"})

    async def test_empty_fields_clear_overrides(self):
        identifier = device_id(DEVICE)
        flow = self.make_flow(
            {"lan_hosts": {identifier: "192.168.1.2"}, "lan_passcodes": {identifier: "secret"}}
        )
        await flow.async_step_init({"connection_mode": "lan", "device": identifier})
        result = await flow.async_step_device({"host": "", "passcode": ""})
        self.assertEqual(
            result["data"], {"connection_mode": "lan", "lan_hosts": {}, "lan_passcodes": {}}
        )

    async def test_rejects_hostname_and_ipv6(self):
        flow = self.make_flow()
        await flow.async_step_init({"connection_mode": "lan", "device": device_id(DEVICE)})
        for host in ("example.com", "::1", "192.168.1.300"):
            result = await flow.async_step_device({"host": host})
            self.assertEqual(result["type"], "form")
            self.assertEqual(result["errors"], {"host": "invalid_host"})


class MinimalInventoryTests(unittest.TestCase):
    def test_filters_secrets_invalid_entries_and_duplicates(self):
        inventory = minimal_inventory(
            [
                {**DEVICE, "token": "secret", "status": {"switch": True}, "passcode": "local"},
                None,
                {"productKey": "wifi", "mac": None},
                {**OTHER_DEVICE, "sno": 42, "did": False},
            ]
        )
        self.assertEqual(inventory, [{**DEVICE, "passcode": "local"}, {**OTHER_DEVICE, "sno": 42}])
        self.assertEqual(minimal_inventory([DEVICE, DEVICE]), [DEVICE])
