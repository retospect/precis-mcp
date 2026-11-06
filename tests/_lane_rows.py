"""Lane-table fixtures for the ``llm_account``-derived lanes (td568401 S1c).

``SEED_LANES`` is the permanent pin: a literal copy of the table the
``llm_account`` rows must keep deriving, row for row, order included —
the launchable-adapter rows only (``kimi``/``metered`` stay account
rows, pinned in ``tests/test_llm_accounts.py``). ``use_lanes`` stands an
arbitrary table for fake-store tests by patching
``precis.utils.llm.accounts.list_accounts`` — ``lanes.load_lanes`` reads
the seam through the module attribute, so no test here needs a DB. A
stood lane on an unlaunchable adapter derives to nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.plans.lanes import ANY_CLASS, Lane
from precis.utils.llm.accounts import Account

#: Literal copy of the seed the ``llm_account`` rows derive (migration
#: 0280 + 0283): the preference order rides ``lane_rank``, and
#: ``credential_pool`` carries the account's ``plan`` (nothing reads it).
SEED_LANES: tuple[Lane, ...] = (
    Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=True,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
    ),
    Lane(
        name="codex",
        adapter="codex_exec",
        credential_pool="subscription",
        cap=1,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=True,
        vendor="openai",
    ),
    Lane(
        name="claude-sub",
        adapter="claude_p",
        credential_pool="subscription",
        cap=2,
        task_classes=frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=(
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5",
            "claude-opus-4-8",
        ),
        default_model="claude-sonnet-5",
        model_prefix="claude-",
    ),
)


def seed_lane(name: str) -> Lane:
    """The ``SEED_LANES`` row called ``name``."""
    return next(ln for ln in SEED_LANES if ln.name == name)


def _account_of(lane: Lane, rank: int) -> Account:
    """The ``llm_account`` row that derives back to ``lane``."""
    return Account(
        name=lane.name,
        vendor=lane.vendor,
        plan=lane.credential_pool,
        adapter=lane.adapter,
        profile=None,
        api_key_ref=None,
        concurrency=lane.cap,
        task_classes=tuple(sorted(lane.task_classes)),
        models=lane.models,
        default_model=lane.default_model,
        enabled=lane.enabled,
        entitlement={},
        privacy={},
        promo_expiry=None,
        pools=(),
        updated_at=datetime.now(UTC),
        lane_rank=rank,
    )


def use_lanes(monkeypatch: pytest.MonkeyPatch, *table: Lane) -> tuple[Lane, ...]:
    """Stand ``table`` (default :data:`SEED_LANES`) as the lane table:
    patch ``accounts.list_accounts`` to return rows that derive to it —
    rank = position. Returns the lanes for lookups."""
    from precis.utils.llm import accounts

    lanes = table or SEED_LANES
    rows = [_account_of(lane, i) for i, lane in enumerate(lanes)]
    monkeypatch.setattr(accounts, "list_accounts", lambda store: list(rows))
    return lanes


__all__ = ["SEED_LANES", "seed_lane", "use_lanes"]
