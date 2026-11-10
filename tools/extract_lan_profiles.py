"""Copy inert Wi-Fi datapoint wire layouts from the supplied APK.

UI text and application code are excluded. IDs, positions and scaling retain
their original values; the LAN codec must select the declared protocol type.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZipFile

from extract_apk import configured_products


def extract(apk: Path, output: Path) -> dict:
    profiles = {}
    with ZipFile(apk) as archive:
        configured = configured_products(archive.read("assets/index.android.bundle").decode())
        for member in archive.namelist():
            if not member.startswith("assets/productConfig/") or not member.endswith(".json"):
                continue
            raw = json.loads(archive.read(member))
            key = raw["product_key"]
            if configured.get(key, {}).get("transport") != "wifi":
                continue
            profiles[key] = {
                name: raw[name]
                for name in ("packetVersion", "protocolType", "product_key", "entities")
            }
            for entity in profiles[key]["entities"]:
                for attr in entity["attrs"]:
                    for name in ("display_name", "desc"):
                        attr.pop(name, None)
                entity.pop("display_name", None)
    output.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return profiles


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("custom_components/bololo/lan_profiles.json")
    )
    arguments = parser.parse_args()
    print(f"Recovered {len(extract(arguments.apk, arguments.output))} Wi-Fi wire layouts")
