"""Operation reports, reconnect gaps and repeated cached status must not invent cycles."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.bololo.history import HISTORY_LIMIT, OperationHistory, history_kinds
from custom_components.bololo.model_overrides import P30, Z60
from custom_components.bololo.schema import load_products


@pytest.fixture
def products():
    return load_products()


def test_initial_status_and_repeated_cache_do_not_create_records(products):
    history = OperationHistory()
    attrs = {"boiling": True, "record": 120, "detect_status": False}
    for _ in range(4):
        history.observe("p30", products[P30], attrs, "cloud")
    assert history.summary("p30", "water")["count"] == 0
    assert history.summary("p30", "boil")["count"] == 0


def test_boiling_tracks_actual_report_and_does_not_claim_completion(products):
    history = OperationHistory()
    for value in (False, True, True, False, False):
        history.observe("p30", products[P30], {"boiling": value}, "lan")
    journal = history.summary("p30", "boil")
    assert journal["count"] == 1
    assert journal["records"][0]["ended_at"] is not None
    assert journal["records"][0]["outcome"] == "ended"


def test_identical_dispenses_count_separately_without_reusing_stale_volume(products):
    history = OperationHistory()
    for attrs in (
        {"detect_status": False, "record": 120},
        {"detect_status": True, "record": 120},
        {"detect_status": False, "record": 120},
        {"detect_status": True, "record": 120},
        {"detect_status": False, "record": 120},
    ):
        history.observe("p30", products[P30], attrs, "lan")
    journal = history.summary("p30", "water")
    assert journal["count"] == 2
    assert all(row["volume_ml"] is None for row in journal["records"])


def test_delayed_volume_attaches_to_recent_dispense_once(products):
    history = OperationHistory()
    for attrs in (
        {"detect_status": False, "record": 120},
        {"detect_status": True},
        {"detect_status": False},
        {"record": 180, "bottle_record": 2},
        {"record": 180},
    ):
        history.observe("p30", products[P30], attrs, "cloud")
    journal = history.summary("p30", "water")
    assert journal["count"] == 1
    assert journal["records"][0]["volume_ml"] == 180
    assert journal["total_volume_ml"] == 180


def test_volume_report_when_short_operation_was_missed(products):
    history = OperationHistory()
    for volume in (0, 120, 120, 0, 120):
        history.observe("p30", products[P30], {"record": volume}, "cloud")
    assert history.summary("p30", "water")["count"] == 2


def test_cabinet_pause_resume_is_one_record_and_scene_is_not_disinfection(products):
    history = OperationHistory()
    for status, mode in ((1, 0), (2, 1), (3, 1), (2, 1), (1, 0), (2, 4), (1, 0)):
        history.observe("z60", products[Z60], {"status": status, "work_mode": mode}, "lan")
    journal = history.summary("z60", "sterilize")
    assert journal["count"] == 1
    assert journal["records"][0]["mode"] == 1


@pytest.mark.parametrize(
    "reports",
    [
        ({"status": 2}, {"work_mode": 1}),
        ({"work_mode": 1}, {"status": 2}),
    ],
)
def test_cabinet_status_and_mode_can_arrive_in_separate_reports(products, reports):
    history = OperationHistory()
    history.observe("z60", products[Z60], {"status": 1, "work_mode": 0}, "lan")
    for attrs in (*reports, {"status": 3}, {"status": 2}, {"work_mode": 0}, {"status": 1}):
        history.observe("z60", products[Z60], attrs, "lan")
    journal = history.summary("z60", "sterilize")
    assert journal["count"] == 1
    assert journal["records"][0]["mode"] == 1
    assert journal["records"][0]["outcome"] == "ended"


@pytest.mark.parametrize(
    "switch, mode", [("auto_switch", 1), ("dry_switch", 2), ("disinfection_switch", 3)]
)
@pytest.mark.parametrize("switch_first", [False, True])
def test_cabinet_can_identify_reported_mode_switch_when_work_mode_is_missing(
    products, switch, mode, switch_first
):
    history = OperationHistory()
    history.observe("z60", products[Z60], {"status": 1, switch: False}, "lan")
    reports = [{"status": 2}, {switch: True}]
    if switch_first:
        reports.reverse()
    for attrs in (*reports, {"status": 3}, {"status": 2}, {"status": 1, switch: False}):
        history.observe("z60", products[Z60], attrs, "lan")
    journal = history.summary("z60", "sterilize")
    assert journal["count"] == 1
    assert journal["records"][0]["mode"] == mode
    assert journal["records"][0]["outcome"] == "ended"


def test_cabinet_delayed_mode_keeps_observed_start_time(products):
    history = OperationHistory()
    now = datetime(2026, 11, 10, tzinfo=UTC)
    for offset, attrs in (
        (0, {"status": 1, "work_mode": 0}),
        (30, {"status": 2}),
        (60, {"work_mode": 1}),
        (90, {"status": 1}),
    ):
        history.observe("z60", products[Z60], attrs, "lan", now + timedelta(seconds=offset))
    record = history.summary("z60", "sterilize")["records"][0]
    assert record["started_at"] == (now + timedelta(seconds=30)).isoformat()


def test_cabinet_initial_running_state_and_transport_switch_do_not_invent_start(products):
    history = OperationHistory()
    for attrs, source in (
        ({"status": 2, "work_mode": 0}, "lan"),
        ({"work_mode": 1}, "lan"),
        ({"status": 1, "work_mode": 0}, "lan"),
        ({"status": 2}, "lan"),
        ({"status": 2, "work_mode": 0}, "cloud"),
        ({"work_mode": 1}, "cloud"),
    ):
        history.observe("z60", products[Z60], attrs, source)
    assert history.summary("z60", "sterilize")["count"] == 0


@pytest.mark.parametrize("mode", [4, 5, 255])
def test_cabinet_scene_does_not_reuse_stale_mode_switch(products, mode):
    history = OperationHistory()
    for attrs in (
        {"status": 1, "work_mode": 0, "auto_switch": False},
        {"status": 2, "work_mode": mode, "auto_switch": True},
        {"status": 1},
    ):
        history.observe("z60", products[Z60], attrs, "lan")
    assert history.summary("z60", "sterilize")["count"] == 0


def test_dual_cabinet_chambers_have_separate_records(products):
    product = products["k1mvpEHtjsK000000000000000000000"]
    assert set(history_kinds(product)) == {"cabin1_sterilize", "cabin2_sterilize"}
    history = OperationHistory()
    for status in (1, 2, 1):
        history.observe(
            "dual",
            product,
            {
                "cabin1_status": status,
                "cabin1_work_mode": 3,
                "cabin2_status": 1,
                "cabin2_work_mode": 0,
            },
            "lan",
        )
    assert history.summary("dual", "cabin1_sterilize")["count"] == 1
    assert history.summary("dual", "cabin2_sterilize")["count"] == 0


def test_disconnect_and_restart_do_not_invent_offline_activity(products):
    history = OperationHistory()
    for boiling in (False, True):
        history.observe("p30", products[P30], {"boiling": boiling}, "lan")
    history.observation_gap("p30")
    history.observe("p30", products[P30], {"boiling": False}, "lan")
    saved = history.summary("p30", "boil")
    assert saved["count"] == 1
    assert saved["records"][0]["outcome"] == "observation_gap"
    restored = OperationHistory(history.data)
    restored.observe("p30", products[P30], {"boiling": True}, "lan")
    assert restored.summary("p30", "boil")["count"] == 1


def test_transport_switch_does_not_replay_old_cloud_state(products):
    history = OperationHistory()
    for value, source in ((False, "lan"), (True, "lan"), (False, "cloud"), (True, "lan")):
        history.observe("p30", products[P30], {"boiling": value}, source)
    journal = history.summary("p30", "boil")
    assert journal["count"] == 1
    assert journal["records"][0]["outcome"] == "observation_gap"


def test_history_is_bounded_but_count_survives_restart(products):
    history = OperationHistory()
    for volume in range(HISTORY_LIMIT + 12):
        history.observe("p30", products[P30], {"record": volume}, "lan")
    journal = history.summary("p30", "water")
    assert journal["count"] == HISTORY_LIMIT + 11
    assert len(journal["records"]) == HISTORY_LIMIT
    restored = OperationHistory(history.data)
    assert restored.summary("p30", "water") == journal


def test_water_total_accumulates_confirmed_dispenses_once(products):
    history = OperationHistory()
    for attrs in (
        {"detect_status": False, "record": 0},
        {"detect_status": True},
        {"record": 60},
        {"record": 120},
        {"detect_status": False, "record": 120},
        {"detect_status": False, "record": 120},
        {"record": 180},
        {"record": 180},
        {"record": 0},
    ):
        history.observe("p30", products[P30], attrs, "lan")
    journal = history.summary("p30", "water")
    assert journal["count"] == 2
    assert journal["total_volume_ml"] == 300
    assert history.summary("other", "water")["total_volume_ml"] == 0


def test_water_total_survives_trimming_restart_and_observation_gaps(products):
    history = OperationHistory()
    volumes = range(2000, 2000 + HISTORY_LIMIT + 12)
    history.observe("p30", products[P30], {"record": 0}, "lan")
    for volume in volumes:
        history.observe("p30", products[P30], {"record": volume}, "lan")
    journal = history.summary("p30", "water")
    assert len(journal["records"]) == HISTORY_LIMIT
    assert journal["total_volume_ml"] == sum(volumes) > 99999

    restored = OperationHistory(history.data)
    restored.observe("p30", products[P30], {"record": 3000}, "lan")
    restored.observation_gap("p30")
    restored.observe("p30", products[P30], {"record": 4000}, "lan")
    restored.observe("p30", products[P30], {"record": 100}, "cloud")
    assert restored.summary("p30", "water")["total_volume_ml"] == sum(volumes)
    restored.observe("p30", products[P30], {"record": 120}, "cloud")
    assert restored.summary("p30", "water")["total_volume_ml"] == sum(volumes) + 120


@pytest.mark.parametrize("total", [None, -1, True, "invalid", float("nan"), float("inf")])
def test_legacy_water_total_uses_only_retained_valid_volumes(total):
    history = OperationHistory(
        {
            "p30": {
                "water": {
                    "count": 100,
                    "records": [
                        {"volume_ml": volume}
                        for volume in (120, 180, None, -1, True, float("nan"), float("inf"))
                    ],
                    "total_volume_ml": total,
                }
            }
        }
    )
    assert history.summary("p30", "water")["total_volume_ml"] == 300


def test_missing_invalid_reports_and_old_pending_volume(products):
    history = OperationHistory()
    now = datetime(2026, 11, 10, tzinfo=UTC)
    for attrs in ({"detect_status": False}, {"detect_status": True}, {"detect_status": False}):
        history.observe("p30", products[P30], attrs, "lan", now)
    for value in (float("nan"), float("inf"), True, -1):
        history.observe("p30", products[P30], {"record": value}, "lan", now)
    history.observe("p30", products[P30], {"record": 180}, "lan", now + timedelta(minutes=5))
    assert history.summary("p30", "water")["count"] == 2
    assert history.summary("p30", "water")["total_volume_ml"] == 180
