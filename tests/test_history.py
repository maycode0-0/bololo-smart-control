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


def test_missing_invalid_reports_and_old_pending_volume(products):
    history = OperationHistory()
    now = datetime(2026, 11, 10, tzinfo=UTC)
    for attrs in ({"detect_status": False}, {"detect_status": True}, {"detect_status": False}):
        history.observe("p30", products[P30], attrs, "lan", now)
    for value in (float("nan"), float("inf"), True, -1):
        history.observe("p30", products[P30], {"record": value}, "lan", now)
    history.observe("p30", products[P30], {"record": 180}, "lan", now + timedelta(minutes=5))
    assert history.summary("p30", "water")["count"] == 2
