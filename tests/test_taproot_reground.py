"""Offline unit tests for :mod:`precis.taproot.reground` (T1b's "no source,
no atom" pass, ``docs/backlog/taproot-atom-regrounding.md``).

Every test here is fully offline: :func:`~precis.taproot.reground.verify_atoms`
is exercised with fake ``collect_papers_fn``/``fetch_body_chunks_fn``/
``verify_batch_fn`` (fake chunks, no DB, no LLM call — mirrors
``tests/test_taproot_migrate.py``'s injected-``extract_fn`` pattern);
:func:`~precis.taproot.reground.verify_atoms_batch`'s own dispatch-retry
contract is tested by monkeypatching ``reground.dispatch`` (mirrors
``tests/test_taproot_extract_medium.py``'s pattern for
``extract_claim_strict_medium``, the sibling MEDIUM-tier format-flake
guard this one copies).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.taproot import reground
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.taproot.reground import (
    AtomVerifyResult,
    PaperChunk,
    RegroundingUnavailable,
    candidate_passages,
    collect_source_papers,
    is_hearsay_section,
    verify_atoms,
    verify_atoms_batch,
)
from tests.workers._helpers import seed_ref

#: These tests never touch the DB -- every ``store`` param below is fed to
#: an injected ``collect_papers_fn``/``fetch_body_chunks_fn`` fake that
#: ignores it entirely (mirrors ``tests/test_taproot_backfill.py``'s
#: ``embedder=None`` convention for a faked ``block_fn``). Typed ``Any``
#: so mypy doesn't demand a real ``Store`` for a value nothing reads.
_FAKE_STORE: Any = None


def _claim(sentence: str, **scope: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope=scope)


def _pc(
    chunk_id: int, chunk_ord: int, text: str, section: str | None = None
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id, chunk_ord=chunk_ord, section_path=section, text=text
    )


# ── is_hearsay_section ───────────────────────────────────────────────────


def test_is_hearsay_section_matches_the_named_categories() -> None:
    assert is_hearsay_section("Introduction > References")
    assert is_hearsay_section("Related Work")
    assert is_hearsay_section("Prior Art")
    assert is_hearsay_section("Background")
    assert is_hearsay_section("State of the Art")
    assert is_hearsay_section("Literature Review")
    assert is_hearsay_section("Bibliography")


def test_is_hearsay_section_false_on_body_sections_and_empty() -> None:
    assert not is_hearsay_section("Results > Figure 3")
    assert not is_hearsay_section("Methods")
    assert not is_hearsay_section(None)
    assert not is_hearsay_section("")


# ── candidate_passages — pure ranking ───────────────────────────────────


def test_candidate_passages_excludes_hearsay_sections() -> None:
    atom = "Graphene exhibits a tensile strength of 130 GPa."
    chunks = [
        _pc(1, 0, "Graphene exhibits a tensile strength of 130 GPa here.", "Results"),
        _pc(
            2,
            1,
            "Prior work reported graphene tensile strength of 130 GPa.",
            "Related Work",
        ),
    ]
    cands = candidate_passages(atom, chunks)
    assert [c.chunk_id for c in cands] == [1]


def test_candidate_passages_excludes_front_matter_that_outranks_body_prose() -> None:
    # The gripe-245842 shape, and why the exclusion cannot be left to ranking:
    # the title block is SHORT and made almost entirely of the atom's own topic
    # words, so it beats the real result sentence on overlap every time.
    atom = "Graphene exhibits a tensile strength of 130 GPa."
    chunks = [
        _pc(
            1,
            0,
            "Tensile Strength of Graphene\n\nA. B. Lee, C. D. Park\n\n"
            "Department of Physics",
        ),
        _pc(
            2,
            7,
            "We measured the elastic properties of free-standing monolayers and "
            "found a tensile strength of 130 GPa.",
            "Results",
        ),
    ]
    cands = candidate_passages(atom, chunks)
    assert [c.chunk_id for c in cands] == [2]


def test_candidate_passages_keeps_a_numeric_table() -> None:
    # A table has no sentence but is legitimate evidence for a numeric claim.
    atom = "The binding energy of the I PGNB configuration is -3.34 eV."
    table = _pc(
        1,
        13,
        "| Structure | Binding energy (eV) |\n"
        "|-----------|--------------------|\n"
        "| I PGNB    | -3.34              |\n"
        "| II PGNB   | -3.78              |",
        "Results",
    )
    assert [c.chunk_id for c in candidate_passages(atom, [table])] == [1]


def test_candidate_passages_ranks_by_overlap_descending() -> None:
    atom = "The catalyst achieves 92% yield at RT."
    chunks = [
        _pc(1, 0, "irrelevant prose about something else entirely, at length."),
        _pc(
            2,
            1,
            "The catalyst achieves a 92% yield at room temperature (RT) in "
            "every run we performed.",
        ),
    ]
    cands = candidate_passages(atom, chunks)
    assert cands[0].chunk_id == 2


def test_candidate_passages_folds_unicode_notation_against_ascii_atom() -> None:
    """10^4/10^6 in the atom must match 10⁴/10⁶ in the chunk text — the
    same unicode-superscript folding the migration gates apply."""
    atom = "The rate increases from 10^4 to 10^6 s^-1."
    chunk = _pc(1, 0, "The rate increases from 10⁴ to 10⁶ s⁻¹ under UV.")
    cands = candidate_passages(atom, [chunk])
    assert [c.chunk_id for c in cands] == [1]


def test_candidate_passages_respects_top_k() -> None:
    atom = "X shows high strength."
    chunks = [
        _pc(i, i, f"X shows high strength in sample {i} of the measured set.")
        for i in range(10)
    ]
    cands = candidate_passages(atom, chunks, k=3)
    assert len(cands) == 3


def test_candidate_passages_empty_when_nothing_overlaps() -> None:
    atom = "X shows high strength."
    chunks = [_pc(1, 0, "completely unrelated discussion of something else.")]
    assert candidate_passages(atom, chunks) == []


# ── verify_atoms — hub-level orchestration, fully injected ─────────────


def _never_fetch(store: Any, paper_ref_id: int) -> list[PaperChunk]:
    raise AssertionError("fetch_body_chunks_fn should not have been called")


def _never_verify(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
    raise AssertionError("verify_batch_fn should not have been called")


def _verify_map(mapping: dict[str, tuple[int, str, str | None]]):
    """Fake ``verify_batch_fn``: atom sentence -> ``(chunk_ord, quote,
    bound)``; unmapped atoms come back unsupported. Ignores ``passages``
    (tests only need to prove the *result* flows through post-validation)."""

    def _fn(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
        results = []
        for i, atom in enumerate(atoms):
            hit = mapping.get(atom.sentence)
            if hit is None:
                results.append(AtomVerifyResult(i, False, None, None, None))
            else:
                chunk_ord, quote, bound = hit
                results.append(AtomVerifyResult(i, True, chunk_ord, quote, bound))
        return results

    return _fn


def test_verify_atoms_hanging_hub_makes_no_calls_and_reasons_none() -> None:
    atoms = [_claim("X shows A."), _claim("X shows B.")]
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [],
        fetch_body_chunks_fn=_never_fetch,
        verify_batch_fn=_never_verify,
    )
    assert result.paper_ref_ids == ()
    assert len(result.atoms) == 2
    for ag in result.atoms:
        assert not ag.grounded
        assert ag.reason is None


def test_verify_atoms_no_passage_reason() -> None:
    atoms = [_claim("X shows A.")]
    chunks = [_pc(1, 0, "totally unrelated content about something else entirely.")]
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=_never_verify,
    )
    assert result.paper_ref_ids == (100,)
    assert result.atoms[0].reason == "no-passage"
    assert not result.atoms[0].grounded


def test_verify_atoms_hearsay_only_reason() -> None:
    """The paper's only matching text sits in a Related Work section —
    candidate_passages excludes it, but the delta against the full
    (hearsay-included) chunk set signals hearsay-only, not no-passage."""
    atoms = [_claim("X shows high strength.")]
    # Real prose: the chunk has to survive the grounding-prose gate, or it is
    # excluded as front matter and the reason is no-passage, not hearsay-only.
    chunks = [
        _pc(
            1,
            0,
            "Earlier work by Lee and co-workers shows that X has high strength "
            "under ambient conditions.",
            "Related Work",
        )
    ]
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=_never_verify,
    )
    assert result.atoms[0].reason == "hearsay-only"
    assert not result.atoms[0].grounded


def test_verify_atoms_front_matter_only_is_no_passage_not_hearsay() -> None:
    """The paper's only matching text is its own title page. That is not a
    hearsay section, so naming it ``hearsay-only`` would point the operator at
    the wrong exclusion — the paper simply has no usable passage."""
    atoms = [_claim("X shows high strength.")]
    chunks = [_pc(1, 0, "High Strength in X\n\nA. B. Lee\n\nDept of Physics")]
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=_never_verify,
    )
    assert result.atoms[0].reason == "no-passage"
    assert not result.atoms[0].grounded


def test_verify_atoms_grounds_atom_with_valid_quote_and_bound() -> None:
    atoms = [_claim("X shows a strength of 130 GPa.", quantity="130 GPa")]
    chunks = [_pc(1, 0, "In this study, X shows a strength of 130 GPa under RT.")]
    verify_fn = _verify_map(
        {atoms[0].sentence: (0, "X shows a strength of 130 GPa", "exact")}
    )
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    ag = result.atoms[0]
    assert ag.grounded
    assert ag.reason is None
    assert len(ag.records) == 1
    rec = ag.records[0]
    assert rec.paper_ref_id == 100
    assert rec.chunk_id == 1
    assert rec.chunk_ord == 0
    assert rec.bound == "exact"


def test_verify_atoms_folded_notation_quote_matches_unicode_chunk_text() -> None:
    """The model quotes in ASCII caret notation; the stored chunk text uses
    unicode superscripts — must still validate as the same quote."""
    sentence = "The rate increases from 10^4 to 10^6 s^-1."
    atoms = [_claim(sentence, quantity="10^4 to 10^6 s^-1")]
    chunks = [
        _pc(1, 0, "The rate increases from 10⁴ to 10⁶ s⁻¹ under UV illumination.")
    ]
    verify_fn = _verify_map(
        {sentence: (0, "The rate increases from 10^4 to 10^6 s^-1", "exact")}
    )
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert result.atoms[0].grounded


def test_verify_atoms_rejects_hallucinated_quote_not_in_passage() -> None:
    """The model claims 'supported' but the quote never appears in the
    claimed chunk's text — rejected, never a silently-grounded atom. The
    model DID claim support, so this is the validation-failure reason, not
    the plain "the model said unsupported" one."""
    atoms = [_claim("X shows high conductivity.")]
    chunks = [_pc(1, 0, "X shows high strength in this material under load.")]
    verify_fn = _verify_map({atoms[0].sentence: (0, "X shows high conductivity", None)})
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    ag = result.atoms[0]
    assert not ag.grounded
    assert ag.reason == "quote-validation-failed"


def test_verify_atoms_rejects_quote_not_unique_across_paper() -> None:
    """The quote appears verbatim in TWO non-hearsay chunks of the same
    paper — uniqueness fails even though the claimed chunk itself matches.
    That's a validation failure on a supported=True verdict, not a plain
    "model said unsupported"."""
    atoms = [_claim("X shows the marker property.")]
    chunks = [
        _pc(1, 0, "X shows the marker property in sample A of this run."),
        _pc(2, 1, "X shows the marker property in sample B of this run."),
    ]
    verify_fn = _verify_map(
        {atoms[0].sentence: (0, "X shows the marker property", None)}
    )
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    ag = result.atoms[0]
    assert not ag.grounded
    assert ag.reason == "quote-validation-failed"


def test_verify_atoms_ignores_a_hearsay_chunk_for_uniqueness() -> None:
    """A quote unique among non-hearsay chunks validates even if the SAME
    text also happens to appear in an excluded References section."""
    atoms = [_claim("X shows the marker property.")]
    chunks = [
        _pc(1, 0, "X shows the marker property in this work."),
        _pc(2, 1, "X shows the marker property [12].", "References"),
    ]
    verify_fn = _verify_map(
        {atoms[0].sentence: (0, "X shows the marker property", None)}
    )
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert result.atoms[0].grounded


def test_verify_atoms_grounded_via_second_paper_when_first_paper_rejects() -> None:
    """An atom rejected against paper A's passage may still ground via
    paper B — records aggregate, ungrounded is only the final state.
    Paper A's reject is consistent across both the original call AND the
    reply-level flake retry, so it still stands as a genuine reject."""
    sentence = "X shows a peak at 400 nm."
    atoms = [_claim(sentence, quantity="400 nm")]
    chunks_by_paper = {
        10: [_pc(1, 0, "X shows a peak at 400 nm in reference conditions.")],
        20: [_pc(2, 0, "X shows a peak at 400 nm under UV excitation.")],
    }
    calls = {"n": 0}

    def verify_fn(atoms_: Any, passages: Any) -> list[AtomVerifyResult]:
        calls["n"] += 1
        if any(p.chunk_id == 2 for p in passages):
            return [AtomVerifyResult(0, True, 0, "X shows a peak at 400 nm", "exact")]
        return [AtomVerifyResult(0, False, None, None, None)]

    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [10, 20],
        fetch_body_chunks_fn=lambda s, pid: chunks_by_paper[pid],
        verify_batch_fn=verify_fn,
    )
    ag = result.atoms[0]
    assert ag.grounded
    assert ag.records[0].paper_ref_id == 20
    # paper 10's all-rejected reply triggers the retry guard (called twice),
    # paper 20's supported reply does not (called once) -> 3 total.
    assert calls["n"] == 3


def test_verify_atoms_multiple_atoms_independent_grounding() -> None:
    atom_a = _claim("X shows high strength.")
    atom_b = _claim("X shows an invented property nobody measured.")
    chunks = [_pc(1, 0, "In this work, X shows high strength under RT conditions.")]
    verify_fn = _verify_map({atom_a.sentence: (0, "X shows high strength", None)})
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=[atom_a, atom_b],
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert result.atoms[0].grounded
    assert not result.atoms[1].grounded
    # atom_b shares enough overlap ("shows") to be a weak candidate, so it
    # IS dispatched — but the fake verify_fn has no mapping for it, so it
    # comes back unsupported: verify-rejected, not silently grounded.
    assert result.atoms[1].reason == "verify-rejected"


def test_verify_atoms_quote_validation_failed_vs_verify_rejected_same_hub() -> None:
    """One hub, two atoms: the model claims support for one but its quote
    never checks out (``quote-validation-failed``), while the other is
    genuinely judged unsupported by the model (still ``verify-rejected``) —
    the two reasons must not collapse into each other."""
    atom_invalid = _claim("X shows high conductivity.")
    atom_rejected = _claim("X shows an invented property nobody measured.")
    chunks = [_pc(1, 0, "X shows high strength in this material under load.")]
    verify_fn = _verify_map(
        {atom_invalid.sentence: (0, "X shows high conductivity", None)}
    )
    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=[atom_invalid, atom_rejected],
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert result.atoms[0].reason == "quote-validation-failed"
    assert result.atoms[1].reason == "verify-rejected"


# ── verify_atoms — reply-level flake guard (all-rejected retry) ────────


def test_verify_atoms_all_rejected_batch_retries_once_and_uses_recovered_reply() -> (
    None
):
    """A reply that wholesale-rejects every atom of a batch that DID have
    candidate passages is retried once, and the retry's (grounded)
    results are what actually get used."""
    atoms = [_claim("X shows a strength of 130 GPa.", quantity="130 GPa")]
    chunks = [_pc(1, 0, "In this study, X shows a strength of 130 GPa under RT.")]
    calls = {"n": 0}

    def verify_fn(atoms_: Any, passages: Any) -> list[AtomVerifyResult]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [AtomVerifyResult(0, False, None, None, None)]
        return [AtomVerifyResult(0, True, 0, "X shows a strength of 130 GPa", "exact")]

    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert calls["n"] == 2
    assert result.atoms[0].grounded
    assert result.atoms[0].records[0].bound == "exact"


def test_verify_atoms_all_rejected_on_retry_too_stands_with_two_calls() -> None:
    """A consistent all-reject (both the original call and the retry)
    stands as a genuine rejection -- the guard is a single retry, not a
    loop, and does not paper over a real rejection."""
    atoms = [_claim("X shows a strength of 130 GPa.")]
    chunks = [_pc(1, 0, "In this study, X shows a strength of 130 GPa under RT.")]
    calls = {"n": 0}

    def verify_fn(atoms_: Any, passages: Any) -> list[AtomVerifyResult]:
        calls["n"] += 1
        return [AtomVerifyResult(0, False, None, None, None)]

    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=atoms,
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert calls["n"] == 2
    assert not result.atoms[0].grounded
    assert result.atoms[0].reason == "verify-rejected"


def test_verify_atoms_no_retry_when_at_least_one_atom_is_supported() -> None:
    """A reply with at least one supported verdict is not a flake by this
    guard's definition -- no retry, exactly one dispatch call."""
    atom_a = _claim("X shows high strength.")
    atom_b = _claim("X shows an invented property nobody measured.")
    chunks = [_pc(1, 0, "In this work, X shows high strength under RT conditions.")]
    calls = {"n": 0}

    def verify_fn(atoms_: Any, passages: Any) -> list[AtomVerifyResult]:
        calls["n"] += 1
        return [
            AtomVerifyResult(0, True, 0, "X shows high strength", None),
            AtomVerifyResult(1, False, None, None, None),
        ]

    result = verify_atoms(
        store=_FAKE_STORE,
        hub_ref_id=1,
        atoms=[atom_a, atom_b],
        collect_papers_fn=lambda s, h: [100],
        fetch_body_chunks_fn=lambda s, pid: chunks,
        verify_batch_fn=verify_fn,
    )
    assert calls["n"] == 1
    assert result.atoms[0].grounded


# ── _fold_quote_text / _strip_markup — markup-tolerant quote folding ───


def test_fold_quote_text_strips_bold_emphasis_symmetrically() -> None:
    """A chunk's ``**2350**`` and a model's plain ``2350`` must fold to the
    same normalized text (both sides of a quote match go through the same
    fold, so symmetric stripping is safe)."""
    assert reground._fold_quote_text(
        "capacity of 2350 mg/g"
    ) == reground._fold_quote_text("capacity of **2350** mg/g")


def test_fold_quote_text_still_folds_ascii_caret_against_unicode_superscript() -> None:
    """The markup strip must not regress the pre-existing unicode/notation
    folding this same function already did."""
    assert reground._fold_quote_text("ratio of 10^4") == reground._fold_quote_text(
        "ratio of 10⁴"
    )


def test_fold_quote_text_resolves_markdown_links_but_leaves_bare_brackets_alone() -> (
    None
):
    """``[text](url-or-#anchor)`` folds to its visible text; an ordinary
    ``[...]`` with no following ``(...)`` (not a markdown link at all) is
    left untouched."""
    assert reground._fold_quote_text("[Fe] complex") == "[Fe] complex"
    assert reground._fold_quote_text(
        "[Fe complex](#sec2)"
    ) == reground._fold_quote_text("Fe complex")


# ── collect_source_papers — real DB, both provenance shapes ────────────


def _claim_hub(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def test_collect_source_papers_dedupes_evidence_and_lineage(store: Any) -> None:
    hub = mint_hub(store, _claim_hub("X shows A and X shows B."))
    evidence_paper = seed_ref(store, kind="paper")
    lineage_paper = seed_ref(store, kind="paper")
    both_paper = seed_ref(store, kind="paper")
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=evidence_paper,
        role="corroborates",
        check_retraction=False,
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=both_paper,
        role="corroborates",
        check_retraction=False,
    )
    store.add_link(src_ref_id=hub, dst_ref_id=lineage_paper, relation="derived-from")
    store.add_link(src_ref_id=hub, dst_ref_id=both_paper, relation="derived-from")

    papers = collect_source_papers(store, hub)
    assert papers == sorted({evidence_paper, lineage_paper, both_paper})


def test_collect_source_papers_empty_for_hanging_hub(store: Any) -> None:
    hub = mint_hub(store, _claim_hub("X shows A and X shows B."))
    assert collect_source_papers(store, hub) == []


# ── verify_atoms_batch — dispatch retry/format-flake contract ──────────


def _result(
    *,
    data: dict[str, Any] | None = None,
    text: str = "",
    error: str | None = None,
    timed_out: bool = False,
) -> Any:
    return SimpleNamespace(text=text, data=data, error=error, timed_out=timed_out)


def _good_payload() -> dict[str, Any]:
    return {
        "verdicts": [
            {
                "atom_index": 0,
                "supported": True,
                "chunk_ord": 0,
                "quote": "X shows high strength",
                "bound": None,
            }
        ]
    }


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    calls: list[Any] = []

    def fake_dispatch(req: Any) -> Any:
        calls.append(req)
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(reground, "dispatch", fake_dispatch)
    return calls


_ATOMS = [CanonicalClaim(sentence="X shows high strength.", scope={})]
_PASSAGES = [
    PaperChunk(
        chunk_id=1, chunk_ord=0, section_path=None, text="X shows high strength."
    )
]


def test_verify_atoms_batch_empty_atoms_or_passages_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(monkeypatch, [])
    assert verify_atoms_batch([], _PASSAGES) == []
    assert verify_atoms_batch(_ATOMS, []) == []
    assert calls == []


def test_verify_atoms_batch_good_payload_parses_on_first_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(monkeypatch, [_result(data=_good_payload())])
    results = verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 1
    assert calls[0].tier is reground.Tier.MEDIUM
    assert calls[0].source == "taproot:reground-verify"
    assert results == [AtomVerifyResult(0, True, 0, "X shows high strength", None)]


def test_verify_atoms_batch_timeout_raises_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(
        monkeypatch,
        [
            _result(error="claude -p timed out", timed_out=True),
            _result(data=_good_payload()),
        ],
    )
    with pytest.raises(RegroundingUnavailable):
        verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 1


def test_verify_atoms_batch_non_timeout_error_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reground, "_FLAKE_RETRY_BACKOFF_S", 0.0)
    calls = _patch_dispatch(
        monkeypatch, [_result(error="ECONNREFUSED"), _result(data=_good_payload())]
    )
    results = verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 2
    assert results[0].supported is True


def test_verify_atoms_batch_persistent_dispatch_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reground, "_FLAKE_RETRY_BACKOFF_S", 0.0)
    calls = _patch_dispatch(
        monkeypatch, [_result(error="ECONNREFUSED"), _result(error="ECONNREFUSED")]
    )
    with pytest.raises(RegroundingUnavailable):
        verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 2


def test_verify_atoms_batch_unparseable_reply_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(
        monkeypatch,
        [_result(data=None, text="not json at all"), _result(data=_good_payload())],
    )
    results = verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 2
    assert results[0].supported is True


def test_verify_atoms_batch_persistently_unparseable_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(
        monkeypatch,
        [
            _result(data=None, text="not json at all"),
            _result(data=None, text="still not json"),
        ],
    )
    with pytest.raises(RegroundingUnavailable):
        verify_atoms_batch(_ATOMS, _PASSAGES)
    assert len(calls) == 2


def test_verify_atoms_batch_supported_without_quote_is_coerced_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'supported: true' verdict missing chunk_ord or quote must never
    become a GroundedRecord candidate -- coerced unsupported defensively."""
    payload = {
        "verdicts": [
            {"atom_index": 0, "supported": True, "chunk_ord": None, "quote": None}
        ]
    }
    _patch_dispatch(monkeypatch, [_result(data=payload)])
    results = verify_atoms_batch(_ATOMS, _PASSAGES)
    assert results == [AtomVerifyResult(0, False, None, None, None)]
