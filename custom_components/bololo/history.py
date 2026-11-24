"""Bounded local operation journal based only on observed device reports."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime

from homeassistant.helpers.storage import Store as HistoryStore  # noqa: F401
from homeassistant.util import dt as dt_util

from .schema import Datapoint

HISTORY_LIMIT = 50
NAMES = {"water": "出水记录", "boil": "烧水记录", "sterilize": "消烘记录"}
_BOOL = Datapoint("", "", "bool", False)


def history_kinds(product):
    """Use actual capabilities, including both chambers of dual-cabinet models."""
    points = {point.key: point for point in product.datapoints}
    kinds = {}
    if "boiling" in points:
        kinds["boil"] = ""
        if "record" in points and "detect_status" in points:
            kinds["water"] = ""
    for prefix in ("", "cabin1_", "cabin2_"):
        if all(prefix + key in points for key in ("status", "work_mode", "disinfection_switch")):
            kinds[prefix + "sterilize"] = prefix
    return kinds


def history_name(kind):
    prefix = (
        "标准舱" if kind.startswith("cabin1_") else "节能舱" if kind.startswith("cabin2_") else ""
    )
    return prefix + NAMES[kind.removeprefix("cabin1_").removeprefix("cabin2_")]


def _number(value, maximum=99999):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= maximum else None
    except (TypeError, ValueError, OverflowError):
        return None


def _cabinet_mode(attrs, prefix):
    mode = _number(attrs.get(prefix + "work_mode"))
    if mode not in (None, 0):
        return mode
    switches = [
        index
        for index, key in enumerate(("auto_switch", "dry_switch", "disinfection_switch"), 1)
        if _BOOL.decode(attrs.get(prefix + key)) is True
    ]
    return switches[0] if len(switches) == 1 else mode


class OperationHistory:
    """First poll establishes a baseline; requests alone never create records."""

    def __init__(self, saved=None):
        self.data = {}
        for identifier, kinds in saved.items() if isinstance(saved, dict) else []:
            if not isinstance(kinds, dict):
                continue
            for kind, journal in kinds.items():
                if not isinstance(journal, dict):
                    continue
                count, records = journal.get("count"), journal.get("records")
                if type(count) is int and count >= 0 and isinstance(records, list):
                    self.data.setdefault(identifier, {})[kind] = {
                        "count": count,
                        "records": deepcopy(
                            [item for item in records if isinstance(item, dict)][:HISTORY_LIMIT]
                        ),
                    }
                    if kind == "water":
                        total = _number(journal.get("total_volume_ml"), maximum=math.inf)
                        self.data[identifier][kind]["total_volume_ml"] = (
                            total
                            if total is not None
                            else sum(
                                _number(record.get("volume_ml")) or 0
                                for record in self.data[identifier][kind]["records"]
                            )
                        )
        self._previous = {}
        self._sources = {}
        self._active = {}
        self._pending_water = {}
        self._pending_cabinet = {}
        # A restart loses continuous observation; never call it a completed cycle.
        for kinds in self.data.values():
            for journal in kinds.values():
                for record in journal.get("records", []):
                    if record.get("outcome") in {"running", "paused"}:
                        record["outcome"] = "observation_gap"

    def summary(self, identifier, kind):
        empty = {"count": 0, "records": []}
        if kind == "water":
            empty["total_volume_ml"] = 0
        return deepcopy(self.data.get(identifier, {}).get(kind, empty))

    def _append(self, identifier, kind, record):
        journal = self.data.setdefault(identifier, {}).setdefault(kind, {"count": 0, "records": []})
        journal["count"] += 1
        if kind == "water":
            journal["total_volume_ml"] = journal.get("total_volume_ml", 0) + (
                _number(record.get("volume_ml")) or 0
            )
        journal["records"].insert(0, record)
        del journal["records"][HISTORY_LIMIT:]
        return record

    def observe(self, identifier, product, attrs, source, now: datetime | None = None):
        now = now or dt_util.utcnow()
        stamp = now.isoformat()
        if identifier in self._sources and self._sources[identifier] != source:
            self.observation_gap(identifier)
        self._sources[identifier] = source
        previous = self._previous.get(identifier)
        # Merge partial reports, but only interpret fields present in this report.
        merged = {**(previous or {}), **attrs}
        self._previous[identifier] = merged
        if previous is None:
            return
        for kind, prefix in history_kinds(product).items():
            key = (identifier, kind)
            started_at = stamp
            start_observed = False
            if kind == "water":
                self._water(identifier, attrs, previous, merged, source, now)
                continue
            if kind == "boil":
                if "boiling" not in attrs:
                    continue
                before, after = (
                    _BOOL.decode(previous.get("boiling")),
                    _BOOL.decode(attrs["boiling"]),
                )
                mode = "boiling"
                paused = False
            else:
                if not any(
                    prefix + field in attrs
                    for field in (
                        "status",
                        "work_mode",
                        "auto_switch",
                        "dry_switch",
                        "disinfection_switch",
                    )
                ):
                    continue
                old_status = _number(previous.get(prefix + "status"))
                status = _number(merged.get(prefix + "status"))
                before = old_status in (2, 3) if old_status in (0, 1, 2, 3) else None
                after = status in (2, 3) if status in (0, 1, 2, 3) else None
                mode = _cabinet_mode(merged, prefix)
                if before is False and status == 2:
                    self._pending_cabinet[key] = stamp
                elif after is False:
                    self._pending_cabinet.pop(key, None)
                start_observed = key in self._pending_cabinet
                started_at = self._pending_cabinet.get(key, stamp)
                paused = status == 3
                # Only actual disinfect/dry programs; exclude yogurt/fruit scenes.
                if mode not in (1, 2, 3) and key not in self._active:
                    continue
                if paused and key not in self._active:
                    continue
            if after is True and (before is False or start_observed) and key not in self._active:
                record = self._append(
                    identifier,
                    kind,
                    {
                        "started_at": started_at,
                        "ended_at": None,
                        "mode": mode,
                        "outcome": "running",
                        "source": source,
                    },
                )
                self._active[key] = record
                self._pending_cabinet.pop(key, None)
            record = self._active.get(key)
            if record is None:
                continue
            if after is True:
                record["outcome"] = "paused" if paused else "running"
            elif after is False:
                record.update(ended_at=stamp, outcome="ended")
                self._active.pop(key, None)

    def _water(self, identifier, attrs, previous, merged, source, now):
        key = (identifier, "water")
        before = _BOOL.decode(previous.get("detect_status"))
        after = _BOOL.decode(merged.get("detect_status"))
        volume = _number(attrs.get("record"))
        old_volume = _number(previous.get("record"))
        changed = volume is not None and volume > 0 and volume != old_volume
        if before is False and after is True:
            self._active[key] = {"started_at": now.isoformat(), "initial_volume": old_volume}
        active = self._active.get(key)
        if active and after is True:
            if changed:
                active["volume_ml"] = volume
            return
        if before is True and after is False:
            active = self._active.pop(key, {})
            record = self._append(
                identifier,
                "water",
                {
                    "started_at": active.get("started_at"),
                    "ended_at": now.isoformat(),
                    "volume_ml": volume if changed else active.get("volume_ml"),
                    "bottle_number": _number(merged.get("bottle_record")),
                    "outcome": "ended",
                    "source": source,
                },
            )
            if record["volume_ml"] is None:
                self._pending_water[identifier] = (now, record)
        elif changed:
            pending = self._pending_water.pop(identifier, None)
            if pending and (now - pending[0]).total_seconds() <= 120:
                pending[1]["volume_ml"] = volume
                pending[1]["bottle_number"] = _number(merged.get("bottle_record"))
                self.data[identifier]["water"]["total_volume_ml"] += volume
            else:
                self._append(
                    identifier,
                    "water",
                    {
                        "started_at": None,
                        "ended_at": now.isoformat(),
                        "volume_ml": volume,
                        "bottle_number": _number(merged.get("bottle_record")),
                        "outcome": "reported",
                        "source": source,
                    },
                )

    def observation_gap(self, identifier):
        """Reconnects establish a fresh baseline instead of inventing offline events."""
        self._previous.pop(identifier, None)
        self._sources.pop(identifier, None)
        self._pending_water.pop(identifier, None)
        for key in list(self._pending_cabinet):
            if key[0] == identifier:
                self._pending_cabinet.pop(key)
        for key in list(self._active):
            if key[0] == identifier:
                record = self._active.pop(key)
                record["outcome"] = "observation_gap"
