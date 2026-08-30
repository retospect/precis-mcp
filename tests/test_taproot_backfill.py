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
from precis.taproot.canon import (
    CanonicalClaim,
    ClaimExtraction,
    MergeCandidate,
    NotClaim,
    Verdict,
)
from precis.taproot.seniority import is_claim_hub
from tests.workers._helpers import seed_chunk, seed_ref

# ── fakes ────────────────────────────────────────────────────────────────


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def _verdict(v: str, c: float) -> Verdict:
    return {"verdict": v, "confidence": c, "rationale": "test"}  # type: ignore[typeddict-item]


_EMPTY_EXTRACTION = ClaimExtraction(atoms=(), compound=None, not_claims=())


def _extract_const(sentence: str | None):
    """Fake ``ExtractFn``: NO-CLAIM (``sentence is None``) or a single atom,
    no compound — mirrors an already-atomic real extraction (step-1
    invariant: a lone atom never carries a compound)."""

    def _fn(span: str) -> ClaimExtraction:
        if sentence is None:
            return _EMPTY_EXTRACTION
        return ClaimExtraction(atoms=(_claim(sentence),), compound=None, not_claims=())

    return _fn


def _extract_multi(
    atoms: list[str], compound: str | None, not_claims: list[NotClaim] | None = None
):
    """Fake ``ExtractFn``: a decomposed extraction — ``atoms`` sentences plus
    an optional bundling ``compound`` sentence and ``not_claims``. Mirrors
    the real decomposed shape (:func:`precis.taproot.canon._coerce_extraction`'s
    invariant) so backfill's per-atom + per-compound cascade fan-out is
    exercised the same way a real ``extract_claim`` result would drive it."""

    def _fn(span: str) -> ClaimExtraction:
        return ClaimExtraction(
            atoms=tuple(_claim(s) for s in atoms),
            compound=_claim(compound) if compound is not None else None,
            not_claims=tuple(not_claims or ()),
        )

    return _fn


def _block_none(
    claim: CanonicalClaim, store: Any, embedder: Any
) -> list[MergeCandidate]:
    return []


def _block_hit(hub_ref_id: int, claim_text: str):
    def _b(claim: CanonicalClaim, store: Any, embedder: Any) -> list[MergeCandidate]:
        return [MergeCandidate(hub_ref_id=hub_ref_id, claim=claim_text, distance=0.05)]

    return _b


def _block_map(mapping: dict[str, tuple[int, str]]):
    """Fake ``BlockFn``: ``claim.sentence`` → ``(hub_ref_id, matched_claim_text)``,
    or no candidates for a sentence not in ``mapping`` — lets a multi-atom
    extraction converge some claims onto existing hubs while others mint
    fresh, in one call."""

    def _b(claim: CanonicalClaim, store: Any, embedder: Any) -> list[MergeCandidate]:
        hit = mapping.get(claim.sentence)
        return (
            [MergeCandidate(hub_ref_id=hit[0], claim=hit[1], distance=0.02)]
            if hit
            else []
        )

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


# ── front-matter fixture (shared by the DB-backed gate tests) ──────────

_FRONT_MATTER = """**Printed Touch Sensors Using Carbon NanoBud Material**

*Anton S. Anisimov, David P. Brown, Bjorn F. Mikladal, Kunjal Parikh,
Erkki Soininen, Martti Sonninen, Dewei Tian, Ilkka Varjos*

> **Canatu Oy, Helsinki, Finland, Intel Corporation, Santa Clara, USA**"""


# ── DB-backed fixtures ───────────────────────────────────────────────────


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _seed_draft_para(
    draft: DraftHandler, hub: Hub, text: str, *, draft_id: str = "nt"
) -> int:
    """Seed a one-paragraph draft ``draft_id`` (default ``nt``) with ``text``;
    return its body chunk_id. Pass a distinct ``draft_id`` for a second,
    independent draft — appending a second paragraph to the SAME draft would
    land it right after the title (before the first, already-rewritten
    paragraph), so ``order[-1]`` would resolve to the wrong chunk."""
    proj = _proj(hub)
    draft.put(id=draft_id, title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=draft_id)
    assert ref is not None
    title_handle = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id=draft_id,
        chunk_kind="paragraph",
        text=text,
        at={"after": "¶" + title_handle},
    )
    order = hub.live_store.drafts.reading_order(ref.id)
    return int(order[-1].chunk_id)


def _finding_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding' AND retired_at IS NULL"
        ).fetchone()
    return int(row[0]) if row else 0


#: A body passage that passes ``_has_grounding_prose`` — a terminated,
#: mostly-lowercase sentence. Chunk fixtures must look like real body prose:
#: a two-word stub reads as title/author front matter and is (correctly)
#: refused as evidence grounding (gripe 245842).
_PROSE = "The measured ribbons remain semiconducting at room temperature."


def _pc_of(store: Store, *, paper_title: str = "src paper") -> tuple[int, str]:
    """A paper + one chunk on it; return (paper_ref_id, 'pc<chunk_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=_PROSE)
    return paper, f"pc{chunk_id}"


def _fetched_pa(store: Store, *, paper_title: str = "fetched paper") -> tuple[int, str]:
    """A FETCHED paper (has ≥1 body chunk) cited whole; return
    (paper_ref_id, 'pa<ref_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    seed_chunk(store, ref_id=paper, text=_PROSE)
    return paper, f"pa{paper}"


def _stub_pa(store: Store, *, paper_title: str = "stub paper") -> tuple[int, str]:
    """A STUB paper (0 body chunks, un-fetched) cited whole; return
    (paper_ref_id, 'pa<ref_id>')."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    return paper, f"pa{paper}"


def _fetched_pa_c(
    store: Store, *, paper_title: str = "fetched paper", text: str = _PROSE
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
    # evidence edge paper --> hub written — born withheld: a mechanical
    # draft-citation edge carries provenance keys but NO support verdict
    # (nothing read the passage), so the publish gate holds it until a
    # verifier certifies it.
    with hub.live_store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT meta FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper, plan.hub_ref_id),
        ).fetchone()
    assert edge is not None
    edge_meta = dict(edge[0] or {})
    assert "support" not in edge_meta
    assert "caveats" not in edge_meta
    assert edge_meta["origin"] == "draft-backfill"
    assert edge_meta["arm"] == "pc"
    assert edge_meta["source_handle"] == pc
    assert edge_meta["draft_chunk"] == f"dc{dc}"


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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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


def _set_slug(store: Store, ref_id: int, slug: str) -> None:
    """seed_ref registers no cite_key, so ``Ref.slug`` is None and
    mint_citation cannot build a handle. Register one (the refs.slug column
    itself was dropped in v2; the slug reads from ``ref_identifiers``)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'cite_key', %s, 'manual') ON CONFLICT DO NOTHING",
            (ref_id, slug),
        )
        conn.commit()


def _citation_refs(store: Store) -> list[Any]:
    """Every live citation ref, oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'citation' AND retired_at IS NULL "
            "ORDER BY ref_id"
        ).fetchall()
    out = []
    for (rid,) in rows:
        ref = store.get_ref(kind="citation", id=rid)
        assert ref is not None
        out.append(ref)
    return out


def _ref_tag_values(store: Store, ref_id: int, namespace: str) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, namespace),
        ).fetchall()
    return {r[0] for r in rows}


def test_apply_reground_records_claim_passage_citation(
    draft: DraftHandler, hub: Hub
) -> None:
    # The locate proves a claim-passage binding, but the plan used to keep only
    # the chunk_id — leaving the rewritten [pc] a bare pointer at a paragraph.
    # Assert the binding is persisted as a citation audit record: the claim is
    # the cite-group span, the source is the located passage.
    paper, pa, chunk_id = _fetched_pa_c(
        hub.live_store,
        text="The ribbons conduct at room temperature under ambient pressure.",
    )
    _set_slug(hub.live_store, paper, "ribbons24")
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")
    assert _citation_refs(hub.live_store) == []

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

    assert result.plans[0].action == "reground"
    assert result.plans[0].reground_grounds[0][1] == chunk_id
    cites = _citation_refs(hub.live_store)
    assert len(cites) == 1, result.plans[0].note
    meta = cites[0].meta or {}
    # The claim is the prose the citation grounds, not a bare handle.
    assert "Ribbons are semiconducting" in meta["claim"]
    # The source is the located passage, verbatim.
    assert (
        meta["source_quote"]
        == "The ribbons conduct at room temperature under ambient pressure."
    )
    # No score is invented — the locate returns a decision, not a confidence.
    assert meta["source_handle"] == "ribbons24~0"
    assert meta.get("verifier_confidence") is None
    # A lowercase open tag, not the closed ORIGIN: axis — whose members are
    # fenced out of default search. Open tags land whole in the OPEN sentinel
    # namespace, so the prefix is part of the value.
    assert "origin:draft-backfill" in _ref_tag_values(
        hub.live_store, cites[0].id, "OPEN"
    )


def test_apply_reground_nomatch_records_no_citation(
    draft: DraftHandler, hub: Hub
) -> None:
    # No passage located → no rewrite and no audit record: a citation whose
    # claim was never grounded is worse than none.
    _, pa, _ = _fetched_pa_c(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")

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
    assert _citation_refs(hub.live_store) == []


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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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
        hub.live_store, paper_title="good", text=f"A clean passage: {_PROSE}"
    )
    _, bad, _bc = _fetched_pa_c(
        hub.live_store, paper_title="bad", text=f"NOMATCH here, but still: {_PROSE}"
    )
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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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
    order = hub.live_store.drafts.reading_order(nt_ref.id)
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


# ── decomposition: multi-atom + compound (docs/backlog/taproot-atomic-claims.md) ─


def _conjunct_atom_ids(store: Store, compound_hub_id: int) -> set[int]:
    """Atom hub ids linked ``conjunct-of`` onto ``compound_hub_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s AND relation = 'conjunct-of'",
            (compound_hub_id,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _edges_from(store: Store, src_ref_id: int) -> set[int]:
    """Every hub this ref directly links to (``src_ref_id`` → ``dst_ref_id``)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s", (src_ref_id,)
        ).fetchall()
    return {int(r[0]) for r in rows}


#: The taproot-vocabulary relations apply_extraction/attach_evidence write —
#: excludes the draft edit door's own bookkeeping edges (``draft-of``, the
#: mention-tracking ``cites`` a rewritten ``[fi<hub>]`` cite triggers), which
#: would otherwise leak into a raw ``links`` table count.
_TAPROOT_RELATIONS = ("establishes", "corroborates", "contradicts", "conjunct-of")


def _taproot_links_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE relation = ANY(%s)",
            (list(_TAPROOT_RELATIONS),),
        ).fetchone()
    return int(row[0]) if row else 0


def test_apply_decomposes_multi_atom_mints_hubs_links_and_evidence_on_atoms_only(
    draft: DraftHandler, hub: Hub
) -> None:
    # A bundled [pc] group decomposes to 2 atoms + a surviving compound:
    # 3 hubs minted, 2 conjunct-of links (atom -> compound), and the
    # supporter paper's evidence edge lands on the atoms only, never the
    # compound (step 3).
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "new"  # the compound's own placement action
    assert len(plan.atom_plans) == 2
    compound_hub = plan.hub_ref_id
    assert compound_hub is not None
    assert _finding_count(hub.live_store) == before + 3  # 2 atoms + 1 compound

    atom_hub_ids = _conjunct_atom_ids(hub.live_store, compound_hub)
    assert len(atom_hub_ids) == 2
    assert compound_hub not in atom_hub_ids

    # The paper's evidence edge lands on both atoms, never the compound.
    paper_edges = _edges_from(hub.live_store, paper)
    assert paper_edges == atom_hub_ids
    assert compound_hub not in paper_edges

    # Prose collapses to the ONE compound cite.
    assert result.rewritten_text is not None
    assert f"[fi{compound_hub}]" in result.rewritten_text
    assert "[pc" not in result.rewritten_text


def test_apply_needs_review_atom_contributes_no_conjunct_link(
    draft: DraftHandler, hub: Hub
) -> None:
    # Regression: an atom that resolves to needs_review must NOT get a
    # conjunct-of link (nothing to link — apply_placement returned no hub for
    # it) while its sibling atom and the compound still land normally.
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = seed_claim_hub(
        hub.live_store, sentence="Atom one.", scope={}, supporters=[{"paper": paper_a}]
    )
    existing_hub = existing["hub_ref_id"]

    paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")

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
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_map({"Atom one.": (existing_hub, "Atom one.")}),
        judge_fn=lambda a, b: _verdict("same", 0.40),  # low-confidence same
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),  # not confirmed
    )

    plan = result.plans[0]
    compound_hub = plan.hub_ref_id
    assert compound_hub is not None
    assert _todos(hub.live_store) == before_todos + 1  # atom 1's needs_review

    atom_hub_ids = _conjunct_atom_ids(hub.live_store, compound_hub)
    assert len(atom_hub_ids) == 1  # only atom 2 linked; atom 1 contributed nothing
    assert existing_hub not in atom_hub_ids

    # Atom 1's existing hub got no new evidence edge from this call either.
    with hub.live_store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper_b, existing_hub),
        ).fetchone()
    assert edge is None


def test_apply_multi_atom_fanout_supporters_attach_to_every_atom(
    draft: DraftHandler, hub: Hub
) -> None:
    # Two [pc] cites grounding ONE bundled span: the primary supporter
    # attaches via apply_extraction's atom loop, and the remaining
    # supporter(s) (plan.supporters[1:]) fan out corroborates to EVERY atom
    # hub — never the compound.
    paper1, pc1 = _pc_of(hub.live_store, paper_title="p1")
    paper2, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc1}][{pc2}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    plan = result.plans[0]
    compound_hub = plan.hub_ref_id
    assert compound_hub is not None
    atom_hub_ids = _conjunct_atom_ids(hub.live_store, compound_hub)
    assert len(atom_hub_ids) == 2

    p1_edges = _edges_from(hub.live_store, paper1)
    p2_edges = _edges_from(hub.live_store, paper2)
    assert p1_edges == atom_hub_ids
    assert p2_edges == atom_hub_ids
    assert compound_hub not in p1_edges
    assert compound_hub not in p2_edges


def test_apply_multi_atom_reconverges_onto_existing_hubs_idempotently(
    draft: DraftHandler, hub: Hub
) -> None:
    # A second draft, citing a different paper for the SAME bundled claim,
    # converges onto the atom + compound hubs the first run minted — no
    # duplicate hub, no duplicate conjunct-of link, and the new supporter's
    # evidence lands on the (pre-existing) atom hubs.
    paper1, pc1 = _pc_of(hub.live_store, paper_title="p1")
    dc1 = _seed_draft_para(draft, hub, f"Bundle claim [{pc1}].")

    first = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc1,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )
    compound_hub = first.plans[0].hub_ref_id
    assert compound_hub is not None
    atom_hub_ids = sorted(_conjunct_atom_ids(hub.live_store, compound_hub))
    assert len(atom_hub_ids) == 2

    findings_before = _finding_count(hub.live_store)
    conjuncts_before = _taproot_links_count(hub.live_store)

    paper2, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc2 = _seed_draft_para(draft, hub, f"Bundle claim [{pc2}].", draft_id="nt2")

    mapping = {
        "Atom one.": (atom_hub_ids[0], "Atom one."),
        "Atom two.": (atom_hub_ids[1], "Atom two."),
        "Bundle claim.": (compound_hub, "Bundle claim."),
    }

    second = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc2,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_map(mapping),
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,
    )

    plan2 = second.plans[0]
    assert plan2.action == "attach"
    assert plan2.hub_ref_id == compound_hub
    assert _finding_count(hub.live_store) == findings_before  # no new hub minted

    # No duplicate conjunct-of edges — link_claims no-oped on the existing pair,
    # plus 2 new evidence edges (paper2 -> each atom hub) is the only taproot
    # growth (the draft edit door's own draft-of/cites bookkeeping edges are
    # excluded — scoped to the taproot vocabulary).
    assert _taproot_links_count(hub.live_store) == conjuncts_before + 2
    assert set(_edges_from(hub.live_store, paper2)) == set(atom_hub_ids)
    assert compound_hub not in _edges_from(hub.live_store, paper2)

    assert second.rewritten_text is not None
    assert f"[fi{compound_hub}]" in second.rewritten_text


def test_apply_not_claims_memo_lands_on_compound_hub_meta(
    draft: DraftHandler, hub: Hub
) -> None:
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")

    not_claims: list[NotClaim] = [
        {"text": "enables next-gen tech", "reason": "forward-looking"}
    ]
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(
            ["Atom one.", "Atom two."], "Bundle claim.", not_claims=not_claims
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    compound_hub = result.plans[0].hub_ref_id
    assert compound_hub is not None
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (compound_hub,)
        ).fetchone()
    assert row is not None
    memo = dict(row[0].get("taproot_not_claims") or {})
    assert len(memo) == 1
    entry = next(iter(memo.values()))
    assert entry["text"] == "enables next-gen tech"
    assert entry["reason"] == "forward-looking"


# ── prose-less grounding is refused (gripe 245842) ───────────────────────


def test_apply_pc_on_front_matter_chunk_is_ungroundable(
    draft: DraftHandler, hub: Hub
) -> None:
    # A [pc] naming a title/author block would mint a "bibliography-stub" hub:
    # an edge that says "this paper exists", not "this passage supports the
    # claim". Skip the group, leave the prose alone.
    paper = seed_ref(hub.live_store, title="front-matter paper", kind="paper")
    fm = seed_chunk(hub.live_store, ref_id=paper, ord=0, text=_FRONT_MATTER)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [pc{fm}].")
    links_before = _links_count(hub.live_store)
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_never_called,  # never reached — no groundable supporter
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert result.plans[0].action == "ungroundable"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before
    assert _finding_count(hub.live_store) == findings_before


def test_apply_pc_on_retired_chunk_is_ungroundable(
    draft: DraftHandler, hub: Hub
) -> None:
    # A retired chunk still resolves (handle resolution filters refs.retired_at,
    # not chunks.retired_at) but is dead text — a re-chunk retires the row and
    # inserts a replacement, so the old id cites content no reader can reach.
    paper = seed_ref(hub.live_store, title="rechunked paper", kind="paper")
    old = seed_chunk(hub.live_store, ref_id=paper, ord=0, text=_PROSE)
    with hub.live_store.pool.connection() as conn:
        conn.execute("UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (old,))
        conn.commit()
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [pc{old}].")
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert result.plans[0].action == "ungroundable"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before


def test_apply_pc_run_drops_only_the_prose_less_supporter(
    draft: DraftHandler, hub: Hub
) -> None:
    # A mixed run still grounds: the front-matter supporter drops (the run
    # collapses to one [fi] either way, so no citeable loss) and the body
    # passage carries the evidence edge.
    fm_paper = seed_ref(hub.live_store, title="front-matter paper", kind="paper")
    fm = seed_chunk(hub.live_store, ref_id=fm_paper, ord=0, text=_FRONT_MATTER)
    good_paper, good_pc = _pc_of(hub.live_store, paper_title="body paper")
    dc = _seed_draft_para(
        draft, hub, f"Ribbons are semiconducting [pc{fm}][{good_pc}]."
    )

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
    assert [h for h, _ in plan.supporters] == [good_pc]
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text
    with hub.live_store.pool.connection() as conn:
        srcs = [
            r[0]
            for r in conn.execute(
                "SELECT src_ref_id FROM links WHERE dst_ref_id = %s", (plan.hub_ref_id,)
            ).fetchall()
        ]
    assert fm_paper not in srcs
    assert good_paper in srcs


def test_reground_locate_never_sees_the_front_matter_chunk(
    draft: DraftHandler, hub: Hub
) -> None:
    # The original defect: a title block is short and dense with the claim's
    # topic words, so it WINS the unigram overlap. Filtering the candidate pool
    # means even a "pick the first chunk" locate lands on the body passage.
    paper = seed_ref(hub.live_store, title="fetched paper", kind="paper")
    seed_chunk(hub.live_store, ref_id=paper, ord=0, text=_FRONT_MATTER)
    body = seed_chunk(hub.live_store, ref_id=paper, ord=1, text=_PROSE)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [pa{paper}].")

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
        locate_fn=_locate_first,  # picks chunks[0] — the pool's first entry
    )

    assert result.plans[0].action == "reground"
    assert result.plans[0].reground_targets == [body]
    assert result.rewritten_text is not None
    assert f"[pc{body}]" in result.rewritten_text


def test_reground_on_a_front_matter_only_paper_is_nomatch(
    draft: DraftHandler, hub: Hub
) -> None:
    # Filtering can empty the pool. The honest outcome is a skip that leaves
    # the [pa] in place — never a grounding on the only chunk there is.
    paper = seed_ref(hub.live_store, title="front-matter only", kind="paper")
    seed_chunk(hub.live_store, ref_id=paper, ord=0, text=_FRONT_MATTER)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [pa{paper}].")
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

    assert result.plans[0].action == "reground-nomatch"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before
