"""Tests for the planner-coroutine cost guardrails.

These caps had **no test coverage at all**, and both dollar checks
summed a ``meta.cost_usd`` key that nothing in the codebase ever wrote
— so they read $0.00 forever and never fired. A soft-deleted project
tree then burned $291 over five days against a nominal $20/day ceiling.

Every test here asserts a cap *fires on real recorded spend*, so the
same silent death can't recur: the assertions fail if the query stops
matching the ledger.
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.workers.planner_guardrails import RoundContext, check_parent


def _todo(store: Store, title: str, *, parent_id: int | None = None) -> int:
    """Insert a dispatchable (``meta.llm_tier``-set) todo, return its id."""
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"llm_tier": "opus", "job_type": "plan_tick"},
        parent_id=parent_id,
    )
    return int(ref.id)


def _log_spend(
    store: Store, ref_id: int | None, cost: float, *, hours_ago: float = 0.0
) -> None:
    """Write one ``llm_call_log`` row — the ledger the caps read."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (ts, source, transport, model, cost_usd, ref_id) "
            "VALUES (now() - make_interval(mins => %s), 'plan_tick', "
            "'claude_agent', 'claude-opus-4-8', %s, %s)",
            (int(hours_ago * 60), cost, ref_id),
        )
        conn.commit()


def _soft_delete(store: Store, ref_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (ref_id,))
        conn.commit()


@pytest.fixture(autouse=True)
def _caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every cap so a changed default can't silently pass a test."""
    monkeypatch.setenv("PRECIS_MAX_TICKS", "10")
    monkeypatch.setenv("PRECIS_MAX_TODO_USD", "2.0")
    monkeypatch.setenv("PRECIS_MAX_TREE_USD", "10.0")
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "20.0")


# ── per-todo cost cap ────────────────────────────────────────────


def test_per_todo_cost_cap_fires_on_logged_spend(store: Store) -> None:
    """THE regression: spend recorded in ``llm_call_log`` trips the cap.

    ``plan_tick`` stamps ``LlmRequest.ref_id = parent_ref_id``, so the
    todo's own ledger rows *are* its lifetime cost. Before the fix this
    read a never-written ``meta.cost_usd`` and returned $0.00.
    """
    rid = _todo(store, "expensive planner")
    _log_spend(store, rid, 1.40)
    _log_spend(store, rid, 1.30)  # $2.70 total, over the $2 cap

    verdict = check_parent(store, parent_ref_id=rid)

    assert verdict.allow is False
    assert verdict.halt_tag == "halt:cost-cap"
    assert "2.70" in (verdict.reason or "")


def test_per_todo_cost_cap_allows_under_budget(store: Store) -> None:
    rid = _todo(store, "cheap planner")
    _log_spend(store, rid, 0.55)

    assert check_parent(store, parent_ref_id=rid).allow is True


def test_per_todo_cost_cap_ignores_other_todos_spend(store: Store) -> None:
    """Attribution is by ``ref_id`` — a sibling's spend must not halt us."""
    mine = _todo(store, "mine")
    theirs = _todo(store, "theirs")
    _log_spend(store, theirs, 5.00)

    assert check_parent(store, parent_ref_id=mine).allow is True


# ── per-tree cost cap ────────────────────────────────────────────


def test_tree_cost_cap_fires_across_wide_fanout(store: Store) -> None:
    """The fan-out hole: each sibling under its own cap, tree far over.

    A per-*todo* cap alone gives N siblings N × $2 of headroom — 258 of
    them is $516 nobody authorised. The tree cap is what actually bounds
    a project.
    """
    root = _todo(store, "project root")
    kids = [_todo(store, f"section {i}", parent_id=root) for i in range(6)]
    for k in kids:
        _log_spend(store, k, 1.80)  # under the $2 per-todo cap...

    # ...but $10.80 across the tree, over the $10 tree cap.
    verdict = check_parent(store, parent_ref_id=kids[0])

    assert verdict.allow is False
    assert verdict.halt_tag == "halt:tree-cost-cap"


def test_tree_cost_cap_counts_spend_under_deleted_ancestor(store: Store) -> None:
    """Deleting the parent must not reset the tree's budget."""
    root = _todo(store, "root")
    kid = _todo(store, "kid", parent_id=root)
    _log_spend(store, root, 9.00)
    _log_spend(store, kid, 1.50)
    _soft_delete(store, root)

    verdict = check_parent(store, parent_ref_id=kid)

    assert verdict.allow is False
    assert verdict.halt_tag == "halt:tree-cost-cap"


def test_tree_cost_cap_allows_unrelated_trees(store: Store) -> None:
    mine = _todo(store, "my root")
    other = _todo(store, "other root")
    # Over the $10 tree cap for *that* tree, under the $20 daily ceiling
    # so this asserts tree isolation and nothing else.
    _log_spend(store, _todo(store, "other kid", parent_id=other), 12.00)

    assert check_parent(store, parent_ref_id=mine).allow is True


# ── global daily ceiling ─────────────────────────────────────────


def test_daily_ceiling_fires_on_fleetwide_spend(store: Store) -> None:
    """The ceiling covers *all* logged spend, not just this todo's."""
    rid = _todo(store, "innocent planner")
    for _ in range(5):
        _log_spend(store, None, 4.50)  # $22.50 fleetwide, over $20

    verdict = check_parent(store, parent_ref_id=rid)

    assert verdict.allow is False
    # Global ceiling deliberately tags nothing — it aborts the round.
    assert verdict.halt_tag is None
    assert "daily ceiling" in (verdict.reason or "")


def test_daily_ceiling_ignores_spend_outside_the_window(store: Store) -> None:
    rid = _todo(store, "planner")
    _log_spend(store, None, 100.00, hours_ago=30)

    assert check_parent(store, parent_ref_id=rid).allow is True


# ── tick cap (unchanged behaviour, previously untested) ──────────


def test_tick_cap_fires(store: Store) -> None:
    rid = _todo(store, "spinning planner")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || '{\"tick_count\": 10}'::jsonb "
            "WHERE ref_id = %s",
            (rid,),
        )
        conn.commit()

    verdict = check_parent(store, parent_ref_id=rid)

    assert verdict.allow is False
    assert verdict.halt_tag == "halt:tick-cap"


def test_clean_parent_is_allowed(store: Store) -> None:
    """Control: no ticks, no spend ⇒ dispatch proceeds."""
    assert check_parent(store, parent_ref_id=_todo(store, "fresh")).allow is True


# ── round memo + fail-closed ─────────────────────────────────────


def test_round_context_shares_one_tree_walk_across_siblings(store: Store) -> None:
    """Siblings under one root must not each re-walk the same subtree."""
    root = _todo(store, "root")
    kids = [_todo(store, f"kid {i}", parent_id=root) for i in range(4)]
    _log_spend(store, root, 1.00)

    ctx = RoundContext()
    for k in kids:
        check_parent(store, parent_ref_id=k, ctx=ctx)

    # One memo entry (keyed by root), not one per sibling.
    assert list(ctx.tree_cost.values()) == [1.00]
    assert ctx.daily_cost == 1.00


def test_round_context_still_halts_the_right_parents(store: Store) -> None:
    """The memo is an optimisation — verdicts must be unchanged by it."""
    root = _todo(store, "root")
    kids = [_todo(store, f"kid {i}", parent_id=root) for i in range(6)]
    for k in kids:
        _log_spend(store, k, 1.80)

    ctx = RoundContext()
    assert all(
        check_parent(store, parent_ref_id=k, ctx=ctx).halt_tag == "halt:tree-cost-cap"
        for k in kids
    )


def test_dispatch_fails_closed_when_guardrail_errors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken guardrail must never wave a candidate through.

    Silently allowing dispatch on a guardrail error re-creates exactly the
    bug this module exists to prevent — a cost cap that isn't enforced.
    One bad candidate is skipped; the round continues for the rest.
    """
    from precis.workers import dispatch as dispatch_mod

    rid = _todo(store, "candidate")

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError("llm_call_log unavailable")

    monkeypatch.setattr(dispatch_mod.planner_guardrails, "check_parent", _boom)
    result = dispatch_mod.run_dispatch_pass(store)

    assert result.ok == 0
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s AND kind = 'job'",
            (rid,),
        ).fetchone()
    assert n is not None and int(n[0]) == 0
