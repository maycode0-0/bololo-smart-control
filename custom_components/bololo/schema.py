"""Product metadata and strict engineering-value conversions."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_overrides import override_attribute

_INTERNAL = re.compile(
    r"reserve|data_rev|calib|timestamp|bottle_(?:tag|record|weight)|del_bottles|error_value", re.I
)
_COMMAND_CODES = {
    "work_mode",
    "temp_mode",
    "scene_mode_switch",
    "custom_mode",
    "smart_scenes",
    "night_mode",
    "remote_control_switch",
    "region_switch",
    "single_memory_boil",
    "filter_life",
    "Plasma_disabled",
}


@dataclass(frozen=True)
class Datapoint:
    """One documented scalar product datapoint."""

    key: str
    name: str
    kind: str
    writable: bool
    options: tuple[str, ...] = ()
    minimum: float = 0
    maximum: float = 100
    step: float = 1
    internal: bool = False
    labels: tuple[str, ...] = ()
    numeric_enum: bool = False

    @property
    def platform(self) -> str:
        if self.kind == "bool":
            return "switch" if self.writable else "binary_sensor"
        if self.writable:
            return "select" if self.kind == "enum" else "number"
        return "sensor"

    def decode(self, value: Any) -> bool | float | str | None:
        """Cloud values already use engineering units; never rescale them."""
        if value is None:
            return None
        if self.kind == "string":
            return str(value)
        if self.kind == "bool":
            if value in (True, 1, "1", "true", "True"):
                return True
            if value in (False, 0, "0", "false", "False"):
                return False
            return None
        if self.kind == "enum":
            if isinstance(value, str) and value in self.options:
                return self.labels[self.options.index(value)] if self.labels else value
            try:
                number = float(value)
                index = int(number)
                if number == index and 0 <= index < len(self.options):
                    return self.labels[index] if self.labels else self.options[index]
            except (TypeError, ValueError, OverflowError):
                pass
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def encode(self, value: Any) -> bool | int | float | str:
        """Validate writes against the APK's public datapoint definition."""
        if not self.writable:
            raise ValueError("Datapoint is read only")
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError("Expected a boolean")
            return value
        if self.kind == "enum":
            choices = self.labels or self.options
            if value not in choices:
                raise ValueError("Invalid option")
            wire = self.options[choices.index(value)]
            return int(wire) if self.numeric_enum else wire
        if isinstance(value, bool):
            raise ValueError("Expected a number")
        number = float(value)
        if not math.isfinite(number) or not self.minimum <= number <= self.maximum:
            raise ValueError("Value outside documented bounds")
        steps = (number - self.minimum) / self.step
        if not math.isclose(steps, round(steps), abs_tol=1e-6):
            raise ValueError("Value does not match documented step")
        return int(number) if number.is_integer() else number


@dataclass(frozen=True)
class Product:
    """One packaged product profile."""

    key: str
    name: str
    transport: str
    datapoints: tuple[Datapoint, ...]


def load_products() -> dict[str, Product]:
    """Read installed protocol metadata (call in an executor)."""
    profiles = json.loads(Path(__file__).with_name("products.json").read_text(encoding="utf-8"))
    products = {}
    for key, profile in profiles.items():
        points = []
        for original in profile["attrs"]:
            attr = override_attribute(key, original)
            if attr is None:
                continue
            kind = attr["data_type"]
            text = attr.get("display_name", "") + attr.get("desc", "")
            if kind not in {"bool", "enum", "uint8", "uint16", "uint32"}:
                continue
            if _INTERNAL.search(attr["name"]) and "reserve" in attr["name"].lower():
                continue
            if "预留" in text or "废除" in text:
                continue
            if attr["type"] not in {"status_writable", "status_readonly"}:
                continue
            options = tuple(attr.get("enum", ()))
            if kind == "enum" and not options:
                continue
            points.append(
                Datapoint(
                    key=attr["name"],
                    name=attr.get("display_name") or attr["name"],
                    kind=kind,
                    writable=attr["type"] == "status_writable",
                    options=options,
                    labels=tuple(attr.get("enum_labels", ())),
                    numeric_enum=attr.get("numeric_enum", False),
                    minimum=attr.get("minimum", 0),
                    maximum=attr.get("maximum", 100),
                    step=attr.get("step", 1),
                    internal=bool(
                        attr.get("internal")
                        or _INTERNAL.search(attr["name"])
                        or attr["name"].startswith("custom_")
                        or (kind not in {"bool", "enum"} and attr["name"] in _COMMAND_CODES)
                        or attr["name"] in {"filter_life", "Plasma_disabled"}
                        or any(s in text for s in ("校准", "误差", "同步", "复位"))
                    ),
                )
            )
        products[key] = Product(key, profile["name"], profile["transport"], tuple(points))
    return products


def device_id(device: dict[str, Any]) -> str:
    """Identity remains stable when the user renames a device."""
    return f"{device['productKey']}_{re.sub(r'[^a-z0-9]', '', device['mac'].lower())}"
