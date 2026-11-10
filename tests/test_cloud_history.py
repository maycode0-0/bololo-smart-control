"""Cloud backfill resumes across restarts and atomically replaces edited windows."""

import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from custom_components.bololo.api import BololoAuthError, BololoConnectionError
from custom_components.bololo.cloud_history import CloudHistoryManager, _date_ms
from custom_components.bololo.schema import device_id

DEVICE = {"productKey": "p30", "mac": "AA:BB", "sno": "private-serial"}
IDENTIFIER = device_id(DEVICE)
NOW = 200_000_000


def record(identifier, date=NOW - 100, volume=120):
    return {
        "id": identifier,
        "date": date,
        "data": json.dumps({"waterOutput": volume, "token": "secret"}),
        "mac": "private-mac",
        "token": "private-token",
        "sno": "private-sno",
        "bottleName": "Bottle",
    }


class MemoryStore:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, value):
        self.data = deepcopy(value)


class CloudHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.manager = CloudHistoryManager(None, self.store, page_size=2, max_pages=1)
        self.cloud = SimpleNamespace(async_get_water_history=AsyncMock())
        self.clock = patch(
            "custom_components.bololo.cloud_history.time.time", return_value=NOW / 1000
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def sync(self, records, total=None):
        self.cloud.async_get_water_history.return_value = {
            "records": records,
            "total": len(records) if total is None else total,
        }
        await self.manager.async_sync(DEVICE, self.cloud)
        return self.manager.summary(IDENTIFIER)

    async def test_bounded_backfill_resumes_fixed_window_after_restart(self):
        first = await self.sync([record(1), record(2)], 3)
        self.assertFalse(first["complete"])
        self.assertEqual(first["staged_count"], 2)
        self.assertEqual(first["synced_count"], 0)
        self.assertEqual(self.cloud.async_get_water_history.call_args.kwargs["current"], 1)
        self.manager = CloudHistoryManager(None, self.store, page_size=9, max_pages=1)
        await self.manager.async_load()
        with patch(
            "custom_components.bololo.cloud_history.time.time", return_value=(NOW + 5000) / 1000
        ):
            final = await self.sync([record(3)], 3)
        self.assertTrue(final["complete"])
        self.assertFalse(final["syncing"])
        self.assertEqual(final["synced_count"], 3)
        self.cloud.async_get_water_history.assert_awaited_with(
            DEVICE, current=2, size=2, start_time=0, end_time=NOW
        )
        self.assertIsNotNone(final["last_synced"])

    async def test_complete_cache_survives_partial_incremental_failure(self):
        first = await self.sync([record(1), record(2)])
        self.assertTrue(first["complete"])
        await self.sync([record(1, volume=180), record(3)], 3)
        self.cloud.async_get_water_history.side_effect = BololoConnectionError("secret server text")
        await self.manager.async_sync(DEVICE, self.cloud)
        summary = self.manager.summary(IDENTIFIER)
        self.assertEqual(summary["records"], first["records"])
        self.assertEqual(summary["sync_error"], "BololoConnectionError")
        self.assertEqual(summary["staged_count"], 2)
        self.assertNotIn("secret server text", repr(self.store.data))
        self.cloud.async_get_water_history.side_effect = None
        final = await self.sync([record(4)], 3)
        self.assertEqual({item["id"] for item in final["records"]}, {1, 3, 4})
        self.assertEqual(
            next(item for item in final["records"] if item["id"] == 1)["waterOutput"], 180
        )

    async def test_incremental_replaces_overlap_and_retains_older_records(self):
        old = record(1, date=NOW - 100_000_000)
        await self.sync([old, record(2)])
        summary = await self.sync([record(3)])
        self.assertEqual({item["id"] for item in summary["records"]}, {1, 3})
        self.assertEqual(summary["server_total"], 1)
        self.assertEqual(summary["server_total_scope"], "sync_window")
        self.assertEqual(
            self.cloud.async_get_water_history.call_args.kwargs["start_time"], NOW - 86_400_000
        )

    async def test_empty_incremental_window_removes_deleted_records(self):
        await self.sync([record(1)])
        summary = await self.sync([])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["synced_count"], 0)

    async def test_duplicate_keys_within_page_restart_window(self):
        self.manager.page_size = 5
        no_id = record(9)
        no_id.pop("id")
        same = {**no_id, "token": "different ignored secret"}
        summary = await self.sync([record(1), record(1), no_id, same])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["staged_count"], 0)
        self.assertEqual(summary["sync_error"], "BololoError")
        self.assertEqual(self.store.data["devices"][IDENTIFIER]["pending"]["current"], 1)
        summary = await self.sync([record(1), no_id, same])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["staged_count"], 0)
        summary = await self.sync([record(1), no_id])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["synced_count"], 2)

    async def test_repeated_page_does_not_complete_incomplete_dataset(self):
        await self.sync([record(1), record(2)], 4)
        summary = await self.sync([record(1), record(2)], 4)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["synced_count"], 0)
        self.assertEqual(summary["staged_count"], 0)
        self.assertEqual(summary["sync_error"], "BololoError")
        await self.sync([record(1), record(2)], 4)
        self.assertEqual(self.cloud.async_get_water_history.call_args.kwargs["current"], 1)
        summary = await self.sync([record(3), record(4)], 4)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["synced_count"], 4)

    async def test_sanitizes_nested_data_and_bounds_only_exposed_details(self):
        self.manager.page_size = 100
        summary = await self.sync([record(index, date=NOW - index) for index in range(80)])
        self.assertEqual(summary["synced_count"], 80)
        self.assertEqual(len(summary["records"]), 50)
        self.assertEqual(len(self.store.data["devices"][IDENTIFIER]["records"]), 80)
        self.assertEqual(summary["records"][0]["id"], 0)
        encoded = json.dumps(self.store.data)
        for secret in ["secret", "private-mac", "private-token", "private-sno"]:
            self.assertNotIn(secret, encoded)
        summary["records"][0]["bottleName"] = "Changed by caller"
        self.assertEqual(self.manager.summary(IDENTIFIER)["records"][0]["bottleName"], "Bottle")

    async def test_malformed_page_preserves_complete_history(self):
        first = await self.sync([record(1)])
        for response in [
            {"records": [{}], "total": 1},
            {"records": [record(2)], "total": True},
            {"records": [record(2)], "total": 3},
            {"records": [{**record(2), "data": "broken JSON"}], "total": 1},
            {"records": [record(2, date=NOW + 1)], "total": 1},
        ]:
            self.cloud.async_get_water_history.return_value = response
            await self.manager.async_sync(DEVICE, self.cloud)
            summary = self.manager.summary(IDENTIFIER)
            self.assertEqual(summary["records"], first["records"])
            self.assertIsNotNone(summary["sync_error"])

    async def test_total_change_restarts_window_instead_of_skipping_records(self):
        await self.sync([record(1), record(2)], 3)
        await self.sync([record(3)], 4)
        self.assertEqual(self.manager.summary(IDENTIFIER)["staged_count"], 0)
        await self.sync([record(4)], 1)
        self.assertEqual(self.cloud.async_get_water_history.call_args.kwargs["current"], 1)
        self.assertTrue(self.manager.summary(IDENTIFIER)["complete"])

    async def test_auth_failure_is_isolated_and_visible_by_type(self):
        self.cloud.async_get_water_history.side_effect = BololoAuthError("private")
        await self.manager.async_sync(DEVICE, self.cloud)
        self.assertEqual(self.manager.summary(IDENTIFIER)["sync_error"], "BololoAuthError")
        self.manager = CloudHistoryManager(None, self.store)
        await self.manager.async_load()
        self.assertEqual(self.manager.summary(IDENTIFIER)["sync_error"], "BololoAuthError")

    async def test_cancellation_propagates_and_keeps_committed_cursor(self):
        await self.sync([record(1), record(2)], 3)
        self.cloud.async_get_water_history.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.manager.async_sync(DEVICE, self.cloud)
        self.assertEqual(self.store.data["devices"][IDENTIFIER]["pending"]["current"], 2)

    async def test_storage_failure_keeps_last_complete_cache(self):
        first = await self.sync([record(1)])
        self.store.async_save = AsyncMock(side_effect=OSError("disk full"))
        await self.sync([record(2)])
        summary = self.manager.summary(IDENTIFIER)
        self.assertEqual(summary["records"], first["records"])
        self.assertEqual(summary["sync_error"], "OSError")

    async def test_restores_sanitized_complete_cache(self):
        await self.sync([record(1)])
        self.manager = CloudHistoryManager(None, self.store)
        await self.manager.async_load()
        summary = self.manager.summary(IDENTIFIER)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["synced_count"], 1)

    def test_naive_cloud_date_uses_home_assistant_timezone(self):
        with patch("homeassistant.util.dt.DEFAULT_TIME_ZONE", ZoneInfo("Asia/Shanghai")):
            self.assertEqual(_date_ms("2026-11-10 12:00:00"), _date_ms("2026-11-10T04:00:00+00:00"))
