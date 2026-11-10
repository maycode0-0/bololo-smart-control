"""Load and unload the actual integration and all five HA entity platforms."""

import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bololo.const import DOMAIN


@contextmanager
def isolated_config_directory():
    # Python's 0700 Windows temporary-dir ACL excludes the sandbox's test token.
    parent = Path(__file__).resolve().parent
    directory = parent / f"bololo-ha-test-{uuid4().hex}"
    directory.mkdir()
    try:
        yield str(directory)
    finally:
        if directory.resolve().parent != parent:
            raise RuntimeError("Refusing cleanup outside the test directory")
        shutil.rmtree(directory)


class FullSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_platform_forwarding_and_unload(self):
        # HA's disk writer uses POSIX fchmod; persistence is outside this test.
        with (
            isolated_config_directory() as config_dir,
            patch("homeassistant.helpers.storage.Store._async_write_data", new=AsyncMock()),
        ):
            source = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
            shutil.copytree(source, Path(config_dir) / "custom_components" / DOMAIN)
            hass = HomeAssistant(config_dir)
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await er.async_load(hass)
            await dr.async_load(hass)
            hass.auth = await auth.auth_manager_from_config(hass, [], [])
            entry = ConfigEntry(
                domain=DOMAIN,
                title="Test BOLOLO account",
                data={"username": "test", "password": "secret", "country_code": "86"},
                options={},
                unique_id="account-1",
                version=1,
                minor_version=1,
                source="user",
                discovery_keys=MappingProxyType({}),
            )
            product_key = "k1mvp9K2mCI000000000000000000000"
            device = {
                "productKey": product_key,
                "mac": "AA:BB:CC:DD:EE:FF",
                "name": "Kitchen warmer",
            }
            client = SimpleNamespace(
                uid="account-1",
                async_login=AsyncMock(),
                async_get_devices=AsyncMock(return_value=[device]),
                async_get_status=AsyncMock(
                    return_value={
                        "switch": True,
                        "temp_set": 40,
                        "temp_current": 39,
                        "pattern": "tap_water",
                        "status_time": "2026-11-10 12:00:00",
                    }
                ),
                async_control=AsyncMock(),
            )
            try:
                with (
                    patch("custom_components.bololo.BololoClient", return_value=client),
                    patch("custom_components.bololo.async_get_clientsession"),
                    patch("custom_components.bololo._create_lan_client", return_value=None),
                ):
                    await hass.config_entries.async_add(entry)
                    await hass.async_block_till_done()
                self.assertEqual(entry.state, ConfigEntryState.LOADED)
                coordinator = hass.data[DOMAIN][entry.entry_id]
                registry = er.async_get(hass)
                entries = er.async_entries_for_config_entry(registry, entry.entry_id)
                self.assertTrue(entries)
                self.assertEqual(
                    {item.domain for item in entries},
                    {"switch", "number", "select", "sensor", "binary_sensor"},
                )
                power = next(item for item in entries if item.unique_id.endswith("_switch"))
                self.assertEqual(hass.states.get(power.entity_id).state, "on")
                target = next(item for item in entries if item.unique_id.endswith("_temp_set"))
                self.assertEqual(hass.states.get(target.entity_id).state, "40.0")
                source_select = next(
                    item for item in entries if item.unique_id.endswith("_pattern")
                )
                await hass.services.async_call(
                    "switch", "turn_off", {"entity_id": power.entity_id}, blocking=True
                )
                client.async_control.assert_awaited_with(device, {"switch": False})
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": target.entity_id, "value": 41},
                    blocking=True,
                )
                client.async_control.assert_awaited_with(device, {"temp_set": 41})
                await hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": source_select.entity_id, "option": "直饮水"},
                    blocking=True,
                )
                client.async_control.assert_awaited_with(device, {"pattern": "pure_water"})
                self.assertTrue(coordinator._listeners)
                self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
                await hass.async_block_till_done()
                self.assertNotIn(entry.entry_id, hass.data[DOMAIN])
                self.assertFalse(coordinator._listeners)
                self.assertIsNone(coordinator._unsub_refresh)
            finally:
                await hass.async_stop(force=True)


if __name__ == "__main__":
    unittest.main()
