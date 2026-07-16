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

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
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
        logs = [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
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
