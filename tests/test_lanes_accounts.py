"""td568401 S1c: the lane table derives from ``llm_account`` rows.

``lanes.load_lanes`` must reproduce the seed row for row, order included
— the permanent pin is the literal copy in
``tests/_lane_rows.py::SEED_LANES`` (src may not hold one). Rows on an
adapter the builder cannot launch never derive.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans import lanes
from precis.plans.lanes import ANY_CLASS, Lane
from tests._lane_rows import SEED_LANES, seed_lane, use_lanes


def test_derived_table_equals_the_seed(store: Any) -> None:
    """``load_lanes`` on the seeded accounts reproduces the seed
    table row for row, order included — behaviour is identical to the
    code seed it replaces. The ``kimi`` row derives a lane since
    td573681 (``kimi_tree`` is launchable) but stays disabled; the
    ``metered`` row stays an account — ``api`` is not launchable, so
    it derives to no lane. ``cost_class`` is compared apart (td573580
    review): devin's seed promo expiry passes on 2026-12-01 and the
    literal pin would go red that day."""
    assert [replace(ln, cost_class="") for ln in lanes.load_lanes(store)] == [
        replace(ln, cost_class="") for ln in SEED_LANES
    ]


def test_kimi_row_derives_a_disabled_lane(store: Any) -> None:
    """td573681: the seeded kimi account row now derives a lane — it
    keeps the row's ``enabled=False``, so the pick never offers it
    until an operator lights the row."""
    kimi = lanes.get_lane("kimi", store)
    assert kimi is not None and kimi.adapter == "kimi_tree"
    assert kimi.enabled is False
    enabled = {ln.name for ln in lanes.load_lanes(store) if ln.enabled}
    assert "kimi" not in enabled


def test_an_enabled_kimi_lane_is_pickable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the kimi row is lit, its lane picks like any other."""
    kimi = Lane(
        name="kimi",
        adapter="kimi_tree",
        credential_pool="subscription",
        cap=1,
        task_classes=frozenset({"code"}),
        enabled=True,
    )
    use_lanes(monkeypatch, kimi)
    (derived,) = lanes.load_lanes(object())
    assert derived == kimi
    assert lanes.pick_lane(object(), "code", lanes=(derived,), counts={}) == kimi


def test_cost_class_from_the_account_row() -> None:
    """td573580 review: the class follows ``plan`` and ``promo_expiry`` —
    no expiry or one not past is free, an expired promo or any other
    plan is subscription, ``metered`` is metered."""
    today = datetime.now(UTC).date()
    cases = [
        (SimpleNamespace(plan="free-promo", promo_expiry=None), lanes.COST_FREE),
        (
            SimpleNamespace(plan="free-promo", promo_expiry=today + timedelta(30)),
            lanes.COST_FREE,
        ),
        (
            SimpleNamespace(plan="free-promo", promo_expiry=today),
            lanes.COST_FREE,
        ),
        (
            SimpleNamespace(plan="free-promo", promo_expiry=today - timedelta(1)),
            lanes.COST_SUBSCRIPTION,
        ),
        (SimpleNamespace(plan="metered", promo_expiry=None), lanes.COST_METERED),
        (
            SimpleNamespace(plan="subscription", promo_expiry=None),
            lanes.COST_SUBSCRIPTION,
        ),
    ]
    for account, want in cases:
        assert lanes._cost_class(account) == want, account


def test_an_enabled_api_adapter_account_is_not_a_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled account on an adapter the builder cannot launch must
    never surface as a lane — a picked one would mint jobs that die at
    ``get_adapter``."""
    api = Lane(
        name="metered",
        adapter="api",
        credential_pool="metered",
        cap=1,
        task_classes=frozenset({ANY_CLASS}),
        enabled=True,
    )
    use_lanes(monkeypatch, api)
    assert lanes.load_lanes(object()) == ()


def test_launchable_adapters_match_the_slice_adapter_registry() -> None:
    """The drift guard for the restated adapter set."""
    from precis.workers.job_types._slice_adapters import ADAPTERS

    assert set(ADAPTERS) == set(lanes.LAUNCHABLE_ADAPTERS)


def test_pick_lane_returns_none_on_an_empty_lane_list() -> None:
    assert lanes.pick_lane(None, "code", lanes=(), counts={}) is None


def _parked(lane: Lane, reason: str = "auth:not-logged-in") -> Lane:
    """``lane`` with the health park stamped (gr588221): ``parked_reason``
    set holds the lane out of dispatch exactly like ``enabled=False``
    without touching the human ``enabled`` switch."""
    return replace(
        lane, parked_reason=reason, parked_at=datetime.now(UTC) - timedelta(1)
    )


def test_derive_lanes_carries_the_park_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account row's ``parked_reason``/``parked_at`` derive onto the
    lane — the probe's stamp is what pick_lane and the ladder read."""
    sub = _parked(seed_lane("claude-sub"))
    use_lanes(monkeypatch, sub)
    (derived,) = lanes.load_lanes(object())
    assert derived.parked and derived.parked_reason == "auth:not-logged-in"
    assert derived.parked_at == sub.parked_at
    assert derived.enabled  # parked never writes the human switch


def test_a_parked_lane_is_skipped_like_a_disabled_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr588221: ``pick_lane`` and ``ladder`` treat ``parked_reason`` set as
    "skip this lane" — the devin lane still picks and climbs; the parked
    claude-sub contributes nothing. A class whose only fit lane is parked
    reads as the no-enabled-lane case."""
    devin = seed_lane("devin")
    sub = _parked(seed_lane("claude-sub"))
    use_lanes(monkeypatch, devin, sub)
    table = lanes.load_lanes(object())
    pick = lanes.pick_lane(object(), "code", lanes=table, counts={})
    assert pick is not None and pick.name == "devin"
    assert all(r.lane != "claude-sub" for r in lanes.ladder("code", lanes=table))
    # The parked lane keeps its human switch — the park is not an edit.
    assert next(ln for ln in table if ln.name == "claude-sub").enabled
    # Only fit lane parked → same as no enabled lane.
    use_lanes(monkeypatch, sub)
    assert (
        lanes.pick_lane(object(), "code", lanes=lanes.load_lanes(object()), counts={})
        is None
    )
    assert lanes.ladder("code", lanes=lanes.load_lanes(object())) == ()


def test_park_helpers_stamp_and_clear_without_touching_enabled(
    store: Any,
) -> None:
    """``park_lane``/``unpark_lane``/``record_lane_health`` write only the
    health columns: the account's ``enabled`` (the human switch), plan and
    classes are untouched by every call."""
    from precis.utils.llm import accounts

    now = datetime.now(UTC)
    before = accounts.get_account(store, "claude-sub")
    assert before is not None and before.parked_reason is None
    assert lanes.park_lane(store, "claude-sub", "auth:not-logged-in", now) is True
    parked = accounts.get_account(store, "claude-sub")
    assert parked is not None
    assert parked.parked_reason == "auth:not-logged-in"
    assert (
        parked.parked_at is not None
        and abs((parked.parked_at - now).total_seconds()) < 5
    )
    assert parked.health_outcome == "auth" and parked.health_checked_at is not None
    assert parked.enabled == before.enabled
    # A second park keeps the FIRST parked_at — a flap does not extend
    # the >6h alert clock.
    assert lanes.park_lane(
        store, "claude-sub", "auth:http-401", now + timedelta(hours=1)
    )
    again = accounts.get_account(store, "claude-sub")
    assert again is not None
    assert again.parked_reason == "auth:http-401"
    assert again.parked_at == parked.parked_at
    # Unpark clears the park and records the green outcome.
    assert lanes.unpark_lane(store, "claude-sub", now + timedelta(hours=2)) is True
    healthy = accounts.get_account(store, "claude-sub")
    assert healthy is not None
    assert healthy.parked_reason is None and healthy.parked_at is None
    assert healthy.health_outcome == "ok"
    assert healthy.enabled == before.enabled
    # Refresh-only: the stamp moves, the park state does not appear.
    assert lanes.record_lane_health(store, "claude-sub", "unprobed", now)
    stamped = accounts.get_account(store, "claude-sub")
    assert stamped is not None and stamped.health_outcome == "unprobed"
    assert stamped.parked_reason is None
    # Unknown lane names refuse, they never insert a row.
    assert lanes.park_lane(store, "no-such-lane", "auth:x", now) is False
    assert lanes.unpark_lane(store, "no-such-lane", now) is False
