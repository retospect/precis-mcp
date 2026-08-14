"""Taproot atomic-claims migration — Phase 2 apply
(:mod:`precis.taproot.apply_migrate`).

DB-backed (real ``refs``/``chunks``/``links`` via the ``store`` fixture,
mirroring ``tests/test_taproot_hub.py``/``tests/test_taproot_migrate.py``'s
split), but fully offline: the placement cascade (``block``/``judge``/
``merge_confirm``) and the evidence-repoint verify function are always
injected fakes — no LLM call, no embedder, no network anywhere in this
file (mirrors ``tests/test_taproot_backfill.py``'s fake-cascade pattern).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from precis.taproot.apply_migrate import apply_dry_run
from precis.taproot.canon import Candidate, CanonicalClaim, Verdict
from precis.taproot.directed import QualifyResult
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from tests.workers._helpers import seed_chunk, seed_ref

# ── fakes ────────────────────────────────────────────────────────────────


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def _verdict(v: str, c: float) -> Verdict:
    return {"verdict": v, "confidence": c, "rationale": "test"}  # type: ignore[typeddict-item]


def _block_none(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
    return []


def _block_map(mapping: dict[str, tuple[int, str]]):
    """Fake ``BlockFn``: ``claim.sentence`` -> ``(hub_ref_id,
    matched_claim_text)``, or no candidates when the sentence isn't in
    ``mapping`` — mirrors ``test_taproot_backfill.py``'s helper of the
    same name."""

    def _b(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
        hit = mapping.get(claim.sentence)
        return (
            [Candidate(hub_ref_id=hit[0], claim=hit[1], distance=0.02)] if hit else []
        )

    return _b


def _never_called(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("cascade fn should not have been called")


def _judge_same_high(a: str, b: str) -> Verdict:
    return _verdict("same", 0.99)


def _verify_map(supported_sentences: set[str]):
    """Fake ``VerifyFn``: supports iff ``proposed`` is one of
    ``supported_sentences`` — ignores the passage entirely, so tests don't
    need a real grounding chunk to exercise the add/prune decision."""

    def _fn(proposed: str, passage: str) -> QualifyResult:
        return QualifyResult(
            supported=proposed in supported_sentences,
            claim=_claim(proposed) if proposed in supported_sentences else None,
            quote=None,
            reason="test",
        )

    return _fn


def _verify_passage_contains(needle: str):
    """Fake ``VerifyFn`` that supports iff ``needle`` is literally in the
    passage handed to it — used to prove real grounding-chunk text
    actually reaches ``extract_verify_fn``, not just a stub sentence
    lookup."""

    def _fn(proposed: str, passage: str) -> QualifyResult:
        ok = needle in passage
        return QualifyResult(
            supported=ok, claim=_claim(proposed) if ok else None, quote=None, reason="t"
        )

    return _fn


_FIXED_NOW = datetime(2026, 8, 14, 3, 0, 0, tzinfo=UTC)


def _now_fn() -> datetime:
    return _FIXED_NOW


class _TodoCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, Any]]] = []

    def __call__(self, hub_ref_id: int, reason: str, detail: dict[str, Any]) -> None:
        self.calls.append((hub_ref_id, reason, detail))


def _row(
    hub_ref_id: int,
    verdict: str,
    *,
    atoms: list[str] | None = None,
) -> dict[str, Any]:
    """One dry-run outcome row — the shape
    :func:`precis.taproot.migrate.dump_outcomes_jsonl` serializes."""
    extraction: dict[str, Any] | None = None
    if atoms is not None:
        extraction = {
            "atoms": [{"sentence": s, "scope": {}} for s in atoms],
            "compound": None,
            "not_claims": [],
        }
    return {
        "hub": hub_ref_id,
        "score": 0,
        "cohort": "likely-compound",
        "control": False,
        "sentence": "irrelevant original sentence",
        "verdict": verdict,
        "gate_meta": {},
        "extraction": extraction,
        "error": None,
        "junk_candidate": False,
        "escalated_verdict": None,
        "escalated_gate_meta": {},
        "escalated_extraction": None,
        "escalation_error": None,
    }


def _meta(store: Any, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0] or {})


def _edge_exists(store: Any, src: int, dst: int, relation: str | None = None) -> bool:
    with store.pool.connection() as conn:
        if relation is not None:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = %s",
                (src, dst, relation),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (src, dst),
            ).fetchone()
    return row is not None


# ── pass-through / already-stamped / not-found ─────────────────────────────


def test_pass_through_stamps_only(store: Any) -> None:
    hub = mint_hub(store, _claim("Pd/C catalyzes Suzuki coupling at RT."))

    report = apply_dry_run(
        store,
        [_row(hub, "pass-through")],
        now_fn=_now_fn,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.stamped_passthrough == 1
    assert report.split_applied == 0
    assert report.partial_failures == 0
    assert _meta(store, hub)["taproot_decomposed_at"] == _FIXED_NOW.isoformat()
    assert report.hubs[0].action == "stamped_passthrough"


def test_already_stamped_hub_is_skipped(store: Any) -> None:
    hub = mint_hub(store, _claim("Pd/C catalyzes Suzuki coupling at RT."))
    store.update_ref(hub, meta_patch={"taproot_decomposed_at": "2020-01-01T00:00:00"})

    report = apply_dry_run(
        store,
        [_row(hub, "pass-through")],
        now_fn=_now_fn,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.skipped_already_stamped == 1
    assert report.stamped_passthrough == 0
    # Untouched — still the original stamp, not overwritten.
    assert _meta(store, hub)["taproot_decomposed_at"] == "2020-01-01T00:00:00"


def test_hub_not_found_is_a_partial_failure(store: Any) -> None:
    report = apply_dry_run(
        store,
        [_row(999_999_999, "pass-through")],
        now_fn=_now_fn,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.partial_failures == 1
    assert report.hubs[0].action == "error"


# ── no-claim ────────────────────────────────────────────────────────────


def test_no_claim_with_evidence_files_needs_review_and_is_not_stamped(
    store: Any,
) -> None:
    hub = mint_hub(store, _claim("Graphene has record strength."))
    paper = seed_ref(store, kind="paper")
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        check_retraction=False,
    )
    todo = _TodoCollector()

    report = apply_dry_run(
        store,
        [_row(hub, "no-claim")],
        now_fn=_now_fn,
        todo_fn=todo,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.no_claim_needs_review == 1
    assert report.no_claim_unevidenced == 0
    assert "taproot_decomposed_at" not in _meta(store, hub)
    assert len(todo.calls) == 1
    assert todo.calls[0][0] == hub


def test_no_claim_without_evidence_is_counted_and_untouched(store: Any) -> None:
    hub = mint_hub(store, _claim("This is not really a claim."))
    todo = _TodoCollector()

    report = apply_dry_run(
        store,
        [_row(hub, "no-claim")],
        now_fn=_now_fn,
        todo_fn=todo,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.no_claim_unevidenced == 1
    assert report.no_claim_needs_review == 0
    assert todo.calls == []
    assert "taproot_decomposed_at" not in _meta(store, hub)


# ── lossy / nested / error verdicts ─────────────────────────────────────


def test_lossy_nested_error_verdicts_are_skipped_and_never_stamped(store: Any) -> None:
    for verdict in ("lossy", "nested", "error"):
        hub = mint_hub(store, _claim(f"claim for verdict {verdict}"))
        report = apply_dry_run(
            store,
            [_row(hub, verdict)],
            now_fn=_now_fn,
            block_fn=_never_called,
            judge_fn=_never_called,
            merge_confirm_fn=_never_called,
            extract_verify_fn=_never_called,
        )
        assert report.skipped_verdict == 1
        assert "taproot_decomposed_at" not in _meta(store, hub)


# ── split — no existing evidence ────────────────────────────────────────


def test_split_with_no_evidence_mints_atoms_links_and_stamps(store: Any) -> None:
    compound = mint_hub(
        store,
        _claim(
            "Carbon nanomaterials have exceptional mechanical characteristics "
            "and enable next-generation electronics."
        ),
    )
    atom_a = "Carbon nanomaterials have exceptional mechanical characteristics."
    atom_b = "Carbon nanomaterials enable next-generation electronics."

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, atom_b])],
        now_fn=_now_fn,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,  # no evidence edges — never reached
    )

    assert report.split_applied == 1
    assert report.atoms_placed == 2
    assert report.atoms_needs_review == 0
    assert report.edges_repointed == 0
    assert _meta(store, compound)["taproot_decomposed_at"] == _FIXED_NOW.isoformat()

    # Two fresh atom hubs, each conjunct-of the (now-compound) original.
    with store.pool.connection() as conn:
        atom_hub_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
                "AND relation = 'conjunct-of'",
                (compound,),
            ).fetchall()
        ]
    assert len(atom_hub_ids) == 2
    for atom_hub in atom_hub_ids:
        with store.pool.connection() as conn:
            title = conn.execute(
                "SELECT title FROM refs WHERE ref_id = %s", (atom_hub,)
            ).fetchone()[0]
        assert title in (atom_a, atom_b)


# ── split — evidence re-point ────────────────────────────────────────────


def test_split_repoints_evidence_only_to_the_verified_atom(store: Any) -> None:
    """The core add-first behaviour: one paper edge on the original hub,
    two atoms, only one verifies — the edge moves to that atom alone
    (never a blanket copy to both)."""
    compound = mint_hub(
        store,
        _claim("X shows high strength and X shows high conductivity."),
    )
    paper = seed_ref(store, kind="paper")
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        check_retraction=False,
    )
    atom_a = "X shows high strength."
    atom_b = "X shows high conductivity."

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, atom_b])],
        now_fn=_now_fn,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_map({atom_a}),  # only atom_a verifies
    )

    assert report.split_applied == 1
    assert report.edges_repointed == 1
    assert report.edges_kept_needs_review == 0

    with store.pool.connection() as conn:
        atom_hubs = {
            str(
                conn.execute(
                    "SELECT title FROM refs WHERE ref_id = %s", (r[0],)
                ).fetchone()[0]
            ): int(r[0])
            for r in conn.execute(
                "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
                "AND relation = 'conjunct-of'",
                (compound,),
            ).fetchall()
        }
    assert set(atom_hubs) == {atom_a, atom_b}

    # Original hub's edge is gone; atom_a's hub carries it; atom_b's does not.
    assert not _edge_exists(store, paper, compound, "corroborates")
    assert _edge_exists(store, paper, atom_hubs[atom_a], "corroborates")
    assert not _edge_exists(store, paper, atom_hubs[atom_b], "corroborates")


def test_split_keeps_edge_and_files_needs_review_when_no_atom_verifies(
    store: Any,
) -> None:
    compound = mint_hub(store, _claim("X shows A and X shows B."))
    paper = seed_ref(store, kind="paper")
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        check_retraction=False,
    )
    todo = _TodoCollector()

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=["X shows A.", "X shows B."])],
        now_fn=_now_fn,
        todo_fn=todo,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_map(set()),  # nothing verifies
    )

    assert report.edges_repointed == 0
    assert report.edges_kept_needs_review == 1
    # The original edge survives -- never pruned with zero replacement.
    assert _edge_exists(store, paper, compound, "corroborates")
    assert any("no atom verified" in c[1] for c in todo.calls)
    # Still stamped -- the split itself succeeded, only the repoint needed
    # a human (an un-repointed edge is a filed review, not an abort).
    assert "taproot_decomposed_at" in _meta(store, compound)


def test_split_real_grounding_passage_reaches_verify_fn(store: Any) -> None:
    """Proves the grounding-passage resolution path (real chunk text, not
    a stubbed lookup) actually feeds extract_verify_fn."""
    compound = mint_hub(store, _claim("X shows A and X shows B."))
    paper = seed_ref(store, kind="paper")
    chunk_id = seed_chunk(
        store, ref_id=paper, text="X shows a UNIQUE-MARKER property.", ord=0
    )
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk_id}"},
        check_retraction=False,
    )

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=["X shows A.", "X shows B."])],
        now_fn=_now_fn,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_passage_contains("UNIQUE-MARKER"),
    )

    assert report.edges_repointed == 1
    assert report.edges_kept_needs_review == 0


def test_split_confirms_add_even_when_it_degrades_to_ref_level(store: Any) -> None:
    """A real, successful add must not read as failed just because
    ``attach_evidence`` re-derives grounding at write time and lands
    ref-level (``src_chunk_id`` NULL) rather than matching the original
    edge's chunk exactly -- the over-strict-confirm regression."""
    compound = mint_hub(store, _claim("X shows A and X shows B."))
    paper = seed_ref(store, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text="grounding passage", ord=0)
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk_id}"},
        check_retraction=False,
    )
    # Retire the grounding chunk *after* the original edge was written: a
    # re-attach at repoint time can no longer resolve it and legitimately
    # degrades to a ref-level edge, even though the add itself succeeds.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (chunk_id,)
        )
        conn.commit()
    atom_a = "X shows A."

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, "X shows B."])],
        now_fn=_now_fn,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_map({atom_a}),
    )

    assert report.edges_repointed == 1
    assert report.edges_kept_needs_review == 0
    assert report.partial_failures == 0
    assert not _edge_exists(store, paper, compound, "corroborates")


# ── split — atom placement onto a compound downgrades to needs_review ────


def test_split_atom_attach_onto_compound_hub_downgrades_to_needs_review(
    store: Any,
) -> None:
    other_compound = mint_hub(store, _claim("other compound sentence"))
    other_atom = mint_hub(store, _claim("other atom sentence"))
    link_claims(
        store,
        from_hub_ref_id=other_atom,
        to_hub_ref_id=other_compound,
        relation="conjunct-of",
    )

    compound = mint_hub(store, _claim("X shows A and X shows B."))
    atom_a = "X shows A."
    atom_b = "X shows B."
    todo = _TodoCollector()

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, atom_b])],
        now_fn=_now_fn,
        todo_fn=todo,
        # atom_a "converges" onto the existing compound hub; atom_b mints fresh.
        block_fn=_block_map({atom_a: (other_compound, "other compound sentence")}),
        judge_fn=_judge_same_high,
        merge_confirm_fn=_never_called,  # high confidence -> no escalation
        extract_verify_fn=_never_called,  # no evidence edges on this hub
    )

    assert report.atoms_placed == 1
    assert report.atoms_needs_review == 1
    assert any("compound" in c[1] for c in todo.calls)
    # atom_a never linked onto the compound target it collided with.
    assert not _edge_exists(store, other_compound, compound)
    with store.pool.connection() as conn:
        linked_titles = [
            str(
                conn.execute(
                    "SELECT title FROM refs WHERE ref_id = %s", (r[0],)
                ).fetchone()[0]
            )
            for r in conn.execute(
                "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
                "AND relation = 'conjunct-of'",
                (compound,),
            ).fetchall()
        ]
    assert linked_titles == [atom_b]


# ── split — malformed extraction ─────────────────────────────────────────


def test_split_with_fewer_than_two_atoms_is_a_partial_failure(store: Any) -> None:
    hub = mint_hub(store, _claim("X shows A and X shows B."))

    report = apply_dry_run(
        store,
        [_row(hub, "split", atoms=["only one atom"])],
        now_fn=_now_fn,
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_never_called,
    )

    assert report.partial_failures == 1
    assert report.split_applied == 0
    assert "taproot_decomposed_at" not in _meta(store, hub)
    assert report.hubs[0].action == "error"


# ── split — whole-hub abort / rollback ───────────────────────────────────


def _conjunct_of_count(store: Any, hub_ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE dst_ref_id = %s "
            "AND relation = 'conjunct-of'",
            (hub_ref_id,),
        ).fetchone()
    return int(row[0])


def test_split_aborts_and_rolls_back_when_a_write_fails_mid_hub(store: Any) -> None:
    """A real (unfaked) write failure partway through a split — one atom's
    ``mint_hub`` converges onto the *original* hub's own ref_id (identical
    sentence+scope -> identical content-derived pub_id), so linking it
    ``conjunct-of`` the original is a self-loop and ``link_claims`` raises.
    The whole hub's transaction must roll back: no stamp, no atom hub for
    the OTHER (never-reached) atom, no conjunct-of links, the pre-existing
    evidence edge untouched, and — pinning fix #1 — no todo filed."""
    original_sentence = "X shows A and X shows B."
    compound = mint_hub(store, _claim(original_sentence))
    paper = seed_ref(store, kind="paper")
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        check_retraction=False,
    )
    todo = _TodoCollector()
    atom_a = original_sentence  # identical to the compound's own mint sentence
    atom_b = "X shows B."

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, atom_b])],
        now_fn=_now_fn,
        todo_fn=todo,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_map({atom_a, atom_b}),
    )

    assert report.split_applied == 0
    assert report.partial_failures == 1
    assert report.hubs[0].action == "error"
    assert "taproot_decomposed_at" not in _meta(store, compound)
    # atom_b was never reached (atom_a raised first) -- no ref for it exists.
    with store.pool.connection() as conn:
        row = conn.execute("SELECT 1 FROM refs WHERE title = %s", (atom_b,)).fetchone()
    assert row is None
    assert _conjunct_of_count(store, compound) == 0
    assert _edge_exists(store, paper, compound, "corroborates")
    assert todo.calls == []


def test_split_aborts_via_add_first_backstop_and_discards_pending_reviews(
    store: Any, monkeypatch: Any
) -> None:
    """Force the add-first invariant's post-write backstop
    (``_live_evidence_count`` monkeypatched to always read 0) inside an
    otherwise-legitimate split: one edge verifies+adds+would-prune, a
    SECOND edge verifies nothing (queuing a needs_review BEFORE the
    backstop trips). The whole hub must still roll back completely, and —
    pinning fix #1 — the review that was already queued must NOT surface
    as a todo once the transaction it was queued inside gets discarded."""
    import precis.taproot.apply_migrate as apply_migrate_mod

    compound = mint_hub(store, _claim("X shows A and X shows B."))
    paper = seed_ref(store, kind="paper")
    other_paper = seed_ref(store, kind="paper")
    chunk1 = seed_chunk(store, ref_id=paper, text="passage with MARKER1", ord=0)
    chunk2 = seed_chunk(store, ref_id=other_paper, text="passage with MARKER2", ord=0)
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk1}"},
        check_retraction=False,
    )
    attach_evidence(
        store,
        hub_ref_id=compound,
        paper_ref_id=other_paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk2}"},
        check_retraction=False,
    )
    todo = _TodoCollector()
    atom_a = "X shows A."
    atom_b = "X shows B."

    def _verify_fn(proposed: str, passage: str) -> QualifyResult:
        # Only MARKER1's edge verifies (against atom_a); MARKER2's edge
        # verifies nothing -> queues a needs_review before the backstop
        # forces the whole hub to roll back.
        ok = "MARKER1" in passage and proposed == atom_a
        return QualifyResult(supported=ok, claim=None, quote=None, reason="t")

    monkeypatch.setattr(
        apply_migrate_mod, "_live_evidence_count", lambda store, conn, ids: 0
    )

    report = apply_dry_run(
        store,
        [_row(compound, "split", atoms=[atom_a, atom_b])],
        now_fn=_now_fn,
        todo_fn=todo,
        block_fn=_block_none,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
        extract_verify_fn=_verify_fn,
    )

    assert report.split_applied == 0
    assert report.partial_failures == 1
    assert "taproot_decomposed_at" not in _meta(store, compound)
    # Both original edges intact -- the prune that would have happened for
    # the MARKER1 edge was rolled back along with everything else.
    assert _edge_exists(store, paper, compound, "corroborates")
    assert _edge_exists(store, other_paper, compound, "corroborates")
    assert _conjunct_of_count(store, compound) == 0
    # The MARKER2 edge's "no atom verified" review was queued before the
    # backstop fired -- it must be discarded, not filed.
    assert todo.calls == []
