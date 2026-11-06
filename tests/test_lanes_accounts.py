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
from tests._lane_rows import SEED_LANES, use_lanes


def test_derived_table_equals_the_seed(store: Any) -> None:
    """``load_lanes`` on the seeded accounts reproduces the seed
    table row for row, order included — behaviour is identical to the
    code seed it replaces. The ``kimi``/``metered`` account rows stay
    accounts: ``kimi_tree`` and ``api`` are not launchable, so they
    derive to no lane. ``cost_class`` is compared apart (td573580
    review): devin's seed promo expiry passes on 2026-12-01 and the
    literal pin would go red that day."""
    assert [replace(ln, cost_class="") for ln in lanes.load_lanes(store)] == [
        replace(ln, cost_class="") for ln in SEED_LANES
    ]


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
