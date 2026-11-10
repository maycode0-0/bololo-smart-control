"""Bounded Gizwits v3 framing and v4 variable datapoints recovered from the APK.

See analysis/lan/NATIVE_PROTOCOL.md for the static native source anchors. This
codec never invokes the Android library and has no external dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass
from typing import Any

from .lan_errors import LanProtocolError

MAGIC = b"\x00\x00\x00\x03"
MAX_FRAME_SIZE = 65536
SN_COMMANDS = {147, 148}


@dataclass(frozen=True)
class Frame:
    cmd: int
    sn: int
    payload: bytes


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        digit = value & 127
        value >>= 7
        output.append(digit | (128 if value else 0))
        if not value:
            return bytes(output)


def frame(cmd: int, payload: bytes = b"", sn: int = 0) -> bytes:
    """Encode a native command; only the sequenced DP commands contain SN."""
    try:
        body = b"\x00" + cmd.to_bytes(2, "big")
        if cmd in SN_COMMANDS:
            body += sn.to_bytes(4, "big")
        body += payload
    except (OverflowError, AttributeError, TypeError) as err:
        raise LanProtocolError("Invalid command header") from err
    if len(body) > MAX_FRAME_SIZE:
        raise LanProtocolError("LAN frame exceeds size limit")
    return MAGIC + _varint(len(body)) + body


def _body(body: bytes) -> Frame:
    if len(body) < 3 or body[0] != 0:
        raise LanProtocolError("Unsupported LAN header or session flag")
    cmd = int.from_bytes(body[1:3], "big")
    if cmd in SN_COMMANDS:
        if len(body) < 7:
            raise LanProtocolError("Missing datapoint sequence")
        return Frame(cmd, int.from_bytes(body[3:7], "big"), body[7:])
    return Frame(cmd, 0, body[3:])


def parse_frame(data: bytes) -> Frame:
    if not data.startswith(MAGIC):
        raise LanProtocolError("Invalid LAN magic")
    length = 0
    for index in range(4):
        offset = 4 + index
        if offset >= len(data):
            raise LanProtocolError("Truncated LAN length")
        digit = data[offset]
        length |= (digit & 127) << (7 * index)
        if not digit & 128:
            if length > MAX_FRAME_SIZE or len(data) != offset + 1 + length:
                raise LanProtocolError("Invalid LAN frame length")
            return _body(data[offset + 1 :])
    raise LanProtocolError("Invalid LAN variable length")


async def async_read_frame(reader: asyncio.StreamReader) -> Frame:
    if await reader.readexactly(4) != MAGIC:
        raise LanProtocolError("Invalid LAN magic")
    length = 0
    for index in range(4):
        digit = (await reader.readexactly(1))[0]
        length |= (digit & 127) << (7 * index)
        if not digit & 128:
            if length > MAX_FRAME_SIZE:
                raise LanProtocolError("LAN frame exceeds size limit")
            return _body(await reader.readexactly(length))
    raise LanProtocolError("Invalid LAN variable length")


def _string(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise LanProtocolError("Truncated LAN string length")
    size = int.from_bytes(data[offset : offset + 2], "big")
    end = offset + 2 + size
    if end > len(data):
        raise LanProtocolError("Truncated LAN string")
    return data[offset + 2 : end], end


def discovery(data: bytes) -> dict[str, str] | None:
    message = parse_frame(data)
    if message.cmd not in {4, 5}:
        return None
    try:
        did, offset = _string(message.payload, 0)
        mac, offset = _string(message.payload, offset)
        _, offset = _string(message.payload, offset)  # Wi-Fi software version
        product, _ = _string(message.payload, offset)
        mac_text = mac.decode("ascii") if len(mac) == 12 else mac.hex()
        if len(mac_text) != 12 or any(c not in "0123456789abcdefABCDEF" for c in mac_text):
            raise LanProtocolError("Invalid discovered MAC")
        if not product or len(product) > 32 or len(did) > 22:
            raise LanProtocolError("Invalid discovered identity")
        return {
            "productKey": product.decode("ascii"),
            "mac": mac_text.lower(),
            "did": did.decode("utf8"),
        }
    except UnicodeError as err:
        raise LanProtocolError("Invalid discovered text") from err


def bind_passcode(payload: bytes) -> str:
    raw, _ = _string(payload, 0)
    if not raw or len(raw) > 32:
        raise LanProtocolError("Invalid LAN passcode")
    try:
        return raw.decode("ascii")
    except UnicodeError as err:
        raise LanProtocolError("Invalid LAN passcode text") from err


def login_payload(passcode: str) -> bytes:
    try:
        raw = passcode.encode("ascii")
    except (UnicodeError, AttributeError) as err:
        raise LanProtocolError("Invalid LAN passcode text") from err
    if not 1 <= len(raw) <= 32:
        raise LanProtocolError("Invalid LAN passcode length")
    return len(raw).to_bytes(2, "big") + raw


def login_success(payload: bytes) -> bool:
    if len(payload) != 1:
        raise LanProtocolError("Invalid LAN login response")
    return payload[0] == 0


def _attributes(profile: dict[str, Any]) -> list[dict[str, Any]]:
    if profile.get("protocolType") != "var_len" or profile.get("packetVersion") != "0x00000004":
        raise LanProtocolError("Unsupported LAN datapoint profile")
    entities = profile.get("entities", [])
    if len(entities) != 1:
        raise LanProtocolError("Unsupported LAN entity count")
    attrs = sorted(entities[0]["attrs"], key=lambda attr: attr["id"])
    if not attrs or [attr["id"] for attr in attrs] != list(range(len(attrs))):
        raise LanProtocolError("Invalid LAN datapoint identifiers")
    return attrs


def encode_read(profile: dict[str, Any]) -> bytes:
    attrs = _attributes(profile)
    return b"\x12" + b"\xff" * ((len(attrs) + 7) // 8)


def _selected(profile: dict[str, Any], flags: int) -> list[dict[str, Any]]:
    return [attr for attr in _attributes(profile) if flags & (1 << attr["id"])]


def encode_write(profile: dict[str, Any], values: dict[str, Any]) -> bytes:
    attrs = _attributes(profile)
    selected = [attr for attr in attrs if attr["name"] in values]
    if not values or len(selected) != len(values):
        raise LanProtocolError("Unknown or empty LAN datapoint write")
    flags = 0
    packed_bits = 0
    bit_count = 0
    byte_values = bytearray()
    for attr in selected:
        if attr["type"] != "status_writable":
            raise LanProtocolError("Read-only LAN datapoint")
        flags |= 1 << attr["id"]
        value = values[attr["name"]]
        kind = attr["data_type"]
        width = attr["position"]["len"]
        if kind in {"bool", "enum"}:
            if kind == "bool":
                if not isinstance(value, bool):
                    raise LanProtocolError("LAN boolean value required")
                raw = int(value)
            else:
                options = attr["enum"]
                if isinstance(value, str) and value in options:
                    raw = options.index(value)
                elif (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value < len(options)
                ):
                    raw = value
                else:
                    raise LanProtocolError("Invalid LAN enumeration")
            if raw >= 1 << width:
                raise LanProtocolError("LAN bit field overflow")
            packed_bits |= raw << bit_count
            bit_count += width
        elif kind in {"uint8", "uint16", "uint32"}:
            spec = attr["uint_spec"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise LanProtocolError("Invalid LAN numeric value")
            raw_value = (value - spec["addition"]) / spec["ratio"]
            raw = round(raw_value)
            if (
                not math.isclose(raw_value, raw, abs_tol=1e-6)
                or not spec["min"] <= raw <= spec["max"]
            ):
                raise LanProtocolError("LAN value outside datapoint range")
            try:
                byte_values.extend(raw.to_bytes(width, "big"))
            except OverflowError as err:
                raise LanProtocolError("LAN numeric overflow") from err
        else:
            raise LanProtocolError("Unsupported LAN write datatype")
    return (
        b"\x11"
        + flags.to_bytes((len(attrs) + 7) // 8, "big")
        + packed_bits.to_bytes((bit_count + 7) // 8, "big")
        + byte_values
    )


def decode_status(profile: dict[str, Any], payload: bytes) -> dict[str, Any]:
    attrs = _attributes(profile)
    flag_size = (len(attrs) + 7) // 8
    if len(payload) < 1 + flag_size or payload[0] not in {0x11, 0x13, 0x14}:
        raise LanProtocolError("Invalid LAN datapoint report")
    flags = int.from_bytes(payload[1 : 1 + flag_size], "big")
    # Padding bits are ignored by the native decoder as well.
    selected = _selected(profile, flags)
    bit_count = sum(a["position"]["len"] for a in selected if a["position"]["unit"] == "bit")
    bit_size = (bit_count + 7) // 8
    expected = (
        1
        + flag_size
        + bit_size
        + sum(a["position"]["len"] for a in selected if a["position"]["unit"] == "byte")
    )
    if len(payload) != expected:
        raise LanProtocolError("Incorrect LAN datapoint report size")
    bits = int.from_bytes(payload[1 + flag_size : 1 + flag_size + bit_size], "big")
    bit_offset = 0
    byte_offset = 1 + flag_size + bit_size
    result = {}
    for attr in selected:
        width = attr["position"]["len"]
        if attr["position"]["unit"] == "bit":
            raw = (bits >> bit_offset) & ((1 << width) - 1)
            bit_offset += width
        else:
            raw_bytes = payload[byte_offset : byte_offset + width]
            raw = int.from_bytes(raw_bytes, "big")
            byte_offset += width
        kind = attr["data_type"]
        if kind == "bool":
            value = bool(raw)
        elif kind == "enum":
            options = attr["enum"]
            value = options[raw] if raw < len(options) else raw
        elif kind.startswith("uint"):
            spec = attr["uint_spec"]
            value = raw * spec["ratio"] + spec["addition"]
        elif kind == "binary":
            value = base64.b64encode(raw_bytes).decode("ascii")
        else:
            raise LanProtocolError("Unsupported LAN report datatype")
        result[attr["name"]] = value
    return result
