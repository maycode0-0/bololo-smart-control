"""Verify HACS extraction layout, immutable rebuilds and tag/version guards."""

import importlib.util
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location(
    "build_release", Path(__file__).resolve().parents[1] / "tools" / "build_release.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def release_source():
    # Match the HA tests: Windows sandbox tokens cannot read pytest's 0700 TMP.
    parent = Path(__file__).resolve().parent
    root = parent / f"bololo-release-test-{uuid4().hex}"
    root.mkdir()
    component = root / "custom_components" / "bololo"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        json.dumps({"domain": "bololo", "version": "0.1.1"}), encoding="utf-8"
    )
    for name in ("__init__.py", "lan.py", "lan_codec.py"):
        (component / name).write_text("# component source\n", encoding="utf-8")
    (component / "lan_profiles.json").write_text("{}", encoding="utf-8")
    translations = component / "translations"
    translations.mkdir()
    (translations / "zh-Hans.json").write_text('{"title":"波咯咯"}', encoding="utf-8")
    cache = component / "__pycache__"
    cache.mkdir()
    (cache / "lan.cpython-312.pyc").write_bytes(b"not distributable")
    (component / "debug.log").write_text("private debug data", encoding="utf-8")
    notes = root / "docs" / "releases"
    notes.mkdir(parents=True)
    (notes / "0.1.1.md").write_text("Release notes", encoding="utf-8")
    try:
        yield root
    finally:
        if root.resolve().parent != parent:
            raise RuntimeError("Refusing cleanup outside the test directory")
        shutil.rmtree(root)


def test_hacs_and_manual_zips_extract_to_the_correct_directory(release_source):
    hacs, manual = builder.build_release(release_source, "0.1.1")
    with ZipFile(hacs) as archive, ZipFile(manual) as full:
        assert "manifest.json" in archive.namelist()
        assert "custom_components/bololo/manifest.json" in full.namelist()
        assert "translations/zh-Hans.json" in archive.namelist()
        assert not any(
            "__pycache__" in name or name.endswith(".log") for name in archive.namelist()
        )
        for name in archive.namelist():
            assert archive.read(name) == full.read("custom_components/bololo/" + name)


def test_same_source_rebuild_is_independent_of_file_timestamps(release_source):
    paths = builder.build_release(release_source)
    original = [path.read_bytes() for path in paths]
    for source in (release_source / "custom_components").rglob("*"):
        os.utime(source, (1750000000, 1750000000))
    assert [path.read_bytes() for path in builder.build_release(release_source)] == original


def test_tag_version_mismatch_cannot_produce_release_assets(release_source):
    with pytest.raises(ValueError, match="does not match"):
        builder.build_release(release_source, "0.1.2")
    assert not (release_source / "dist").exists()


def test_missing_notes_or_protocol_files_cannot_be_released(release_source):
    (release_source / "docs/releases/0.1.1.md").unlink()
    with pytest.raises(ValueError, match="Missing release notes"):
        builder.build_release(release_source)
    (release_source / "docs/releases/0.1.1.md").write_text("Notes", encoding="utf-8")
    (release_source / "custom_components/bololo/lan_profiles.json").unlink()
    with pytest.raises(ValueError, match="Incomplete integration source"):
        builder.build_release(release_source)
