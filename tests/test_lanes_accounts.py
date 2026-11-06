"""td568401 S1c: the lane table derives from ``llm_account`` rows.

``lanes.load_lanes`` must reproduce the seed row for row, order included
— the permanent pin is the literal copy in
``tests/_lane_rows.py::SEED_LANES`` (src may not hold one). Rows on an
adapter the builder cannot launch never derive.
"""

from __future__ import annotations

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
    derive to no lane."""
    assert lanes.load_lanes(store) == SEED_LANES


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
