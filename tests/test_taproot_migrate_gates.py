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
