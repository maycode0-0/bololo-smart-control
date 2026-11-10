"""Resumable, privately stored cloud dispensing history with atomic windows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from copy import deepcopy
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .api import BololoError
from .schema import device_id

_DAY_MS = 86_400_000
_FIELDS = ("id", "bottleId", "bottleName", "babyId", "remark", "type", "temperature")
_ERROR_TYPES = {
    "BololoError",
    "BololoAuthError",
    "BololoConnectionError",
    "BololoCaptchaRequired",
    "BololoRateLimitError",
    "OSError",
    "ValueError",
    "TypeError",
    "OverflowError",
}


def _date_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise BololoError("Invalid history date")
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
        if parsed is None:
            raise BololoError("Invalid history date") from None
        timestamp = dt_util.as_utc(parsed).timestamp() * 1000
    if not math.isfinite(timestamp) or timestamp < 0:
        raise BololoError("Invalid history date")
    return int(timestamp)


def _record(raw: Any) -> dict[str, Any]:
    """Copy documented business fields, never raw device/account payloads."""
    if not isinstance(raw, dict):
        raise BololoError("Invalid history record")
    details = raw.get("data", {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError as err:
            raise BololoError("Invalid history data") from err
    if not isinstance(details, dict):
        raise BololoError("Invalid history data")
    merged = {**details, **raw}
    result = {"date": _date_ms(merged.get("date", merged.get("reportTime")))}
    for field in _FIELDS:
        value = merged.get(field)
        if isinstance(value, str):
            result[field] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise BololoError("Invalid history value")
            result[field] = value
    if "waterOutput" in merged:
        value = merged["waterOutput"]
        if isinstance(value, bool):
            raise BololoError("Invalid history volume")
        try:
            volume = float(value)
        except (TypeError, ValueError) as err:
            raise BololoError("Invalid history volume") from err
        if not math.isfinite(volume) or volume < 0:
            raise BololoError("Invalid history volume")
        result["waterOutput"] = volume
    return result


def _key(record: dict[str, Any]) -> str:
    if record.get("id") is not None:
        return "id:" + str(record["id"])
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "hash:" + hashlib.sha256(encoded.encode()).hexdigest()


def _empty() -> dict[str, Any]:
    return {
        "records": {},
        "complete": False,
        "watermark": 0,
        "last_synced": None,
        "server_total": None,
        "sync_error": None,
        "pending": None,
        "window_start": 0,
        "window_end": 0,
    }


class CloudHistoryManager:
    """Read a bounded number of pages per run, retaining a resumable cursor.

    A completed cache remains visible until every page in a replacement window
    succeeds. Incremental windows overlap one day to pick up recent edits and
    deletions; older edits are outside this bounded refresh window.
    """

    def __init__(self, hass, store, *, page_size: int = 100, max_pages: int = 10):
        if page_size < 1 or max_pages < 1:
            raise ValueError("Positive history page limits are required")
        self.hass = hass
        self.store = store
        self.page_size = page_size
        self.max_pages = max_pages
        self._devices: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def async_load(self):
        async with self._lock:
            try:
                saved = await self.store.async_load()
            except (OSError, ValueError):
                return
            if not isinstance(saved, dict) or not isinstance(saved.get("devices"), dict):
                return
            restored = {}
            for identifier, raw in saved["devices"].items():
                if not isinstance(identifier, str) or not isinstance(raw, dict):
                    continue
                try:
                    value = _empty()
                    value["records"] = self._restore_records(raw.get("records", {}))
                    value["complete"] = raw.get("complete") is True
                    value["watermark"] = _date_ms(raw.get("watermark", 0))
                    value["window_start"] = _date_ms(raw.get("window_start", 0))
                    value["window_end"] = _date_ms(raw.get("window_end", 0))
                    value["last_synced"] = raw.get("last_synced")
                    if value["last_synced"] is not None:
                        datetime.fromisoformat(value["last_synced"])
                    value["server_total"] = self._total(raw.get("server_total"), allow_none=True)
                    if raw.get("sync_error") in _ERROR_TYPES:
                        value["sync_error"] = raw["sync_error"]
                    pending = raw.get("pending")
                    if isinstance(pending, dict):
                        current = pending["current"]
                        seen = pending["seen"]
                        size = pending.get("size", self.page_size)
                        if any(
                            isinstance(item, bool) or not isinstance(item, int)
                            for item in (current, seen, size)
                        ) or (current < 1 or seen < 0 or size < 1):
                            raise ValueError("Invalid history cursor")
                        value["pending"] = {
                            "start": _date_ms(pending["start"]),
                            "end": _date_ms(pending["end"]),
                            "current": current,
                            "seen": seen,
                            "size": size,
                            "total": self._total(pending.get("total"), allow_none=True),
                            "records": self._restore_records(pending["records"]),
                        }
                    restored[identifier] = value
                except (BololoError, KeyError, ValueError, TypeError, OverflowError):
                    # Invalid staging must not invalidate a valid complete cache.
                    try:
                        value = _empty()
                        value["records"] = self._restore_records(raw.get("records", {}))
                        restored[identifier] = value
                    except BololoError:
                        pass
            self._devices = restored

    @staticmethod
    def _restore_records(raw):
        if not isinstance(raw, dict):
            raise BololoError("Invalid saved history")
        records = [_record(item) for item in raw.values()]
        return {_key(item): item for item in records}

    @staticmethod
    def _total(value, *, allow_none=False):
        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BololoError("Invalid history total")
        return value

    async def _commit(self, identifier, value):
        candidate = {**self._devices, identifier: value}
        await self.store.async_save({"devices": deepcopy(candidate)})
        self._devices = candidate

    async def async_sync(self, device, cloud):
        """Resume cloud reads; failures never escape into device state polling."""
        identifier = device_id(device)
        async with self._lock:
            work = deepcopy(self._devices.get(identifier, _empty()))
            if work["pending"] is None:
                start = max(0, work["watermark"] - _DAY_MS) if work["complete"] else 0
                work["pending"] = {
                    "start": start,
                    "end": max(start, int(time.time() * 1000)),
                    "current": 1,
                    "seen": 0,
                    "size": self.page_size,
                    "total": None,
                    "records": {},
                }
            try:
                for _ in range(self.max_pages):
                    pending = work["pending"]
                    data = await cloud.async_get_water_history(
                        device,
                        current=pending["current"],
                        size=pending["size"],
                        start_time=pending["start"],
                        end_time=pending["end"],
                    )
                    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                        raise BololoError("Invalid history page")
                    total = self._total(data.get("total"))
                    if pending["total"] is not None and total != pending["total"]:
                        # Pagination shifted during the scan: restart this fixed window.
                        pending.update(current=1, seen=0, total=None, records={})
                        raise BololoError("History changed during pagination")
                    page = [_record(item) for item in data["records"]]
                    page_keys = [_key(item) for item in page]
                    if len(set(page_keys)) != len(page_keys) or any(
                        key in pending["records"] for key in page_keys
                    ):
                        # A repeated/shifted page can satisfy the server's count
                        # while omitting other records. Start the window again.
                        pending.update(current=1, seen=0, total=None, records={})
                        raise BololoError("Duplicate records during history pagination")
                    seen = pending["seen"] + len(page)
                    if (
                        len(page) > pending["size"]
                        or seen > total
                        or (len(page) < pending["size"] and seen < total)
                        or any(
                            not pending["start"] <= item["date"] <= pending["end"] for item in page
                        )
                    ):
                        raise BololoError("Inconsistent history page")
                    pending["records"].update({_key(item): item for item in page})
                    pending.update(total=total, seen=seen, current=pending["current"] + 1)
                    work.update(server_total=total, sync_error=None)
                    if seen == total:
                        retained = {
                            key: item
                            for key, item in work["records"].items()
                            if not pending["start"] <= item["date"] <= pending["end"]
                        }
                        retained.update(pending["records"])
                        work.update(
                            records=retained,
                            complete=True,
                            watermark=pending["end"],
                            window_start=pending["start"],
                            window_end=pending["end"],
                            last_synced=dt_util.utcnow().isoformat(),
                            pending=None,
                        )
                    await self._commit(identifier, work)
                    work = deepcopy(work)
                    if work["pending"] is None:
                        break
            except (BololoError, OSError, ValueError, TypeError, OverflowError) as err:
                # Persist only the error type, never server text or private payloads.
                work["sync_error"] = type(err).__name__
                try:
                    await self._commit(identifier, work)
                except OSError:
                    previous = deepcopy(self._devices.get(identifier, _empty()))
                    previous["sync_error"] = "OSError"
                    self._devices[identifier] = previous

    def summary(self, identifier):
        value = self._devices.get(identifier, _empty())
        records = sorted(value["records"].values(), key=lambda item: item["date"], reverse=True)
        pending = value["pending"]
        return {
            "records": deepcopy(records[:50]),
            "synced_count": len(records),
            "server_total": value["server_total"],
            "server_total_scope": "sync_window",
            "complete": value["complete"],
            "syncing": pending is not None,
            "sync_error": value["sync_error"],
            "last_synced": value["last_synced"],
            "window_start": pending["start"] if pending else value["window_start"],
            "window_end": pending["end"] if pending else value["window_end"],
            "staged_count": len(pending["records"]) if pending else 0,
        }
