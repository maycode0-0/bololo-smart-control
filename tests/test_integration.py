"""Exercise integration behavior against the installed Home Assistant APIs."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bololo.api import (
    BololoAuthError,
    BololoCaptchaRequired,
    BololoConnectionError,
    BololoRateLimitError,
)
from custom_components.bololo.config_flow import BololoConfigFlow
from custom_components.bololo.const import DOMAIN
from custom_components.bololo.coordinator import BololoCoordinator, DeviceState
from custom_components.bololo.entity import async_setup_datapoints
from custom_components.bololo.model_overrides import Z60
from custom_components.bololo.number import BololoNumber
from custom_components.bololo.schema import Datapoint, Product, device_id, load_products
from custom_components.bololo.select import BololoSelect
from custom_components.bololo.switch import BololoSwitch

POWER = Datapoint("switch", "Power", "bool", True)
TEMP = Datapoint("temp_set", "Target temperature", "uint8", True, minimum=37, maximum=95)
MODE = Datapoint("pattern", "Water source", "enum", True, options=("tap_water", "pure_water"))
WIFI_PRODUCT = Product("wifi", "Test warmer", "wifi", (POWER, TEMP, MODE))
BLE_PRODUCT = Product("ble", "Test pump", "ble", (POWER,))
DEVICE = {"productKey": "wifi", "mac": "AA:BB", "name": "Kitchen warmer"}
DEVICE_2 = {"productKey": "wifi", "mac": "CC:DD", "name": "Second warmer"}


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(str(Path(__file__).parent))
        self.entry = SimpleNamespace(
            entry_id="test-entry", async_on_unload=Mock(), async_start_reauth=Mock()
        )
        self.client = SimpleNamespace(
            async_get_devices=AsyncMock(return_value=[DEVICE]),
            async_get_status=AsyncMock(return_value={"switch": True, "temp_set": 40, "pattern": 0}),
            async_control=AsyncMock(),
        )
        self.coordinator = BololoCoordinator(
            self.hass, self.entry, self.client, {"wifi": WIFI_PRODUCT, "ble": BLE_PRODUCT}
        )

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    async def populate(self):
        self.coordinator.data = await self.coordinator._async_update_data()

    async def test_ble_and_unknown_devices_are_excluded_from_cloud_polls(self):
        self.client.async_get_devices.return_value = [
            DEVICE,
            {"productKey": "ble", "mac": "11:22"},
            {"productKey": "future", "mac": "33:44"},
        ]
        await self.populate()
        self.assertEqual(set(self.coordinator.data), {device_id(DEVICE)})
        self.client.async_get_status.assert_awaited_once_with(DEVICE)
        self.assertEqual(self.coordinator.unsupported, {"ble": "ble", "future": "unknown"})

    async def test_device_failure_preserves_cache_but_marks_unavailable(self):
        await self.populate()
        self.client.async_get_devices.return_value = [DEVICE, DEVICE_2]

        async def status(device):
            if device == DEVICE:
                raise BololoConnectionError("offline")
            return {"switch": False}

        self.client.async_get_status.side_effect = status
        await self.populate()
        old = self.coordinator.data[device_id(DEVICE)]
        other = self.coordinator.data[device_id(DEVICE_2)]
        self.assertFalse(old.available)
        self.assertEqual(old.attrs["temp_set"], 40)
        self.assertTrue(other.available)

    async def test_authentication_failure_starts_ha_reauth_path(self):
        self.client.async_get_status.side_effect = BololoAuthError("expired")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._async_update_data()

    async def test_list_network_failure_is_updatefailed(self):
        self.client.async_get_devices.side_effect = BololoConnectionError("timeout")
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_write_validates_range_before_network(self):
        await self.populate()
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_write(device_id(DEVICE), TEMP, 96)
        self.client.async_control.assert_not_awaited()

    async def test_write_enum_preserves_cloud_enum_string_and_refreshes(self):
        await self.populate()
        self.coordinator.async_request_refresh = AsyncMock()
        await self.coordinator.async_write(device_id(DEVICE), MODE, "pure_water")
        self.client.async_control.assert_awaited_once_with(DEVICE, {"pattern": "pure_water"})
        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_write_rejected_when_device_removed_or_offline(self):
        await self.populate()
        self.coordinator.data[device_id(DEVICE)].available = False
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_write(device_id(DEVICE), POWER, True)
        self.coordinator.data = {}
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_write(device_id(DEVICE), POWER, True)
        self.client.async_control.assert_not_awaited()

    async def test_write_auth_error_schedules_reauth_without_replay(self):
        await self.populate()
        self.client.async_control.side_effect = BololoAuthError("expired")
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_write(device_id(DEVICE), POWER, True)
        self.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.client.async_control.assert_awaited_once()

    async def test_z60_start_mode_cancels_prior_mode_in_single_request(self):
        products = await self.hass.async_add_executor_job(load_products)
        product = products[Z60]
        device = {"productKey": Z60, "mac": "AA:BB"}
        identifier = device_id(device)
        points = {point.key: point for point in product.datapoints}
        self.coordinator.data = {
            identifier: DeviceState(
                device,
                product,
                {"auto_switch": True, "disinfection_switch": False, "dry_switch": False},
                True,
            )
        }
        self.coordinator.async_request_refresh = AsyncMock()
        await self.coordinator.async_write(identifier, points["dry_switch"], True)
        self.client.async_control.assert_awaited_once_with(
            device, {"dry_switch": True, "auto_switch": False, "disinfection_switch": False}
        )
        # Never speculate that the command was actually applied to the appliance.
        self.assertTrue(self.coordinator.data[identifier].attrs["auto_switch"])
        self.assertFalse(self.coordinator.data[identifier].attrs["dry_switch"])

    async def test_z60_turning_mode_off_does_not_start_or_stop_other_modes(self):
        product = (await self.hass.async_add_executor_job(load_products))[Z60]
        device = {"productKey": Z60, "mac": "AA:BB"}
        identifier = device_id(device)
        points = {point.key: point for point in product.datapoints}
        self.coordinator.data = {
            identifier: DeviceState(device, product, {"auto_switch": True}, True)
        }
        self.coordinator.async_request_refresh = AsyncMock()
        await self.coordinator.async_write(identifier, points["dry_switch"], False)
        self.client.async_control.assert_awaited_once_with(device, {"dry_switch": False})

    async def test_entities_decode_values_and_report_missing_data_unavailable(self):
        await self.populate()
        switch = BololoSwitch(self.coordinator, device_id(DEVICE), POWER)
        number = BololoNumber(self.coordinator, device_id(DEVICE), TEMP)
        select = BololoSelect(self.coordinator, device_id(DEVICE), MODE)
        self.assertTrue(switch.available)
        self.assertTrue(switch.is_on)
        self.assertEqual(number.native_value, 40)
        self.assertEqual(number.native_unit_of_measurement, "°C")
        self.assertEqual(select.current_option, "tap_water")
        self.assertEqual(select.options, ["tap_water", "pure_water"])
        self.assertEqual(switch.device_info["name"], "Kitchen warmer")
        del self.coordinator.data[device_id(DEVICE)].attrs["temp_set"]
        self.assertFalse(number.available)
        self.coordinator.last_update_success = False
        self.assertFalse(switch.available)

    async def test_discovery_adds_new_devices_once(self):
        await self.populate()
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.coordinator}
        self.coordinator.async_add_listener = Mock(return_value=Mock())
        add = Mock()
        async_setup_datapoints(self.hass, self.entry, add, "switch", BololoSwitch)
        self.assertEqual(len(add.call_args.args[0]), 1)
        discover = self.coordinator.async_add_listener.call_args.args[0]
        discover()
        self.assertEqual(add.call_count, 1)
        self.coordinator.data[device_id(DEVICE_2)] = DeviceState(
            DEVICE_2, WIFI_PRODUCT, {"switch": False}, True
        )
        discover()
        self.assertEqual(add.call_count, 2)
        self.assertEqual(len(add.call_args.args[0]), 1)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, source="user", entry=None):
        flow = BololoConfigFlow()
        flow.hass = MagicMock()
        flow.hass.data = {"http.auth": "test-flow-refresh-id"}
        flow.handler = DOMAIN
        flow.flow_id = "test-flow"
        flow.context = {"source": source}
        if entry is not None:
            if not hasattr(entry, "title"):
                entry.title = "BOLOLO account"
            flow.context["entry_id"] = entry.entry_id
        flow.hass.config_entries.flow.async_progress_by_handler.return_value = []
        flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = None
        flow.hass.config_entries.async_get_known_entry.return_value = entry
        return flow

    async def test_user_flow_creates_one_account_entry(self):
        flow = self.make_flow()
        client = SimpleNamespace(
            uid="account-id",
            auth={"token": "test-session", "uid": "account-id"},
            async_login=AsyncMock(),
            async_get_devices=AsyncMock(return_value=[]),
        )
        with (
            patch("custom_components.bololo.config_flow.async_get_clientsession"),
            patch("custom_components.bololo.config_flow.BololoClient", return_value=client),
        ):
            result = await flow.async_step_user(
                {"username": " user ", "password": "secret", "country_code": "+86"}
            )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["username"], "user")
        self.assertEqual(result["data"]["country_code"], "86")
        self.assertEqual(flow.unique_id, "account-id")

    async def test_invalid_password_keeps_form_and_reports_invalid_auth(self):
        flow = self.make_flow()
        client = SimpleNamespace(async_login=AsyncMock(side_effect=BololoAuthError("invalid")))
        with (
            patch("custom_components.bololo.config_flow.async_get_clientsession"),
            patch("custom_components.bololo.config_flow.BololoClient", return_value=client),
        ):
            result = await flow.async_step_user(
                {"username": "user", "password": "bad", "country_code": "86"}
            )
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_reauth_updates_existing_entry_instead_of_adding_duplicate(self):
        entry = SimpleNamespace(
            entry_id="entry-1",
            unique_id="account-id",
            data={"username": "user", "password": "old", "country_code": "86"},
        )
        flow = self.make_flow("reauth", entry)
        client = SimpleNamespace(
            uid="account-id",
            auth={"token": "test-session", "uid": "account-id"},
            async_login=AsyncMock(),
            async_get_devices=AsyncMock(return_value=[]),
        )
        with (
            patch("custom_components.bololo.config_flow.async_get_clientsession"),
            patch("custom_components.bololo.config_flow.BololoClient", return_value=client),
        ):
            result = await flow.async_step_user(
                {"username": "user", "password": "new", "country_code": "86"}
            )
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(
            flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]["password"], "new"
        )
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")

    async def test_reauth_different_account_is_rejected(self):
        entry = SimpleNamespace(entry_id="entry-1", unique_id="account-id", data={})
        flow = self.make_flow("reauth", entry)
        client = SimpleNamespace(
            uid="different-id",
            auth={"token": "test-session", "uid": "different-id"},
            async_login=AsyncMock(),
            async_get_devices=AsyncMock(return_value=[]),
        )
        with (
            patch("custom_components.bololo.config_flow.async_get_clientsession"),
            patch("custom_components.bololo.config_flow.BololoClient", return_value=client),
        ):
            with self.assertRaises(AbortFlow) as context:
                await flow.async_step_user(
                    {"username": "other", "password": "secret", "country_code": "86"}
                )
        self.assertEqual(context.exception.reason, "unique_id_mismatch")

    def sms_client(self):
        return SimpleNamespace(
            uid="account-id",
            auth={"token": "session", "refresh_token": "refresh", "uid": "account-id"},
            async_request_sms=AsyncMock(),
            async_login_sms=AsyncMock(),
            async_get_devices=AsyncMock(return_value=[]),
            async_get_captcha=AsyncMock(
                return_value={"id": "image-id", "image": "data:image/jpeg;base64,aW1hZ2U="}
            ),
        )

    async def start_sms(self, flow, client):
        with (
            patch("custom_components.bololo.config_flow.async_get_clientsession"),
            patch("custom_components.bololo.config_flow.BololoClient", return_value=client),
        ):
            return await flow.async_step_user(
                {
                    "username": "13800138000",
                    "country_code": "86",
                    "mobile_country_code": "CN",
                    "auth_method": "sms",
                }
            )

    async def test_showing_default_sms_form_does_not_send_message(self):
        flow = self.make_flow()
        with patch("custom_components.bololo.config_flow.BololoClient") as constructor:
            result = await flow.async_step_user()
        self.assertEqual(result["step_id"], "user")
        constructor.assert_not_called()
        validated = result["data_schema"]({"username": "13800138000"})
        self.assertEqual(validated["auth_method"], "sms")
        self.assertEqual(validated["mobile_country_code"], "CN")

    async def test_sms_flow_explicit_send_then_create_without_otp(self):
        flow, client = self.make_flow(), self.sms_client()
        result = await self.start_sms(flow, client)
        self.assertEqual(result["step_id"], "sms")
        client.async_request_sms.assert_awaited_once_with(None, None)
        result = await flow.async_step_sms({"code": "012345"})
        self.assertEqual(result["type"], "create_entry")
        client.async_login_sms.assert_awaited_once_with("012345")
        self.assertEqual(result["data"]["auth"], client.auth)
        self.assertEqual(result["data"]["auth_method"], "sms")
        self.assertNotIn("password", result["data"])
        self.assertNotIn("012345", repr(result["data"]))

    async def test_entered_code_is_verified_even_if_resend_checkbox_remains_checked(self):
        flow, client = self.make_flow(), self.sms_client()
        await self.start_sms(flow, client)
        flow._sent_at = float("-inf")
        result = await flow.async_step_sms({"code": "123456", "resend": True})
        self.assertEqual(result["type"], "create_entry")
        client.async_login_sms.assert_awaited_once_with("123456")
        self.assertEqual(client.async_request_sms.await_count, 1)

    async def test_invalid_otp_never_resends_and_can_be_retried(self):
        flow, client = self.make_flow(), self.sms_client()
        await self.start_sms(flow, client)
        client.async_login_sms.side_effect = [BololoAuthError("invalid"), None]
        result = await flow.async_step_sms({"code": "bad"})
        self.assertEqual(result["errors"], {"base": "invalid_code"})
        result = await flow.async_step_sms({"code": "new"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(client.async_request_sms.await_count, 1)

    async def test_device_fetch_retry_does_not_reuse_consumed_otp(self):
        flow, client = self.make_flow(), self.sms_client()
        await self.start_sms(flow, client)
        client.async_get_devices.side_effect = [BololoConnectionError("timeout"), []]
        result = await flow.async_step_sms({"code": "012345"})
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        result = await flow.async_step_sms({})
        self.assertEqual(result["type"], "create_entry")
        client.async_login_sms.assert_awaited_once()

    async def test_explicit_resend_obeys_cooldown_even_after_timeout(self):
        flow, client = self.make_flow(), self.sms_client()
        client.async_request_sms.side_effect = BololoConnectionError("timeout")
        with patch("custom_components.bololo.config_flow.time.monotonic", return_value=100):
            await self.start_sms(flow, client)
            result = await flow.async_step_sms({"resend": True})
        self.assertEqual(result["errors"], {"base": "sms_cooldown"})
        self.assertEqual(client.async_request_sms.await_count, 1)
        client.async_request_sms.side_effect = None
        with patch("custom_components.bololo.config_flow.time.monotonic", return_value=161):
            result = await flow.async_step_sms({"resend": True})
        self.assertEqual(result["step_id"], "sms")
        self.assertEqual(client.async_request_sms.await_count, 2)

    async def test_rate_limit_and_human_captcha_paths(self):
        flow, client = self.make_flow(), self.sms_client()
        client.async_request_sms.side_effect = [BololoCaptchaRequired("challenge"), None]
        result = await self.start_sms(flow, client)
        self.assertEqual(result["step_id"], "captcha")
        self.assertIn("/api/bololo/captcha/", result["description_placeholders"]["captcha_image"])
        client.async_login_sms.assert_not_awaited()
        result = await flow.async_step_captcha({"captcha_code": "ABCD"})
        self.assertEqual(result["step_id"], "sms")
        client.async_request_sms.assert_awaited_with("image-id", "ABCD")
        flow, client = self.make_flow(), self.sms_client()
        client.async_request_sms.side_effect = BololoRateLimitError("limited")
        result = await self.start_sms(flow, client)
        self.assertEqual(result["errors"], {"base": "sms_rate_limit"})

    async def test_invalid_and_expired_captcha_show_error_with_new_image(self):
        for code, error in [(1000018, "invalid_captcha"), (612, "expired_captcha")]:
            flow, client = self.make_flow(), self.sms_client()
            client.async_request_sms.side_effect = [
                BololoCaptchaRequired("challenge", code=610),
                BololoCaptchaRequired("rejected", code=code),
            ]
            first = await self.start_sms(flow, client)
            first_image = first["description_placeholders"]["captcha_image"]
            result = await flow.async_step_captcha({"captcha_code": "wrong"})
            self.assertEqual(result["step_id"], "captcha")
            self.assertEqual(result["errors"], {"base": error})
            self.assertNotEqual(result["description_placeholders"]["captcha_image"], first_image)
            self.assertEqual(client.async_get_captcha.await_count, 2)

    async def test_blank_image_code_does_not_trigger_another_sms_request(self):
        flow, client = self.make_flow(), self.sms_client()
        client.async_request_sms.side_effect = BololoCaptchaRequired("challenge", code=610)
        await self.start_sms(flow, client)
        result = await flow.async_step_captcha({"captcha_code": " "})
        self.assertEqual(result["errors"], {"base": "captcha_required"})
        client.async_request_sms.assert_awaited_once()

    async def test_sms_reauth_removes_old_password_and_keeps_lan_options(self):
        entry = SimpleNamespace(
            entry_id="entry-1",
            unique_id="account-id",
            data={
                "username": "13800138000",
                "password": "old",
                "country_code": "86",
                "connection_mode": "lan",
                "other": "kept",
            },
        )
        flow, client = self.make_flow("reauth", entry), self.sms_client()
        await self.start_sms(flow, client)
        result = await flow.async_step_sms({"code": "012345"})
        self.assertEqual(result["reason"], "reauth_successful")
        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertNotIn("password", data)
        self.assertEqual(data["connection_mode"], "lan")
        self.assertEqual(data["other"], "kept")

    async def test_sms_reauth_cannot_switch_accounts(self):
        entry = SimpleNamespace(entry_id="entry-1", unique_id="original", data={})
        flow, client = self.make_flow("reauth", entry), self.sms_client()
        await self.start_sms(flow, client)
        with self.assertRaises(AbortFlow) as error:
            await flow.async_step_sms({"code": "012345"})
        self.assertEqual(error.exception.reason, "unique_id_mismatch")
        flow.hass.config_entries.async_update_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
