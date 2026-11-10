"""Constraints observed in the two official device templates.

Sources and SHA-256 digests are recorded in analysis/FINDINGS.md. These are
user-interface limits, which can be narrower than a protocol carrier's limits.
"""

P30 = "k1mvp9K2mCI000000000000000000000"
Z60 = "k1mvpUrX2eb000000000000000000000"
Z60_MODES = ("auto_switch", "disinfection_switch", "dry_switch")

Z60_BOUNDS = {
    "auto_time": (40, 80, 10),
    "auto_time_lowtemp": (80, 100, 10),
    "dry_time": (20, 60, 10),
    "dry_time_lowtemp": (50, 90, 10),
    "disinfection_time": (10, 30, 5),
}

# These are calibration/provisioning fields or preference slots, not ordinary
# operation controls in the P30 official template.
P30_INTERNAL = {
    "quantitative_fit",
    "boiling_ponit_detect",
    "error_value",
    "auto_unlock",
    "appointment",
    "appointment_boiling",
    "night_mode",
    "self_cleaning",
}


def override_attribute(product_key: str, original: dict) -> dict | None:
    """Return a copy; never change extracted metadata or invent wire fields."""
    attr = dict(original)
    if product_key == P30:
        if attr["name"] == "water_output":
            return None
        if attr["name"] == "detect_status":
            attr["type"] = "status_readonly"
        if attr["name"] == "wateryield":
            attr["step"] = 5
        if attr["name"] == "pattern":
            attr["enum_labels"] = ["自来水", "直饮水"]
        if attr["name"] == "work_state":
            # The official template includes state 3 absent in the older JSON.
            attr.update(
                enum=["Heating", "Keeping", "Cooling", "Boiling"],
                enum_labels=["加热中", "保温中", "降温中", "煮沸中"],
            )
        if attr["name"] == "night_mode":
            attr.update(
                data_type="enum", enum=["0", "1"], enum_labels=["日间", "夜间"], numeric_enum=True
            )
        if attr["name"] in P30_INTERNAL:
            attr["internal"] = True
    if product_key == Z60:
        if bounds := Z60_BOUNDS.get(attr["name"]):
            attr["minimum"], attr["maximum"], attr["step"] = bounds
        if attr["name"] == "temp_mode":
            attr.update(
                data_type="enum",
                enum=["0", "1"],
                enum_labels=["低温柔烘 45°C", "标准深烘 70°C"],
                numeric_enum=True,
            )
    return attr
