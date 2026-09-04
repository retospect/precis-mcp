"""Tests for quest gaps + health — slice 3 of the quest layer.

Covers the read-time, mechanical primitives in :mod:`precis.quest.gaps` (the
exploration queue + momentum + the alignment floor) and their surfacing in the
handler's ``view='tree'`` rollup, the per-quest ``view='gaps'``, and the
corpus-wide ``id='/gaps'`` dashboard. Runs against real PG (the ``store``
fixture) so the ``serves`` walk + tag/ref_events SQL is exercised end to end.
"""

from __future__ import annotations

import re
import time
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.quest.gaps import (
    _GAPS_TAG_MAX_STRUCTURES,
    quest_alignment,
    quest_gaps,
    quest_momentum,
)
from precis.store import Store, Tag


def _handler(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _gap_kinds(store: Any, qid: int) -> list[str]:
    return [g.kind for g in quest_gaps(store, qid)]


# ── gaps ──────────────────────────────────────────────────────────────


class TestGaps:
    def test_thin_support_when_no_servers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A lonely striving nothing serves"))
        assert "thin-support" in _gap_kinds(store, qid)

    def test_no_literature_when_servers_but_no_paper(self, store: Any) -> None:
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        h = _handler(store)
        qid = _created_id(h.put(text="Work under way, no papers yet"))
        for i in range(2):
            t = id_of(th.put(text=f"work item {i}").body)
            store.add_link(src_ref_id=t, dst_ref_id=qid, relation="serves")
        kinds = _gap_kinds(store, qid)
        assert "no-literature" in kinds
        assert "thin-support" not in kinds  # 2 servers clears the thin flag

    def test_paper_server_clears_no_literature(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A grounded striving"))
        for title in ("paper A", "paper B"):
            p = seed_ref(store, title=title)
            store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")
        assert "no-literature" not in _gap_kinds(store, qid)

    def test_low_mastery_served_concept(self, store: Any) -> None:
        from precis.handlers.concept import ConceptHandler

        ch = ConceptHandler(hub=Hub(store=store))

        def _cid(resp: Any) -> int:
            m = re.search(r"\bcn(\d+)\b", resp.body)
            assert m is not None
            return int(m.group(1))

        h = _handler(store)
        qid = _created_id(h.put(text="Needs a hard idea understood"))
        c = _cid(ch.put(text="proton-coupled electron transfer — a hard concept"))
        store.add_link(src_ref_id=c, dst_ref_id=qid, relation="serves")
        low = [g for g in quest_gaps(store, qid) if g.kind == "low-mastery"]
        assert low, "a freshly-minted (mastery 0.0) served concept is a gap"
        assert low[0].handle == f"cn{c}"

    def test_open_hypothesis_then_answered(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A tested striving"))
        h.put(id=qid, text="maybe Fe–N₄ sites work", entry="hypothesis")
        assert "open-hypothesis" in _gap_kinds(store, qid)
        # a later result / dead-end closes it
        h.put(id=qid, text="barrier too high — no", entry="dead-end")
        assert "open-hypothesis" not in _gap_kinds(store, qid)


# ── structure fan-out cap (gr311678) ────────────────────────────────────


def _seed_synthetic_structures(store: Store, quest_id: int, n: int) -> list[int]:
    """Bulk-insert ``n`` ``structure`` refs, each ``serves``-linked to
    ``quest_id`` — a single round trip per statement so N=10,000 stays fast
    to set up (mirrors the gr311344 dossier's synthetic-fan-out repro)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "INSERT INTO refs (kind, set_by, title) "
            "SELECT 'structure', 'system', 'synthetic structure ' || g "
            "FROM generate_series(1, %s) AS g "
            "RETURNING ref_id",
            (n,),
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "SELECT unnest(%s::bigint[]), %s, 'serves', 'system'",
            (ids, quest_id),
        )
        conn.commit()
    return ids


class TestGapsStructureFanoutCap:
    """``quest_gaps`` bounds the per-structure ``tags_for`` fan-out the same
    way ``quest_alignment`` bounds its per-server embedding fan-out
    (``_ALIGN_MAX_SERVERS``) — an unbounded N+1 here scaled linearly with a
    quest's ``serves`` fan-out (gr311678, 2.86s at N=10,000 in the dossier
    repro)."""

    def test_needs_experiment_gap_preserved_under_cap(self, store: Any) -> None:
        """Well under the cap: the needs-experiment gap still fires for the
        tagged structures, and no fan-out cap warning appears."""
        h = _handler(store)
        qid = _created_id(h.put(text="A striving with graduated candidates"))
        ids = _seed_synthetic_structures(store, qid, 5)
        graduated = set(ids[:2])
        for sid in graduated:
            store.add_tag(sid, Tag.open("needs-experiment"), set_by="system")

        gaps = quest_gaps(store, qid)
        kinds = [g.kind for g in gaps]
        assert kinds.count("needs-experiment") == len(graduated)
        flagged_handles = {g.handle for g in gaps if g.kind == "needs-experiment"}
        assert flagged_handles == {f"st{sid}" for sid in graduated}
        assert "fanout-capped" not in kinds

    def test_fanout_capped_gap_is_honest_when_truncated(self, store: Any) -> None:
        """Past the cap, the excess structures are (honestly) not checked —
        a ``fanout-capped`` gap says so instead of silently under-reporting,
        and a needs-experiment tag past the cap boundary is not surfaced."""
        h = _handler(store)
        qid = _created_id(h.put(text="A striving with a huge structure fan-out"))
        n = _GAPS_TAG_MAX_STRUCTURES + 5
        ids = _seed_synthetic_structures(store, qid, n)
        # Tag one structure inside the checked window and one past it.
        store.add_tag(ids[0], Tag.open("needs-experiment"), set_by="system")
        store.add_tag(ids[-1], Tag.open("needs-experiment"), set_by="system")

        gaps = quest_gaps(store, qid)
        capped = [g for g in gaps if g.kind == "fanout-capped"]
        assert len(capped) == 1
        assert "5" in capped[0].detail

        flagged_handles = {g.handle for g in gaps if g.kind == "needs-experiment"}
        assert flagged_handles == {f"st{ids[0]}"}  # the past-cap tag is skipped

    def test_wall_clock_budget_at_10k_synthetic_serves(self, store: Any) -> None:
        """gaps + tree views stay well under budget at N=10,000 synthetic
        ``serves`` edges — was 2.86s / 1.53s pre-fix (gr311344 dossier).

        Budget is deliberately generous (10s, not ~2s) so it stays
        congestion-proof on a shared/contended gate runner while still
        catching the linear-with-fan-out regression this test guards: an
        uncapped re-introduction of the N+1 would need ~3.5x today's
        pre-fix time (2.86s) to even approach 10s, and the cap makes the
        capped path roughly O(1) in server count regardless.
        """
        h = _handler(store)
        qid = _created_id(h.put(text="A striving with a huge server fan-out"))
        _seed_synthetic_structures(store, qid, 10_000)

        t0 = time.monotonic()
        gaps = quest_gaps(store, qid)
        gaps_elapsed = time.monotonic() - t0
        # 10k structures, no papers → "no-literature" plus the fanout-capped hint.
        assert gaps
        assert gaps_elapsed < 10.0, f"view='gaps' path took {gaps_elapsed:.2f}s"

        t0 = time.monotonic()
        body = h.get(id=qid, view="tree").body
        tree_elapsed = time.monotonic() - t0
        assert body
        assert tree_elapsed < 10.0, f"view='tree' took {tree_elapsed:.2f}s"


# ── momentum ──────────────────────────────────────────────────────────


class TestMomentum:
    def test_quiet_when_empty(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="Brand new striving"))
        assert quest_momentum(store, qid).label == "quiet"

    def test_active_after_recent_logbook(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A busy striving"))
        for i in range(3):
            h.put(id=qid, text=f"observation {i}", entry="observation")
        m = quest_momentum(store, qid)
        assert m.recent_entries == 3
        assert m.label == "active"

    def test_entries_short_circuit_matches_default(self, store: Any) -> None:
        """Passing precomputed ``entries=`` (the quest-hub route's own
        already-fetched logbook blocks) skips the internal
        ``list_chunks_for_ref`` re-query but yields the same momentum —
        the shape the web dashboard route relies on to avoid a duplicate
        query per page load."""
        from precis.quest.logbook import LOG_KIND

        h = _handler(store)
        qid = _created_id(h.put(text="A busy striving"))
        for i in range(3):
            h.put(id=qid, text=f"observation {i}", entry="observation")
        log_entries = [
            b for b in store.chunks.list_chunks_for_ref(qid) if b.chunk_kind == LOG_KIND
        ]
        assert len(log_entries) == 3
        m_default = quest_momentum(store, qid)
        m_short = quest_momentum(store, qid, entries=log_entries)
        assert m_short.recent_entries == m_default.recent_entries == 3
        assert m_short.label == m_default.label == "active"

    def test_open_and_blocked_todo_servers(self, store: Any) -> None:
        from tests.conftest import id_of

        th = TodoHandler(hub=Hub(store=store))
        h = _handler(store)
        qid = _created_id(h.put(text="A striving with work in flight"))
        t_open = id_of(th.put(text="open work").body)
        t_blocked = id_of(th.put(text="blocked work").body)
        for t in (t_open, t_blocked):
            store.add_link(src_ref_id=t, dst_ref_id=qid, relation="serves")
        # bubble a child-failure onto the blocked todo (the same open tag the
        # job failure-bubble writes).
        store.add_tag(t_blocked, Tag.open("child-failed:999"), set_by="system")
        m = quest_momentum(store, qid)
        assert m.open_todo_servers == 2  # neither is done
        assert m.blocked_todo_servers == 1


# ── alignment floor ───────────────────────────────────────────────────


class TestAlignment:
    def test_cosine_pure(self) -> None:
        from precis.quest.gaps import _cosine

        assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
        assert abs(_cosine([1.0, 1.0], [1.0, 0.0]) - (1 / 2**0.5)) < 1e-9
        assert _cosine([], [1.0]) == 0.0  # degenerate

    def test_floor_is_noop_without_embeddings(self, store: Any) -> None:
        # No embedder runs in the test env → cards carry no vector → the
        # alignment floor checks nothing and flags nothing (best-effort).
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        p = seed_ref(store, title="a server with no embedding")
        store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")
        flags, checked = quest_alignment(store, qid)
        assert checked == 0 and flags == []


# ── surfacing in the handler views ────────────────────────────────────


class TestGapViews:
    def test_tree_shows_health_and_gaps(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A NO→NH₃ catalyst"))
        body = h.get(id=qid, view="tree").body
        assert "health" in body and "momentum" in body
        # a lonely quest surfaces its thin-support gap in the tree rollup
        assert "gaps" in body and "thin-support" in body

    def test_view_gaps_focuses_one_quest(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        h = _handler(store)
        qid = _created_id(h.put(text="A well-supported striving"))
        for title in ("paper A", "paper B"):
            store.add_link(
                src_ref_id=seed_ref(store, title=title),
                dst_ref_id=qid,
                relation="serves",
            )
        body = h.get(id=qid, view="gaps").body
        assert body.startswith("# gaps")
        assert "no gaps" in body  # 2 papers → well-supported

    def test_gaps_dashboard_lists_active_quests(self, store: Any) -> None:
        h = _handler(store)
        _created_id(h.put(text="Striving alpha"))
        body = h.get(id="/gaps").body
        assert "exploration queue" in body
        assert "Striving alpha" in body


class TestUnknownViewError:
    """An unrecognised ``view=`` on a concrete quest id must enumerate the
    quest-specific views — not fall through to the base error that lists only
    links/log/raw — and warn off the two shapes callers actually guess
    (``logbook``/``deeds``, both saturated through the skill prose)."""

    def test_guessed_deeds_view_enumerates_quest_views(self, store: Any) -> None:
        # 'logbook' is now a real view (the full notebook); 'deeds' remains a
        # guessed, unrecognised one — it's just the milestone-typed slice of
        # the log, not its own view.
        import pytest

        from precis.errors import Unsupported

        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        with pytest.raises(Unsupported) as ei:
            h.get(id=qid, view="deeds")
        err = ei.value
        # the six quest views are named (not just links/log/raw)
        for v in ("tree", "gaps", "dossier", "frontier", "leaderboard", "logbook"):
            assert v in (err.options or [])
        hint = " ".join(err.next if isinstance(err.next, list) else [err.next or ""])
        assert "logbook" in hint and "deeds" in hint and "view='log'" in hint

    def test_real_base_view_still_passes_through(self, store: Any) -> None:
        # 'log' is a genuine base view → no error, renders the ledger.
        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        assert h.get(id=qid, view="log").body is not None
