"""Calibration test for :func:`precis.taproot.migrate.classify_extraction`
(P0-2 coverage / P0-3 containment gates) against the labelled 25-hub
Phase-1 pilot fixture (``migration_pilot_25.jsonl`` — adjudicated against
untruncated prod data, see ``docs/backlog/
taproot-migration-extraction-quality-gates.md``). Pure function, no DB, no
model — this asserts the *gates*, not the extractor.

**Known false positives (documented, not a bug to chase further).** The
gates are lexical (token-set overlap); they can't distinguish "reworded
but complete" from "a real clause is gone" any better than bag-of-words
ever can. Four of the fixture's five correct ``pass-through`` rows drop an
illustrative example or a summarizing/restating clause that a human judged
non-essential — the same *lexical* pattern as the six pass-throughs that
really are lossy, so no single recall threshold gets all of them right
(the ``lossy``-vs-``pass-through`` recall values interleave; see
:data:`precis.taproot.migrate._LOSSY_RECALL_THRESHOLD_PASS_THROUGH`'s
docstring for the exact numbers). fi176365/fi176449/fi176468 fall below
the recall ratio; fi176361 (round 2) clears the ratio but drops exactly
:data:`precis.taproot.migrate._LOSSY_MISSING_CONTENT_CAP_PASS_THROUGH`
content words — the absolute cap that catches the fi176441 truncation
class flags it too. Per the backlog's own sequencing note, the gate is
calibrated to catch every truly-lossy row (the dangerous class — a
silent, permanent stamp on a still-compound hub) rather than to be
precise about the safe ones (a false ``lossy`` only costs an extra P2-10
escalation call). These four rows are marked ``xfail`` below, not
skipped — a future gate improvement that fixes one should surprise this
suite (``xfail`` without ``strict=False`` still reports XPASS loudly).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.taproot.canon import CanonicalClaim, ClaimExtraction, NotClaim
from precis.taproot.migrate import Verdict, classify_extraction

FIXTURE = Path(__file__).parent / "fixtures" / "taproot" / "migration_pilot_25.jsonl"

#: fi176365/fi176449/fi176468/fi176361 — see the module docstring. All
#: four are real ``pass-through`` rows a coverage gate flags ``lossy``
#: because they drop an illustrative example or a summarizing clause
#: (the first three below the recall ratio, fi176361 at the round-2
#: missing-content-word cap); the fixture's truly-lossy pass-throughs
#: (fi176545, fi176764, fi176812, fi176275, fi176360) drop real content
#: at a lexically-similar or higher recall, so no single threshold
#: separates both groups (see the gate constants' docstrings in
#: ``precis/taproot/migrate.py`` for the exact numbers).
_KNOWN_FALSE_POSITIVES = frozenset({"fi176365", "fi176449", "fi176468", "fi176361"})


def _load_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()
    ]


def _build_extraction(row: dict[str, Any]) -> ClaimExtraction:
    atoms = tuple(
        CanonicalClaim(sentence=atom["sentence"], scope=atom.get("scope", {}))
        for atom in row["atoms"]
    )
    compound = (
        CanonicalClaim(sentence=row["compound"], scope={})
        if row.get("compound")
        else None
    )
    not_claims = tuple(
        NotClaim(text=nc["text"], reason=nc["reason"])
        for nc in row.get("not_claims", [])
    )
    return ClaimExtraction(atoms=atoms, compound=compound, not_claims=not_claims)


def _params() -> list[Any]:
    params = []
    for row in _load_rows():
        marks = (
            [
                pytest.mark.xfail(
                    reason=(
                        "known lexical-recall false positive — dropping an "
                        "illustrative example/summary clause reads the same "
                        "as real loss to a bag-of-words gate; see module "
                        "docstring"
                    ),
                    strict=True,
                )
            ]
            if row["hub"] in _KNOWN_FALSE_POSITIVES
            else []
        )
        params.append(pytest.param(row, id=row["hub"], marks=marks))
    return params


@pytest.mark.parametrize("row", _params())
def test_classify_extraction_matches_expected_gated_verdict(
    row: dict[str, Any],
) -> None:
    extraction = _build_extraction(row)
    verdict, gate_meta = classify_extraction(row["sentence"], extraction)
    assert verdict == row["expected_gated_verdict"], (
        f"{row['hub']}: expected {row['expected_gated_verdict']!r}, got "
        f"{verdict!r} ({row['notes']!r}); gate_meta={gate_meta}"
    )


def test_fixture_known_false_positives_are_exactly_four_pass_through_rows() -> None:
    """Guards the calibration note itself: if this ever drifts (a fixture
    edit, a gate change), the count/shape of the accepted false positives
    should be re-examined, not silently grow."""
    rows_by_hub = {row["hub"]: row for row in _load_rows()}
    assert rows_by_hub.keys() >= _KNOWN_FALSE_POSITIVES
    for hub in _KNOWN_FALSE_POSITIVES:
        assert rows_by_hub[hub]["expected_gated_verdict"] == "pass-through"
        assert rows_by_hub[hub]["model_verdict"] == "pass-through"


def test_fixture_has_every_verdict_shape_represented() -> None:
    """Sanity check on the fixture itself, not the gate — catches a
    silently-truncated or malformed fixture file before it produces a
    misleadingly-green run."""
    rows = _load_rows()
    assert len(rows) == 25
    verdicts: set[Verdict] = {row["expected_gated_verdict"] for row in rows}
    assert verdicts == {"pass-through", "split", "lossy", "nested", "no-claim"}


# ── notation folding + citation stripping (100-hub prod run, 2026-08-15) ────
#
# claude-haiku normalizes notation and (correctly) drops bibliography; the
# gates must compare measurements, not glyphs, and must never require
# citation fragments as content. Loss direction only — the invention
# direction keeps the full original as its allowlist.

from precis.taproot.migrate import (
    _number_bearing_tokens,
    _strip_citation_tail,
)


def test_number_tokens_fold_unicode_superscripts_and_dashes() -> None:
    assert _number_bearing_tokens("rates of 10^4-10^6 s^-1") == _number_bearing_tokens(
        "rates of 10⁴–10⁶ s^-1"
    )


def test_number_tokens_split_digit_leading_hyphenates() -> None:
    """ "13-residue" and the scope rephrasing "13 residues" must yield the
    same measurement token (the SpyCatcher false-invented case)."""
    assert _number_bearing_tokens("a 13-residue peptide") == ["13"]
    assert _number_bearing_tokens("13 residues") == ["13"]


def test_number_tokens_keep_catalog_names_excluded() -> None:
    """Letter-leading tokens never split: MOF-5 stays a catalog name
    (fi176435's exclusion), so its digit can't read as a measurement."""
    assert _number_bearing_tokens("frameworks such as MOF-5 and ZIF-8") == []


def test_number_tokens_fold_approximation_prefix() -> None:
    """ "near 10 kHz" rendered as "~10 kHz" is the same measurement."""
    assert _number_bearing_tokens("~10 kHz") == _number_bearing_tokens("10 kHz")


def test_number_tokens_digit_substring_hole_stays_closed() -> None:
    """The original P0-2 defense: a dropped "9" must not hide in "409"."""
    assert "9" not in _number_bearing_tokens("a 409 nm shift")


def test_citation_tail_stripped_only_past_first_third() -> None:
    tail = "Claim text here that is long enough. Canonical references: X (2008)."
    assert "2008" not in _strip_citation_tail(tail)
    headed = "References: the full body follows " + "x " * 40
    assert _strip_citation_tail(headed).startswith("References:")


def test_inline_citation_spans_are_not_required_content() -> None:
    """A split that drops "Phys. Rev. Lett. 57, 1761, 1986"-style inline
    citations (comma form, vol(issue) form, parenthesized multi-cite) must
    not gate lossy for the citation numbers."""
    sentence = (
        "Quantum interference produces conductance swings of 100x in single "
        "molecules (Zhang 2009; Mak 2008), as shown in Phys. Rev. Lett. 57, "
        "1761, 1986 and J. Phys. Chem. 123(24), 5035-5047."
    )
    atoms = (
        CanonicalClaim(
            sentence="Quantum interference produces conductance swings of 100x.",
            scope={},
        ),
        CanonicalClaim(
            sentence="The conductance swings occur in single molecules.",
            scope={},
        ),
    )
    compound = CanonicalClaim(sentence=sentence, scope={})
    _verdict, meta = classify_extraction(
        sentence, ClaimExtraction(atoms=atoms, compound=compound, not_claims=())
    )
    # Only the number gate is pinned: the citation *words* ("Phys. Rev.
    # Lett.") still count toward content recall — a separate, accepted
    # limitation (they're a tiny fraction of real hub bodies).
    assert meta["missing_numbers"] == ()


def test_kept_citation_numbers_are_not_invented() -> None:
    """The invention direction allowlists the FULL sentence: an atom that
    kept a citation year did not invent it."""
    sentence = "The effect was first measured in 2008 (PRL 2008)."
    atoms = (
        CanonicalClaim(sentence="The effect was first measured in 2008.", scope={}),
        CanonicalClaim(sentence="The measurement appeared in PRL.", scope={}),
    )
    compound = CanonicalClaim(sentence=sentence, scope={})
    _verdict, meta = classify_extraction(
        sentence, ClaimExtraction(atoms=atoms, compound=compound, not_claims=())
    )
    assert meta["invented_numbers"] == ()
