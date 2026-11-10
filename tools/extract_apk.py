"""Recover minimal protocol metadata from a BOLOLO APK without executing code.

Usage: python tools/extract_apk.py path/to/app.apk
Only JSON product definitions and static transport declarations are consumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile


def product_records(source: str) -> list[str]:
    """Split the static product_list array while respecting JavaScript strings."""
    marker = "product_list:["
    start = source.find(marker)
    if start < 0:
        raise ValueError("Static product_list declaration was not found")
    depth = 0
    quote = None
    escaped = False
    record_start = None
    records = []
    for offset in range(start + len(marker), len(source)):
        char = source[offset]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char in "{[":
            if depth == 0:
                if char != "{":
                    raise ValueError("Unexpected non-object product entry")
                record_start = offset
            depth += 1
        elif char in "}]":
            if depth == 0 and char == "]":
                return records
            depth -= 1
            if depth == 0 and record_start is not None:
                records.append(source[record_start : offset + 1])
                record_start = None
    raise ValueError("Static product_list declaration is incomplete")


def transports_from_bundle(bundle: str) -> dict[str, str]:
    """Read declarations only; never evaluate APK JavaScript."""
    result = {}
    for record in product_records(bundle):
        key = re.search(r'\bproductKey:"([^"\\]+)"', record)
        transport = re.search(r'\bm2mProtoc:"(BLE|WIFI)"', record)
        if key and transport:
            result[key[1]] = transport[1].lower()
    return result


def literal_properties(source: str) -> dict[str, str]:
    """Read object property spans without evaluating JavaScript expressions."""
    if not (source.startswith("{") and source.endswith("}")):
        return {}
    spans = []
    depth = 0
    quote = None
    escaped = False
    start = 1
    for offset, char in enumerate(source[1:-1], 1):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'", "`"):
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            spans.append(source[start:offset])
            start = offset + 1
    spans.append(source[start:-1])
    result = {}
    for span in spans:
        match = re.fullmatch(r'\s*("(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*)\s*:\s*(.*)', span, re.S)
        if match:
            key = json.loads(match[1]) if match[1].startswith('"') else match[1]
            result[key] = match[2].strip()
    return result


def literal_string(value: str | None) -> str | None:
    """Decode a JSON string literal only, never an expression."""
    if not value or not value.startswith('"'):
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, str) else None


def configured_products(bundle: str) -> dict[str, dict]:
    result = {}
    for record in product_records(bundle):
        fields = literal_properties(record)
        key = literal_string(fields.get("productKey"))
        if not key:
            continue
        languages = literal_properties(fields.get("language", "{}"))
        zh = literal_properties(languages.get("zh", "{}"))
        labels = {key: label for key, value in zh.items() if (label := literal_string(value))}
        result[key] = {
            "transport": (literal_string(fields.get("m2mProtoc")) or "unknown").lower(),
            "name": literal_string(fields.get("name")),
            "labels": labels,
        }
    return result


def normalize_product(raw: dict, transport: str, configured: dict | None = None) -> dict:
    """Keep public datapoint names, types, descriptions and engineering bounds."""
    ui = {
        element["key"].split(".")[-1]: element
        for section in raw.get("ui", {}).get("sections", [])
        for element in section.get("elements", [])
        if "key" in element
    }
    configured = configured or {}
    labels = configured.get("labels", {})
    attrs = []
    for entity in raw.get("entities", []):
        for attr in entity.get("attrs", []):
            item = {
                key: attr[key]
                for key in ("name", "display_name", "data_type", "type", "enum", "desc")
                if key in attr
            }
            item["display_name"] = labels.get(
                "DP-" + attr["name"], item.get("display_name", attr["name"])
            )
            if enum := attr.get("enum"):
                enum_labels = [
                    labels.get(f"DP-{attr['name']}@{index}", value)
                    for index, value in enumerate(enum)
                ]
                if enum_labels != enum and len(set(enum_labels)) == len(enum_labels):
                    item["enum_labels"] = enum_labels
            spec = attr.get("uint_spec")
            if spec:
                ui_spec = ui.get(attr["name"], {}).get("object", {}).get("uint_spec", {})
                ratio = spec.get("ratio", 1)
                addition = spec.get("addition", 0)
                item["minimum"] = ui_spec.get("min", spec["min"] * ratio + addition)
                item["maximum"] = ui_spec.get("max", spec["max"] * ratio + addition)
                item["step"] = ui_spec.get("step", ratio)
            attrs.append(item)
    name = labels.get("PL_PRODUCT_NAME") or configured.get("name") or raw["name"]
    aliases = list(
        dict.fromkeys(
            value
            for value in [raw["name"], configured.get("name"), labels.get("PL_PRODUCT_NAME")]
            if value
        )
    )
    return {
        "name": name,
        "schema_name": raw["name"],
        "aliases": aliases,
        "transport": transport,
        "attrs": attrs,
    }


def extract(apk: Path, output: Path, summary: Path | None = None) -> dict:
    with ZipFile(apk) as archive:
        bundle = archive.read("assets/index.android.bundle").decode("utf-8")
        configured = configured_products(bundle)
        profiles = {}
        sources = {}
        for member in sorted(archive.namelist()):
            if not re.fullmatch(r"assets/productConfig/[^/]+\.json", member):
                continue
            raw = json.loads(archive.read(member))
            key = raw["product_key"]
            if key in profiles:
                raise ValueError(f"Duplicate product key: {key}")
            metadata = configured.get(key, {})
            profiles[key] = normalize_product(raw, metadata.get("transport", "unknown"), metadata)
            sources[key] = member
    if not profiles:
        raise ValueError("The APK has no packaged product definitions")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if summary:
        hasher = hashlib.sha256()
        with apk.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        result = {
            "source": "ordinary archive/static inspection; no APK code executed",
            "apk_sha256": digest,
            "transport_source": "assets/index.android.bundle module 1268 product_list",
            "products": [
                {"product_key": key, "source_member": sources[key], **profile}
                for key, profile in profiles.items()
            ],
        }
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return profiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("custom_components/bololo/products.json")
    )
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    profiles = extract(args.apk, args.output, args.summary)
    totals = {
        name: sum(p["transport"] == name for p in profiles.values())
        for name in ("wifi", "ble", "unknown")
    }
    print(f"Recovered {len(profiles)} product profiles: {totals}")


if __name__ == "__main__":
    main()
