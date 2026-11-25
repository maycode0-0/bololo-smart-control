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

from custom_components.bololo.cloud_history import CloudHistoryManager
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

    async def test_incomplete_cloud_history_does_not_publish_local_sum_as_total(self):
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
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["local_observed_total_ml"], 300)
        self.assertEqual(sensor.extra_state_attributes["sync_status"], "cloud_disabled")
        self.assertEqual(sensor.device_class, SensorDeviceClass.VOLUME)
        self.assertEqual(sensor.native_unit_of_measurement, UnitOfVolume.MILLILITERS)
        self.assertEqual(sensor.state_class, SensorStateClass.TOTAL)
        self.assertEqual(sensor.unique_id, self.identifier + "_record")
        self.assertTrue(sensor.available)
        await self.coordinator.async_shutdown()
        saved = store.async_save.call_args.args[0]["devices"]
        self.coordinator.history = OperationHistory(saved)
        await self.poll()
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["local_observed_total_ml"], 300)
        self.client.async_get_status.return_value = {"record": 60}
        await self.poll()
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["local_observed_total_ml"], 360)

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
        self.assertIsNone(total.native_value)
        self.assertEqual(history.native_value, 2)
        self.manager.summary.return_value = {
            "complete": True,
            "synced_count": 20,
            "total_volume_ml": 3200,
            "total_volume_complete": True,
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

    async def test_total_exposes_backfill_progress_instead_of_a_partial_number(self):
        self.manager.summary.return_value = {
            "complete": False,
            "total_volume_complete": False,
            "synced_count": 0,
            "staged_count": 200,
            "server_total": 350,
            "server_total_scope": "sync_window",
            "syncing": True,
            "sync_error": None,
            "records": [],
        }
        await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        self.assertIsNone(total.native_value)
        self.assertTrue(total.available)
        attributes = total.extra_state_attributes
        self.assertEqual(attributes["sync_status"], "syncing")
        self.assertEqual(attributes["staged_count"], 200)
        self.assertEqual(attributes["server_total"], 350)
        self.assertFalse(attributes["cloud_history_complete"])

    async def test_restored_partial_history_reports_cloud_disabled_in_lan_mode(self):
        store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
        manager = CloudHistoryManager(None, store, page_size=1, max_pages=1)
        self.cloud.async_get_water_history.return_value = {
            "records": [{"id": 1, "date": 1000, "waterOutput": 120}],
            "total": 2,
        }
        await manager.async_sync(self.device, self.cloud)
        store.async_load.return_value = store.async_save.call_args.args[0]
        restored = CloudHistoryManager(None, store)
        await restored.async_load()
        self.coordinator.cloud_history = restored
        self.cloud.async_get_water_history.reset_mock()
        self.client.mode = "lan"
        await self.poll()
        await asyncio.sleep(0)
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        self.assertIsNone(total.native_value)
        self.assertEqual(total.extra_state_attributes["sync_status"], "cloud_disabled")
        self.assertEqual(total.extra_state_attributes["staged_count"], 1)
        self.cloud.async_get_water_history.assert_not_awaited()
        self.client.mode = "auto"
        self.assertEqual(total.extra_state_attributes["sync_status"], "syncing")

    async def test_total_reports_missing_serial_without_falling_back_to_local_zero(self):
        self.device.pop("sno")
        self.manager.summary.return_value = {"complete": False, "records": []}
        await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        self.assertIsNone(total.native_value)
        self.assertEqual(total.extra_state_attributes["sync_status"], "missing_serial")

    async def test_complete_record_pagination_with_missing_volumes_is_not_a_complete_total(self):
        self.manager.summary.return_value = {
            "complete": True,
            "total_volume_complete": False,
            "missing_volume_count": 1,
            "total_volume_ml": 120,
            "records": [],
        }
        await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        self.assertIsNone(total.native_value)
        self.assertEqual(total.extra_state_attributes["sync_status"], "missing_volume")
        self.assertEqual(total.extra_state_attributes["missing_volume_count"], 1)

    async def test_saved_complete_total_remains_visible_when_sync_or_device_is_offline(self):
        self.manager.summary.return_value = {
            "complete": True,
            "total_volume_complete": True,
            "total_volume_ml": 3200,
            "sync_error": "BololoConnectionError",
            "last_synced": "2026-11-25T00:00:00+00:00",
            "oldest_record_date": 123,
            "newest_record_date": 456,
            "records": [],
        }
        await self.poll()
        point = next(
            p
            for p in self.coordinator.data[self.identifier].product.datapoints
            if p.key == "record"
        )
        total = BololoSensor(self.coordinator, self.identifier, point)
        self.coordinator.data[self.identifier].available = False
        self.coordinator.last_update_success = False
        self.assertTrue(total.available)
        self.assertEqual(total.native_value, 3200)
        attributes = total.extra_state_attributes
        self.assertEqual(attributes["sync_status"], "error")
        self.assertEqual(attributes["sync_error"], "BololoConnectionError")
        self.assertEqual(attributes["oldest_record_date"], 123)
        self.assertEqual(attributes["newest_record_date"], 456)

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
