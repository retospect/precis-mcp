"""The shared ``release.round`` record: row shape and the store twin
(``precis.plans.release`` mirrors ``scripts/round``'s publish, td573429)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis import settings as psettings
from precis.plans import release as plans_release
from precis.store import Store

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
OWNER = {"host": "fake-node", "checkout": "/fake/checkout"}
BASE = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


def _rel(**over: object) -> dict:
    rel = {
        "n": 1,
        "branch": "release/r1",
        "base": BASE,
        "state": "cut",
        "cut_at": "2026-11-06T11:00:00Z",
    }
    rel.update(over)
    return rel


def test_row_from_open_record_has_no_deploy() -> None:
    row = plans_release.round_record(_rel(), n=1, owner=OWNER, now=NOW)
    assert row == {
        "n": 1,
        "branch": "release/r1",
        "base": BASE,
        "state": "cut",
        "owner": OWNER,
        "cut_at": "2026-11-06T11:00:00Z",
    }


def test_row_from_deploying_record_maps_the_journal() -> None:
    dep = {
        "sha": BASE,
        "started_at": "2026-11-06T11:30:00Z",
        "rollout_at": "2026-11-06T11:40:00Z",
        "runtime_confirmed_at": "2026-11-06T11:55:00Z",
        "runtime_evidence": "health green",
        "skipped_hosts": {"fake-host": "aa" * 20},
    }
    row = plans_release.round_record(
        _rel(state="confirmed", deployment=dep), n=1, owner=OWNER, now=NOW
    )
    assert row["deploy"] == {
        "sha": BASE,
        "rollout_at": "2026-11-06T11:40:00Z",
        "confirmed_at": "2026-11-06T11:55:00Z",
        "evidence": "health green",
    }


def test_row_omits_absent_deploy_keys() -> None:
    row = plans_release.round_record(
        _rel(state="deploying", deployment={"sha": BASE, "started_at": "x"}),
        n=1,
        owner=OWNER,
        now=NOW,
    )
    assert row["deploy"] == {"sha": BASE}


def test_record_round_then_read_round(store: Store) -> None:
    row = plans_release.round_record(_rel(), n=1, owner=OWNER, now=NOW)
    line = plans_release.record_round(store, row)
    assert line == "round record recorded: r1 cut"
    assert plans_release.read_round(store) == row


def test_read_round_tolerates_unset_and_garbage(store: Store) -> None:
    assert plans_release.read_round(store) is None
    for bad in ("{not json", "[1, 2]", '"just a string"', "null", "7"):
        psettings.set_setting(plans_release.ROUND_KEY, bad, store=store)
        assert plans_release.read_round(store) is None, bad
