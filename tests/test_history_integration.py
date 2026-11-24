"""History sync must stay independent of LAN reads and integration shutdown."""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.bololo.coordinator import BololoCoordinator
from custom_components.bololo.history import OperationHistory
from custom_components.bololo.model_overrides import P30
from custom_components.bololo.schema import device_id, load_products
from custom_components.bololo.sensor import BololoHistorySensor, BololoSensor


class HistoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(str(Path(__file__).parent))
        self.device = {"productKey": P30, "mac": "AA:BB", "sno": "serial"}
        self.identifier = device_id(self.device)
        self.cloud = SimpleNamespace(async_get_water_history=AsyncMock())
        self.client = SimpleNamespace(
            mode="auto",
            cloud=self.cloud,
            async_get_devices=AsyncMock(return_value=[self.device]),
            async_get_status=AsyncMock(return_value={"boiling": False}),
            async_control=AsyncMock(),
            last_transport={self.identifier: "lan"},
        )
        self.manager = SimpleNamespace(
            async_sync=AsyncMock(),
            summary=Mock(return_value={"complete": True, "synced_count": 4, "records": []}),
        )
        self.coordinator = BololoCoordinator(
            self.hass,
            SimpleNamespace(entry_id="test", async_start_reauth=Mock(), async_on_unload=Mock()),
            self.client,
            load_products(),
            cloud_history=self.manager,
        )

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    async def poll(self):
        self.coordinator.data = await self.coordinator._async_update_data()

    async def test_cloud_sync_does_not_block_lan_and_is_cancelled_on_shutdown(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def sync(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.manager.async_sync.side_effect = sync
        await asyncio.wait_for(self.poll(), 1)
        await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(self.coordinator.data[self.identifier].available)
        await self.poll()
        self.assertEqual(self.manager.async_sync.await_count, 1)
        await self.coordinator.async_shutdown()
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.coordinator._history_tasks)

    async def test_lan_only_never_schedules_cloud_history(self):
        self.client.mode = "lan"
        await self.poll()
        await asyncio.sleep(0)
        self.manager.async_sync.assert_not_awaited()

    async def test_immediate_sync_cleans_task_and_can_refresh_again(self):
        await self.poll()
        await asyncio.sleep(0)
        self.assertFalse(self.coordinator._history_tasks)
        self.coordinator._history_refresh_at.clear()
        await self.poll()
        await asyncio.sleep(0)
        self.assertEqual(self.manager.async_sync.await_count, 2)

    async def test_saved_history_remains_readable_when_device_is_offline(self):
        self.client.mode = "lan"
        await self.poll()
        sensor = BololoHistorySensor(self.coordinator, self.identifier, "boil")
        cloud = BololoHistorySensor(self.coordinator, self.identifier, "cloud_water")
        self.coordinator.data[self.identifier].available = False
        self.coordinator.last_update_success = False
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 0)
        self.assertEqual(cloud.native_value, 4)
        self.assertEqual(sensor.extra_state_attributes["record_source"], "home_assistant")

    async def test_water_total_sensor_uses_persisted_sum_instead_of_latest_report(self):
        self.client.mode = "lan"
        self.manager.summary.return_value = {"complete": False, "synced_count": 0, "records": []}
        store = SimpleNamespace(async_delay_save=Mock(), async_save=AsyncMock())
        self.coordinator._history_store = store
        for volume in (0, 120, 120, 180, 180):
            self.client.async_get_status.return_value = {"record": volume}
            await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        sensor = BololoSensor(self.coordinator, self.identifier, point)
        self.assertEqual(sensor.native_value, 300)
        self.assertEqual(sensor.device_class, SensorDeviceClass.VOLUME)
        self.assertEqual(sensor.native_unit_of_measurement, UnitOfVolume.MILLILITERS)
        self.assertEqual(sensor.state_class, SensorStateClass.TOTAL)
        self.assertEqual(sensor.unique_id, self.identifier + "_record")
        self.assertTrue(sensor.available)
        await self.coordinator.async_shutdown()
        saved = store.async_save.call_args.args[0]["devices"]
        self.coordinator.history = OperationHistory(saved)
        await self.poll()
        self.assertEqual(sensor.native_value, 300)
        self.client.async_get_status.return_value = {"record": 60}
        await self.poll()
        self.assertEqual(sensor.native_value, 360)

    async def test_complete_cloud_water_history_is_authoritative_without_double_counting(self):
        self.client.mode = "lan"
        self.manager.summary.return_value = {"complete": False, "synced_count": 0, "records": []}
        for volume in (0, 120, 180):
            self.client.async_get_status.return_value = {"record": volume}
            await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        history = BololoHistorySensor(self.coordinator, self.identifier, "water")
        self.assertEqual(total.native_value, 300)
        self.assertEqual(history.native_value, 2)
        self.manager.summary.return_value = {
            "complete": True,
            "synced_count": 20,
            "total_volume_ml": 3200,
            "records": [{"id": 1, "date": 123, "waterOutput": 120}],
            "last_synced": "2026-11-24T00:00:00+00:00",
        }
        for _ in range(3):
            await self.poll()
            self.assertEqual(total.native_value, 3200)
            self.assertEqual(history.native_value, 20)
        self.assertEqual(total.extra_state_attributes["record_source"], "cloud")
        self.assertEqual(history.extra_state_attributes["record_source"], "cloud")
        self.assertEqual(history.extra_state_attributes["records"][0]["waterOutput"], 120)
        self.assertEqual(total.state_class, SensorStateClass.TOTAL)
        # Cloud corrections replace the saved result, rather than being added again.
        self.manager.summary.return_value["total_volume_ml"] = 3000
        self.assertEqual(total.native_value, 3000)

    async def test_bottle_commands_are_whitelisted_and_do_not_fake_device_state(self):
        self.client.mode = "lan"
        await self.poll()
        self.coordinator.async_request_refresh = AsyncMock()
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_bottle_command(self.identifier, {"auto_unlock": True})
        self.client.async_control.assert_not_awaited()
        await self.coordinator.async_bottle_command(self.identifier, {"bottle_tag": 1})
        self.client.async_control.assert_awaited_once_with(self.device, {"bottle_tag": 1})
        self.assertNotIn("bottle_tag", self.coordinator.data[self.identifier].attrs)
