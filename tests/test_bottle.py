"""Bottle marking requires physical confirmations and never repeats a failed save."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.bololo.api import BololoConnectionError
from custom_components.bololo.bottle import BottleOptionsMixin
from custom_components.bololo.const import DOMAIN
from custom_components.bololo.model_overrides import P30


class BottleFlow(BottleOptionsMixin):
    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


class BottleFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = {"productKey": P30, "mac": "AA:BB", "name": "Kitchen P30"}
        self.state = SimpleNamespace(
            product=SimpleNamespace(key=P30, name="P30"),
            device=self.device,
            available=True,
            attrs={"new_bottles": 0, "bottle_weight": 0},
        )
        self.cloud = SimpleNamespace(
            async_get_babies=AsyncMock(return_value=[{"id": 12, "name": "Baby"}]),
            async_get_bottles=AsyncMock(return_value=[]),
            async_create_bottle=AsyncMock(),
        )
        self.coordinator = SimpleNamespace(
            client=SimpleNamespace(mode="auto", cloud=self.cloud),
            data={"p30": self.state},
            last_update_success=True,
            async_request_refresh=AsyncMock(),
            async_bottle_command=AsyncMock(),
        )
        self.flow = BottleFlow()
        self.flow.config_entry = SimpleNamespace(
            entry_id="entry", options={"connection_mode": "auto", "lan_hosts": {"p30": "1.2.3.4"}}
        )
        self.flow.hass = SimpleNamespace(data={DOMAIN: {"entry": self.coordinator}})

    async def start(self):
        result = await self.flow.async_step_add_bottle({"device": "p30"})
        self.assertEqual(result["step_id"], "bottle_details")
        return await self.flow.async_step_bottle_details({"baby": "12", "name": "奶瓶1"})

    async def measure(self):
        await self.start()
        await self.flow.async_step_bottle_empty_tray({"confirm": True})
        await self.flow.async_step_bottle_place({"confirm": True})
        self.state.attrs = {"new_bottles": 3, "bottle_weight": 1234}
        return await self.flow.async_step_bottle_result({"confirm": True})

    async def test_full_flow_preserves_options_and_unlock_preference(self):
        self.assertEqual((await self.start())["step_id"], "bottle_empty_tray")
        self.coordinator.async_bottle_command.assert_awaited_once_with(
            "p30", {"bottle_tag": 0, "new_bottles": 0}
        )
        await self.flow.async_step_bottle_empty_tray()
        self.assertEqual(self.coordinator.async_bottle_command.await_count, 1)
        await self.flow.async_step_bottle_empty_tray({"confirm": True})
        self.coordinator.async_bottle_command.assert_awaited_with("p30", {"bottle_tag": 1})
        await self.flow.async_step_bottle_place({"confirm": True})
        self.coordinator.async_bottle_command.assert_awaited_with("p30", {"bottle_tag": 2})
        self.state.attrs = {"new_bottles": 3, "bottle_weight": 1234}
        result = await self.flow.async_step_bottle_result({"confirm": True})
        self.assertEqual(result["step_id"], "bottle_save")
        self.cloud.async_create_bottle.assert_not_awaited()
        result = await self.flow.async_step_bottle_save({"confirm": True})
        self.cloud.async_create_bottle.assert_awaited_once_with(
            {"name": "奶瓶1", "icon": "", "babyId": 12, "num": 3, "bottleWeight": 1234}
        )
        self.assertEqual(result["data"], self.flow.config_entry.options)
        for call in self.coordinator.async_bottle_command.await_args_list:
            self.assertNotIn("auto_unlock", call.args[1])

    async def test_lan_only_never_contacts_cloud(self):
        self.coordinator.client.mode = "lan"
        result = await self.flow.async_step_add_bottle({"device": "p30"})
        self.assertEqual(result["reason"], "bottle_cloud_required")
        self.cloud.async_get_babies.assert_not_awaited()

    async def test_invalid_device_baby_and_name_do_not_send_commands(self):
        result = await self.flow.async_step_add_bottle({"device": "unknown"})
        self.assertEqual(result["errors"], {"device": "bottle_invalid_device"})
        await self.flow.async_step_add_bottle({"device": "p30"})
        result = await self.flow.async_step_bottle_details({"baby": "99", "name": "12345678"})
        self.assertEqual(
            result["errors"], {"name": "bottle_invalid_name", "baby": "bottle_invalid_baby"}
        )
        self.coordinator.async_bottle_command.assert_not_awaited()

    async def test_requires_existing_baby(self):
        self.cloud.async_get_babies.return_value = []
        result = await self.flow.async_step_add_bottle({"device": "p30"})
        self.assertEqual(result["reason"], "bottle_no_baby")

    async def test_pending_result_rechecks_without_repeating_weighing(self):
        await self.start()
        await self.flow.async_step_bottle_empty_tray({"confirm": True})
        await self.flow.async_step_bottle_place({"confirm": True})
        calls = self.coordinator.async_bottle_command.await_count
        for _ in range(2):
            result = await self.flow.async_step_bottle_result({"confirm": True})
            self.assertEqual(result["errors"], {"base": "bottle_result_pending"})
        self.assertEqual(self.coordinator.async_bottle_command.await_count, calls)

    async def test_stale_previous_number_does_not_complete_new_registration(self):
        await self.start()
        await self.flow.async_step_bottle_empty_tray({"confirm": True})
        self.state.attrs = {"new_bottles": 5, "bottle_weight": 1234}
        result = await self.flow.async_step_bottle_place({"confirm": True})
        self.assertEqual(result["errors"], {"base": "bottle_reset_pending"})
        self.assertEqual(self.coordinator.async_bottle_command.await_count, 2)

    async def test_failed_or_invalid_measurement_never_saves(self):
        await self.start()
        for number, weight, error in [
            (254, 1234, "bottle_registration_rejected"),
            (255, 1234, "bottle_too_light"),
            (3, float("nan"), "bottle_result_pending"),
            (3, 0, "bottle_result_pending"),
            (float("inf"), 1234, "bottle_result_pending"),
            (1.5, 1234, "bottle_result_pending"),
            (True, 1234, "bottle_result_pending"),
            (3, True, "bottle_result_pending"),
        ]:
            self.state.attrs = {"new_bottles": number, "bottle_weight": weight}
            result = await self.flow.async_step_bottle_result({"confirm": True})
            self.assertEqual(result["errors"], {"base": error})
        self.cloud.async_create_bottle.assert_not_awaited()

    async def test_cloud_timeout_is_never_replayed(self):
        await self.measure()
        self.cloud.async_create_bottle.side_effect = BololoConnectionError("timeout")
        for _ in range(2):
            result = await self.flow.async_step_bottle_save({"confirm": True})
            self.assertEqual(result["errors"], {"base": "bottle_save_unknown"})
        self.cloud.async_create_bottle.assert_awaited_once()
        result = await self.flow.async_step_bottle_save({"cancel": True})
        self.assertEqual(result["type"], "create_entry")
        self.coordinator.async_bottle_command.assert_awaited_with(
            "p30", {"bottle_tag": 0, "new_bottles": 0}
        )

    async def test_device_command_failure_aborts_without_retry(self):
        self.coordinator.async_bottle_command.side_effect = HomeAssistantError("timeout")
        result = await self.start()
        self.assertEqual(result["reason"], "bottle_command_uncertain")
        self.coordinator.async_bottle_command.assert_awaited_once()

    async def test_failed_reset_reports_failure(self):
        await self.start()
        self.coordinator.async_bottle_command.side_effect = HomeAssistantError("timeout")
        result = await self.flow.async_step_bottle_empty_tray({"cancel": True})
        self.assertEqual(result["reason"], "bottle_reset_failed")
