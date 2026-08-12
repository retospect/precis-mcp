"""Whole-draft taproot backfill (:mod:`precis.taproot.backfill`).

Two layers:

* Pure segmenter (``segment_cite_groups``) — no DB, exercises the
  cite-group-anchored (not per-sentence) partition and the skip rules.
* DB-backed ``plan_chunk`` / ``apply_chunk`` over real ``refs``/``chunks``/
  ``links`` via the ``hub``/``store`` fixtures, with the cascade functions
  (extract / block / judge / merge_confirm) injected so convergence is
  deterministic and no LLM/embedder runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.store.store import Store
from precis.taproot.backfill import (
    apply_chunk,
    plan_chunk,
    segment_cite_groups,
)
from precis.taproot.canon import Candidate, CanonicalClaim, Verdict
from precis.taproot.seniority import is_claim_hub
from tests.workers._helpers import seed_chunk, seed_ref

# ── fakes ────────────────────────────────────────────────────────────────


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def _verdict(v: str, c: float) -> Verdict:
    return {"verdict": v, "confidence": c, "rationale": "test"}  # type: ignore[typeddict-item]


def _extract_const(sentence: str | None):
    return lambda span: _claim(sentence) if sentence is not None else None


def _block_none(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
    return []


def _block_hit(hub_ref_id: int, claim_text: str):
    def _b(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
        return [Candidate(hub_ref_id=hub_ref_id, claim=claim_text, distance=0.05)]

    return _b


def _never_called(*_a: Any, **_k: Any) -> Any:  # judge/merge_confirm not reached
    raise AssertionError("cascade fn should not have been called")


# ── segmentation (pure) ──────────────────────────────────────────────────


def test_segment_single_pc_group() -> None:
    groups = segment_cite_groups("Ribbons are semiconducting [pc12].")
    assert len(groups) == 1
    assert groups[0].handles == ["pc12"]
    assert groups[0].span_text == "Ribbons are semiconducting"


def test_segment_adjacent_pc_share_one_span() -> None:
    groups = segment_cite_groups("A broadly held result [pc1][pc2].")
    assert len(groups) == 1
    assert groups[0].handles == ["pc1", "pc2"]


def test_segment_adjacent_pc_with_space_share_span() -> None:
    groups = segment_cite_groups("A result [pc1] [pc2].")
    assert len(groups) == 1
    assert groups[0].handles == ["pc1", "pc2"]


def test_segment_skips_fi_and_pinned_cites() -> None:
    # [fi5] already a hub cite; [fi5>pc1] hub-pinned; [pc2+pa9] authorial pin.
    text = "Done [fi5]. Pinned [fi5>pc1]. Also [pc2+pa9]. But raw [pc7]."
    groups = segment_cite_groups(text)
    assert [g.handles for g in groups] == [["pc7"]]


def test_segment_fi_cite_advances_boundary_no_readback() -> None:
    # The pc-cite grounds only the prose AFTER the preceding fi-cite.
    text = "First claim [fi5]. Second distinct claim [pc9]."
    groups = segment_cite_groups(text)
    assert len(groups) == 1
    assert groups[0].span_text == "Second distinct claim"


def test_segment_pc_after_fi_zero_gap_is_own_group() -> None:
    # Stacked with NO separating space: [fi9][pc2]. pc2 must NOT fold back
    # across the fi-cite into pc1's run — it starts its own (empty-span)
    # group, so its evidence is never misattributed to the first claim and
    # its marker is never silently dropped.
    groups = segment_cite_groups("First claim [pc1]. Second fact[fi9][pc2].")
    assert [g.handles for g in groups] == [["pc1"], ["pc2"]]
    assert groups[0].span_text == "First claim"
    assert groups[1].span_text == ""  # after the fi boundary → no-claim later


def test_segment_multiple_groups_in_order() -> None:
    text = "Claim one [pc1]. Then claim two [pc2]."
    groups = segment_cite_groups(text)
    assert [g.handles for g in groups] == [["pc1"], ["pc2"]]
    assert groups[0].span_text == "Claim one"
    assert groups[1].span_text == "Then claim two"


def test_segment_empty_when_no_pc_cites() -> None:
    assert segment_cite_groups("All converted [fi1] and [fi2].") == []


# ── segmentation: the [pa] arm ───────────────────────────────────────────


def test_segment_single_pa_group() -> None:
    groups = segment_cite_groups("A landmark result [pa42].")
    assert len(groups) == 1
    assert groups[0].handles == ["pa42"]
    assert groups[0].kind == "pa"
    assert groups[0].span_text == "A landmark result"


def test_segment_adjacent_pa_share_one_span() -> None:
    groups = segment_cite_groups("A broadly held result [pa1][pa2].")
    assert len(groups) == 1
    assert groups[0].handles == ["pa1", "pa2"]
    assert groups[0].kind == "pa"


def test_segment_pa_and_pc_never_share_a_group() -> None:
    # A whole-paper [pa] and a passage [pc] cite are routed differently, so a
    # kind switch breaks contiguity even with no separating space: two groups.
    groups = segment_cite_groups("A widely held result [pc1][pa2].")
    assert [(g.kind, g.handles) for g in groups] == [
        ("pc", ["pc1"]),
        ("pa", ["pa2"]),
    ]
    assert groups[0].span_text == "A widely held result"
    assert groups[1].span_text == ""  # empty span after the pc → no-claim later


def test_segment_pc_after_pa_zero_gap_is_own_group() -> None:
    groups = segment_cite_groups("A widely held result [pa1][pc2].")
    assert [(g.kind, g.handles) for g in groups] == [
        ("pa", ["pa1"]),
        ("pc", ["pc2"]),
    ]


# ── DB-backed fixtures ───────────────────────────────────────────────────


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _seed_draft_para(draft: DraftHandler, hub: Hub, text: str) -> int:
    """Seed a one-paragraph draft ``nt`` with ``text``; return its body
    chunk_id."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_handle = hub.live_store.reading_order(ref.id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=text,
        at={"after": "¶" + title_handle},
    )
    order = hub.live_store.reading_order(ref.id)
    return int(order[-1].chunk_id)


def _finding_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding' AND deleted_at IS NULL"
        ).fetchone()
    return int(row[0]) if row else 0


def _pc_of(store: Store, *, paper_title: str = "src paper") -> tuple[int, str]:
    """A paper + one chunk on it; return (paper_ref_id, 'pc<chunk_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text="grounding passage")
    return paper, f"pc{chunk_id}"


def _fetched_pa(store: Store, *, paper_title: str = "fetched paper") -> tuple[int, str]:
    """A FETCHED paper (has ≥1 body chunk) cited whole; return
    (paper_ref_id, 'pa<ref_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    seed_chunk(store, ref_id=paper, text="some body passage")
    return paper, f"pa{paper}"


def _stub_pa(store: Store, *, paper_title: str = "stub paper") -> tuple[int, str]:
    """A STUB paper (0 body chunks, un-fetched) cited whole; return
    (paper_ref_id, 'pa<ref_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    return paper, f"pa{paper}"


def _fetched_pa_c(
    store: Store, *, paper_title: str = "fetched paper", text: str = "some body passage"
) -> tuple[int, str, int]:
    """A FETCHED paper cited whole, exposing its chunk_id — return
    (paper_ref_id, 'pa<ref_id>', chunk_id). For re-ground tests that assert the
    rewrite targets a specific [pc<chunk>]."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=text)
    return paper, f"pa{paper}", chunk_id


def _locate_first(
    span: str, chunks: list[tuple[int, int, str]]
) -> tuple[int, int, str] | None:
    """Deterministic fake LocateFn: pick the paper's first chunk (no LLM)."""
    return chunks[0] if chunks else None


def _locate_none(
    span: str, chunks: list[tuple[int, int, str]]
) -> tuple[int, int, str] | None:
    """Fake LocateFn that locates no passage → reground-nomatch."""
    return None


def _locate_by_sentinel(
    span: str, chunks: list[tuple[int, int, str]]
) -> tuple[int, int, str] | None:
    """Fake LocateFn: None when the paper's first chunk text contains
    ``NOMATCH``, else that chunk — lets a multi-supporter run make one paper
    locate and another fail (the all-or-nothing guard)."""
    if not chunks:
        return None
    return None if "NOMATCH" in chunks[0][2] else chunks[0]


def _links_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM links").fetchone()
    return int(row[0]) if row else 0


def _edge_is_ref_level(store: Store, *, paper_ref_id: int, hub_ref_id: int) -> bool:
    """True iff the paper→hub edge exists AND is ref-level (src_chunk_id NULL —
    the whole-paper, ungrounded [pa]-arm edge)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper_ref_id, hub_ref_id),
        ).fetchone()
    return row is not None and row[0] is None


# ── plan_chunk (read-only) ───────────────────────────────────────────────


def test_plan_new_claim_writes_nothing(draft: DraftHandler, hub: Hub) -> None:
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    before = _finding_count(hub.live_store)

    result = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert [p.action for p in result.plans] == ["new"]
    assert _finding_count(hub.live_store) == before  # dry-run minted nothing


def test_resolve_backfill_chunks_by_slug_and_handle(
    draft: DraftHandler, hub: Hub
) -> None:
    # Guards the --draft slug path (resolved via store.get_ref /
    # ref_identifiers, NOT a refs.slug column) and the --chunk dc<id> path.
    from argparse import Namespace

    from precis.cli.taproot import _resolve_backfill_chunks

    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"A claim [{pc}].")  # seeds draft "nt"

    by_slug = _resolve_backfill_chunks(
        hub.live_store, Namespace(chunk=None, draft="nt")
    )
    assert dc in by_slug
    by_handle = _resolve_backfill_chunks(
        hub.live_store, Namespace(chunk=f"dc{dc}", draft=None)
    )
    assert by_handle == [dc]


def test_resolve_backfill_chunks_unknown_slug_raises(hub: Hub) -> None:
    from argparse import Namespace

    from precis.cli.taproot import _resolve_backfill_chunks

    with pytest.raises(BadInput):
        _resolve_backfill_chunks(
            hub.live_store, Namespace(chunk=None, draft="no-such-draft-xyz")
        )


def test_plan_rejects_non_draft_chunk(hub: Hub) -> None:
    paper = seed_ref(hub.live_store, kind="paper")
    pchunk = seed_chunk(hub.live_store, ref_id=paper, text="body [pc1].")
    with pytest.raises(BadInput):
        plan_chunk(hub.live_store, embedder=None, chunk_id=pchunk)


# ── apply_chunk (writes) ─────────────────────────────────────────────────


def test_apply_mints_hub_and_rewrites_prose(draft: DraftHandler, hub: Hub) -> None:
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert plan.hub_ref_id is not None
    assert is_claim_hub(hub.live_store, plan.hub_ref_id)
    # prose rewritten pc → fi
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text
    assert "[pc" not in result.rewritten_text
    # evidence edge paper --> hub written
    with hub.live_store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper, plan.hub_ref_id),
        ).fetchone()
    assert edge is not None


def test_apply_converges_onto_existing_hub(draft: DraftHandler, hub: Hub) -> None:
    from precis.taproot.authoring import seed_claim_hub

    # An existing hub for the same claim, grounded by paper A.
    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = seed_claim_hub(
        hub.live_store,
        sentence="Ribbons are semiconducting.",
        scope={},
        supporters=[{"paper": paper_a}],
    )
    hub_id = existing["hub_ref_id"]

    # A different draft citing paper B for the same claim.
    paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting (reworded)."),
        block_fn=_block_hit(hub_id, "Ribbons are semiconducting."),
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,  # high-confidence same → no escalation
    )

    plan = result.plans[0]
    assert plan.action == "attach"
    assert plan.hub_ref_id == hub_id
    assert _finding_count(hub.live_store) == before  # converged, no new hub
    assert result.rewritten_text is not None
    assert f"[fi{hub_id}]" in result.rewritten_text
    # paper B now also grounds the shared hub
    with hub.live_store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper_b, hub_id),
        ).fetchone()
    assert edge is not None


def test_apply_attach_evidence_raise_leaves_prose_untouched(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gr191953: an "attach" plan has plan.hub_ref_id populated at PLAN time
    # (the ANN-matched candidate, set before any write runs) — a false
    # "landed" signal. If attach_evidence then raises (transient DB error,
    # FK hiccup, …), the prose must NOT be rewritten to cite [fi<hub>] for a
    # hub whose evidence edge never actually landed this call.
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = seed_claim_hub(
        hub.live_store,
        sentence="Ribbons are semiconducting.",
        scope={},
        supporters=[{"paper": paper_a}],
    )
    hub_id = existing["hub_ref_id"]

    paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    original_text = f"Ribbons are semiconducting [{pc}]."
    dc = _seed_draft_para(draft, hub, original_text)
    links_before = _links_count(hub.live_store)

    def _raising_attach_evidence(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("simulated transient write failure")

    monkeypatch.setattr("precis.taproot.hub.attach_evidence", _raising_attach_evidence)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting (reworded)."),
        block_fn=_block_hit(hub_id, "Ribbons are semiconducting."),
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,  # high-confidence same → no escalation
    )

    plan = result.plans[0]
    assert plan.action == "error"
    # No evidence edge landed on this call.
    assert _links_count(hub.live_store) == links_before
    # Prose must be left exactly as it was — no [fi<hub>] cite for a hub
    # whose evidence edge never landed.
    assert result.rewritten_text is None
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (dc,)
        ).fetchone()
    assert row is not None
    text = row[0]
    assert text == original_text
    assert f"[fi{hub_id}]" not in text


def test_apply_collapses_adjacent_cites_to_one_hub(
    draft: DraftHandler, hub: Hub
) -> None:
    paper1, pc1 = _pc_of(hub.live_store, paper_title="p1")
    paper2, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc = _seed_draft_para(draft, hub, f"A widely held result [{pc1}][{pc2}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("A widely held result."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert [h for h, _ in plan.supporters] == [pc1, pc2]
    # one [fi<hub>] cite, both pc markers gone
    assert result.rewritten_text is not None
    assert result.rewritten_text.count(f"[fi{plan.hub_ref_id}]") == 1
    assert "[pc" not in result.rewritten_text
    # both papers attached to the one hub
    with hub.live_store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE dst_ref_id = %s AND src_ref_id IN (%s, %s)",
            (plan.hub_ref_id, paper1, paper2),
        ).fetchone()
    assert n is not None and int(n[0]) == 2


def test_apply_collapse_space_separated_cites_leaves_no_artifact(
    draft: DraftHandler, hub: Hub
) -> None:
    # "[pc1] [pc2]." (space between) must collapse to a single "[fi<hub>]."
    # with NO leftover space before the period — the whole contiguous run is
    # replaced in one span-edit, so there is no cleanup regex to corrupt
    # unrelated markdown elsewhere in the chunk.
    _, pc1 = _pc_of(hub.live_store, paper_title="p1")
    _, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc = _seed_draft_para(draft, hub, f"A widely held result [{pc1}] [{pc2}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("A widely held result."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    fi = result.plans[0].hub_ref_id
    assert result.rewritten_text is not None
    assert f"[fi{fi}]." in result.rewritten_text  # clean: cite then period
    assert f"[fi{fi}] ." not in result.rewritten_text  # no stranded space
    assert "[pc" not in result.rewritten_text


def test_apply_leaves_no_claim_span_untouched(draft: DraftHandler, hub: Hub) -> None:
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"See prior work [{pc}].")
    before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const(None),  # NO-CLAIM
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert result.plans[0].action == "no-claim"
    assert result.rewritten_text is None  # nothing rewrote
    assert _finding_count(hub.live_store) == before


def test_apply_skips_unresolvable_pc(draft: DraftHandler, hub: Hub) -> None:
    dc = _seed_draft_para(draft, hub, "Claim with a dangling cite [pc999999999].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_never_called,  # never reached — supporter resolution fails first
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert result.plans[0].action == "unresolved"
    assert result.rewritten_text is None


def test_apply_needs_review_files_todo_and_leaves_prose(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = seed_claim_hub(
        hub.live_store,
        sentence="Ribbons are semiconducting.",
        scope={},
        supporters=[{"paper": paper_a}],
    )
    hub_id = existing["hub_ref_id"]
    _, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")

    def _todos(store: Store) -> int:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'todo' "
                "AND title LIKE 'taproot: review backfill%'"
            ).fetchone()
        return int(row[0]) if row else 0

    before_todos = _todos(hub.live_store)
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting (maybe)."),
        block_fn=_block_hit(hub_id, "Ribbons are semiconducting."),
        judge_fn=lambda a, b: _verdict("same", 0.40),  # low-confidence same
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),  # not confirmed
    )

    assert result.plans[0].action == "needs_review"
    assert result.rewritten_text is None  # prose left as [pc…]
    assert _todos(hub.live_store) == before_todos + 1  # review todo filed


def test_apply_is_idempotent_at_draft_level(draft: DraftHandler, hub: Hub) -> None:
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")

    apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    # The draft's current body chunk now reads [fi…]; a fresh backfill of it
    # finds no pc-cites → zero cite-groups.
    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    new_dc = int(order[-1].chunk_id)
    second = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=new_dc,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )
    assert second.plans == []


# ── the [pa] arm: stub-skip / re-ground / ref-level promote ────────────────


def test_plan_stub_pa_is_fetch_first_and_writes_nothing(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC5 (dry-run) + AC2: a whole-paper cite to an un-fetched stub is
    # classified stub-fetch-first without ever reaching the claim extractor.
    _, pa = _stub_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"A landmark result [{pa}].")

    result = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        ref_level=True,  # even with the override, a stub is never promoted
        extract_fn=_never_called,  # classified by block-count before extract
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert [p.action for p in result.plans] == ["stub-fetch-first"]
    assert result.plans[0].group.kind == "pa"


def test_apply_stub_pa_skipped_no_write_prose_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC2: a stub [pa] mints no hub, no edge, and leaves its [pa] token.
    _, pa = _stub_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"A landmark result [{pa}].")
    findings_before = _finding_count(hub.live_store)
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=True,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert result.plans[0].action == "stub-fetch-first"
    assert result.rewritten_text is None  # prose left as [pa…]
    assert _finding_count(hub.live_store) == findings_before  # no hub
    assert _links_count(hub.live_store) == links_before  # no edge


def test_plan_fetched_pa_default_is_reground(draft: DraftHandler, hub: Hub) -> None:
    # AC1/AC5 (dry-run): a fetched [pa] WITHOUT --ref-level re-grounds to the
    # located passage — action 'reground' carrying the target chunk_id, and the
    # dry-run writes nothing.
    _, pa, chunk_id = _fetched_pa_c(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")
    before = _finding_count(hub.live_store)

    result = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,  # not reached in default [pa] mode
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,
    )

    p = result.plans[0]
    assert p.action == "reground"
    assert p.group.kind == "pa"
    assert p.reground_targets == [chunk_id]
    assert _finding_count(hub.live_store) == before  # dry-run wrote nothing


def test_apply_ref_level_pa_mints_ungrounded_and_rewrites(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC3: fetched [pa] + --ref-level → ref-level (ungrounded) edge, [pa]→[fi].
    paper, pa = _fetched_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=True,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert plan.ungrounded is True
    assert result.n_ungrounded == 1
    assert plan.hub_ref_id is not None
    assert is_claim_hub(hub.live_store, plan.hub_ref_id)
    # prose rewritten pa → fi
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text
    assert "[pa" not in result.rewritten_text
    # the evidence edge is ref-level (whole-paper, no grounding chunk)
    assert _edge_is_ref_level(
        hub.live_store, paper_ref_id=paper, hub_ref_id=plan.hub_ref_id
    )


def test_apply_ref_level_mixed_pa_run_skips_and_preserves_both_tokens(
    draft: DraftHandler, hub: Hub
) -> None:
    # Regression (reviewer finding 1): a contiguous same-kind run mixing a stub
    # and a fetched [pa] must NOT be promoted — the prose collapse rewrites the
    # WHOLE run to one [fi], which would silently erase the stub's token (no
    # edge, no trace, and draft chunks are append-only). It is skipped
    # (fetch-first); both [pa] tokens survive, no hub, no edge.
    _, stub = _stub_pa(hub.live_store, paper_title="stub")
    _, fetched = _fetched_pa(hub.live_store, paper_title="fetched")
    dc = _seed_draft_para(draft, hub, f"A broadly held result [{stub}][{fetched}].")
    findings_before = _finding_count(hub.live_store)
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=True,
        extract_fn=_never_called,  # classified by block-count before extract
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    # One group (same-kind fold), skipped as mixed.
    assert len(result.plans) == 1
    assert result.plans[0].group.handles == [stub, fetched]
    assert result.plans[0].action == "stub-fetch-first"
    assert "mixed" in result.plans[0].note
    assert result.rewritten_text is None  # nothing rewrote
    assert _finding_count(hub.live_store) == findings_before  # no hub
    assert _links_count(hub.live_store) == links_before  # no edge
    # Both [pa] tokens still present in the live draft chunk — nothing erased.
    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    live_dc = int(order[-1].chunk_id)
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (live_dc,)
        ).fetchone()
    assert row is not None
    text = row[0]
    assert f"[{stub}]" in text and f"[{fetched}]" in text


def test_apply_fetched_pa_default_regrounds_pa_to_pc(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC1: fetched [pa] default → rewrite [pa]→[pc<chunk>] at the located
    # passage. A cite refinement, NOT a promote: no hub, no evidence edge.
    _, pa, chunk_id = _fetched_pa_c(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")
    findings_before = _finding_count(hub.live_store)
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,
    )

    plan = result.plans[0]
    assert plan.action == "reground"
    assert plan.reground_targets == [chunk_id]
    assert plan.hub_ref_id is None  # refinement, not a promote
    assert result.rewritten_text is not None
    assert f"[pc{chunk_id}]" in result.rewritten_text
    assert "[pa" not in result.rewritten_text
    assert _finding_count(hub.live_store) == findings_before  # no hub
    assert _links_count(hub.live_store) == links_before  # no edge


def test_plan_mixed_stub_fetched_pc_reports_per_group_action(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC5: a chunk mixing a stub [pa], a fetched [pa], and a [pc] reports the
    # correct per-group action (fetch-first / re-ground / promote) and writes
    # nothing. ref_level is OFF so the fetched [pa] stays re-ground.
    _, stub = _stub_pa(hub.live_store, paper_title="stub")
    _, fetched = _fetched_pa(hub.live_store, paper_title="fetched")
    _, pc = _pc_of(hub.live_store, paper_title="chunked")
    dc = _seed_draft_para(
        draft,
        hub,
        f"Stub claim [{stub}]. Whole-paper claim [{fetched}]. Passage claim [{pc}].",
    )
    findings_before = _finding_count(hub.live_store)

    result = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_extract_const("Passage claim."),  # only the pc group extracts
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,  # the fetched [pa] group locates its passage
    )

    by_kind = {(p.group.kind, tuple(p.group.handles)): p.action for p in result.plans}
    assert by_kind[("pa", (stub,))] == "stub-fetch-first"
    assert by_kind[("pa", (fetched,))] == "reground"
    assert by_kind[("pc", (pc,))] == "new"
    assert _finding_count(hub.live_store) == findings_before  # dry-run wrote nothing


def test_apply_ref_level_pa_converges_after_rewrite_failure(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC4 (retirement invariant, idempotent re-convergence): if the prose
    # rewrite fails AFTER the hub/edge commit, the draft still shows the direct
    # [pa] token (a valid grounded cite), and a re-run converges onto the same
    # hub (content-derived pub_id) and completes the rewrite — no duplicate hub
    # or edge.
    paper, pa = _fetched_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")

    class _EditFails:
        """A draft handler that mints as usual but fails the prose rewrite."""

        def edit(self, **_kw: Any) -> None:
            raise RuntimeError("simulated draft-edit failure after edge commit")

    with pytest.raises(RuntimeError):
        apply_chunk(
            hub.live_store,
            embedder=None,
            draft_handler=_EditFails(),
            chunk_id=dc,
            ref_level=True,
            extract_fn=_extract_const("Ribbons are semiconducting."),
            block_fn=_block_none,
            judge_fn=_never_called,
            merge_confirm_fn=_never_called,
        )

    # The hub + edge committed; the draft prose still reads [pa…] (grounded).
    assert _finding_count(hub.live_store) == 1
    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    live_dc = int(order[-1].chunk_id)
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (live_dc,)
        ).fetchone()
    assert row is not None
    text = row[0]
    assert f"[{pa}]" in text and "[fi" not in text

    links_after_fail = _links_count(hub.live_store)
    # Re-run with a working handler: converges by pub_id (no duplicate hub),
    # add_link is a no-op on the existing edge, and the rewrite completes.
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=live_dc,
        ref_level=True,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert _finding_count(hub.live_store) == 1  # no duplicate hub
    assert _links_count(hub.live_store) == links_after_fail  # no duplicate edge
    plan = result.plans[0]
    assert plan.hub_ref_id is not None
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text
    assert "[pa" not in result.rewritten_text


# ── slice 2: [pa]→[pc] re-ground ─────────────────────────────────────────────


def test_apply_reground_multi_supporter_rewrites_all_to_pc(
    draft: DraftHandler, hub: Hub
) -> None:
    # A contiguous [pa1][pa2] run (one span, both fetched) re-grounds each
    # supporter to its own passage → [pc<c1>][pc<c2>] (one pc per pa, same
    # count), which the existing [pc] path folds to one hub on a later run.
    _, p1, c1 = _fetched_pa_c(hub.live_store, paper_title="one")
    _, p2, c2 = _fetched_pa_c(hub.live_store, paper_title="two")
    dc = _seed_draft_para(draft, hub, f"A jointly-supported claim [{p1}][{p2}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,
    )

    plan = result.plans[0]
    assert plan.group.handles == [p1, p2]  # one folded run
    assert plan.action == "reground"
    assert plan.reground_targets == [c1, c2]
    assert result.rewritten_text is not None
    assert f"[pc{c1}][pc{c2}]" in result.rewritten_text
    assert "[pa" not in result.rewritten_text


def test_apply_reground_multi_supporter_partial_nomatch_skips_whole_run(
    draft: DraftHandler, hub: Hub
) -> None:
    # All-or-nothing: a [pa1][pa2] run where locate finds a passage for pa1 but
    # NOT pa2 skips the WHOLE run (reground-nomatch, no write) — a partial
    # rewrite would collapse the run's span and erase pa2's token (append-only).
    _, good, _gc = _fetched_pa_c(
        hub.live_store, paper_title="good", text="clean passage"
    )
    _, bad, _bc = _fetched_pa_c(hub.live_store, paper_title="bad", text="NOMATCH here")
    dc = _seed_draft_para(draft, hub, f"A jointly-supported claim [{good}][{bad}].")
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_by_sentinel,
    )

    assert len(result.plans) == 1  # one folded run
    assert result.plans[0].group.handles == [good, bad]
    assert result.plans[0].action == "reground-nomatch"
    assert result.rewritten_text is None  # nothing rewrote
    assert _links_count(hub.live_store) == links_before
    # Both [pa] tokens survive in the live draft chunk — nothing erased.
    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    live_dc = int(order[-1].chunk_id)
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (live_dc,)
        ).fetchone()
    assert row is not None
    text = row[0]
    assert f"[{good}]" in text and f"[{bad}]" in text


def test_apply_reground_partial_unresolved_handle_skips_whole_run(
    draft: DraftHandler, hub: Hub
) -> None:
    # Regression (reviewer, slice 2): a contiguous [pa_ok][pa_bad] run where one
    # handle doesn't resolve to a paper must skip the WHOLE run (no partial
    # rewrite) — else the run's span-replace would collapse two [pa] tokens into
    # one [pc] and erase the unresolved token (append-only draft chunk). The
    # slice-1 erasure class, reached via an unresolved handle rather than a stub.
    _, good, _gc = _fetched_pa_c(hub.live_store, paper_title="good")
    bad = "pa999999999"  # no such ref → resolve_paper_ref_id raises BadInput
    dc = _seed_draft_para(draft, hub, f"A jointly-cited claim [{good}][{bad}].")
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,  # never reached — the run is skipped pre-route
    )

    assert len(result.plans) == 1  # one folded run
    assert result.plans[0].group.handles == [good, bad]
    assert result.plans[0].action == "unresolved"
    assert result.rewritten_text is None  # nothing rewrote
    assert _links_count(hub.live_store) == links_before
    # Both tokens survive — the unresolved one was NOT erased.
    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    live_dc = int(order[-1].chunk_id)
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (live_dc,)
        ).fetchone()
    assert row is not None
    text = row[0]
    assert f"[{good}]" in text and f"[{bad}]" in text


def test_apply_reground_nomatch_single_leaves_pa(draft: DraftHandler, hub: Hub) -> None:
    # A fetched [pa] whose locate finds no passage → reground-nomatch, no write,
    # token left [pa] (author re-grounds by hand or uses --ref-level).
    _, pa, _c = _fetched_pa_c(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_none,
    )

    assert result.plans[0].action == "reground-nomatch"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before


def test_reground_then_promote_yields_chunk_grounded_hub(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC1 end-to-end (two-step): re-ground [pa]→[pc], then the EXISTING [pc]
    # promote path (unchanged) mints a hub whose evidence edge is
    # CHUNK-GROUNDED (src_chunk_id NOT NULL), not ref-level/ungrounded.
    paper, pa, chunk_id = _fetched_pa_c(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")

    # Step 1 — re-ground the [pa] to its passage [pc].
    r1 = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=False,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_first,
    )
    assert r1.plans[0].action == "reground"
    assert f"[pc{chunk_id}]" in (r1.rewritten_text or "")

    nt_ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert nt_ref is not None
    order = hub.live_store.reading_order(nt_ref.id)
    live_dc = int(order[-1].chunk_id)

    # Step 2 — promote the freshly-grounded [pc] via the existing path.
    r2 = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=live_dc,
        ref_level=False,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = r2.plans[0]
    assert plan.group.kind == "pc"
    assert plan.action == "new"
    assert plan.hub_ref_id is not None
    assert plan.ungrounded is False  # chunk-grounded, not ref-level
    assert f"[fi{plan.hub_ref_id}]" in (r2.rewritten_text or "")
    # The evidence edge grounds at the passage chunk (src_chunk_id NOT NULL).
    assert not _edge_is_ref_level(
        hub.live_store, paper_ref_id=paper, hub_ref_id=plan.hub_ref_id
    )
