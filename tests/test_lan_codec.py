"""Fixed native-format vectors and hostile-stream checks."""

import asyncio
import json
from pathlib import Path

import pytest

from custom_components.bololo import lan_codec as codec
from custom_components.bololo.lan_errors import LanProtocolError


@pytest.fixture
def profiles():
    return json.loads(
        (Path(__file__).parents[1] / "custom_components/bololo/lan_profiles.json").read_text(
            encoding="utf-8"
        )
    )


def test_native_header_login_and_sequence_vectors():
    assert codec.frame(3) == bytes.fromhex("0000000303000003")
    assert codec.frame(6) == bytes.fromhex("0000000303000006")
    assert codec.frame(8, codec.login_payload("abcd")) == bytes.fromhex(
        "0000000309000008000461626364"
    )
    assert codec.frame(148, sn=0x12345678) == bytes.fromhex("000000030700009412345678")
    assert codec.parse_frame(bytes.fromhex("000000030400000900")) == codec.Frame(9, 0, b"\0")
    assert codec.login_success(b"\0") is True
    assert codec.login_success(b"\1") is False
    assert codec.bind_passcode(bytes.fromhex("000461626364")) == "abcd"


def test_p30_native_datapoint_vectors(profiles):
    profile = profiles["k1mvp9K2mCI000000000000000000000"]
    assert codec.encode_read(profile) == bytes.fromhex("12ffffffff")
    assert codec.encode_write(profile, {"temp_set": 45, "switch": True}) == bytes.fromhex(
        "1100008001012d"
    )
    assert codec.decode_status(profile, bytes.fromhex("1300018001012d2a")) == {
        "switch": True,
        "temp_set": 45,
        "temp_current": 42,
    }
    # Read state can contain an enum value added in a newer official UI bundle.
    assert codec.decode_status(profile, bytes.fromhex("130000080003")) == {"work_state": 3}
    assert codec.encode_write(profile, {"pattern": "pure_water", "switch": True}) == bytes.fromhex(
        "110000100103"
    )


def test_z60_native_mode_vector(profiles):
    profile = profiles["k1mvpUrX2eb000000000000000000000"]
    assert codec.encode_read(profile) == bytes.fromhex("12ffffffffffff")
    assert codec.encode_write(
        profile, {"disinfection_switch": False, "auto_switch": True, "dry_switch": False}
    ) == bytes.fromhex("1100000000007001")


@pytest.mark.parametrize(
    "payload", [b"", b"\x13", bytes.fromhex("1300018001012d"), bytes.fromhex("1300018001012d2a00")]
)
def test_truncated_or_excess_datapoints_rejected(profiles, payload):
    with pytest.raises(LanProtocolError):
        codec.decode_status(profiles["k1mvp9K2mCI000000000000000000000"], payload)


@pytest.mark.parametrize(
    "values",
    [
        {"temp_current": 45},
        {"unknown": 1},
        {"temp_set": 96},
        {"temp_set": True},
        {"switch": 1},
        {"pattern": "unknown"},
        {},
    ],
)
def test_invalid_writes_rejected_before_socket(profiles, values):
    with pytest.raises(LanProtocolError):
        codec.encode_write(profiles["k1mvp9K2mCI000000000000000000000"], values)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"bad!",
        bytes.fromhex("000000038080808080"),
        bytes.fromhex("000000030100"),
        bytes.fromhex("0000000303000094"),
        bytes.fromhex("0000000303010003"),
        bytes.fromhex("000000030300000300"),
    ],
)
def test_invalid_frames_rejected(data):
    with pytest.raises(LanProtocolError):
        codec.parse_frame(data)


@pytest.mark.asyncio
async def test_fragmented_and_coalesced_stream_frames():
    reader = asyncio.StreamReader()
    first = codec.frame(147, b"\x13" + bytes(200), sn=123)
    second = codec.frame(148, sn=124)
    pending = asyncio.create_task(codec.async_read_frame(reader))
    reader.feed_data(first[:5])
    await asyncio.sleep(0)
    assert not pending.done()
    reader.feed_data(first[5:] + second)
    assert await pending == codec.Frame(147, 123, b"\x13" + bytes(200))
    assert await codec.async_read_frame(reader) == codec.Frame(148, 124, b"")


@pytest.mark.asyncio
async def test_stream_oversize_rejected_before_body_allocation():
    reader = asyncio.StreamReader()
    reader.feed_data(bytes.fromhex("00000003818004"))  # 65537 bytes
    with pytest.raises(LanProtocolError):
        await codec.async_read_frame(reader)


def test_discovery_native_length_fields():
    payload = (
        bytes.fromhex("0003646964000c")
        + b"aabbccddeeff"
        + bytes.fromhex("0003312e300020")
        + b"k1mvp9K2mCI000000000000000000000"
        + bytes(8)
    )
    assert codec.discovery(codec.frame(4, payload)) == {
        "did": "did",
        "mac": "aabbccddeeff",
        "productKey": "k1mvp9K2mCI000000000000000000000",
    }
    with pytest.raises(LanProtocolError):
        codec.discovery(codec.frame(4, payload[:10]))


def test_variable_bit_groups_cross_byte_boundary():
    # 9 bool values pack as a big-endian two-byte bit vector, before uint16 data.
    attrs = [
        {
            "id": i,
            "name": f"b{i}",
            "data_type": "bool",
            "position": {"unit": "bit", "len": 1},
            "type": "status_writable",
        }
        for i in range(9)
    ]
    attrs.append(
        {
            "id": 9,
            "name": "scaled",
            "data_type": "uint16",
            "position": {"unit": "byte", "len": 2},
            "type": "status_writable",
            "uint_spec": {"min": 0, "max": 65535, "ratio": 0.1, "addition": -10},
        }
    )
    profile = {
        "protocolType": "var_len",
        "packetVersion": "0x00000004",
        "entities": [{"attrs": attrs}],
    }
    values = {f"b{i}": i in {0, 8} for i in range(9)} | {"scaled": 15.5}
    assert codec.encode_write(profile, values) == bytes.fromhex("1103ff010100ff")
    assert codec.decode_status(profile, bytes.fromhex("1403ff010100ff")) == values
