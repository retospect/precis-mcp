"""``precis.workers.activity`` — the in-process "what is this worker doing
right now" registry (see the module docstring for why it exists)."""

from __future__ import annotations

import pytest

from precis.workers import activity


@pytest.fixture(autouse=True)
def _reset_activity_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module-global state — reset to empty before each test so one test's
    pass never leaks into the next (mirrors ``test_heartbeat_pass.py``'s
    ``_reset_throttle`` pattern of monkeypatching private module state)."""
    monkeypatch.setattr(activity, "_state", {})


def test_snapshot_is_empty_before_first_use() -> None:
    assert activity.snapshot() == {}


def test_set_pass_records_name_and_since() -> None:
    activity.set_pass("fetch_oa")
    snap = activity.snapshot()
    assert snap["pass"] == "fetch_oa"
    assert isinstance(snap["since"], str)
    assert "idle" not in snap


def test_note_attaches_detail_to_the_active_pass() -> None:
    activity.set_pass("fetch_oa")
    activity.note("stub 3/10")
    snap = activity.snapshot()
    assert snap["detail"] == "stub 3/10"
    assert snap["pass"] == "fetch_oa"


def test_note_before_set_pass_is_a_noop() -> None:
    assert activity.snapshot() == {}
    activity.note("should be ignored")
    assert activity.snapshot() == {}


def test_note_after_clear_is_a_noop() -> None:
    activity.set_pass("fetch_oa")
    activity.clear()
    activity.note("should be ignored")
    snap = activity.snapshot()
    assert "detail" not in snap


def test_clear_records_last_pass_and_finished() -> None:
    activity.set_pass("fetch_oa")
    activity.clear()
    snap = activity.snapshot()
    assert snap["idle"] is True
    assert snap["last_pass"] == "fetch_oa"
    assert isinstance(snap["finished"], str)
    assert "pass" not in snap
    assert "detail" not in snap


def test_clear_with_no_active_pass_omits_last_pass() -> None:
    activity.clear()  # no set_pass beforehand
    snap = activity.snapshot()
    assert snap["idle"] is True
    assert "last_pass" not in snap
    assert isinstance(snap["finished"], str)


def test_a_fresh_set_pass_drops_the_previous_passs_detail() -> None:
    activity.set_pass("chase")
    activity.note("ref 5")
    activity.set_pass("fetch_oa")
    snap = activity.snapshot()
    assert snap["pass"] == "fetch_oa"
    assert "detail" not in snap


def test_snapshot_returns_a_copy_not_live_state() -> None:
    activity.set_pass("fetch_oa")
    snap = activity.snapshot()
    snap["pass"] = "mutated"
    snap["new_key"] = "should not leak back"
    fresh = activity.snapshot()
    assert fresh["pass"] == "fetch_oa"
    assert "new_key" not in fresh
