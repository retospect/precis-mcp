"""Tests for the quest-loop anti-spin fixes (2026-07-17).

The loop's first live run fixated: one quest minted ~10 restatements of a single
hypothesis, which the allocator rated "active" (activity, not progress) and so
kept re-picking until the 12-tick cool. These cover the coupled fix:

* **cascade** — the progress clock resets on *any* external progress (a new
  ``result`` / a resolved hypothesis / an acquired paper), not just a compute
  frontier gain, so a reasoning-only spin is now legible to it;
* **allocator** — the score's activity term is replaced by a geometric
  ``progress_factor`` (a spinner decays) plus an anti-starvation ``aging`` bonus
  (a long-waiting quest rises), i.e. weighted round-robin;
* **tick** — a near-duplicate ``hypothesis`` is dropped, and a ``searches``
  action links held papers as servers (the grounding half of the loop).

Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import allocator as alloc
from precis.quest import cascade
from precis.quest.logbook import append_entry
from precis.quest.tick import run_quest_tick


def _mk_quest(store: Any, text: str, *, prio: str | None = None) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text, tags=[prio] if prio else None)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _ticks_since(store: Any, qid: int) -> int:
    ref = store.get_ref(kind="quest", id=qid)
    return int((ref.meta or {}).get("ticks_since_frontier_improve", 0) or 0)


def _fake_dispatch(payload: dict[str, Any]) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text="", error=None, cost_usd=0.0, paused=False
        )

    return _d


# ── cascade: broadened progress signal ────────────────────────────────


class TestBroadenedProgress:
    def test_pure_reasoning_climbs_the_stall_clock(self, store: Any) -> None:
        qid = _mk_quest(store, "A reasoning quest")
        append_entry(store, qid, text="a thought", entry_type="note", by="agent")
        cascade.update_cascade_state(store, qid, reviewed=False)
        append_entry(store, qid, text="another thought", entry_type="note", by="agent")
        cascade.update_cascade_state(store, qid, reviewed=False)
        # No external evidence → the clock only climbs.
        assert _ticks_since(store, qid) >= 2

    def test_a_result_entry_is_progress(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest that lands a result")
        append_entry(store, qid, text="a thought", entry_type="note", by="agent")
        cascade.update_cascade_state(store, qid, reviewed=False)
        assert _ticks_since(store, qid) >= 1
        append_entry(store, qid, text="measured X=1.2", entry_type="result", by="agent")
        cascade.update_cascade_state(store, qid, reviewed=False)
        assert _ticks_since(store, qid) == 0  # reset by the new result

    def test_resolving_a_hypothesis_is_progress(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest with an open hypothesis")
        append_entry(
            store, qid, text="H: yield rises", entry_type="hypothesis", by="agent"
        )
        cascade.update_cascade_state(store, qid, reviewed=False)
        cascade.update_cascade_state(store, qid, reviewed=False)
        assert _ticks_since(store, qid) >= 1
        # A dead-end resolves the open hypothesis (not a `result`, so this
        # isolates the resolved-hypothesis signal) → progress.
        append_entry(store, qid, text="ruled out", entry_type="dead-end", by="agent")
        cascade.update_cascade_state(store, qid, reviewed=False)
        assert _ticks_since(store, qid) == 0


# ── allocator: progress decay + aging ─────────────────────────────────


class TestProgressAndAging:
    def test_progress_factor_decays_and_floors(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest")
        assert alloc.progress_factor(store, qid) == 1.0  # fresh
        alloc._merge_meta(store, qid, {"ticks_since_frontier_improve": 2})
        assert alloc.progress_factor(store, qid) == alloc.PROGRESS_DECAY**2
        alloc._merge_meta(store, qid, {"ticks_since_frontier_improve": 100})
        assert alloc.progress_factor(store, qid) == alloc.STALL_FLOOR

    def test_stalled_scores_below_fresh_at_equal_priority(self, store: Any) -> None:
        fresh = _mk_quest(store, "Fresh", prio="PRIO:normal")
        stalled = _mk_quest(store, "Stalled", prio="PRIO:normal")
        alloc._merge_meta(store, stalled, {"ticks_since_frontier_improve": 8})
        assert alloc.raw_score(store, fresh) > alloc.raw_score(store, stalled)

    def test_aging_lifts_a_long_waiter(self, store: Any) -> None:
        # Two tried quests with equal smoothed score; the one not picked for
        # many rounds gets the aging bonus and wins the pick.
        recent = _mk_quest(store, "Recently picked", prio="PRIO:normal")
        waiter = _mk_quest(store, "Long waiter", prio="PRIO:normal")
        store.set_setting(alloc._ROUND_KEY, "20")
        alloc._merge_meta(
            store, recent, {"picks": 1, "ewma_score": 0.5, "last_pick_round": 20}
        )
        alloc._merge_meta(
            store, waiter, {"picks": 1, "ewma_score": 0.5, "last_pick_round": 2}
        )
        assert alloc.pick_score(store, waiter) > alloc.pick_score(store, recent)
        pick = alloc.pick_next_quest(store)
        assert pick is not None and pick.quest_id == waiter

    def test_record_pick_advances_round_and_stamps(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest")
        before = alloc._current_round(store)
        alloc._record_pick(store, qid, raw=0.4)
        assert alloc._current_round(store) == before + 1
        ref = store.get_ref(kind="quest", id=qid)
        assert (ref.meta or {}).get("last_pick_round") == before + 1


# ── tick: hypothesis dedup + lit-search action ────────────────────────


class TestTickGrounding:
    def test_near_duplicate_hypothesis_is_dropped(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest")
        append_entry(
            store,
            qid,
            text="Published Winfree data will reveal a monotonic redundancy-yield curve",
            entry_type="hypothesis",
            by="agent",
        )
        before = len(store.list_blocks_for_ref(qid))
        payload = {
            "logbook": [
                {
                    "entry_type": "hypothesis",
                    "text": "Winfree published data reveals the monotonic redundancy yield curve",
                }
            ],
            "dossier_markdown": "",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.hypotheses_deduped == 1
        # +1, not +0: a successful tick always writes one chars-metered `cost`
        # deed now (gripe 162594), independent of whether the duplicate
        # hypothesis itself was dropped.
        assert len(store.list_blocks_for_ref(qid)) == before + 1

    def test_distinct_hypothesis_is_kept(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest")
        append_entry(
            store,
            qid,
            text="Redundant binding raises yield",
            entry_type="hypothesis",
            by="agent",
        )
        payload = {
            "logbook": [
                {
                    "entry_type": "hypothesis",
                    "text": "Cooling rate controls defect density in tiles",
                }
            ],
            "dossier_markdown": "",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.hypotheses_deduped == 0
        assert out.logbook_added >= 1

    def test_search_action_links_papers(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest that needs grounding")
        stand_in = _mk_quest(store, "A stand-in for a held paper")  # any linkable ref
        payload = {
            "logbook": [],
            "searches": ["dna tile yield"],
            "dossier_markdown": "",
            "proposals": [],
        }

        def _search(_store: Any, _q: str, _exclude: list[int]) -> list[int]:
            return [stand_in]

        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(payload),
            compute=True,
            search_fn=_search,
        )
        assert out.searches_run == 1
        assert out.papers_linked == 1
        served = {
            ln.src_ref_id
            for ln in store.links_for(qid, direction="in", relation="serves")
        }
        assert stand_in in served
