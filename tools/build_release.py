"""Build deterministic HACS and full-path installation ZIPs, then verify them."""

import argparse
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:b(0|[1-9]\d*))?")


def build_release(root: Path, expected_version: str | None = None) -> tuple[Path, Path]:
    """HACS extracts its ZIP directly into custom_components/bololo/."""
    component = root / "custom_components" / "bololo"
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if manifest.get("domain") != "bololo" or not VERSION.fullmatch(version):
        raise ValueError("Invalid BOLOLO release manifest")
    if expected_version is not None and version != expected_version:
        raise ValueError(f"Manifest version {version} does not match {expected_version}")
    if not (root / "docs" / "releases" / f"{version}.md").is_file():
        raise ValueError(f"Missing release notes for {version}")
    files = {
        path.relative_to(component).as_posix(): path.read_bytes()
        for path in sorted(component.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
    }
    required = {"manifest.json", "__init__.py", "lan.py", "lan_codec.py", "lan_profiles.json"}
    if not required <= files.keys():
        raise ValueError("Incomplete integration source")
    output = root / "dist"
    output.mkdir(exist_ok=True)
    hacs = output / "bololo-smart-control.zip"
    manual = output / f"bololo-{version}.zip"
    for target, prefix in ((hacs, ""), (manual, "custom_components/bololo/")):
        contents = {prefix + name: data for name, data in files.items()}
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in contents.items():
                entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                entry.compress_type = ZIP_DEFLATED
                archive.writestr(entry, data, compresslevel=9)
        with ZipFile(target) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(contents):
                raise ValueError(f"Invalid release archive: {target.name}")
            for name, data in contents.items():
                if archive.read(name) != data:
                    raise ValueError(f"Release content mismatch: {name}")
    return hacs, manual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", help="Refuse to package a different manifest version")
    arguments = parser.parse_args()
    for output in build_release(Path(__file__).resolve().parents[1], arguments.expected_version):
        print(output)


if __name__ == "__main__":
    main()
