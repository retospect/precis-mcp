"""Tests for the quest allocator — which striving ticks next (slice 4d).

Covers the raw bandit score (priority × momentum × promise), the pick (highest
eligible score), the weekly proportional budget (over-budget quests skipped),
self-cooling of a cold quest to dormant, and the gated allocator pass (dark
unless enabled; picks + ticks + folds the EWMA). The tick is monkeypatched so no
live model runs. Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import allocator as alloc
from precis.quest.logbook import append_entry


def _mk_quest(store: Any, text: str, *, prio: str | None = None) -> int:
    h = QuestHandler(hub=Hub(store=store))
    tags = [prio] if prio else None
    resp = h.put(text=text, tags=tags)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _backdate_logbook(store: Any, quest_id: int, *, days: int) -> None:
    """Push this quest's logbook chunks ``days`` into the past (window tests)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = now() - make_interval(days => %s) "
            "WHERE ref_id = %s AND chunk_kind = 'quest_log'",
            (days, quest_id),
        )
        conn.commit()


# ── scoring ───────────────────────────────────────────────────────────


class TestScoring:
    def test_hotter_priority_scores_higher(self, store: Any) -> None:
        hot = _mk_quest(store, "Urgent", prio="PRIO:urgent")  # prio 1
        cold = _mk_quest(store, "Low", prio="PRIO:low")  # prio 8
        assert alloc.raw_score(store, hot) > alloc.raw_score(store, cold)

    def test_pick_selects_highest(self, store: Any) -> None:
        _cold = _mk_quest(store, "Low", prio="PRIO:low")
        hot = _mk_quest(store, "Urgent", prio="PRIO:urgent")
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == hot

    def test_untried_quest_bootstraps_over_hot_spinner(self, store: Any) -> None:
        # A converged-on quest with a high EWMA + many picks must not starve a
        # never-ticked one: the exploration bonus alone can't overcome a locked
        # EWMA, so untried quests are bootstrapped first (cold-start fix).
        spinner = _mk_quest(store, "Hot spinner", prio="PRIO:urgent")  # hottest
        alloc._merge_meta(store, spinner, {"picks": 500, "ewma_score": 0.9})
        cold = _mk_quest(store, "Never ticked", prio="PRIO:low")  # coldest prio
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == cold

    def test_hottest_untried_leads_among_untried(self, store: Any) -> None:
        _cold = _mk_quest(store, "Low", prio="PRIO:low")
        hot = _mk_quest(store, "Urgent", prio="PRIO:urgent")
        # both untried → the hotter raw score bootstraps first
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == hot

    def test_bandit_resumes_once_all_tried(self, store: Any) -> None:
        # With every quest picked at least once, ranking falls back to the
        # smoothed bandit — the higher EWMA wins.
        a = _mk_quest(store, "A", prio="PRIO:normal")
        b = _mk_quest(store, "B", prio="PRIO:normal")
        alloc._merge_meta(store, a, {"picks": 3, "ewma_score": 0.2})
        alloc._merge_meta(store, b, {"picks": 3, "ewma_score": 0.8})
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == b

    def test_dormant_quests_are_not_picked(self, store: Any) -> None:
        h = QuestHandler(hub=Hub(store=store))
        active = _mk_quest(store, "Active", prio="PRIO:normal")
        dorm = _mk_quest(store, "Dormant", prio="PRIO:urgent")
        h.tag(id=dorm, add=["STATUS:dormant"])
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == active


# ── weekly budget ─────────────────────────────────────────────────────


class TestBudget:
    def test_over_budget_quest_is_skipped(self, store: Any) -> None:
        a = _mk_quest(store, "A", prio="PRIO:normal")
        b = _mk_quest(store, "B", prio="PRIO:normal")
        # equal priority → equal shares; total 10 → 5 each. A "spends" 6 chars.
        append_entry(store, a, text="sim", entry_type="result", by="agent", chars=6)
        pick = alloc.pick_next_quest(store, total_budget=10.0)
        assert pick is not None and pick.quest_id == b

    def test_no_budget_means_no_cap(self, store: Any) -> None:
        a = _mk_quest(store, "A", prio="PRIO:urgent")
        append_entry(store, a, text="sim", entry_type="result", by="agent", chars=999)
        assert alloc.over_budget(store, a, [a], total_budget=None) is False

    def test_weekly_chars_sums(self, store: Any) -> None:
        a = _mk_quest(store, "A")
        append_entry(store, a, text="x", entry_type="result", by="agent", chars=150)
        append_entry(store, a, text="y", entry_type="cost", by="agent", chars=200)
        assert alloc.weekly_chars(store, a) == 350

    def test_window_days_narrows_the_tote(self, store: Any) -> None:
        # A 6-char usage 3 days ago: inside a 7d window (over its 5-char
        # share → skip), outside a 1d window (under → eligible). Proves the
        # window plumbs through over_budget (design §9: tab tunes 7d → 24/48h).
        a = _mk_quest(store, "A", prio="PRIO:normal")
        b = _mk_quest(store, "B", prio="PRIO:normal")
        append_entry(store, a, text="old sim", entry_type="cost", by="agent", chars=6)
        _backdate_logbook(store, a, days=3)
        assert (
            alloc.over_budget(store, a, [a, b], total_budget=10.0, window_days=7)
            is True
        )
        assert (
            alloc.over_budget(store, a, [a, b], total_budget=10.0, window_days=1)
            is False
        )

    def test_window_days_none_uses_env_default(self, store: Any) -> None:
        # window_days=None resolves to BUDGET_WINDOW_DAYS (7 by default), so
        # recent usage is counted exactly as before the param existed.
        a = _mk_quest(store, "A", prio="PRIO:normal")
        b = _mk_quest(store, "B", prio="PRIO:normal")
        append_entry(store, a, text="sim", entry_type="cost", by="agent", chars=6)
        assert (
            alloc.over_budget(store, a, [a, b], total_budget=10.0, window_days=None)
            is True
        )


# ── self-cooling ──────────────────────────────────────────────────────


class TestCooling:
    def test_cold_quest_cools_to_dormant(self, store: Any) -> None:
        q = _mk_quest(store, "A cold striving", prio="PRIO:normal")
        alloc._merge_meta(
            store,
            q,
            {
                "tick_count": alloc.COOL_AFTER_TICKS + 1,
                "ticks_since_frontier_improve": alloc.COOL_AFTER_TICKS + 1,
                "promise": 0.0,
            },
        )
        cooled = alloc.cool_stalled(store)
        assert q in cooled
        assert "STATUS:dormant" in [str(t) for t in store.tags_for(q)]
        logs = [b for b in store.list_blocks_for_ref(q) if b.chunk_kind == "quest_log"]
        assert any((b.meta or {}).get("entry_type") == "reflection" for b in logs)

    def test_fresh_quest_not_cooled(self, store: Any) -> None:
        q = _mk_quest(store, "A fresh striving")
        assert alloc.cool_stalled(store) == []
        assert "STATUS:active" in [str(t) for t in store.tags_for(q)]


# ── the gated pass ────────────────────────────────────────────────────


class TestAllocatorPass:
    def test_disabled_by_default(self, store: Any) -> None:
        _mk_quest(store, "A striving")
        out = alloc.run_allocator_pass(store, enabled=False)
        assert out["enabled"] is False and out["picked"] is None

    def test_enabled_picks_and_ticks_and_folds_ewma(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.quest import tick as tick_mod

        calls: list[int] = []

        def _fake_tick(_store: Any, qid: int, **_kw: Any) -> Any:
            calls.append(qid)
            return SimpleNamespace(status="succeeded", quest_id=qid)

        monkeypatch.setattr(tick_mod, "run_quest_tick", _fake_tick)
        q = _mk_quest(store, "A striving", prio="PRIO:urgent")
        out = alloc.run_allocator_pass(store, enabled=True)
        assert out["picked"] == q and out["status"] == "succeeded"
        assert calls == [q]
        meta = store.get_ref(kind="quest", id=q).meta
        assert meta["picks"] == 1 and "ewma_score" in meta

    def test_breaker_pause_skips_without_recording_pick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        # A paused tick (window-scoped breaker trip) must not burn the pick:
        # picked=None (so the dispatch pass reports a skip, not a failure) and
        # no picks/EWMA bump (so a capped window doesn't prematurely cool it).
        from precis.quest import tick as tick_mod

        def _paused_tick(_store: Any, qid: int, **_kw: Any) -> Any:
            return SimpleNamespace(
                status="paused", quest_id=qid, note="paused: budget cap reached"
            )

        monkeypatch.setattr(tick_mod, "run_quest_tick", _paused_tick)
        q = _mk_quest(store, "A striving", prio="PRIO:urgent")
        out = alloc.run_allocator_pass(store, enabled=True)
        assert out["picked"] is None and out["status"] == "paused"
        meta = store.get_ref(kind="quest", id=q).meta
        assert "picks" not in meta and "ewma_score" not in meta
