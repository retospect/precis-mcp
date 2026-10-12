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
silent, permanent stamp on a still-composite hub) rather than to be
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

from precis.taproot.canon import (
    BAD_EXPLAINS_PAIR,
    CanonicalClaim,
    ClaimExtraction,
    NotClaim,
)
from precis.taproot.migrate import (
    VERDICTS,
    Verdict,
    _containment_findings,
    _explains_problems,
    classify_extraction,
)

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
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _build_extraction(row: dict[str, Any]) -> ClaimExtraction:
    atoms = tuple(
        CanonicalClaim(sentence=atom["sentence"], scope=atom.get("scope", {}))
        for atom in row["atoms"]
    )
    composite = (
        CanonicalClaim(sentence=row["composite"], scope={})
        if row.get("composite")
        else None
    )
    not_claims = tuple(
        NotClaim(text=nc["text"], reason=nc["reason"])
        for nc in row.get("not_claims", [])
    )
    explains = tuple((int(a), int(b)) for a, b in row.get("explains", []))
    return ClaimExtraction(
        atoms=atoms, composite=composite, not_claims=not_claims, explains=explains
    )


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


def test_number_tokens_split_glued_degree_sign() -> None:
    """ "19 degrees" rendered as "19°" (canary passage 2) is the same angle;
    "300°C" and "300 °C" are the same temperature."""
    assert _number_bearing_tokens("angles of 19, 39 and 113 degrees") == [
        "19",
        "39",
        "113",
    ]
    assert _number_bearing_tokens("angles of 19°, 39° and 113°") == ["19", "39", "113"]
    assert _number_bearing_tokens("at 300°C") == _number_bearing_tokens("at 300 °C")


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
    composite = CanonicalClaim(sentence=sentence, scope={})
    _verdict, meta = classify_extraction(
        sentence, ClaimExtraction(atoms=atoms, composite=composite, not_claims=())
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
    composite = CanonicalClaim(sentence=sentence, scope={})
    _verdict, meta = classify_extraction(
        sentence, ClaimExtraction(atoms=atoms, composite=composite, not_claims=())
    )
    assert meta["invented_numbers"] == ()


# ── explains gate (the causal split, migration 0203) ───────────────────────

_CAUSAL_SENTENCE = (
    "Nanoporous gold, a dealloyed Au-Ag material, oxidizes CO at room "
    "temperature because its undercoordinated surface gold atoms bind "
    "molecular oxygen strongly."
)
_EFFECT = CanonicalClaim(
    sentence="Nanoporous gold oxidizes CO at room temperature.", scope={}
)
_MECHANISM = CanonicalClaim(
    sentence=(
        "Nanoporous gold oxidizes CO at room temperature as its "
        "undercoordinated surface gold atoms bind molecular oxygen strongly."
    ),
    scope={},
)


def _causal(
    explains: tuple[tuple[int, int], ...],
    atoms: tuple[CanonicalClaim, ...] = (_EFFECT, _MECHANISM),
) -> ClaimExtraction:
    return ClaimExtraction(
        atoms=atoms,
        composite=CanonicalClaim(sentence=_CAUSAL_SENTENCE, scope={}),
        not_claims=(),
        explains=explains,
    )


def test_bad_explains_is_a_declared_verdict() -> None:
    assert "bad-explains" in VERDICTS
    assert set(VERDICTS) == {
        "pass-through",
        "split",
        "lossy",
        "nested",
        "bad-explains",
        "no-claim",
        "error",
    }


@pytest.mark.parametrize(
    ("explains", "fragment"),
    [
        (((1, 5),), "names no emitted atom"),  # index past the last atom
        (((-2, 0),), "names no emitted atom"),  # negative index
        ((BAD_EXPLAINS_PAIR,), "names no emitted atom"),  # parser's sentinel
        (((1, 1),), "explains itself"),  # self-pair
        (((0, 1), (1, 0)), "cycle"),  # two-cycle
    ],
)
def test_malformed_explains_is_held_as_bad_explains(
    explains: tuple[tuple[int, int], ...], fragment: str
) -> None:
    verdict, meta = classify_extraction(_CAUSAL_SENTENCE, _causal(explains))
    assert verdict == "bad-explains"
    assert any(fragment in p for p in meta["explains_problems"]), meta
    assert meta["explains"] == [list(p) for p in explains]


def test_explains_cycle_through_a_chain_is_detected() -> None:
    a = CanonicalClaim(sentence="A is true.", scope={})
    b = CanonicalClaim(sentence="B is true.", scope={})
    c = CanonicalClaim(sentence="C is true.", scope={})
    ext = _causal(((0, 1), (1, 2), (2, 0)), atoms=(a, b, c))
    assert _explains_problems(ext)
    assert classify_extraction(_CAUSAL_SENTENCE, ext)[0] == "bad-explains"
    # the same chain without the closing edge is a sound DAG
    assert _explains_problems(_causal(((0, 1), (1, 2)), atoms=(a, b, c))) == []
    # a diamond (two explainers of one effect, one explainer of two effects)
    # is not a cycle
    d = CanonicalClaim(sentence="D is true.", scope={})
    assert (
        _explains_problems(_causal(((0, 2), (1, 2), (0, 3)), atoms=(a, b, c, d))) == []
    )


def test_sound_explains_pair_passes_and_is_recorded_in_gate_meta() -> None:
    verdict, meta = classify_extraction(_CAUSAL_SENTENCE, _causal(((1, 0),)))
    assert verdict == "split"
    assert meta["explains"] == [[1, 0]]
    assert "explains_problems" not in meta


def test_no_explains_leaves_gate_meta_unchanged() -> None:
    _verdict, meta = classify_extraction(_CAUSAL_SENTENCE, _causal(()))
    assert "explains" not in meta


def test_explains_pair_is_exempt_from_pairwise_containment() -> None:
    """The mechanism atom names the effect's subject and restates its
    predicate (containment 1.0 of the effect) — the pairwise nested gate
    would fire on it. Declared as an explains pair it must not; declared as
    nothing (a plain peer split) it still does."""
    ext_plain = _causal(())
    pairwise_plain = _containment_findings(
        ext_plain.atoms, _CAUSAL_SENTENCE, frozenset()
    )
    assert any("atoms" in f for f in pairwise_plain)  # would fire without the pair
    assert classify_extraction(_CAUSAL_SENTENCE, ext_plain)[0] == "nested"

    verdict, meta = classify_extraction(_CAUSAL_SENTENCE, _causal(((1, 0),)))
    assert verdict == "split", meta
    assert "containment" not in meta


def test_explains_exemption_is_only_for_the_declared_pair() -> None:
    """A third atom that restates one of the pair is not exempted."""
    dup = CanonicalClaim(sentence=_EFFECT.sentence, scope={})
    ext = _causal(((1, 0),), atoms=(_EFFECT, _MECHANISM, dup))
    verdict, meta = classify_extraction(_CAUSAL_SENTENCE, ext)
    assert verdict == "nested"
    assert {"atoms": (0, 2), "ratio": 1.0} in meta["containment"]


def test_explains_pair_does_not_exempt_the_composite_check() -> None:
    """An atom that alone covers the whole composite is a fake split no
    matter what pair it carries."""
    sentence = _MECHANISM.sentence
    ext = ClaimExtraction(
        atoms=(_EFFECT, _MECHANISM),
        composite=CanonicalClaim(sentence=sentence, scope={}),
        not_claims=(),
        explains=((1, 0),),
    )
    verdict, meta = classify_extraction(sentence, ext)
    assert verdict == "nested"
    assert any(f.get("vs") == "composite" for f in meta["containment"])


def test_pilot_rows_with_explains_pairs_are_sound() -> None:
    """The re-labelled fi176422 / fi176399 rows carry their pairs through
    the fixture and clear the explains gate."""
    rows = {row["hub"]: row for row in _load_rows()}
    for hub in ("fi176422", "fi176399"):
        assert rows[hub]["explains"], hub
        ext = _build_extraction(rows[hub])
        assert ext.explains == tuple(tuple(p) for p in rows[hub]["explains"])
        assert _explains_problems(ext) == [], hub
        assert classify_extraction(rows[hub]["sentence"], ext)[0] == "split", hub
