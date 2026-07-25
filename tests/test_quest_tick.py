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
from precis.quest import compute as compute_mod
from precis.quest import tick as tick_mod
from precis.quest.dossier import (
    append_ledger_entry,
    dossier_ref_id,
    ensure_dossier,
    ensure_ledger_chunk,
    paper_ref_id,
    read_dossier,
    read_ledger,
    read_narrative,
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

    def test_soft_deleted_dossier_resolves_none(self, store: Any) -> None:
        """A dossier draft soft-deleted out from under its ``dossier-of``
        link (the link row survives the delete) must resolve to ``None`` —
        the web hub should show "no dossier yet", never a live button
        pointing at a tombstoned ref."""
        qid = _mk_quest(store, "A striving whose dossier gets deleted")
        did = ensure_dossier(store, qid)
        assert dossier_ref_id(store, qid) == did
        store.soft_delete_draft(did)
        assert dossier_ref_id(store, qid) is None


# ── the pinned ledger (ADR 0064 §A) ─────────────────────────────────────


class TestDossierLedger:
    def test_ensure_dossier_creates_a_pinned_ledger_chunk(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving that needs a ledger")
        did = ensure_dossier(store, qid)
        chunks = store.reading_order(did)
        ledger_chunks = [c for c in chunks if (c.meta or {}).get("pinned") == "ledger"]
        assert len(ledger_chunks) == 1
        assert "## Tried" in ledger_chunks[0].text
        assert "## Ruled out" in ledger_chunks[0].text
        assert "## Open" in ledger_chunks[0].text

    def test_rewrite_dossier_leaves_ledger_byte_identical(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        added = append_ledger_entry(
            store, qid, "ruled-out", "Pt/Al2O3 — barrier too high"
        )
        assert added is True
        before = read_ledger(store, qid)
        rewrite_dossier(store, qid, "# Understanding\n\nFresh synthesis.")
        rewrite_dossier(store, qid, "# Understanding v2\n\nAnother pass entirely.")
        after = read_ledger(store, qid)
        assert after == before  # untouched by two whole-rewrites
        assert "Pt/Al2O3 — barrier too high" in after

    def test_ensure_ledger_chunk_heals_an_old_narrative_only_dossier(
        self, store: Any
    ) -> None:
        from precis.quest import dossier as dossier_mod

        qid = _mk_quest(store, "A striving with a pre-ADR-0064 dossier")
        # Build the dossier the OLD way — a single narrative chunk, no pinned
        # ledger — the shape a live prod quest is in pre-migration.
        qref = store.get_ref(kind="quest", id=qid)
        ref, _heading = store.create_draft(
            name=f"quest-{qid}-dossier",
            title=f"Dossier — {qref.title}",
            project_ref_id=qid,
            meta={"dossier_of_quest": qid},
            relation=dossier_mod._RELATION,
        )
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Pre-existing narrative synthesis.",
            split=False,
        )
        did = ref.id
        assert dossier_ref_id(store, qid) == did
        before = store.reading_order(did)
        assert not any((c.meta or {}).get("pinned") == "ledger" for c in before)

        handle = ensure_ledger_chunk(store, qid)

        after = store.reading_order(did)
        ledger_chunks = [c for c in after if (c.meta or {}).get("pinned") == "ledger"]
        assert len(ledger_chunks) == 1
        assert ledger_chunks[0].handle == handle
        assert "Pre-existing narrative synthesis." in read_narrative(store, qid)
        # idempotent — a second heal doesn't create a duplicate
        assert ensure_ledger_chunk(store, qid) == handle
        assert (
            len(
                [
                    c
                    for c in store.reading_order(did)
                    if (c.meta or {}).get("pinned") == "ledger"
                ]
            )
            == 1
        )

    def test_append_ledger_entry_appends_under_heading_and_dedups(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        assert (
            append_ledger_entry(store, qid, "tried", "Fe–N4 single-atom sites") is True
        )
        ledger = read_ledger(store, qid)
        assert "## Tried\n- Fe–N4 single-atom sites" in ledger
        # a byte-identical bullet under the same heading is deduped
        assert (
            append_ledger_entry(store, qid, "tried", "Fe–N4 single-atom sites") is False
        )
        assert read_ledger(store, qid).count("Fe–N4 single-atom sites") == 1

    def test_append_ledger_entry_clamps_unknown_section_to_open(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        assert append_ledger_entry(store, qid, "bogus", "clamped entry") is True
        ledger = read_ledger(store, qid)
        open_block = ledger.split("## Open", 1)[1]
        assert "clamped entry" in open_block

    def test_append_ledger_entry_skips_blank_text(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert append_ledger_entry(store, qid, "open", "   ") is False


# ── owner generalization (ADR 0064 §B) ──────────────────────────────────


class TestDossierOwnerGeneralization:
    """The dossier owner is any process, not just a quest (ADR 0064 §B —
    docs/proposals/dossier-owner-generalization.md). The coupling was Python
    only; the ``dossier-of`` edge is already owner-agnostic."""

    def test_non_quest_owner_full_round_trip(self, store: Any) -> None:
        # A non-quest process (a `memory` ref stands in for any living-review
        # owner) can own a dossier: create → rewrite → append ledger → read
        # narrative + ledger back, all succeed, and the `dossier-of` edge
        # points at the non-quest owner.
        owner = store.insert_ref(
            kind="memory", slug=None, title="A living review process"
        )
        did = ensure_dossier(store, owner.id)
        assert did is not None
        assert dossier_ref_id(store, owner.id) == did
        # title seed derives from the owner's title via the kind-agnostic read
        oref = store.get_ref(kind="draft", id=did)
        assert "A living review process" in (oref.title or "")

        rewrite_dossier(store, owner.id, "# Understanding\n\nMOF linkers look apt.")
        assert append_ledger_entry(store, owner.id, "ruled-out", "zeolite Y") is True

        _did, _h, body = read_dossier(store, owner.id)
        assert "MOF linkers look apt." in read_narrative(store, owner.id)
        assert "zeolite Y" in read_ledger(store, owner.id)
        assert "MOF linkers" in body and "zeolite Y" in body

        # the dossier-of edge points at the NON-quest owner
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT dst_ref_id FROM links "
                "WHERE src_ref_id = %s AND relation = 'dossier-of'",
                (did,),
            ).fetchone()
        assert row is not None and int(row[0]) == owner.id

    def test_legacy_dossier_of_quest_key_still_resolves(self, store: Any) -> None:
        # A prod dossier stamped with only the OLD meta.dossier_of_quest key
        # (no dossier_of_owner) must still resolve through every read path —
        # resolution is link-based, so the meta-key rename needs no backfill.
        qid = _mk_quest(store, "A striving with a pre-§B dossier")
        ref, _heading = store.create_draft(
            name=f"quest-{qid}-dossier",  # the old naming, too
            title="Dossier — legacy",
            project_ref_id=qid,
            meta={"dossier_of_quest": qid},  # only the LEGACY owner key
            relation="dossier-of",
        )
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Legacy narrative.",
            split=False,
        )
        assert dossier_ref_id(store, qid) == ref.id
        did, _h, text = read_dossier(store, qid)
        assert did == ref.id and "Legacy narrative." in text
        # ensure_* is idempotent on the legacy ref — no second dossier minted
        assert ensure_dossier(store, qid) == ref.id
        assert "Legacy narrative." in read_narrative(store, qid)


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

    def test_ledger_add_is_pinned_and_survives_a_later_rewrite(
        self, store: Any
    ) -> None:
        # ADR 0064 §A: a model-emitted `ledger_add` is applied BEFORE the
        # dossier rewrite, so it's pinned even though this same tick also
        # whole-rewrites the narrative — and it must still be there after a
        # SUBSEQUENT rewrite too (the loop-breaker the whole feature is for).
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dossier_markdown": "# Understanding\n\nFirst pass.",
            "ledger_add": [
                {"section": "ruled-out", "text": "Cu single-atom — relax fails"}
            ],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.ledger_added == 1
        assert "Cu single-atom — relax fails" in read_ledger(store, qid)

        rewrite_dossier(store, qid, "# Understanding v2\n\nSomething else.")
        assert "Cu single-atom — relax fails" in read_ledger(store, qid)

    def test_ledger_added_counts_only_applied_non_deduped_entries(
        self, store: Any
    ) -> None:
        # A dedup-skipped repeat (byte-identical bullet under the same
        # heading) and a blank-text entry must not inflate the engagement
        # signal the coordinator's punt-vs-dry split reads.
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "ledger_add": [
                {"section": "tried", "text": "Fe–N4 single-atom sites"},
                {"section": "tried", "text": "Fe–N4 single-atom sites"},  # dup
                {"section": "open", "text": "   "},  # blank, skipped
            ],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.ledger_added == 1

    def test_ledger_added_zero_when_no_ledger_add(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"logbook": []}))
        assert out.status == "succeeded"
        assert out.ledger_added == 0


class TestModelCannotFabricateResults:
    """gripes 171148/171149: a local model proposer fabricated a numeric
    barrier ("barrier=0.892 eV") inside a `result` logbook entry — the loop
    treated it as a trusted measurement, believed the quest solved, and
    stopped proposing candidates (dry ticks). The model may narrate, but only
    the system (:mod:`precis.quest.compute`) may author a `result` /
    `milestone` / `cost` entry, and a stated barrier number must always read
    as unverified."""

    def _logs(self, store: Any, qid: int) -> list[Any]:
        blocks = store.list_blocks_for_ref(qid)
        return [b for b in blocks if b.chunk_kind == "quest_log"]

    def test_model_result_with_fabricated_barrier_is_downgraded_and_flagged(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [
                {
                    "entry_type": "result",
                    "text": "catpath result: barrier=0.892 eV new leader",
                }
            ]
        }
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        logs = [b for b in self._logs(store, qid) if "barrier=0.892" in b.text]
        assert len(logs) == 1
        entry = logs[0]
        assert entry.meta["entry_type"] == "observation"  # NOT "result"
        assert entry.text.startswith("[unverified model claim] ")
        # never counted/treated as a trusted result
        assert not any(
            (b.meta or {}).get("entry_type") == "result" for b in self._logs(store, qid)
        )

    def test_model_milestone_is_clamped_to_observation(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {"logbook": [{"entry_type": "milestone", "text": "quest solved"}]}
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        logs = [b for b in self._logs(store, qid) if "quest solved" in b.text]
        assert len(logs) == 1
        assert logs[0].meta["entry_type"] == "observation"

    def test_normal_hypothesis_observation_dead_end_pass_through_untouched(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [
                {"entry_type": "hypothesis", "text": "Try Fe-N4 sites"},
                {"entry_type": "observation", "text": "The tail looks stalled"},
                {"entry_type": "dead-end", "text": "Cu adatom beaten by frontier"},
            ]
        }
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        logs = self._logs(store, qid)
        by_text = {b.text: b.meta["entry_type"] for b in logs}
        assert by_text["Try Fe-N4 sites"] == "hypothesis"
        assert by_text["The tail looks stalled"] == "observation"
        assert by_text["Cu adatom beaten by frontier"] == "dead-end"
        # none of these carry an eV/barrier claim, so no prefix is added
        assert not any(t.startswith("[unverified model claim]") for t in by_text)


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

    def test_prompt_shows_ledger_ruled_out_entries_not_open(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        append_ledger_entry(store, qid, "ruled-out", "Pd(111) bare — beaten on barrier")
        append_ledger_entry(store, qid, "tried", "Fe-N4 single atom sites")
        append_ledger_entry(store, qid, "open", "Does co-adsorbed H help?")
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "Ruled-out ledger (do NOT re-propose these directions)" in prompt
        assert "Pd(111) bare — beaten on barrier" in prompt
        assert "Fe-N4 single atom sites" in prompt
        # the Open section is a to-do list, not a "do not re-propose" constraint
        assert "Does co-adsorbed H help?" not in prompt


def test_dossier_relation_registered() -> None:
    from precis.store.types import _INVERSE_RELATIONS

    assert _INVERSE_RELATIONS["dossier-of"] == "has-dossier"
    assert _INVERSE_RELATIONS["has-dossier"] == "dossier-of"


# ── paper relation (the quest web dashboard's "Paper" hub link) ─────────


class TestPaperRelation:
    """``paper-of`` — the SEPARATE reader-facing draft a quest may have,
    distinct from its dossier. Migration 0089. Nothing mints this draft yet
    (docs/design/paper-writing-pipeline.md); only the relation + a read-only
    resolver (:func:`paper_ref_id`) exist so the web dashboard can link one
    in when some other writer creates it."""

    def test_no_paper_resolves_none(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving with no paper yet")
        assert paper_ref_id(store, qid) is None
        # a dossier existing doesn't imply a paper — they're separate drafts
        ensure_dossier(store, qid)
        assert paper_ref_id(store, qid) is None

    def test_linked_paper_resolves(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving with a reader-facing paper")
        ref, _heading = store.create_draft(
            name=f"quest-{qid}-paper",
            title="Paper — draft",
            project_ref_id=qid,
            relation="paper-of",
        )
        assert paper_ref_id(store, qid) == ref.id
        # the paper is NOT the dossier — a quest can have neither, either, or
        # both, and they resolve independently
        assert dossier_ref_id(store, qid) is None

    def test_soft_deleted_paper_resolves_none(self, store: Any) -> None:
        """Mirrors the dossier case: a ``paper-of`` link surviving the
        soft-delete of its target draft must not resolve to a live id."""
        qid = _mk_quest(store, "A striving whose paper gets deleted")
        ref, _heading = store.create_draft(
            name=f"quest-{qid}-paper-deleted",
            title="Paper — draft",
            project_ref_id=qid,
            relation="paper-of",
        )
        assert paper_ref_id(store, qid) == ref.id
        store.soft_delete_draft(ref.id)
        assert paper_ref_id(store, qid) is None


def test_paper_relation_registered() -> None:
    from precis.store.types import _INVERSE_RELATIONS

    assert _INVERSE_RELATIONS["paper-of"] == "has-paper"
    assert _INVERSE_RELATIONS["has-paper"] == "paper-of"


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
        # "adatom" is prose (one of the placement knobs the agent picks from —
        # "pick ANY dopant element ... its placement (an adatom on the
        # surface / ...)"), NOT a param_space enumeration — PARAM_SPACE
        # carries no chemistry menu (removed; see catalyst_seed.PARAM_SPACE).
        assert "adatom" in prompt
        assert "pick ANY dopant element" in prompt

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

    def test_reaction_context_steers_toward_novelty_without_enumerating_elements(
        self, store: Any
    ) -> None:
        # gripe 171149: the loop kept re-proposing the same handful of
        # adatoms once it believed it had "solved" the quest. The design
        # change (rework) removed the code-owned element shortlist entirely
        # — the novelty steer states the PRINCIPLE ("don't repeat what's
        # tried") and the agent picks the lever using its own chemistry
        # judgment; no Python code names a specific element anywhere.
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "NOT already in the frontier" in prompt
        assert "own chemistry judgment" in prompt
        assert "do not repeat a composition already tried" in prompt
        # no closed element menu anywhere in the prompt
        assert "∈ {" not in prompt

    def test_prompt_describes_knobs_in_prose_not_a_choices_menu(
        self, store: Any
    ) -> None:
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "pick ANY dopant element" in prompt
        assert "your own chemistry judgment" in prompt
        assert "Only the fcc(111) facet is buildable today" in prompt
        # the illustrative `Cu` syntax example is explicitly flagged as such,
        # never presented as a menu entry
        assert "not a suggested element" in prompt

    def test_prompt_makes_unevaluated_barriers_unciteable(self, store: Any) -> None:
        # gripes 171148/171149: the frontier caveat must say an "awaiting a
        # sim" candidate has an UNKNOWN barrier the model may not cite/rank
        # on, and that the model never emits result/milestone entries itself.
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "UNKNOWN barrier" in prompt
        assert "may NOT cite, claim, or rank on a barrier" in prompt
        assert "You do not emit" in prompt and "result" in prompt

    def test_creed_block_present_without_a_champion_yet(self, store: Any) -> None:
        # No converged candidate yet — the creed still renders (moving-target
        # framing, "first move" framing) but omits a fabricated "champion".
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "explorer's creed" in prompt
        assert "relentless catalysis researcher" in prompt
        assert 'Forbidden: never write "solved"' in prompt
        assert "Champion to beat" not in prompt
        assert "you have the first move" in prompt

    def test_creed_block_states_the_champion_once_one_exists(self, store: Any) -> None:
        # A converged, measured candidate makes the frontier non-empty — the
        # creed's "champion to beat" line names its barrier, reframing the
        # graduation threshold as a moving target rather than a fixed line.
        from precis.quest.catalyst_seed import seed_catalyst_quest

        qid, created = seed_catalyst_quest(store)
        assert created
        candidate = compute_mod.ensure_candidate(
            store, qid, {"name": "Champion candidate", "structure": _tick_spec("Fe")}
        )
        assert candidate is not None
        store.stamp_ref_meta(candidate, {"barrier": 0.42})
        store.structure_record_run(
            candidate,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-9.0,
        )
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        assert "Champion to beat" in prompt
        assert "0.42" in prompt
        assert "Tried:" in prompt
        assert "Champion candidate 0.42 (BEST)" in prompt


def _tick_spec(element: str) -> dict[str, Any]:
    return {
        "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
        "ops": [{"op": "add_atom", "element": element, "frac": [0.0, 0.0, 0.5]}],
    }


class TestFrontierSummaryNamesUnevaluated:
    """The unevaluated band used to report only a bare count — the model
    inside a tick couldn't see *which* of its own candidates was still
    awaiting a sim."""

    def test_unevaluated_candidate_handle_appears_in_the_prompt(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        converged = compute_mod.ensure_candidate(
            store, qid, {"name": "done candidate", "structure": _tick_spec("Fe")}
        )
        assert converged is not None
        store.structure_record_run(
            converged,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=5,
            max_disp=0.0,
            energy=-10.0,
        )
        pending = compute_mod.ensure_candidate(
            store, qid, {"name": "pending candidate", "structure": _tick_spec("Co")}
        )
        assert pending is not None and pending != converged
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        from precis.utils import handle_registry

        pending_handle = handle_registry.try_format("structure", pending)
        assert pending_handle is not None
        assert pending_handle in prompt
        assert "awaiting a sim" in prompt

    def test_ruled_out_candidate_is_named_so_it_is_not_re_proposed(
        self, store: Any
    ) -> None:
        # gripe 171149: a candidate the ledger already killed (a `ruled-out:`
        # tag, e.g. relax-failed) must be named in the frontier section so the
        # model does not re-propose it — otherwise it silently reads as merely
        # "awaiting a sim" (unexplored) rather than dead.
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        dead = compute_mod.ensure_candidate(
            store, qid, {"name": "dead candidate", "structure": _tick_spec("Ni")}
        )
        assert dead is not None
        store.add_tag(dead, Tag.open("ruled-out:relax-failed"), set_by="system")
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest)
        from precis.utils import handle_registry

        dead_handle = handle_registry.try_format("structure", dead)
        assert dead_handle is not None
        assert dead_handle in prompt
        assert "ruled out" in prompt


class TestReviewLogbookTail:
    """A frontier review steps back over accumulated history, so it should
    read a deeper logbook tail than a cheap local tick's trailing-8 window."""

    def test_review_sees_a_deeper_tail_than_a_local_tick(self, store: Any) -> None:
        from precis.quest.logbook import append_entry

        qid = _mk_quest(store, "A striving")
        for i in range(12):
            append_entry(
                store,
                qid,
                text=f"logbook entry number {i}",
                entry_type="observation",
                by="agent",
            )
        quest = store.get_ref(kind="quest", id=qid)
        local_prompt = build_tick_prompt(store, quest, review=False)
        review_prompt = build_tick_prompt(store, quest, review=True)
        old_entry = "logbook entry number 2"  # outside the trailing-8 window
        assert old_entry not in local_prompt
        assert old_entry in review_prompt


class TestFrontierAlwaysOn:
    """The Pareto frontier (rung 4c's review-only measurement table) now
    renders on every tick, local or review — the model reasons from the same
    numbers either way."""

    def test_frontier_section_appears_on_a_local_tick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        monkeypatch.setattr(
            tick_mod,
            "_frontier_summary",
            lambda s, q, **_kw: "SENTINEL-FRONTIER-LOCAL",
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
            tick_mod,
            "_frontier_summary",
            lambda s, q, **_kw: "SENTINEL-FRONTIER-REVIEW",
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


def _sequenced_dispatch(
    payloads: list[dict[str, Any] | None],
) -> tuple[Any, list[Any]]:
    """A ``dispatch_fn`` stub returning successive canned payloads per call,
    holding the last payload once the list is exhausted. Shared by the
    primary tick call AND the commit ladder's re-prompt calls (they use the
    same ``disp`` callable), so this lets a test script a whole tick's worth
    of LLM turns. Records every ``LlmRequest`` so a test can assert call
    count / tier escalation."""
    calls: list[Any] = []

    def _d(req: Any) -> Any:
        idx = min(len(calls), len(payloads) - 1)
        calls.append(req)
        return SimpleNamespace(
            data=payloads[idx], text="", error=None, cost_usd=0.01, paused=False
        )

    return _d, calls


class TestCommitReRepromptLadder:
    """Core-principle rework: code never picks the chemistry — it only
    guarantees the AGENT is asked to act. When the model dispatches zero
    sims for ``PRECIS_QUEST_FORCE_EXPERIMENT_EVERY`` (default 2) consecutive
    ticks, ``run_quest_tick`` re-prompts the SAME model with a hard "commit
    now" directive, escalates one tier if that still comes back empty, and
    backs off — never fabricating a dispatch — if the model still proposes
    nothing after that."""

    _EMPTY: dict[str, Any] = {"logbook": [], "dossier_markdown": "", "proposals": []}

    @staticmethod
    def _proposal(name: str = "Fe adatom") -> dict[str, Any]:
        return {
            "logbook": [],
            "dossier_markdown": "",
            "proposals": [
                {"name": name, "rationale": "x", "structure": _tick_spec("Fe")}
            ],
        }

    def _stub_run_compute_step(self, monkeypatch: Any) -> list[list[dict[str, Any]]]:
        """A fake ``run_compute_step`` that never touches real compute — a
        non-empty ``proposals`` list "dispatches" (records 1 sim), an empty
        one dispatches nothing. Records every call's proposals so a test can
        assert how many times, and with what, the tick invoked it."""
        calls: list[list[dict[str, Any]]] = []

        def _fake(
            _store: Any,
            _quest_id: int,
            proposals: list[dict[str, Any]],
            *,
            hub: Any = None,
            dispatch: bool = True,
            by: str = "agent",
        ) -> Any:
            proposals = list(proposals or [])
            calls.append(proposals)
            n = 1 if proposals else 0
            return compute_mod.ComputeStep(
                candidates_created=n,
                sims_dispatched=n,
                results_harvested=0,
                ruled_out=0,
                notes=[],
                graduated=0,
            )

        monkeypatch.setattr(compute_mod, "run_compute_step", _fake)
        return calls

    def _logs(self, store: Any, qid: int) -> list[Any]:
        return [
            b for b in store.list_blocks_for_ref(qid) if b.chunk_kind == "quest_log"
        ]

    def test_first_dry_tick_advances_counter_without_a_commit_reprompt(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        disp, reqs = _sequenced_dispatch([self._EMPTY])
        out = run_quest_tick(store, qid, dispatch_fn=disp, compute=True)
        assert out.status == "succeeded"
        assert out.sims_dispatched == 0
        assert len(reqs) == 1  # only the primary pass — stall below threshold
        assert calls == [[]]
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 1
        assert not any(
            "committed after re-prompt" in (b.text or "")
            for b in self._logs(store, qid)
        )

    def test_commit_reprompt_succeeds_at_the_current_tier(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        disp, reqs = _sequenced_dispatch([self._EMPTY, self._EMPTY, self._proposal()])
        run_quest_tick(store, qid, dispatch_fn=disp, compute=True)  # tick 1: dry
        out = run_quest_tick(
            store, qid, dispatch_fn=disp, compute=True
        )  # tick 2: ladder
        assert out.status == "succeeded"
        assert len(reqs) == 3  # 2 primary passes + 1 successful commit re-prompt
        assert reqs[-1].tier == reqs[0].tier  # no escalation needed
        assert "COMMIT NOW" in reqs[-1].prompt
        assert len(calls) == 3
        assert calls[-1][0]["name"] == "Fe adatom"
        assert out.sims_dispatched == 1
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 0
        assert any(
            "committed after re-prompt" in (b.text or "")
            for b in self._logs(store, qid)
        )

    def test_empty_reprompt_escalates_one_tier_then_succeeds(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        disp, reqs = _sequenced_dispatch(
            [self._EMPTY, self._EMPTY, self._EMPTY, self._proposal()]
        )
        run_quest_tick(store, qid, dispatch_fn=disp, compute=True)  # tick 1: dry
        out = run_quest_tick(
            store, qid, dispatch_fn=disp, compute=True
        )  # tick 2: ladder
        assert out.status == "succeeded"
        assert len(reqs) == 4  # 2 primary passes + 2 ladder rungs
        from precis.utils.llm.router import Tier

        assert reqs[2].tier != Tier.CLOUD_SUPER  # first rung: current tier
        assert reqs[3].tier == Tier.CLOUD_SUPER  # escalated rung
        assert len(calls) == 3  # 2 dry primary passes + 1 successful ladder dispatch
        assert out.sims_dispatched == 1
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 0

    def test_both_rungs_empty_backs_off_without_crashing(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        disp, reqs = _sequenced_dispatch([self._EMPTY])  # always empty
        run_quest_tick(store, qid, dispatch_fn=disp, compute=True)  # tick 1: dry
        out = run_quest_tick(
            store, qid, dispatch_fn=disp, compute=True
        )  # tick 2: ladder
        assert out.status == "succeeded"  # the tick itself never fails
        assert len(reqs) == 4  # 2 primary passes + 2 ladder rungs, all empty
        # run_compute_step only ever saw the two (empty) primary passes — the
        # ladder never fabricated a dispatch of its own.
        assert calls == [[], []]
        assert out.sims_dispatched == 0
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 2  # NOT reset
        # a genuine decline (both rungs answered, neither proposed) reads
        # differently in the logbook than an unreachable-agent back-off.
        assert any(
            "agent declined to propose an untried variant" in (b.text or "")
            for b in self._logs(store, qid)
        )
        assert not any(
            "agent unreachable" in (b.text or "") for b in self._logs(store, qid)
        )

    def test_both_rungs_erroring_reads_as_unreachable_not_a_decline(
        self, store: Any, monkeypatch: Any
    ) -> None:
        # gripe fold-in B: an LLM transport/breaker/quota error must not read
        # the same as a genuine "the model looked and declined" — the whole
        # point of the ladder's log line is diagnosing which one happened.
        self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        # Prime the stall counter directly (as if a prior dry tick already
        # ran) — this tick's own primary pass succeeds-but-empty, pushing
        # the stall to the force-every threshold, then both ladder rungs
        # error (simulated breaker trip).
        store.stamp_ref_meta(qid, {"ticks_since_experiment": 1})

        calls: list[Any] = []

        def _mixed_disp(req: Any) -> Any:
            calls.append(req)
            if len(calls) == 1:  # this tick's primary pass: succeed, empty
                return SimpleNamespace(
                    data=self._EMPTY, text="", error=None, cost_usd=0.01, paused=False
                )
            # both ladder rungs error
            return SimpleNamespace(
                data=None, text="", error="breaker tripped", cost_usd=0.0, paused=False
            )

        out = run_quest_tick(store, qid, dispatch_fn=_mixed_disp, compute=True)
        assert out.status == "succeeded"
        assert len(calls) == 3  # 1 primary + 2 erroring ladder rungs
        assert any(
            "agent unreachable (LLM error/paused)" in (b.text or "")
            for b in self._logs(store, qid)
        )
        assert not any(
            "agent declined to propose" in (b.text or "")
            for b in self._logs(store, qid)
        )

    def test_a_tick_that_dispatches_never_triggers_the_ladder(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")
        store.stamp_ref_meta(qid, {"ticks_since_experiment": 5})
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch(self._proposal()), compute=True
        )
        assert out.sims_dispatched == 1
        assert len(calls) == 1  # no ladder call
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 0

    def test_exception_in_commit_path_degrades_gracefully_and_still_stamps(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)
        qid = _mk_quest(store, "A striving")

        n = {"c": 0}

        def _raising_disp(_req: Any) -> Any:
            n["c"] += 1
            if n["c"] > 2:  # the two primary (dry) tick calls succeed; the
                # commit ladder's first re-prompt raises (simulated transport bug).
                raise RuntimeError("transport boom")
            return SimpleNamespace(
                data=self._EMPTY, text="", error=None, cost_usd=0.01, paused=False
            )

        run_quest_tick(store, qid, dispatch_fn=_raising_disp, compute=True)  # tick 1
        out = run_quest_tick(
            store, qid, dispatch_fn=_raising_disp, compute=True
        )  # tick 2
        assert out.status == "succeeded"  # the tick degrades, never crashes
        assert calls == [[], []]  # only the two primary (dry) passes
        qref = store.get_ref(kind="quest", id=qid)
        assert qref is not None
        assert qref.meta.get("ticks_since_experiment") == 2  # still stamped
        assert any(
            "commit re-prompt ladder errored" in (b.text or "")
            for b in self._logs(store, qid)
        )
