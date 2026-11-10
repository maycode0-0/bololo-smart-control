"""Validate the two requested models against packaged protocol definitions."""

import math

import pytest

from custom_components.bololo.schema import Datapoint, device_id, load_products

P30 = "k1mvp9K2mCI000000000000000000000"
Z60 = "k1mvpUrX2eb000000000000000000000"


@pytest.fixture
def products():
    return load_products()


def test_requested_products_use_marketing_names_and_wifi(products):
    assert products[P30].name == "智能泡奶机P30"
    assert products[Z60].name == "刘海妹妹全能Z60系列"
    assert products[P30].transport == products[Z60].transport == "wifi"
    assert sum(p.transport == "wifi" for p in products.values()) == 13
    assert sum(p.transport == "ble" for p in products.values()) == 7


def test_p30_temperature_and_volume_bounds(products):
    points = {p.key: p for p in products[P30].datapoints}
    temperature = points["temp_set"]
    assert (temperature.minimum, temperature.maximum) == (37, 95)
    assert temperature.encode(45) == 45
    with pytest.raises(ValueError):
        temperature.encode(96)
    assert (points["wateryield"].minimum, points["wateryield"].maximum) == (30, 500)
    assert not points["temp_current"].writable


def test_z60_engineering_bounds_are_not_scaled_twice(products):
    points = {p.key: p for p in products[Z60].datapoints}
    clean = points["clean_mode_time"]
    assert (clean.minimum, clean.maximum) == (3, 60)
    assert clean.decode("10") == 10
    assert clean.encode(10) == 10
    with pytest.raises(ValueError):
        clean.encode(2)
    assert not any("Data_Rev" in p.key for p in points.values())
    assert points["custom_dis_time"].internal
    assert points["work_mode"].internal


def test_p30_enum_supports_both_reports_but_writes_template_string(products):
    pattern = next(p for p in products[P30].datapoints if p.key == "pattern")
    assert pattern.platform == "select"
    assert pattern.decode(0) == "自来水"
    assert pattern.decode("1") == "直饮水"
    assert pattern.decode("pure_water") == "直饮水"
    assert pattern.decode(1.5) is None
    assert pattern.decode(9) is None
    assert pattern.encode("直饮水") == "pure_water"
    with pytest.raises(ValueError):
        pattern.encode("unknown")


def test_template_overrides_limit_physical_commands(products):
    points = {p.key: p for p in products[Z60].datapoints}
    assert (points["auto_time"].minimum, points["auto_time"].maximum, points["auto_time"].step) == (
        40,
        80,
        10,
    )
    assert points["temp_mode"].encode("低温柔烘 45°C") == 0
    assert points["temp_mode"].decode(1) == "标准深烘 70°C"
    assert not points["temp_mode"].internal
    with pytest.raises(ValueError):
        points["auto_time"].encode(45)
    p30 = {p.key: p for p in products[P30].datapoints}
    assert p30["wateryield"].step == 5
    assert "water_output" not in p30
    assert p30["detect_status"].platform == "binary_sensor"
    assert p30["boiling_ponit_detect"].internal
    assert p30["night_mode"].encode("夜间") == 1
    assert p30["work_state"].decode(3) == "煮沸中"


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("0", False),
        ("1", True),
        ("false", False),
        (None, None),
        ("invalid", None),
    ],
)
def test_bool_state_is_not_python_string_truthiness(value, expected):
    assert Datapoint("switch", "Power", "bool", True).decode(value) is expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 101, 1.5, True])
def test_invalid_numeric_writes_are_rejected(value):
    point = Datapoint("level", "Level", "uint8", True)
    with pytest.raises(ValueError):
        point.encode(value)


def test_nonfinite_reports_and_readonly_writes():
    point = Datapoint("temp", "Temperature", "uint8", False)
    assert point.decode(math.nan) is None
    assert point.decode("invalid") is None
    with pytest.raises(ValueError):
        point.encode(40)


def test_device_identity_ignores_renames_and_mac_punctuation():
    assert device_id({"productKey": P30, "mac": "AA:BB:CC", "name": "old"}) == device_id(
        {"productKey": P30, "mac": "aabbcc", "name": "new"}
    )
