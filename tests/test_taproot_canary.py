"""Offline tests for the taproot extraction canary (round 2 of
``docs/backlog/taproot-migration-extraction-quality-gates.md``): the
packaged fixture is read for real, the extractor is a stub — no LLM, no
DB, runs in the normal gate. The live canary is the CLI
(``precis taproot-migrate canary``).

The failure-mode stubs mirror what the labelled-25 A/B re-run actually
observed on the SMALL tier: truncation to a single leading-fragment atom
(caught by the coverage gates as ``lossy``) and empty output on
claim-bearing passages (caught as a no-claim mismatch).
"""

from __future__ import annotations

import argparse

import pytest

from precis.cli.taproot_migrate import _resolve_extract_fn, _run_dry_run
from precis.taproot.canon import (
    CanonicalClaim,
    ClaimExtraction,
    extract_claim_strict,
    extract_claim_strict_big,
    extract_claim_strict_medium,
)
from precis.taproot.eval_canon import (
    EXTRACTION_PASSAGES_FIXTURE,
    _load_jsonl,
    canary_extraction,
)

_EMPTY = ClaimExtraction(atoms=(), composite=None, not_claims=())

_NO_CLAIM_PASSAGES = frozenset(
    str(row["passage"])
    for row in _load_jsonl(EXTRACTION_PASSAGES_FIXTURE)
    if int(row["expected_atom_count"]) == 0
)


def _atom(sentence: str) -> ClaimExtraction:
    return ClaimExtraction(
        atoms=(CanonicalClaim(sentence=sentence, scope={}),),
        composite=None,
        not_claims=(),
    )


def _faithful_stub(passage: str) -> ClaimExtraction:
    """Echoes the whole passage as one atom (nothing lost, nothing added)
    and returns empty on the fixture's NO-CLAIM rows — the granularity-
    agnostic 'sound extractor' baseline."""
    if passage in _NO_CLAIM_PASSAGES:
        return _EMPTY
    return _atom(passage)


def test_canary_passes_with_a_faithful_extractor() -> None:
    report = canary_extraction(extract_fn=_faithful_stub, progress=False)
    assert report.total >= 13
    assert report.gate_rejections == []
    assert report.no_claim_mismatches == []
    assert report.ok
    assert "CANARY OK" in report.format()


def test_canary_fails_on_a_truncating_extractor() -> None:
    """The SMALL-collapse shape: keep only a leading fragment of every
    passage. The coverage gates must reject it (``lossy``), failing the
    canary — this is the exact failure the A/B re-run burned 25 hubs to
    discover and the canary exists to catch for 11 calls."""

    def _truncate(passage: str) -> ClaimExtraction:
        return _atom(" ".join(passage.split()[:4]) + ".")

    report = canary_extraction(extract_fn=_truncate, progress=False)
    assert report.gate_rejections
    assert not report.ok
    assert "CANARY FAILED" in report.format()


def test_canary_fails_on_no_claim_mismatch_in_either_direction() -> None:
    always_empty = canary_extraction(extract_fn=lambda _passage: _EMPTY, progress=False)
    assert not always_empty.ok
    assert always_empty.gate_rejections == []
    assert always_empty.no_claim_mismatches

    def _claims_from_junk(passage: str) -> ClaimExtraction:
        return _atom(passage)  # echoes even the NO-CLAIM passages

    never_empty = canary_extraction(extract_fn=_claims_from_junk, progress=False)
    assert not never_empty.ok
    assert any(r.expected_atom_count == 0 for r in never_empty.no_claim_mismatches)


def test_resolve_extract_fn_maps_tiers_to_strict_variants() -> None:
    assert _resolve_extract_fn("haiku") is extract_claim_strict_medium
    assert _resolve_extract_fn("big") is extract_claim_strict_big
    assert _resolve_extract_fn("small") is extract_claim_strict


@pytest.mark.parametrize("tier", ["big", "haiku"])
def test_dry_run_cli_rejects_escalate_with_non_small_tier(tier: str) -> None:
    """--escalate is the SMALL→BIG retry; with any non-SMALL primary it is
    meaningless and must fail loud before touching the DB."""
    args = argparse.Namespace(escalate=True, tier=tier)
    with pytest.raises(SystemExit) as exc_info:
        _run_dry_run(args)
    assert exc_info.value.code == 2


# ── explains (causal split) — fixture rows + the canary's soft metric ────────

_CAUSAL_ROWS = {
    int(row["id"]): row
    for row in _load_jsonl(EXTRACTION_PASSAGES_FIXTURE)
    if row.get("expected_explains_count") is not None
}


def test_fixture_carries_one_split_and_one_vague_inline_causal_row() -> None:
    assert sorted(
        (int(r["expected_atom_count"]), int(r["expected_explains_count"]))
        for r in _CAUSAL_ROWS.values()
    ) == [(1, 0), (2, 1)]
    for row in _CAUSAL_ROWS.values():
        assert "because" in row["passage"]
        assert row["note"]


def _split_on_because(passage: str) -> ClaimExtraction:
    """Splits the split-case passage into effect + mechanism atoms with the
    pair recorded; everything else falls back to the faithful baseline."""
    if "undercoordinated" not in passage:
        return _faithful_stub(passage)
    return ClaimExtraction(
        atoms=(
            CanonicalClaim(
                sentence="Nanoporous gold oxidizes CO at room temperature.", scope={}
            ),
            CanonicalClaim(
                sentence=(
                    "The undercoordinated surface gold atoms of nanoporous gold "
                    "bind molecular oxygen strongly."
                ),
                scope={},
            ),
        ),
        composite=CanonicalClaim(sentence=passage, scope={}),
        not_claims=(),
        explains=((1, 0),),
    )


def test_canary_grades_explains_counts_softly() -> None:
    right = canary_extraction(extract_fn=_split_on_because, progress=False)
    assert right.ok
    assert right.explains_mismatches == []
    assert "explains-count mismatches: 0 of 2 graded" in right.format()

    # A faithful one-atom echo folds the mechanism inline: drift on the split
    # row only — reported, but the canary still passes (granularity call).
    folded = canary_extraction(extract_fn=_faithful_stub, progress=False)
    assert folded.ok
    assert [r.expected_explains_count for r in folded.explains_mismatches] == [1]
    assert "Explains-count drift" in folded.format()


def test_canary_fails_on_a_malformed_explains_pair() -> None:
    def _bad_pair(passage: str) -> ClaimExtraction:
        ext = _split_on_because(passage)
        if ext.explains:
            return ClaimExtraction(
                atoms=ext.atoms,
                composite=ext.composite,
                not_claims=ext.not_claims,
                explains=((1, 1),),
            )
        return ext

    report = canary_extraction(extract_fn=_bad_pair, progress=False)
    assert not report.ok
    assert [r.verdict for r in report.gate_rejections] == ["bad-explains"]
    assert "CANARY FAILED" in report.format()
