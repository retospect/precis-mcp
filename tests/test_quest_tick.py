"""Tests for the quest dossier + quest_tick skeleton — slice 4a of the quest
layer (docs/proposals/quest-layer.md §The autonomous research loop).

Covers: the ``dossier-of`` substrate (create / read / whole-rewrite, 1:1), the
single-step ``run_quest_tick`` with an injected model (applies logbook entries +
rewrites the dossier, tolerates JSON-in-text, clamps bad entry types, fails
cleanly), the ``build_tick_prompt`` context assembly, and the handler's
``view='dossier'``. Runs against real PG (the ``store`` fixture) so migration
0067's ``dossier-of`` relation is exercised.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import tick as tick_mod
from precis.quest.dossier import (
    dossier_ref_id,
    ensure_dossier,
    read_dossier,
    rewrite_dossier,
)
from precis.quest.tick import build_tick_prompt, run_quest_tick


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _fake_dispatch(
    payload: dict[str, Any] | None = None,
    *,
    text: str = "",
    error: str | None = None,
    cost: float | None = 0.01,
    paused: bool = False,
) -> Any:
    """A stand-in for router.dispatch returning a canned LlmResult-shaped obj."""

    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text=text, error=error, cost_usd=cost, paused=paused
        )

    return _d


# ── dossier substrate ─────────────────────────────────────────────────


class TestDossier:
    def test_ensure_creates_and_links_idempotently(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving that needs a dossier")
        did = ensure_dossier(store, qid)
        assert did is not None
        assert dossier_ref_id(store, qid) == did
        # 1:1 — a second ensure returns the same dossier, does not raise
        assert ensure_dossier(store, qid) == did

    def test_no_dossier_reads_empty(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving with no dossier yet")
        did, handle, text = read_dossier(store, qid)
        assert did is None and handle is None and text == ""

    def test_seed_then_whole_rewrite(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        ensure_dossier(store, qid)
        _did, _h, seed = read_dossier(store, qid)
        assert "No synthesis yet" in seed  # born with the seed
        rewrite_dossier(store, qid, "# Understanding\n\nFe–N₄ looks promising.")
        _did2, _h2, text = read_dossier(store, qid)
        assert "Understanding" in text and "promising" in text
        assert "No synthesis yet" not in text  # wholesale replaced


# ── the tick ──────────────────────────────────────────────────────────


class TestQuestTick:
    def test_tick_spend_lands_in_the_tote(self, store: Any) -> None:
        # gripe 162594: the tick's real measured usage (chars) is attributed
        # to the dated ledger (a `cost` logbook entry) so allocator.weekly_chars
        # — and thus the fair-share meter — is honest, not under-counting.
        from precis.quest import allocator as alloc

        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        payload = {"logbook": [{"entry_type": "note", "text": "thinking"}]}
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch(payload, cost=0.02), compute=False
        )
        assert out.status == "succeeded"
        assert alloc.weekly_chars(store, qid) > 0

    def test_zero_cost_tick_still_meters_chars(self, store: Any) -> None:
        # gripe 162594: chars are the meter unit, so a deed lands even when
        # the transport reports no dollar cost (the free/quota-bound lane).
        from precis.quest import allocator as alloc

        qid = _mk_quest(store, "Another striving")
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch({"logbook": []}, cost=None),
            compute=False,
        )
        assert out.status == "succeeded"
        assert alloc.weekly_chars(store, qid) > 0

    def test_applies_logbook_and_rewrites_dossier(self, store: Any) -> None:
        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        payload = {
            "logbook": [
                {"entry_type": "hypothesis", "text": "Try Fe–N₄ single-atom sites"},
                {"entry_type": "observation", "text": "The 2nd PCET is the bottleneck"},
            ],
            "dossier_markdown": "# Understanding\n\nFe–N₄ is the current best lead.",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.logbook_added == 2
        assert out.dossier_rewritten is True

        body = QuestHandler(hub=Hub(store=store)).get(id=qid).body
        assert "hypothesis" in body and "Fe–N₄ single-atom" in body
        _did, _h, dtext = read_dossier(store, qid)
        assert "current best lead" in dtext

    def test_logbook_entries_authored_by_agent(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [{"entry_type": "note", "text": "x"}],
            "dossier_markdown": "",
        }
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]
        assert logs[-1].meta["by"] == "agent"

    def test_clamps_unknown_entry_type_to_note(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [{"entry_type": "garbage", "text": "still recorded"}],
            "dossier_markdown": "",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.logbook_added == 1
        # The trailing entry is now the tick's `cost` accounting deed (gripe
        # 162594); the model's clamped entry is the one carrying its text.
        logs = [
            b
            for b in store.list_blocks_for_ref(qid)
            if b.chunk_kind == "quest_log" and "still recorded" in b.text
        ]
        assert logs[-1].meta["entry_type"] == "note"

    def test_parses_json_from_text_when_no_data(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        text = (
            'here you go: {"logbook": [{"entry_type": "note", "text": "hi"}], '
            '"dossier_markdown": "# D\\n\\nbody"} — done'
        )
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(None, text=text))
        assert out.status == "succeeded"
        assert out.logbook_added == 1 and out.dossier_rewritten is True

    def test_llm_error_fails_cleanly(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(None, error="boom"))
        assert out.status == "failed" and "boom" in out.note
        # nothing written
        assert not [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]

    def test_breaker_pause_is_not_a_failure(self, store: Any) -> None:
        # A window-scoped breaker trip (paused=True) is a pause, not a failure:
        # status is "paused" and nothing is written to the logbook.
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                None, error="budget: daily cap reached", paused=True
            ),
        )
        assert out.status == "paused" and "paused" in out.note
        assert not [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]

    def test_unparseable_output_fails(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch(None, text="no json in here")
        )
        assert out.status == "failed"

    def test_missing_quest_fails(self, store: Any) -> None:
        out = run_quest_tick(store, 999999, dispatch_fn=_fake_dispatch({"logbook": []}))
        assert out.status == "failed" and "not found" in out.note


# ── context assembly + view ───────────────────────────────────────────


class TestPromptAndView:
    def test_prompt_has_statement_gaps_and_schema(self, store: Any) -> None:
        qid = _mk_quest(store, "A NO→NH₃ catalyst")
        qref = store.get_ref(kind="quest", id=qid)
        p = build_tick_prompt(store, qref)
        assert "NO→NH₃" in p
        assert "thin-support" in p  # a lonely quest surfaces this gap
        assert "dossier_markdown" in p  # the JSON contract is in the prompt

    def test_view_dossier_before_and_after(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        h = QuestHandler(hub=Hub(store=store))
        assert "no dossier yet" in h.get(id=qid, view="dossier").body
        run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {"logbook": [], "dossier_markdown": "# Living\n\nsynthesis here"}
            ),
        )
        body = h.get(id=qid, view="dossier").body
        assert "Living" in body and "synthesis here" in body


def test_dossier_relation_registered() -> None:
    from precis.store.types import _INVERSE_RELATIONS

    assert _INVERSE_RELATIONS["dossier-of"] == "has-dossier"
    assert _INVERSE_RELATIONS["has-dossier"] == "dossier-of"


class TestReactionContext:
    """A quest that declares `meta.reaction_config` gets catalyst-slab proposal
    rules injected into its tick prompt (the lit-survey → catpath wire)."""

    def test_barrier_quest_prompt_asks_for_a_slab(self, store: Any) -> None:
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "catalyst slab" in prompt
        assert '"op": "slab"' in prompt
        assert "NO → NH3" in prompt  # substrate → target
        assert "ammonia" in prompt  # the catpath network
        assert "adatom" in prompt  # a param_space design knob

    def test_generic_quest_prompt_has_no_reaction_block(self, store: Any) -> None:
        qid = _mk_quest(store, "A generic materials striving with no reaction")
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "catalyst slab" not in prompt
        assert "Reaction R" not in prompt

    def test_reaction_context_offers_the_full_composition_op_menu(
        self, store: Any
    ) -> None:
        # A local tick + a frontier review must both see set_element/vacancy,
        # not just the two add_atom examples — the model was stuck hand-doping
        # adatoms because that was the only op it had ever seen.
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "set_element" in prompt
        assert "vacancy" in prompt


class TestFrontierAlwaysOn:
    """The Pareto frontier (rung 4c's review-only measurement table) now
    renders on every tick, local or review — the model reasons from the same
    numbers either way."""

    def test_frontier_section_appears_on_a_local_tick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        monkeypatch.setattr(
            tick_mod, "_frontier_summary", lambda s, q: "SENTINEL-FRONTIER-LOCAL"
        )
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, review=False)
        assert "Current Pareto frontier" in prompt
        assert "SENTINEL-FRONTIER-LOCAL" in prompt

    def test_review_banner_does_not_duplicate_the_frontier(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        monkeypatch.setattr(
            tick_mod, "_frontier_summary", lambda s, q: "SENTINEL-FRONTIER-REVIEW"
        )
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, review=True)
        assert prompt.count("Current Pareto frontier") == 1
        assert prompt.count("SENTINEL-FRONTIER-REVIEW") == 1
        assert "senior reviewer" in prompt  # the rest of the banner survives


class TestServedPapersDetail:
    """Served papers carry an abstract snippet in the tick prompt, not just a
    bare title — the model can only judge relevance from real substance."""

    def test_abstract_snippet_and_no_abstract_stub(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        qid = _mk_quest(store, "A striving needing literature")

        with_abstract = seed_ref(store, title="Fe-N4 single-atom catalysts")
        abstract = (
            "We report a breakthrough NO reduction pathway using Fe-N4 sites "
            "embedded in graphene, achieving a markedly lower rate-limiting "
            "barrier than the bare Pd(111) baseline across a wide potential "
            "window, with implications for ambient-condition ammonia synthesis."
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s::jsonb WHERE ref_id = %s",
                (json.dumps({"abstract": abstract}), with_abstract),
            )
            conn.commit()
        store.add_link(src_ref_id=with_abstract, dst_ref_id=qid, relation="serves")

        no_abstract = seed_ref(store, title="A stub reference, no abstract yet")
        store.add_link(src_ref_id=no_abstract, dst_ref_id=qid, relation="serves")

        detail = tick_mod._served_papers_detail(store, qid)
        assert any("breakthrough NO reduction" in d for d in detail)
        assert any("no abstract held" in d for d in detail)

    def test_wired_into_the_tick_prompt(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        qid = _mk_quest(store, "A striving needing literature")
        paper = seed_ref(store, title="A held paper")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s::jsonb WHERE ref_id = %s",
                (json.dumps({"abstract": "A specific measured finding."}), paper),
            )
            conn.commit()
        store.add_link(src_ref_id=paper, dst_ref_id=qid, relation="serves")

        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "Held literature" in prompt
        assert "A specific measured finding." in prompt
