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

import json
import re
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
from precis.taproot.directed import QualifyResult
from precis.taproot.reground import AtomVerifyResult
from precis.taproot.seniority import is_claim_hub
from tests.workers._helpers import seed_chunk, seed_ref

# ── fakes ────────────────────────────────────────────────────────────────


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def _verdict(v: str, c: float) -> Verdict:
    return {"verdict": v, "confidence": c, "rationale": "test"}  # type: ignore[typeddict-item]


_EMPTY_EXTRACTION = ClaimExtraction(atoms=(), composite=None, not_claims=())


def _extract_const(sentence: str | None):
    """Fake ``ExtractFn``: NO-CLAIM (``sentence is None``) or a single atom,
    no composite — mirrors an already-atomic real extraction (step-1
    invariant: a lone atom never carries a composite)."""

    def _fn(span: str) -> ClaimExtraction:
        if sentence is None:
            return _EMPTY_EXTRACTION
        return ClaimExtraction(atoms=(_claim(sentence),), composite=None, not_claims=())

    return _fn


def _extract_multi(
    atoms: list[str],
    composite: str | None,
    not_claims: list[NotClaim] | None = None,
    composite_source: str | None = None,
    explains: tuple[tuple[int, int], ...] = (),
):
    """Fake ``ExtractFn``: a decomposed extraction — ``atoms`` sentences plus
    an optional bundling ``composite`` sentence and ``not_claims``. Mirrors
    the real decomposed shape (:func:`precis.taproot.canon._coerce_extraction`'s
    invariant) so backfill's per-atom cascade fan-out is
    exercised the same way a real ``extract_claim`` result would drive it."""

    def _fn(span: str) -> ClaimExtraction:
        return ClaimExtraction(
            atoms=tuple(_claim(s) for s in atoms),
            composite=_claim(composite) if composite is not None else None,
            not_claims=tuple(not_claims or ()),
            composite_source=composite_source,
            explains=explains,
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


def _verify_all(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
    """Fake ``VerifyBatchFn``: every atom verified by the offered passage —
    the quote IS the passage text, so the verbatim-in-passage check holds."""
    p = passages[0]
    return [
        AtomVerifyResult(i, True, p.chunk_ord, p.text, None)
        for i, _ in enumerate(atoms)
    ]


def _verify_only(supported: set[str]):
    """Fake ``VerifyBatchFn``: only atoms whose sentence is in ``supported``
    verify (against every offered passage) — for the unverified-atom
    refusal and per-atom supporter scoping tests."""

    def _v(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
        p = passages[0]
        return [
            AtomVerifyResult(
                i,
                a.sentence in supported,
                p.chunk_ord if a.sentence in supported else None,
                p.text if a.sentence in supported else None,
                None,
            )
            for i, a in enumerate(atoms)
        ]

    return _v


def _verify_by_passage(mapping: dict[str, set[str]]):
    """Fake ``VerifyBatchFn``: ``mapping[passage_text]`` is the set of atom
    sentences THAT passage supports — the per-supporter scoping case."""

    def _v(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
        p = passages[0]
        ok = mapping.get(p.text, set())
        return [
            AtomVerifyResult(
                i,
                a.sentence in ok,
                p.chunk_ord if a.sentence in ok else None,
                p.text if a.sentence in ok else None,
                None,
            )
            for i, a in enumerate(atoms)
        ]

    return _v


def _qualify_echo(proposed: str, passage: str) -> QualifyResult:
    """Fake ``QualifyFn``: the passage licenses the atom as-is — the claim is
    echoed verbatim (same sentence, so block-map keys still hit) and the quote
    IS the passage text, so the verbatim-in-passage invariant holds."""
    return QualifyResult(
        supported=True, claim=_claim(proposed), quote=passage, reason="test"
    )


def _qualify_rewrite(rewritten: str):
    """Fake ``QualifyFn``: the passage licenses only ``rewritten`` — the
    qualified sentence replaces the draft-span wording (the "statement drawn
    from the source" step)."""

    def _q(proposed: str, passage: str) -> QualifyResult:
        return QualifyResult(
            supported=True,
            claim=_claim(rewritten),
            quote=passage,
            reason="test rewrite",
        )

    return _q


def _qualify_decline(_proposed: str, _passage: str) -> QualifyResult:
    """Fake ``QualifyFn``: the passage asserts the content but will not
    license it as a claim statement — the atom is refused like an unverified one."""
    return QualifyResult(supported=False, claim=None, quote=None, reason="test decline")


def _qualify_unavailable(_proposed: str, _passage: str) -> QualifyResult:
    from precis.taproot.directed import QualifyUnavailable

    raise QualifyUnavailable("dispatch dead")


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


def test_segment_bounds_span_to_trailing_sentence_only() -> None:
    # gr450339: a cite several sentences into a paragraph grounds only the
    # sentence immediately before it — an earlier, unrelated definitional
    # sentence in the same marker-to-marker run must never reach
    # extract_claim (the over-reach that minted fi449493 off the wrong
    # sentence in dr173020).
    text = (
        "Carbon nanobuds consist of a strained seam. "
        "These structures are the subject of this review. "
        "Their properties are conventionally adjusted by doping [pc1]."
    )
    groups = segment_cite_groups(text)
    assert len(groups) == 1
    assert groups[0].handles == ["pc1"]
    assert groups[0].span_text == (
        "Their properties are conventionally adjusted by doping"
    )
    assert "strained seam" not in groups[0].span_text


def test_segment_bounds_pulls_in_prior_sentence_for_short_fragment() -> None:
    # A trailing sentence under ~6 words reads as a dangling fragment (no
    # subject on its own) — the sentence before it is pulled in too, so
    # extract_claim still sees a complete clause.
    text = "Carbon nanobuds consist of a strained seam. This works. Doping helps [pc2]."
    groups = segment_cite_groups(text)
    assert len(groups) == 1
    assert groups[0].span_text == "This works. Doping helps"
    assert "strained seam" not in groups[0].span_text


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
    draft: DraftHandler,
    hub: Hub,
    text: str,
    *,
    draft_id: str = "nt",
    dead_refs: bool = False,
) -> int:
    """Seed a one-paragraph draft ``draft_id`` (default ``nt``) with ``text``;
    return its body chunk_id. Pass a distinct ``draft_id`` for a second,
    independent draft — appending a second paragraph to the SAME draft would
    land it right after the title (before the first, already-rewritten
    paragraph), so ``order[-1]`` would resolve to the wrong chunk.

    ``dead_refs=True`` writes the paragraph through the store instead of the
    ``put`` verb: the handler hard-refuses text that *introduces* a
    handle-shaped reference resolving to nothing, so a test exercising the
    backfill's unresolved-supporter path has to seed that content the way
    legacy rows acquired it."""
    proj = _proj(hub)
    draft.put(id=draft_id, title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=draft_id)
    assert ref is not None
    title_handle = hub.live_store.drafts.reading_order(ref.id)[0].handle
    if dead_refs:
        hub.live_store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text=text,
            at={"after": "¶" + title_handle},
        )
    else:
        draft.put(
            id=draft_id,
            chunk_kind="paragraph",
            text=text,
            at={"after": "¶" + title_handle},
        )
    order = hub.live_store.drafts.reading_order(ref.id)
    return int(order[-1].chunk_id)


def _seed_draft_figure(
    draft: DraftHandler,
    hub: Hub,
    caption: str,
    *,
    draft_id: str = "nt",
) -> int:
    """Seed a one-figure draft ``draft_id`` (default ``nt``) whose caption is
    ``caption``, ``chunk_kind='figure'``; return its body chunk_id. No
    ``image=``/``render=`` — the plain caption-only figure path
    (:meth:`DraftHandler.put`'s general add-a-chunk branch)."""
    proj = _proj(hub)
    draft.put(id=draft_id, title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=draft_id)
    assert ref is not None
    title_handle = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id=draft_id,
        chunk_kind="figure",
        text=caption,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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


def test_apply_later_supporter_failure_reports_partial_not_prose_left(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hub and its primary-supporter edge land, then the second
    supporter's attach raises: the prose IS rewritten to the landed hub, so
    the plan reports ``partial`` — not ``error`` with a note claiming the
    prose was left as [pc…] (taproot-backfill-defects §supporter-error)."""
    import precis.taproot.hub as hub_mod

    _paper1, pc1 = _pc_of(hub.live_store, paper_title="p1")
    paper2, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc1}][{pc2}].")
    real_attach = hub_mod.attach_evidence

    def _attach_fails_on_paper2(*a: Any, **k: Any) -> None:
        if k.get("paper_ref_id") == paper2:
            raise RuntimeError("simulated supporter write failure")
        real_attach(*a, **k)

    monkeypatch.setattr("precis.taproot.hub.attach_evidence", _attach_fails_on_paper2)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "partial"
    assert plan.hub_ref_id is not None
    assert "prose rewritten" in plan.note and "prose left" not in plan.note
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text


def test_apply_extraction_unavailable_is_retryable_not_no_claim(
    draft: DraftHandler, hub: Hub
) -> None:
    """An LLM outage during extraction (the strict extractor's
    ExtractionUnavailable) yields ``extract-unavailable`` with no write and
    the prose untouched — never the final ``no-claim`` verdict."""
    from precis.taproot.canon import ExtractionUnavailable

    _paper, pc = _pc_of(hub.live_store)
    original_text = f"Ribbons are semiconducting [{pc}]."
    dc = _seed_draft_para(draft, hub, original_text)
    before = _finding_count(hub.live_store)

    from datetime import UTC, datetime

    reset = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)

    def _outage(_span: str) -> ClaimExtraction:
        raise ExtractionUnavailable(
            "account quota exhausted: You've hit your session limit",
            reason_class="quota",
            retry_at=reset,
        )

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_outage,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "extract-unavailable"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == before
    # The plan names the router's class and horizon — a quota window is
    # reported as such, not as a dead endpoint, and never as no-claim.
    assert plan.unavailable_class == "quota" and plan.retry_at == reset
    assert "[quota]" in plan.note and reset.isoformat() in plan.note


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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "no-claim"
    assert result.rewritten_text is None  # nothing rewrote
    assert _finding_count(hub.live_store) == before


def test_apply_skips_unresolvable_pc(draft: DraftHandler, hub: Hub) -> None:
    dc = _seed_draft_para(
        draft, hub, "Claim with a dangling cite [pc999999999].", dead_refs=True
    )

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_never_called,  # never reached — supporter resolution fails first
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    p = result.plans[0]
    assert p.action == "reground"
    assert p.group.kind == "pa"
    assert p.reground_targets == [chunk_id]
    assert _finding_count(hub.live_store) == before  # dry-run wrote nothing


def test_apply_ref_level_pa_digs_down_and_grounds_the_edge(
    draft: DraftHandler, hub: Hub
) -> None:
    # AC3 (dig-down ruling, gr720460): fetched [pa] + --ref-level promotes in
    # one step, but only after the dig-down locates a source passage: the
    # evidence edge grounds at that passage (never a whole-paper ungrounded
    # edge), the dug pc handle is named on the edge meta, and [pa]→[fi].
    paper, pa, chunk_id = _fetched_pa_c(hub.live_store)
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
        locate_fn=_locate_first,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert result.n_unbacked == 0
    assert plan.dug_sources == [f"pc{chunk_id}"]
    assert plan.hub_ref_id is not None
    assert is_claim_hub(hub.live_store, plan.hub_ref_id)
    # prose rewritten pa → fi
    assert result.rewritten_text is not None
    assert f"[fi{plan.hub_ref_id}]" in result.rewritten_text
    assert "[pa" not in result.rewritten_text
    # The evidence edge is CHUNK-GROUNDED at the located passage, never
    # ref-level — and its meta names the dug pc handle with the pa arm fingerprint.
    assert not _edge_is_ref_level(
        hub.live_store, paper_ref_id=paper, hub_ref_id=plan.hub_ref_id
    )
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper, plan.hub_ref_id),
        ).fetchone()
    assert row is not None
    meta = row[0] or {}
    assert meta["source_handle"] == f"pc{chunk_id}"
    assert meta["arm"] == "pa"
    assert meta["origin"] == "draft-backfill"


def test_apply_ref_level_pa_unbacked_reports_the_sentence(
    draft: DraftHandler, hub: Hub
) -> None:
    # The dig-down ruling's no-source case (gr720460): a ref-level [pa] whose
    # paper yields NO supporting passage mints NOTHING — the draft sentence is
    # reported unbacked and the prose is left alone. Locate runs BEFORE
    # extraction: an unbacked sentence never reaches the extractor.
    _paper, pa = _fetched_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        ref_level=True,
        extract_fn=_never_called,  # never reached — locate refuses first
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        locate_fn=_locate_none,
        verify_batch_fn=_never_called,
        qualify_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "unbacked"
    assert plan.unbacked == "Ribbons are semiconducting"
    assert plan.hub_ref_id is None
    assert plan.dug_sources == [None]
    assert result.n_unbacked == 1
    assert result.rewritten_text is None  # prose left as-is
    assert _finding_count(hub.live_store) == findings_before


def test_apply_ref_level_pa_partial_locate_still_digs_down(
    draft: DraftHandler, hub: Hub
) -> None:
    # One supporter locates a passage, the other doesn't: the mint proceeds
    # over the located passage (per-atom supporter scoping), the missing
    # supporter is named None in dug_sources — no unbacked verdict when ANY
    # source grounds the sentence.
    _paper1, pa1, chunk_id = _fetched_pa_c(hub.live_store, paper_title="grounds")
    _paper2, pa2, _c2 = _fetched_pa_c(
        hub.live_store, paper_title="silent", text="NOMATCH nothing here"
    )
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa1}][{pa2}].")

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
        locate_fn=_locate_by_sentinel,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert result.n_unbacked == 0
    assert plan.dug_sources == [f"pc{chunk_id}", None]
    assert plan.hub_ref_id is not None
    assert f"[fi{plan.hub_ref_id}]" in (result.rewritten_text or "")


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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
            locate_fn=_locate_first,
            verify_batch_fn=_verify_all,
            qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
    dc = _seed_draft_para(
        draft, hub, f"A jointly-cited claim [{good}][{bad}].", dead_refs=True
    )
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "reground-nomatch"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before


# ── figure caption provenance pointers are never claims (gripe 450329) ────


@pytest.mark.parametrize(
    "caption_tail",
    [
        "Reproduced from",
        "Adapted from",
        "Data from",
        "reproduced from",  # case-insensitive
    ],
)
def test_apply_figure_pointer_caption_is_no_claim_never_extracts(
    draft: DraftHandler, hub: Hub, caption_tail: str
) -> None:
    # A figure caption's trailing image-reuse trail is provenance, not a
    # claim — regardless of exact phrasing, it must be forced no-claim
    # BEFORE extract_claim ever runs (extract_fn=_never_called asserts
    # that). Mirrors dr173020's 6-of-10 inconsistent conversions.
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_figure(
        draft,
        hub,
        f"Panel (a) shows the nanotube ester linkage. {caption_tail} [{pc}].",
    )
    findings_before = _finding_count(hub.live_store)
    links_before = _links_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_never_called,  # never reached — forced no-claim first
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "no-claim"
    assert result.rewritten_text is None  # prose left as [pc…]
    assert _finding_count(hub.live_store) == findings_before  # no hub minted
    assert _links_count(hub.live_store) == links_before  # no evidence edge


def test_apply_figure_pointer_caption_pa_arm_also_no_claim(
    draft: DraftHandler, hub: Hub
) -> None:
    # The same forced-skip applies to a [pa] pointer, and short-circuits
    # BEFORE the [pa] arm's own routing (stub-fetch-first / reground) would
    # otherwise fire — locate_fn is never called either.
    _, pa = _fetched_pa(hub.live_store)
    dc = _seed_draft_figure(
        draft, hub, f"Panel (b) depicts the device. Adapted from [{pa}]."
    )

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
        locate_fn=_never_called,  # never reached — forced no-claim first
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "no-claim"
    assert result.rewritten_text is None


def test_apply_non_figure_chunk_with_reproduced_from_still_extracts(
    draft: DraftHandler, hub: Hub
) -> None:
    # The pointer-idiom skip is scoped to chunk_kind='figure' — ordinary
    # body prose that happens to say "reproduced from" (e.g. describing a
    # reproduction method) is unaffected and still routes through
    # extract_claim normally.
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"The sample was reproduced from [{pc}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("The sample was reproduced from."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "new"
    assert result.rewritten_text is not None


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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = r2.plans[0]
    assert plan.group.kind == "pc"
    assert plan.action == "new"
    assert plan.hub_ref_id is not None
    assert f"[fi{plan.hub_ref_id}]" in (r2.rewritten_text or "")
    # The evidence edge grounds at the passage chunk (src_chunk_id NOT NULL).
    assert not _edge_is_ref_level(
        hub.live_store, paper_ref_id=paper, hub_ref_id=plan.hub_ref_id
    )


# ── decomposition: multi-atom + composite (docs/backlog/taproot-atomic-claims.md) ─


def _conjunct_links_count(store: Store) -> int:
    """``conjunct-of`` links in the graph — the backfill writes none: it
    mints no composite for an atom to be a conjunct of (gr720460)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'conjunct-of'"
        ).fetchone()
    return int(row[0]) if row else 0


def _fi_run(hub_ids: list[int]) -> str:
    """The cite run the backfill writes for a group: one ``[fi<hub>]`` each."""
    return "".join(f"[fi{h}]" for h in hub_ids)


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
    # A bundled [pc] group decomposes to 2 atoms: 2 hubs minted, each with
    # the supporter paper's evidence edge. The bundling composite is never
    # minted (gr720460), so there is no conjunct-of link either.
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "new"  # both atoms mint
    assert len(plan.atom_plans) == 2
    assert len(plan.cite_hub_ids) == 2
    assert plan.hub_ref_id == plan.cite_hub_ids[0]
    assert _finding_count(hub.live_store) == before + 2  # 2 atoms, no composite
    assert _conjunct_links_count(hub.live_store) == 0

    # The paper's evidence edge lands on both atoms.
    assert _edges_from(hub.live_store, paper) == set(plan.cite_hub_ids)

    # Prose cites each atom hub, in atom order, in place of the [pc] run.
    assert result.rewritten_text is not None
    assert _fi_run(plan.cite_hub_ids) in result.rewritten_text
    assert "[pc" not in result.rewritten_text


def _finding_titles(store: Store) -> dict[int, str]:
    """Live finding hubs as ``ref_id -> title`` (the hub's statement)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title FROM refs "
            "WHERE kind = 'finding' AND retired_at IS NULL"
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def test_apply_mints_no_hub_from_the_draft_sentence_without_own_evidence(
    draft: DraftHandler, hub: Hub
) -> None:
    # gr720460: a hub is a claim about the source and carries its own
    # evidence edge to the cited passage. The bundling sentence of a split
    # span is draft prose with no passage attached to it, so the backfill
    # mints no hub for it; the prose cites the atoms the passage verified.
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    before = set(_finding_titles(hub.live_store))

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(
            ["Atom one.", "Atom two."], "Bundle claim.", composite_source="coerced"
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    titles = _finding_titles(hub.live_store)
    minted = set(titles) - before
    # Every minted hub has the paper's evidence edge; none is evidence-less.
    assert minted == _edges_from(hub.live_store, paper)
    assert sorted(titles[i] for i in minted) == ["Atom one.", "Atom two."]
    assert "Bundle claim." not in titles.values()

    # The prose cites exactly the minted atoms, in place of the [pc] run.
    assert result.rewritten_text is not None
    cited = re.findall(r"\[fi(\d+)\]", result.rewritten_text)
    assert {int(i) for i in cited} == minted
    assert len(cited) == 2
    assert "[pc" not in result.rewritten_text


def test_apply_needs_review_atom_contributes_no_conjunct_link(
    draft: DraftHandler, hub: Hub
) -> None:
    # Regression: an atom that resolves to needs_review lands on no hub
    # (apply_placement returned none for it), so the group's cite run would
    # cover only part of the sentence — the prose is left as [pc…]. Its
    # sibling atom still lands with its evidence edge; nothing links
    # conjunct-of (no composite is minted, gr720460).
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "needs_review"
    assert plan.cite_hub_ids == []
    assert result.rewritten_text is None  # prose left as [pc…]
    assert _todos(hub.live_store) == before_todos + 1  # atom 1's needs_review
    assert _conjunct_links_count(hub.live_store) == 0

    # Atom 2 landed on its own hub, with paper B's evidence edge.
    titles = _finding_titles(hub.live_store)
    atom_two = {i for i, t in titles.items() if t == "Atom two."}
    assert len(atom_two) == 1
    assert _edges_from(hub.live_store, paper_b) == atom_two

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
    # hub.
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    atom_hub_ids = set(plan.cite_hub_ids)
    assert len(atom_hub_ids) == 2

    p1_edges = _edges_from(hub.live_store, paper1)
    p2_edges = _edges_from(hub.live_store, paper2)
    assert p1_edges == atom_hub_ids
    assert p2_edges == atom_hub_ids


def test_apply_multi_atom_reconverges_onto_existing_hubs_idempotently(
    draft: DraftHandler, hub: Hub
) -> None:
    # A second draft, citing a different paper for the SAME bundled claim,
    # converges onto the atom hubs the first run minted — no duplicate hub,
    # and the new supporter's evidence lands on the (pre-existing) atom hubs.
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    atom_hub_ids = first.plans[0].cite_hub_ids
    assert len(atom_hub_ids) == 2

    findings_before = _finding_count(hub.live_store)
    links_before = _taproot_links_count(hub.live_store)

    paper2, pc2 = _pc_of(hub.live_store, paper_title="p2")
    dc2 = _seed_draft_para(draft, hub, f"Bundle claim [{pc2}].", draft_id="nt2")

    mapping = {
        "Atom one.": (atom_hub_ids[0], "Atom one."),
        "Atom two.": (atom_hub_ids[1], "Atom two."),
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan2 = second.plans[0]
    assert plan2.action == "attach"
    assert plan2.cite_hub_ids == atom_hub_ids
    assert _finding_count(hub.live_store) == findings_before  # no new hub minted

    # 2 new evidence edges (paper2 -> each atom hub) is the only taproot
    # growth (the draft edit door's own draft-of/cites bookkeeping edges are
    # excluded — scoped to the taproot vocabulary).
    assert _taproot_links_count(hub.live_store) == links_before + 2
    assert set(_edges_from(hub.live_store, paper2)) == set(atom_hub_ids)

    assert second.rewritten_text is not None
    assert _fi_run(atom_hub_ids) in second.rewritten_text


def _findings_with_meta_key(store: Store, key: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding' "
            "AND retired_at IS NULL AND meta ? %s",
            (key,),
        ).fetchone()
    return int(row[0]) if row else 0


def test_apply_not_claims_stay_on_the_plan_with_no_memo_hub(
    draft: DraftHandler, hub: Hub
) -> None:
    # The rejected conjuncts' audit memo lived on the composite hub. The
    # backfill mints none (gr720460), so they are reported on the plan and
    # written to no hub.
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert len(plan.cite_hub_ids) == 2
    assert plan.not_claims == tuple(not_claims)
    assert _findings_with_meta_key(hub.live_store, "taproot_not_claims") == 0


@pytest.mark.parametrize("source", ["model", "coerced"])
def test_apply_mints_no_composite_whichever_way_it_was_written(
    draft: DraftHandler, hub: Hub, source: str
) -> None:
    """A model-written bundling sentence is draft prose just as a coerced
    one is: neither becomes a hub (gr720460)."""
    _, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(
            ["Atom one.", "Atom two."], "Bundle claim.", composite_source=source
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert len(result.plans[0].cite_hub_ids) == 2
    assert "Bundle claim." not in _finding_titles(hub.live_store).values()
    assert _findings_with_meta_key(hub.live_store, "composite_source") == 0


def _edge_rows(store: Store, *, src: int, dst: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    return int(row[0]) if row else 0


def _review_todos(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'todo' "
            "AND title LIKE 'taproot: review backfill%'"
        ).fetchone()
    return int(row[0]) if row else 0


def test_apply_two_atoms_converging_on_one_hub_cite_it_once(
    draft: DraftHandler, hub: Hub
) -> None:
    # Both atoms of a split span attach to the SAME existing hub: the cite
    # run names it once, and the dry run already knows the run (every hub
    # exists at plan time).
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = int(
        seed_claim_hub(
            hub.live_store,
            sentence="Atom one.",
            scope={},
            supporters=[{"paper": paper_a}],
        )["hub_ref_id"]
    )
    paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    both = _block_map(
        {"Atom one.": (existing, "Atom one."), "Atom two.": (existing, "Atom one.")}
    )

    planned = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=both,
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert planned.plans[0].action == "attach"
    assert planned.plans[0].cite_hub_ids == [existing]

    before = _finding_count(hub.live_store)
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=both,
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].cite_hub_ids == [existing]
    assert _finding_count(hub.live_store) == before
    assert result.rewritten_text is not None
    assert f"Bundle claim [fi{existing}]." in result.rewritten_text
    assert result.rewritten_text.count("[fi") == 1
    assert _edge_rows(hub.live_store, src=paper_b, dst=existing) == 1


def test_apply_all_attach_dry_run_names_every_hub(
    draft: DraftHandler, hub: Hub
) -> None:
    # A dry run of a split span whose atoms each attach to a different
    # existing hub reports the whole cite run, not just the first hub.
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    hubs = [
        int(
            seed_claim_hub(
                hub.live_store, sentence=s, scope={}, supporters=[{"paper": paper_a}]
            )["hub_ref_id"]
        )
        for s in ("Atom one.", "Atom two.")
    ]
    _paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")

    planned = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_map(
            {"Atom one.": (hubs[0], "Atom one."), "Atom two.": (hubs[1], "Atom two.")}
        ),
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert planned.plans[0].action == "attach"
    assert planned.plans[0].cite_hub_ids == hubs


def test_apply_rerun_after_partial_landing_reuses_sibling_and_files_no_second_todo(
    draft: DraftHandler, hub: Hub
) -> None:
    # Run 1: atom one is a risky merge (todo filed, no hub), atom two mints,
    # prose stays [pc…]. Run 2 re-plans the same group: atom two converges
    # on the hub run 1 minted (no new hub, no duplicate evidence edge) and
    # atom one's still-open review todo is not filed a second time.
    from precis.taproot.authoring import seed_claim_hub

    paper_a = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = int(
        seed_claim_hub(
            hub.live_store,
            sentence="Atom one.",
            scope={},
            supporters=[{"paper": paper_a}],
        )["hub_ref_id"]
    )
    paper_b, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    todos_before = _review_todos(hub.live_store)

    first = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_map({"Atom one.": (existing, "Atom one.")}),
        judge_fn=lambda a, b: _verdict("same", 0.40),  # low-confidence same
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),  # not confirmed
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert first.plans[0].action == "needs_review"
    assert first.rewritten_text is None
    assert _review_todos(hub.live_store) == todos_before + 1
    (sibling,) = _edges_from(hub.live_store, paper_b)
    assert _finding_titles(hub.live_store)[sibling] == "Atom two."
    findings_before = _finding_count(hub.live_store)

    second = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_map(
            {"Atom one.": (existing, "Atom one."), "Atom two.": (sibling, "Atom two.")}
        ),
        judge_fn=lambda a, b: _verdict("same", 0.40 if a == "Atom one." else 0.99),
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert second.plans[0].action == "needs_review"
    assert second.plans[0].cite_hub_ids == []
    assert second.rewritten_text is None  # prose still [pc…]
    assert _finding_count(hub.live_store) == findings_before  # sibling reused
    assert _edges_from(hub.live_store, paper_b) == {sibling}
    assert _edge_rows(hub.live_store, src=paper_b, dst=sibling) == 1
    assert _review_todos(hub.live_store) == todos_before + 1  # not re-filed


def test_cli_backfill_report_shows_a_lone_atoms_not_claims(
    draft: DraftHandler, hub: Hub, capsys: pytest.CaptureFixture[str]
) -> None:
    # One surviving atom plus a rejected conjunct: with no composite hub to
    # hold the memo, the CLI report is where the rejection is visible.
    from precis.cli.taproot import _print_backfill

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
            ["Atom one."], "Bundle claim.", not_claims=not_claims
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert result.n_not_claims == 1

    _print_backfill([result], "json", applied=True)
    group = json.loads(capsys.readouterr().out)[0]["groups"][0]
    assert group["not_claims"] == not_claims
    assert group["cite_hub_ids"] == [group["hub_ref_id"]]

    _print_backfill([result], "text", applied=True)
    assert (
        "not a claim: 'enables next-gen tech' (forward-looking)"
        in capsys.readouterr().out
    )


def test_cli_backfill_report_shows_an_unbacked_sentence(
    draft: DraftHandler, hub: Hub, capsys: pytest.CaptureFixture[str]
) -> None:
    # The no-source case of the dig-down ruling: the CLI report is where an
    # unbacked sentence surfaces (JSON fields + text line), and the chunk
    # rollup counts it.
    from precis.cli.taproot import _print_backfill

    _paper, pa = _fetched_pa(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pa}].")

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
        locate_fn=_locate_none,
        verify_batch_fn=_never_called,
        qualify_fn=_never_called,
    )
    assert result.n_unbacked == 1

    _print_backfill([result], "json", applied=True)
    chunk = json.loads(capsys.readouterr().out)[0]
    assert chunk["n_unbacked"] == 1
    group = chunk["groups"][0]
    assert group["action"] == "unbacked"
    assert group["unbacked"] == "Ribbons are semiconducting"
    assert group["hub_ref_id"] is None

    _print_backfill([result], "text", applied=True)
    out = capsys.readouterr().out
    assert "1 unbacked sentence(s)" in out
    assert "unbacked: 'Ribbons are semiconducting' —" in out


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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "ungroundable"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before


def test_apply_pc_group_with_no_live_passage_is_unbacked(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pc arm's defensive race (gr720460 dig-down gate): every cited chunk
    # retired between the ungroundable gate and the passage read — simulated
    # by the passage read returning None for the live handle. The ruling is
    # the same as the [pa] arm's: mint nothing, report the sentence unbacked.
    # (Before, this path fell through and minted unverified draft-worded
    # atoms — the exact mint the ruling forbids.)
    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    findings_before = _finding_count(hub.live_store)
    links_before = _links_count(hub.live_store)

    monkeypatch.setattr(
        "precis.taproot.backfill._supporter_passages", lambda *_a, **_k: [None]
    )
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_never_called,
        qualify_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "unbacked"
    assert plan.unbacked == "Ribbons are semiconducting"
    assert plan.hub_ref_id is None
    assert result.n_unbacked == 1
    assert result.rewritten_text is None  # prose left as-is
    assert _finding_count(hub.live_store) == findings_before
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "reground-nomatch"
    assert result.rewritten_text is None
    assert _links_count(hub.live_store) == links_before


def test_default_locate_proposes_table_chunk_holding_claim_numbers(monkeypatch):
    """gripe 453835: a claim whose evidence is a benchmark-table row must reach
    the confirm step with that table as the proposed chunk, not lose to prose
    that merely shares the claim's vocabulary."""
    import precis.workers._chase_llm as chase_llm
    from precis.taproot.backfill import _default_locate

    seen: dict = {}

    def _fake(*, claim, proposed, alternates):
        seen["proposed"] = proposed
        return proposed

    monkeypatch.setattr(chase_llm, "_locate_chunk_in_target", _fake)
    prose = (
        1,
        0,
        "Density functionals on the WATER27 benchmark spread widely in kcal/mol "
        "across the same functional families.",
    )
    table = (
        2,
        1,
        "| set | GGA | meta-GGA |\n|---|---|---|\n| WATER27 | 0.77 | 7.27 |",
    )
    got = _default_locate(
        "on WATER27 the same functionals spread from 0.77 to 7.27 kcal/mol",
        [prose, table],
    )
    assert got == table
    assert seen["proposed"] == table


def test_plan_pa_tagged_unusable_body_is_fetch_first_like_a_stub(
    draft: DraftHandler, hub: Hub
) -> None:
    """A paper tagged body:partial holds chunks but no article body: its
    whole-paper cite is classified exactly like an un-fetched stub."""
    from precis.store.types import Tag

    paper, pa = _fetched_pa(hub.live_store)
    hub.live_store.add_tag(paper, Tag.open("body:partial"), set_by="system")
    dc = _seed_draft_para(draft, hub, f"A landmark result [{pa}].")
    result = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        ref_level=True,
        extract_fn=_never_called,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert [p.action for p in result.plans] == ["stub-fetch-first"]


def test_apply_chunk_mint_cap_refuses_over_mining_one_chunk(
    draft: DraftHandler, hub: Hub
) -> None:
    """gr204874: a second paragraph citing the same pc may not mint past the
    cap; a different pc in the same run is unaffected (happy path)."""
    from precis.taproot.canon import MintCap

    cap = MintCap(limit=1)
    _paper, pc = _pc_of(hub.live_store)
    _paper2, pc2 = _pc_of(hub.live_store, paper_title="other paper")

    def _run(text: str, claim: str, draft_id: str):
        dc = _seed_draft_para(draft, hub, text, draft_id=draft_id)
        return apply_chunk(
            hub.live_store,
            embedder=None,
            draft_handler=draft,
            chunk_id=dc,
            extract_fn=_extract_const(claim),
            block_fn=_block_none,
            judge_fn=_never_called,
            merge_confirm_fn=_never_called,
            mint_cap=cap,
            verify_batch_fn=_verify_all,
            qualify_fn=_qualify_echo,
        )

    first = _run(
        f"Ribbons are semiconducting [{pc}].", "Ribbons are semiconducting.", "d1"
    )
    assert first.plans[0].action == "new"

    second = _run(f"Ribbons have a gap [{pc}].", "Ribbons have a finite gap.", "d2")
    assert second.plans[0].action == "mint-capped"
    assert second.plans[0].hub_ref_id is None
    assert second.rewritten_text is None

    other = _run(f"Tubes are metallic [{pc2}].", "Tubes are metallic.", "d3")
    assert other.plans[0].action == "new"


def test_apply_decomposition_mints_the_explains_edge_between_atom_hubs(
    draft: DraftHandler, hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    """The causal split reaches the graph on the draft-backfill path too:
    the extraction's ``explains`` pair rides GroupPlan into apply_extraction,
    which writes mechanism -> effect between the two ATOM hubs;
    the backfill mints no composite for them to be conjuncts of."""
    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Because claim [{pc}].")

    caplog.set_level("WARNING", logger="precis.taproot.hub")
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(
            ["Effect atom.", "Mechanism atom."],
            "Effect because mechanism.",
            explains=((1, 0), (7, 0)),  # the second pair is unwritable: skipped
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.explains == ((1, 0), (7, 0))
    # the dropped pair is counted on the plan/chunk result and logged with
    # the chunk origin
    assert plan.explains_skipped == 1
    assert result.n_explains_skipped == 1
    assert any(
        f"dc{dc}" in r.getMessage() and "(7, 0)" in r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    )
    assert len(plan.cite_hub_ids) == 2
    with hub.live_store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT s.title, d.title FROM links l "
            "JOIN refs s ON s.ref_id = l.src_ref_id "
            "JOIN refs d ON d.ref_id = l.dst_ref_id WHERE l.relation = 'explains'"
        ).fetchall()
    assert rows == [("Mechanism atom.", "Effect atom.")]


# ── split-atom verification + campaign dedup (Q12 (a)) ──────────────────


def test_split_atom_unverified_by_any_passage_refuses_the_group(
    draft: DraftHandler, hub: Hub
) -> None:
    # A conjunct the cited passage never asserts is an extraction artefact —
    # the WHOLE group is refused (no mint, prose left), never a partial mint
    # that silently drops a conjunct of the sentence the cite run grounds.
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Made-up atom."], "Bundle claim."),
        # The refusal happens before the cascade: block/judge never run.
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_only({"Atom one."}),
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "unverified"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == findings_before


def test_lone_atom_is_verified_and_qualified_before_minting(
    draft: DraftHandler, hub: Hub
) -> None:
    # The dig-down ruling (gr720460) knows no "lone atom" exemption: a
    # single-atom extraction is still verified against the cited passage and
    # qualified against it — the hub's statement is what the source licenses,
    # never the draft span's wording on trust.
    _paper, pc = _pc_of(hub.live_store)
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert plan.atom_supporters is not None  # the lone atom WAS verified
    assert plan.atom_quotes and all(q for q in plan.atom_quotes)


def test_lone_atom_unverified_by_its_passage_refuses_the_group(
    draft: DraftHandler, hub: Hub
) -> None:
    # The cited passage does not assert the lone atom's content: no mint, no
    # rewrite — the same refusal a split-span conjunct gets.
    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        # The refusal happens before the cascade: block/judge never run.
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_only(set()),
        qualify_fn=_never_called,
    )

    plan = result.plans[0]
    assert plan.action == "unverified"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == findings_before


def test_lone_atom_qualify_decline_refuses_the_group(
    draft: DraftHandler, hub: Hub
) -> None:
    # The passage asserts the content but will not license it as a claim
    # statement: refused exactly like an unverified atom (gr720460 dig-down).
    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_decline,
    )

    plan = result.plans[0]
    assert plan.action == "unverified"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == findings_before


def test_lone_atom_qualify_unavailable_is_retryable_not_a_verdict(
    draft: DraftHandler, hub: Hub
) -> None:
    # A qualification dispatch failure (infra) is the retryable
    # verify-unavailable plan — never silently minted, never a refusal.
    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_unavailable,
    )

    plan = result.plans[0]
    assert plan.action == "verify-unavailable"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == findings_before


def test_hub_statement_is_the_qualified_sentence_not_the_draft_span(
    draft: DraftHandler, hub: Hub
) -> None:
    # The ruling's core: the minted hub's statement is what the SOURCE
    # licenses (the qualified sentence), never the draft span's wording.
    _paper, pc = _pc_of(hub.live_store)
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
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_rewrite("The measured ribbons remain semiconducting."),
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert plan.hub_ref_id is not None
    assert _finding_titles(hub.live_store)[plan.hub_ref_id] == (
        "The measured ribbons remain semiconducting."
    )


def test_split_atom_verification_scopes_evidence_to_its_own_passage(
    draft: DraftHandler, hub: Hub
) -> None:
    # Atom two is asserted only by paper2's passage: its primary edge comes
    # from paper2, and paper1 corroborates only the atom it verifies —
    # never an edge from a passage that doesn't assert the claim.
    paper1 = seed_ref(hub.live_store, title="p1", kind="paper")
    t1 = "Atom one is asserted by this first passage only."
    c1 = seed_chunk(hub.live_store, ref_id=paper1, ord=0, text=t1)
    paper2 = seed_ref(hub.live_store, title="p2", kind="paper")
    t2 = "Atom two finds support in this second passage alone."
    c2 = seed_chunk(hub.live_store, ref_id=paper2, ord=0, text=t2)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [pc{c1}][pc{c2}].")

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_by_passage({t1: {"Atom one."}, t2: {"Atom two."}}),
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    atom_hub_ids = plan.cite_hub_ids
    assert len(atom_hub_ids) == 2

    # paper1 edges only the atom its passage verified; paper2 the other.
    p1_edges = _edges_from(hub.live_store, paper1)
    p2_edges = _edges_from(hub.live_store, paper2)
    assert len(p1_edges) == 1
    assert len(p2_edges) == 1
    assert p1_edges != p2_edges
    assert p1_edges | p2_edges == set(atom_hub_ids)


def test_verify_dispatch_failure_is_retryable_not_a_verdict(
    draft: DraftHandler, hub: Hub
) -> None:
    # Infra failure (RegroundingUnavailable) is never silently "nothing
    # verified" — the group is a retryable verify-unavailable, prose left.
    from precis.taproot.reground import RegroundingUnavailable

    def _boom(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
        raise RegroundingUnavailable("dispatch dead")

    _paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Bundle claim [{pc}].")
    findings_before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(["Atom one.", "Atom two."], "Bundle claim."),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_boom,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "verify-unavailable"
    assert result.rewritten_text is None
    assert _finding_count(hub.live_store) == findings_before


def test_campaign_search_converges_where_ann_missed(
    draft: DraftHandler, hub: Hub
) -> None:
    # campaign= dedup: the ANN returns no candidates, but a campaign-tagged
    # hub already carries the claim — lexical recall lands it as a merge
    # candidate and the group attaches instead of minting a duplicate.
    from precis.store.types import Tag
    from precis.taproot.authoring import seed_claim_hub

    paper = seed_ref(hub.live_store, title="paper A", kind="paper")
    existing = seed_claim_hub(
        hub.live_store,
        sentence="Ribbons are semiconducting.",
        scope={},
        supporters=[{"paper": paper}],
    )
    hub.live_store.add_tag(
        int(existing["hub_ref_id"]),
        Tag.open("campaign:test-camp"),
        set_by="system",
    )
    _paper2, pc = _pc_of(hub.live_store, paper_title="paper B")
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")

    planned = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,  # the ANN misses it
        judge_fn=lambda a, b: _verdict("same", 0.99),
        merge_confirm_fn=_never_called,
        campaign="test-camp",
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert planned.plans[0].action == "attach"
    assert planned.plans[0].hub_ref_id == int(existing["hub_ref_id"])

    # And without campaign= the same group would mint a duplicate.
    plain = plan_chunk(
        hub.live_store,
        embedder=None,
        chunk_id=dc,
        extract_fn=_extract_const("Ribbons are semiconducting."),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )
    assert plain.plans[0].action == "new"


# ── fragment-claim refusal (_bad_claim_shape) ───────────────────────────


def test_apply_refuses_extraction_fragments(draft: DraftHandler, hub: Hub) -> None:
    """Every extracted 'claim' that is a verbatim copy of its own chunk
    sentence, opens lowercase, or opens with 'and' is a sliver of the
    source text — the group plans 'bad-claim' and mints nothing."""
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"Ribbons are semiconducting [{pc}].")
    before = _finding_count(hub.live_store)

    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        # A verbatim copy of the group's own span text — the classic
        # harvest fragment.
        extract_fn=_extract_const("Ribbons are semiconducting"),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_never_called,
        qualify_fn=_qualify_echo,
    )

    assert result.plans[0].action == "bad-claim"
    assert "shape check" in (result.plans[0].note or "")
    assert _finding_count(hub.live_store) == before


def test_apply_bad_atoms_dropped_good_atoms_still_mint(
    draft: DraftHandler, hub: Hub
) -> None:
    """A mixed extraction drops only the fragment atoms — the surviving
    atom still verifies and mints, and an ``explains`` pair touching a
    refused atom is dropped rather than re-pointed."""
    paper, pc = _pc_of(hub.live_store)
    dc = _seed_draft_para(draft, hub, f"The bundle holds with two conjuncts [{pc}].")
    result = apply_chunk(
        hub.live_store,
        embedder=None,
        draft_handler=draft,
        chunk_id=dc,
        extract_fn=_extract_multi(
            ["and a dangling sliver", "Atom two survives."],
            "The bundle holds.",
            explains=((0, 1),),
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        verify_batch_fn=_verify_all,
        qualify_fn=_qualify_echo,
    )

    plan = result.plans[0]
    assert plan.action == "new"
    assert plan.hub_ref_id is not None
    # No hub carries the refused sliver's sentence.
    with hub.live_store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT title FROM refs WHERE kind='finding' AND retired_at IS NULL"
        ).fetchall()
    titles = {str(r[0]) for r in rows}
    assert "and a dangling sliver" not in titles
    assert "Atom two survives." in titles
