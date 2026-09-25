"""Tests for :mod:`precis.quest.tagging` — the quest:<id> Drive-scoping tag.

Runs against real PG (the ``store`` fixture) so the ``serves`` walk +
``ref_tags`` upsert are exercised end to end, matching the pattern in
``tests/test_quest_gaps.py``.
"""

from __future__ import annotations

import re
from argparse import Namespace
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.quest import QuestHandler
from precis.quest.search import run_search_step
from precis.quest.tagging import quest_tag_value, tag_serving_papers
from precis.store.types import Tag
from tests.workers._helpers import seed_ref


def _handler(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


class TestQuestTagValue:
    def test_value_is_quest_colon_id(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        assert quest_tag_value(qid, store) == f"quest:{qid}"

    def test_raises_not_found_for_missing_quest(self, store: Any) -> None:
        with pytest.raises(NotFound):
            quest_tag_value(999_999_999, store)


class TestTagServingPapers:
    def test_tags_only_serves_linked_papers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A grounded striving"))

        serving = seed_ref(store, title="serving paper", kind="paper")
        store.add_link(src_ref_id=serving, dst_ref_id=qid, relation="serves")

        # A paper that just happens to exist but does NOT serve this quest —
        # must stay untagged.
        unrelated = seed_ref(store, title="unrelated paper", kind="paper")

        # A non-paper server (todo) — serves the quest but must not be
        # tagged (the tag scopes /drive?k=paper only).
        todo_server = seed_ref(store, title="a todo", kind="todo")
        store.add_link(src_ref_id=todo_server, dst_ref_id=qid, relation="serves")

        n = tag_serving_papers(store, qid)
        assert n == 1

        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(serving)
        assert tag not in store.tags_for(unrelated)
        assert tag not in store.tags_for(todo_server)

    def test_idempotent(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A re-tickable striving"))
        p = seed_ref(store, title="paper", kind="paper")
        store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")

        first = tag_serving_papers(store, qid)
        second = tag_serving_papers(store, qid)
        assert first == 1
        assert second == 1  # same count — no double-count/dup on re-run

        tag = Tag.open(f"quest:{qid}")
        assert store.tags_for(p).count(tag) == 1  # exactly one tag row

    def test_no_servers_tags_nothing(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A lonely striving"))
        assert tag_serving_papers(store, qid) == 0


class TestTagOnJoin:
    """A paper the lit-search step freshly ``serves``-links picks up the
    ``quest:<id>`` tag in the same step, before any backfill runs."""

    def test_run_search_step_tags_newly_linked_papers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A quest that goes looking for papers"))
        paper = seed_ref(store, title="Photocatalytic Nitrate Reduction Study")

        step = run_search_step(store, qid, ["photocatalytic nitrate reduction"])

        assert step.papers_linked == 1
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(paper)


class TestHydeSearchLeg:
    """``run_search_step``'s HyDE corpus leg (dossier-hygiene design): a
    ``searches`` entry carrying ``hypothetical`` routes its corpus lookup
    through the broad-retrieval fusion facility (mocked here) instead of
    ``search_fn``'s plain corpus/acquire composite — the S2+acquire leg
    (``search_fn``) is still driven by the plain keyword ``query``,
    unchanged."""

    def test_hyde_leg_links_a_paper_the_search_fn_never_saw(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        h = _handler(store)
        qid = _created_id(h.put(text="A quest grounded via HyDE"))
        paper = seed_ref(store, title="Proton-Coupled Electron Transfer Study")

        monkeypatch.setattr(
            FusedBlockSearch,
            "run",
            lambda self, **kw: SimpleNamespace(
                hits=[(None, SimpleNamespace(id=paper), 1.0)]
            ),
        )

        # search_fn (the S2+acquire leg) contributes nothing — the link
        # must come from the HyDE corpus leg alone.
        step = run_search_step(
            store,
            qid,
            [
                {
                    "query": "PCET mechanism",
                    "hypothetical": (
                        "Proton-coupled electron transfer governs the "
                        "rate-limiting step at the catalyst surface."
                    ),
                }
            ],
            search_fn=lambda *a, **kw: [],
        )

        assert step.queries_run == 1
        assert step.papers_linked == 1
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(paper)

    def test_mixed_plain_and_hyde_entries_both_run(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        h = _handler(store)
        qid = _created_id(h.put(text="A quest with a mixed search batch"))
        held = seed_ref(store, title="Photocatalytic Nitrate Reduction Study")
        hyde_paper = seed_ref(store, title="Proton-Coupled Electron Transfer Study")

        monkeypatch.setattr(
            FusedBlockSearch,
            "run",
            lambda self, **kw: SimpleNamespace(
                hits=[(None, SimpleNamespace(id=hyde_paper), 1.0)]
            ),
        )

        step = run_search_step(
            store,
            qid,
            [
                "photocatalytic nitrate reduction",
                {
                    "query": "PCET mechanism",
                    "hypothetical": (
                        "Proton-coupled electron transfer governs the "
                        "rate-limiting step at the catalyst surface."
                    ),
                },
            ],
        )

        assert step.queries_run == 2
        assert step.papers_linked == 2
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(held)
        assert tag in store.tags_for(hyde_paper)


class TestRelevanceFloor:
    """quest-tick-incident-fix.md item 4: a scored hit below
    ``PRECIS_QUEST_SEARCH_FLOOR`` links nothing and the drop is logged —
    holds for the plain ``search_fn`` leg and the HyDE fused-block leg
    alike (both now surface the score they used to discard)."""

    def test_below_floor_search_fn_hit_is_dropped_and_logged(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A quest with a search_fn floor"))
        weak = seed_ref(store, title="Weakly Related Paper")
        strong = seed_ref(store, title="Strongly Related Paper")

        monkeypatch.setenv("PRECIS_QUEST_SEARCH_FLOOR", "0.5")

        def _search(
            _store: Any, _q: str, _exclude: list[int]
        ) -> list[tuple[int, float | None]]:
            return [(strong, 0.9), (weak, 0.1)]

        with caplog.at_level("INFO", logger="precis.quest.search"):
            step = run_search_step(store, qid, ["a query"], search_fn=_search)

        assert step.papers_linked == 1
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(strong)
        assert tag not in store.tags_for(weak)
        served = {
            ln.src_ref_id
            for ln in store.links_for(qid, direction="in", relation="serves")
        }
        assert strong in served
        assert weak not in served
        assert any(
            "dropped below-floor hit" in r.getMessage()
            and f"ref={weak}" in r.getMessage()
            for r in caplog.records
        )

    def test_below_floor_hyde_hit_is_dropped_and_logged(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        h = _handler(store)
        qid = _created_id(h.put(text="A quest with a HyDE floor"))
        weak = seed_ref(store, title="Weak HyDE Match")

        monkeypatch.setenv("PRECIS_QUEST_SEARCH_FLOOR", "0.5")
        monkeypatch.setattr(
            FusedBlockSearch,
            "run",
            lambda self, **kw: SimpleNamespace(
                hits=[(None, SimpleNamespace(id=weak), 0.1)]
            ),
        )

        with caplog.at_level("INFO", logger="precis.quest.search"):
            step = run_search_step(
                store,
                qid,
                [{"query": "q", "hypothetical": "a passage"}],
                search_fn=lambda *a, **kw: [],
            )

        assert step.papers_linked == 0
        tag = Tag.open(f"quest:{qid}")
        assert tag not in store.tags_for(weak)
        assert any(
            "dropped below-floor hit" in r.getMessage()
            and f"ref={weak}" in r.getMessage()
            for r in caplog.records
        )

    def test_default_floor_is_log_only_never_filters(
        self, store: Any, caplog: Any
    ) -> None:
        """No ``PRECIS_QUEST_SEARCH_FLOOR`` set — the shipped default (0.0)
        must never drop a real (non-negative) score."""
        h = _handler(store)
        qid = _created_id(h.put(text="A quest with the default floor"))
        weak = seed_ref(store, title="A Weakly Scored Paper")

        def _search(
            _store: Any, _q: str, _exclude: list[int]
        ) -> list[tuple[int, float | None]]:
            return [(weak, 0.0001)]

        step = run_search_step(store, qid, ["a query"], search_fn=_search)

        assert step.papers_linked == 1
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(weak)


class TestAcquireLegBoundedNotUnconditional:
    """quest-tick-incident-fix.md item 5: the S2 acquire leg
    (``make_acquiring_search``) no longer links ``related-to``->quest
    unconditionally inside ``PaperHandler.acquire`` — the only link this
    step can create is ``run_search_step``'s own ``serves`` link, bounded by
    ``MAX_LINK_PER_QUERY`` exactly like the scored legs (previously
    unbounded: up to ``_acquire_per_query()`` related-to links per query,
    regardless of whether the candidate ever made the cut — the path
    qu401863's stray related-to papers came from)."""

    def test_acquired_candidates_bounded_and_no_related_to_link(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.paper import PaperHandler
        from precis.ingest import semantic_scholar as s2mod
        from precis.quest.search import MAX_LINK_PER_QUERY, make_acquiring_search
        from precis.response import Response

        h = _handler(store)
        qid = _created_id(h.put(text="A quest acquiring from Semantic Scholar"))

        n_candidates = MAX_LINK_PER_QUERY + 2
        dois = [f"10.1/acquire-bound-{i}" for i in range(n_candidates)]
        monkeypatch.setattr(
            s2mod,
            "search_s2_papers",
            lambda query, limit: [{"doi": d, "title": f"paper {d}"} for d in dois],
        )

        minted: list[int] = []

        def _fake_acquire(self: Any, **kw: Any) -> Response:
            assert "context_ref_id" not in kw  # item 5: never passed here
            rid = seed_ref(store, title=f"acquired {kw['identifier']}", kind="paper")
            minted.append(rid)
            return Response(body=f"acquire: minted stub paper id={rid}")

        monkeypatch.setattr(PaperHandler, "acquire", _fake_acquire)

        search_fn = make_acquiring_search(qid, Hub(store=store))
        step = run_search_step(store, qid, ["acquire bound query"], search_fn=search_fn)

        assert len(minted) == n_candidates  # every DOI was acquired ...
        assert step.papers_linked == MAX_LINK_PER_QUERY  # ... only 3 linked
        served = {
            ln.src_ref_id
            for ln in store.links_for(qid, direction="in", relation="serves")
        }
        assert len(served & set(minted)) == MAX_LINK_PER_QUERY
        # No related-to link exists at all — the pre-fix unbounded path.
        assert store.links_for(qid, direction="out", relation="related-to") == []


class TestCliQuestSet:
    """``precis quest set`` — the operator lever for an incident (the MCP
    verb path already covered end-to-end in
    tests/test_mcp_put_edit_kwarg_doors.py; this pins the CLI door onto the
    same handler + allowlist)."""

    def test_set_compute_lane_off(self, store: Any) -> None:
        from precis.cli.quest import _cmd_set

        h = _handler(store)
        qid = _created_id(h.put(text="A quest set from the CLI"))

        _cmd_set(store, Namespace(id=qid, key="compute_lane", value="off"))

        live = store.get_ref(kind="quest", id=qid)
        assert live is not None
        assert live.meta.get("compute_lane") == "off"

    def test_set_unknown_key_raises(self, store: Any) -> None:
        from precis.cli.quest import _cmd_set
        from precis.errors import BadInput

        h = _handler(store)
        qid = _created_id(h.put(text="A quest guarded from the CLI too"))

        with pytest.raises(BadInput):
            _cmd_set(store, Namespace(id=qid, key="nope", value="x"))
