"""Tests for two dossier-hygiene follow-ups (2026-08-15):

* **narrative chunking** — the dossier narrative is now stored as many
  small paragraph-level chunks (one thought each), whole-replaced by
  :func:`precis.quest.dossier.rewrite_dossier` every tick, rather than one
  big blob — so the per-chunk embedding/summary cascade re-runs per
  thought. Readers (:func:`precis.quest.dossier.read_narrative`) reassemble
  the live chunk set back into one document.
* **``precis quest dossier-dedup``** — a one-off cleanup for a ledger that
  accumulated near-duplicate attempt nodes before :func:`add_attempt`'s
  upsert discipline landed, reusing that discipline's near-dup machinery
  (:func:`precis.quest.dossier._is_near_dup`,
  :data:`precis.quest.dossier._STATUS_RANK`).

Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis import cli
from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import (
    DedupMerge,
    _element_signature,
    _is_near_dup,
    _ledger_roots,
    _write_node_chunk,
    add_attempt,
    dedup_ledger,
    ensure_dossier,
    read_ledger,
    read_narrative,
    rewrite_dossier,
)
from precis.quest.tick import run_quest_tick


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _fake_dispatch(payload: dict[str, Any]) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text="", error=None, cost_usd=0.01, paused=False
        )

    return _d


def _live_narrative_chunks(store: Any, did: int) -> list[Any]:
    return [
        c
        for c in store.drafts.reading_order(did)
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
    ]


# ── narrative chunking ──────────────────────────────────────────────────


class TestNarrativeChunking:
    def test_rewrite_splits_into_one_chunk_per_paragraph(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        did = ensure_dossier(store, qid)
        md = (
            "Para one, first thought.\n\n"
            "Para two, second thought.\n\n"
            "Para three, third thought."
        )
        rewrite_dossier(store, qid, md)
        chunks = _live_narrative_chunks(store, did)
        assert [c.text for c in chunks] == [
            "Para one, first thought.",
            "Para two, second thought.",
            "Para three, third thought.",
        ]
        # readers reassemble the same document, in chunk order
        assert read_narrative(store, qid) == md

    def test_lone_heading_folds_into_its_following_paragraph(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        did = ensure_dossier(store, qid)
        md = "## Best leads\n\nMOF linkers look promising."
        rewrite_dossier(store, qid, md)
        chunks = _live_narrative_chunks(store, did)
        assert len(chunks) == 1  # heading + paragraph share one chunk
        assert chunks[0].text == md
        assert read_narrative(store, qid) == md

    def test_no_empty_chunks_from_blank_paragraphs(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        did = ensure_dossier(store, qid)
        rewrite_dossier(store, qid, "One thought.\n\n\n\nAnother thought.\n\n   \n\n")
        chunks = _live_narrative_chunks(store, did)
        assert [c.text for c in chunks] == ["One thought.", "Another thought."]

    def test_second_rewrite_replaces_the_whole_set(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        did = ensure_dossier(store, qid)
        rewrite_dossier(store, qid, "First take, paragraph one.\n\nFirst take, two.")
        first_chunks = _live_narrative_chunks(store, did)
        assert len(first_chunks) == 2

        rewrite_dossier(store, qid, "Second take, only paragraph.")
        second_chunks = _live_narrative_chunks(store, did)
        assert [c.text for c in second_chunks] == ["Second take, only paragraph."]
        # the old chunks are gone from the live set entirely — not stranded
        # alongside the new one.
        old_handles = {c.handle for c in first_chunks}
        new_handles = {c.handle for c in second_chunks}
        assert old_handles.isdisjoint(new_handles)
        assert read_narrative(store, qid) == "Second take, only paragraph."

    def test_ledger_survives_a_multi_paragraph_narrative_rewrite_byte_identically(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_attempt(store, qid, "dope with a transition metal") is True
        before = read_ledger(store, qid)

        rewrite_dossier(
            store,
            qid,
            "# Understanding\n\nFirst paragraph of fresh synthesis.\n\n"
            "A second, separate thought entirely.",
        )
        rewrite_dossier(store, qid, "# Understanding v2\n\nJust one paragraph now.")
        after = read_ledger(store, qid)
        assert after == before  # untouched by two multi-paragraph whole-rewrites

    def test_narrative_gate_still_applies_across_multi_paragraph_rewrites(
        self, store: Any
    ) -> None:
        """The growth-ratchet gate (`tick.py`'s `_apply_narrative_gate`)
        reads/writes through `read_narrative`/`rewrite_dossier` same as
        before — a multi-paragraph rewrite that blows the ratchet with no
        progress evidence is still rejected and the prior narrative kept."""
        qid = _mk_quest(store, "A striving")
        rewrite_dossier(store, qid, "Short prior synthesis, ten words long here now.")
        big = "\n\n".join(f"paragraph {i} word{i} word{i}" for i in range(60))
        payload = {"logbook": [], "dossier_markdown": big}
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.dossier_rewritten is False  # gate rejected the blowup
        narrative = read_narrative(store, qid)
        assert "Short prior synthesis" in narrative
        assert "paragraph 0" not in narrative


# ── dossier-dedup ────────────────────────────────────────────────────────


def _seed_ledger_pair(
    store: Any,
    qid: int,
    text_a: str,
    status_a: str,
    text_b: str,
    status_b: str,
) -> tuple[str, str]:
    """Write two SIBLING root ledger nodes directly via the low-level
    storage primitive (bypassing `add_attempt`'s own dedup-before-insert
    upsert) — simulates a ledger that accumulated near-duplicates before
    that upsert discipline landed. Returns their handles."""
    container_handle, did, _roots = _ledger_roots(store, qid)
    a = _write_node_chunk(store, did, container_handle, text_a, status_a)
    b = _write_node_chunk(store, did, container_handle, text_b, status_b)
    return str(a.handle), str(b.handle)


class TestDossierDedup:
    def test_dry_run_reports_the_merge_and_changes_nothing(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _seed_ledger_pair(
            store,
            qid,
            "identify rate-limiting step",
            "open",
            "identify the rate limiting step in the mechanism",
            "tried",
        )
        before = read_ledger(store, qid)

        merges = dedup_ledger(store, qid, dry_run=True)

        assert len(merges) == 1
        m = merges[0]
        assert isinstance(m, DedupMerge)
        assert m.survivor_text == "identify rate-limiting step"  # oldest node
        assert m.prior_status == "open"
        assert m.new_status == "tried"  # advances to the more-advanced status
        assert m.absorbed == [
            ("identify the rate limiting step in the mechanism", "tried")
        ]
        # nothing was written
        assert read_ledger(store, qid) == before

    def test_real_run_merges_keeps_oldest_and_reparents_children(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        _handle_a, handle_b = _seed_ledger_pair(
            store,
            qid,
            "identify rate-limiting step",
            "open",
            "identify the rate limiting step in the mechanism",
            "tried",
        )
        # a child hanging off the about-to-be-absorbed node B
        _container_handle, did, _roots = _ledger_roots(store, qid)
        child = _write_node_chunk(
            store, did, handle_b, "narrow to surface diffusion", "open"
        )

        merges = dedup_ledger(store, qid, dry_run=False)
        assert len(merges) == 1

        ledger = read_ledger(store, qid)
        # the survivor is present, advanced to "tried"
        assert "- [tried] identify rate-limiting step" in ledger
        # the absorbed duplicate is gone
        assert "identify the rate limiting step in the mechanism" not in ledger
        # the child re-parented onto the survivor, nested one level under it
        assert "  - [open] narrow to surface diffusion" in ledger
        # confirm the child chunk really moved (not just re-rendered) —
        # its live parent is now the survivor's own chunk, not retired.
        moved = store.drafts.get_draft_chunk(child.handle)
        survivor_chunk = store.drafts.get_draft_chunk(merges[0].survivor_handle)
        assert moved is not None and not moved.retired
        assert survivor_chunk is not None
        assert moved.parent_chunk_id == survivor_chunk.chunk_id
        absorbed_chunk = store.drafts.get_draft_chunk(handle_b)
        assert absorbed_chunk is not None and absorbed_chunk.retired

        # idempotent — nothing left to merge on a second pass
        assert dedup_ledger(store, qid, dry_run=False) == []
        assert read_ledger(store, qid) == ledger

    def test_survivor_status_never_regresses(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _seed_ledger_pair(
            store,
            qid,
            "identify rate-limiting step",
            "ruled-out",
            "identify the rate limiting step in the mechanism",
            "open",
        )
        merges = dedup_ledger(store, qid, dry_run=False)
        assert len(merges) == 1
        assert merges[0].prior_status == "ruled-out"
        assert merges[0].new_status == "ruled-out"  # never regresses to "open"
        assert "- [ruled-out] identify rate-limiting step" in read_ledger(store, qid)

    def test_no_duplicates_returns_empty(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_attempt(store, qid, "a totally unrelated direction") is True
        assert dedup_ledger(store, qid, dry_run=True) == []
        assert dedup_ledger(store, qid, dry_run=False) == []


class TestElementGuard:
    """Different chemistry never merges, however similar the prose — the
    element-signature veto (:func:`_elements_conflict`). Found live on
    qu164903: a dedup dry-run clustered Rh/Ru/Fe/Co SAA branches into one
    node because :func:`precis.quest.dossier._sig_tokens` drops all <4-char
    tokens, making the element symbol invisible to the Jaccard match."""

    def test_element_signature_extraction(self) -> None:
        sig = _element_signature("Rh-sub on Pd(111) weakens N–O for NO→NH3")
        assert sig == {"Rh", "Pd"}  # one-letter N/O and NO/NH3 never count
        # ambiguous English-word symbols are excluded even capitalised
        assert _element_signature("In situ, As shown, No candidate At all") == set()

    def test_different_elements_never_near_dup(self) -> None:
        rh = "Rh substitutional SAA on Pd(111)"
        assert _is_near_dup("Ru substitutional SAA on Pd(111)", [rh]) is False
        assert (
            _is_near_dup(
                "Surface Ag substitution coverage series (1/2/3-atom SAA)",
                ["Surface Au substitution coverage series (1/2/3-atom SAA)"],
            )
            is False
        )

    def test_same_element_rephrase_still_merges(self) -> None:
        assert (
            _is_near_dup(
                "Rh substitutional single-atom alloy on Pd(111) surface",
                ["Rh substitutional alloy on Pd(111) surface"],
            )
            is True
        )

    def test_upsert_keeps_element_branches_distinct(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_attempt(store, qid, "Rh substitutional SAA on Pd(111)") is True
        # a different element mints a NEW node instead of upserting into Rh
        assert add_attempt(store, qid, "Ru substitutional SAA on Pd(111)") is True
        ledger = read_ledger(store, qid)
        assert "Rh substitutional SAA" in ledger
        assert "Ru substitutional SAA" in ledger

    def test_dedup_ledger_never_clusters_across_elements(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _seed_ledger_pair(
            store,
            qid,
            "Rh substitutional SAA on Pd(111)",
            "active",
            "Ru substitutional SAA on Pd(111)",
            "active",
        )
        assert dedup_ledger(store, qid, dry_run=True) == []


def test_dossier_dedup_cli_subcommand_parses() -> None:
    """`precis quest dossier-dedup <id> [--dry-run]` registers and parses."""
    parser = cli._build_parser()
    args = parser.parse_args(["quest", "dossier-dedup", "7", "--dry-run"])
    assert args.quest_cmd == "dossier-dedup"
    assert args.id == 7
    assert args.dry_run is True

    args2 = parser.parse_args(["quest", "dossier-dedup", "7"])
    assert args2.dry_run is False
