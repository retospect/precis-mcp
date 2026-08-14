"""Offline unit tests for Taproot Phase 1 (``precis.taproot.canon`` +
``precis.taproot.eval_canon``).

Every LLM call is mocked (``canon.dispatch`` monkeypatched, or an injected
stub function) — no live model, no DB. The live-model eval harness itself
(``dedup_judge`` run over the real fixture) is a separate, gated test — see
``tests/test_taproot_eval_canon.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.taproot import canon
from precis.taproot.canon import (
    Candidate,
    CanonicalClaim,
    ClaimExtraction,
    ExtractionUnavailable,
    NotClaim,
    Placement,
    Verdict,
    dedup_judge,
    extract_claim,
    extract_claim_strict,
    merge_confirm,
    place,
)
from precis.taproot.eval_canon import (
    ExtractionReport,
    Report,
    collapse_label,
    eval_canonicalization,
    eval_extraction,
)


def _result(
    *, data: dict[str, Any] | None = None, text: str = "", error: str | None = None
) -> Any:
    """A stand-in for ``LlmResult`` — dispatch() callers only read
    ``.error``, ``.data``, and ``.text``."""
    return SimpleNamespace(text=text, data=data, error=error)


def _verdict(v: canon.Verdict3, confidence: float, rationale: str = "") -> Verdict:
    return Verdict(verdict=v, confidence=confidence, rationale=rationale)


# ── extract_claim — NO-CLAIM detection ──────────────────────────────────


def test_extract_claim_returns_single_atom_on_a_real_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that emits exactly one claim in the ``claims[]`` list, no
    compound, no rejects -> one atom, no compound (already-atomic)."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [
                    {
                        "claim": "Pd/C catalyzes Suzuki coupling at RT with mild base",
                        "method": "Suzuki coupling",
                        "regime": "RT",
                    }
                ],
                "compound": None,
                "not_claims": [],
            }
        ),
    )
    result = extract_claim("Pd/C catalyzes Suzuki coupling at room temperature...")
    assert result == ClaimExtraction(
        atoms=(
            CanonicalClaim(
                sentence="Pd/C catalyzes Suzuki coupling at RT with mild base",
                scope={"method": "Suzuki coupling", "regime": "RT"},
            ),
        ),
        compound=None,
        not_claims=(),
    )
    assert not result.is_empty


def test_extract_claim_splits_a_bundled_passage_into_multiple_atoms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The carbon-nanomaterials worked example
    (docs/backlog/taproot-atomic-claims.md §Worked example): several
    groundable atoms + rejected conjuncts + a surviving compound."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [
                    {
                        "claim": "Carbon nanomaterials exhibit tunable mechanical characteristics."
                    },
                    {
                        "claim": "Carbon nanomaterials exhibit tunable optoelectronic characteristics."
                    },
                ],
                "compound": (
                    "Carbon nanomaterials have exceptional mechanical, "
                    "optoelectronic, and physicochemical characteristics and "
                    "tunability that enable next-generation technologies, "
                    "particularly in advanced electronics."
                ),
                "not_claims": [
                    {
                        "text": "enable next-generation technologies",
                        "reason": "forward-looking",
                    },
                    {
                        "text": "exceptional",
                        "reason": "comparative with no stated comparator",
                    },
                ],
            }
        ),
    )
    result = extract_claim("Carbon nanomaterials have exceptional mechanical...")
    assert len(result.atoms) == 2
    assert result.compound is not None
    assert result.compound.sentence.startswith("Carbon nanomaterials have")
    assert result.not_claims == (
        NotClaim(text="enable next-generation technologies", reason="forward-looking"),
        NotClaim(text="exceptional", reason="comparative with no stated comparator"),
    )
    assert not result.is_empty


def test_extract_claim_folds_a_degenerate_single_atom_compound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that returns a compound bundling a LONE atom with nothing
    rejected is folded to just the atom — never mint a degenerate
    1-conjunct bundle (the invariant's second bullet)."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [
                    {"claim": "Graphene exhibits a tensile strength of ~130 GPa."}
                ],
                "compound": "Graphene exhibits a tensile strength of ~130 GPa.",
                "not_claims": [],
            }
        ),
    )
    result = extract_claim("Graphene has extraordinary tensile strength...")
    assert result.compound is None
    assert len(result.atoms) == 1


def test_extract_claim_keeps_compound_for_a_lone_atom_with_a_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone surviving atom PLUS a rejected conjunct still keeps the
    compound — decomposition did something real (something was dropped
    from the bundle), even though only one atom survived."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [
                    {"claim": "The catalyst achieves a TOF of 450 h⁻¹ at 80°C."}
                ],
                "compound": "The catalyst shows superior activity and a TOF of 450 h⁻¹ at 80°C.",
                "not_claims": [
                    {
                        "text": "shows superior activity",
                        "reason": "comparative w/o comparator",
                    }
                ],
            }
        ),
    )
    result = extract_claim("The catalyst shows superior activity...")
    assert len(result.atoms) == 1
    assert result.compound is not None
    assert len(result.not_claims) == 1


def test_extract_claim_returns_empty_extraction_on_pure_pointer_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "See [12]" / Related-Work chunk -> the model returns an empty
    ``claims`` list -> NO-CLAIM (taproot.md Axis A stage 0'), an empty
    extraction rather than ``None``."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(data={"claims": [], "compound": None, "not_claims": []}),
    )
    result = extract_claim("As shown in prior work [12], ...")
    assert result.is_empty
    assert result == ClaimExtraction(atoms=(), compound=None, not_claims=())


def test_extract_claim_no_claim_still_records_not_claims_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every conjunct rejected -> zero atoms (still NO-CLAIM/empty) but the
    rejects are kept for the compound hub's audit memo (step 8)."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [],
                "compound": None,
                "not_claims": [
                    {
                        "text": "will likely enable superior performance",
                        "reason": "forward-looking",
                    }
                ],
            }
        ),
    )
    result = extract_claim("This approach will likely enable superior performance...")
    assert result.is_empty  # atoms=(), compound=None per the NO-CLAIM invariant
    assert result.not_claims == (
        NotClaim(
            text="will likely enable superior performance", reason="forward-looking"
        ),
    )


def test_extract_claim_tolerates_legacy_single_object_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SMALL-tier model regressing to the old ``{"claim": ...}``
    single-object shape degrades to one atom, no compound/not_claims —
    same fail-safe posture, never a dropped claim on a format regression."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claim": "Pd/C catalyzes Suzuki coupling at RT with mild base",
                "method": "Suzuki coupling",
            }
        ),
    )
    result = extract_claim("Pd/C catalyzes Suzuki coupling at room temperature...")
    assert result == ClaimExtraction(
        atoms=(
            CanonicalClaim(
                sentence="Pd/C catalyzes Suzuki coupling at RT with mild base",
                scope={"method": "Suzuki coupling"},
            ),
        ),
        compound=None,
        not_claims=(),
    )


def test_extract_claim_legacy_shape_null_claim_is_empty_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(data={"claim": None}))
    result = extract_claim("As shown in prior work [12], ...")
    assert result.is_empty


def test_extract_claim_returns_empty_extraction_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="transport down"))
    result = extract_claim("some passage")
    assert result.is_empty
    assert result == ClaimExtraction(atoms=(), compound=None, not_claims=())


def test_extract_claim_strict_raises_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict variant surfaces infra failure as an exception rather
    than degrading to a silent NO-CLAIM — the melchior-incident guard."""
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="ECONNREFUSED"))
    with pytest.raises(ExtractionUnavailable, match="ECONNREFUSED"):
        extract_claim_strict("some passage")


def test_extract_claim_still_degrades_to_empty_on_the_same_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-strict variant's fail-safe posture is unchanged by the
    strict sibling's existence — same dispatch error, still empty."""
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="ECONNREFUSED"))
    result = extract_claim("some passage")
    assert result.is_empty


def test_extract_claim_strict_returns_same_extraction_as_extract_claim_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a successful dispatch, the strict variant parses identically to
    the plain one — the only behavioral difference is on ``res.error``."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claims": [
                    {"claim": "Pd/C catalyzes Suzuki coupling at RT with mild base"}
                ],
                "compound": None,
                "not_claims": [],
            }
        ),
    )
    strict_result = extract_claim_strict("Pd/C catalyzes Suzuki coupling...")
    plain_result = extract_claim("Pd/C catalyzes Suzuki coupling...")
    assert strict_result == plain_result


def test_extract_claim_strict_still_degrades_to_empty_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparseable-but-successful model output is a semantic no-claim, not
    an infra failure — the strict variant does not raise on it."""
    monkeypatch.setattr(
        canon, "dispatch", lambda req: _result(data=None, text="not json at all")
    )
    assert extract_claim_strict("some passage").is_empty


def test_extract_claim_returns_empty_extraction_on_empty_input() -> None:
    assert extract_claim("").is_empty
    assert extract_claim("   ").is_empty


def test_extract_claim_returns_empty_extraction_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canon, "dispatch", lambda req: _result(data=None, text="not json at all")
    )
    assert extract_claim("some passage").is_empty


# ── ClaimExtraction / _coerce_extraction — parse-time invariants ────────


def test_claim_extraction_is_empty_property() -> None:
    assert ClaimExtraction(atoms=(), compound=None, not_claims=()).is_empty
    atom = CanonicalClaim(sentence="x", scope={})
    assert not ClaimExtraction(atoms=(atom,), compound=None, not_claims=()).is_empty


def test_coerce_extraction_no_atoms_drops_any_compound() -> None:
    """NO-CLAIM invariant: zero atoms always yields compound=None, even if
    the model still emitted one."""
    result = canon._coerce_extraction(
        [], CanonicalClaim(sentence="bundle", scope={}), []
    )
    assert result == ClaimExtraction(atoms=(), compound=None, not_claims=())
    assert result.is_empty


def test_coerce_extraction_lone_atom_no_rejects_folds_compound_away() -> None:
    atom = CanonicalClaim(sentence="x", scope={})
    result = canon._coerce_extraction(
        [atom], CanonicalClaim(sentence="x", scope={}), []
    )
    assert result.compound is None
    assert result.atoms == (atom,)


def test_coerce_extraction_lone_atom_with_reject_keeps_compound() -> None:
    atom = CanonicalClaim(sentence="x", scope={})
    compound = CanonicalClaim(sentence="bundle", scope={})
    nc = NotClaim(text="y", reason="vague")
    result = canon._coerce_extraction([atom], compound, [nc])
    assert result.compound is compound
    assert result.not_claims == (nc,)


def test_coerce_extraction_multi_atom_keeps_compound() -> None:
    a1 = CanonicalClaim(sentence="x", scope={})
    a2 = CanonicalClaim(sentence="y", scope={})
    compound = CanonicalClaim(sentence="bundle", scope={})
    result = canon._coerce_extraction([a1, a2], compound, [])
    assert result.compound is compound
    assert result.atoms == (a1, a2)


def test_coerce_extraction_multi_atom_without_compound_degrades_to_empty() -> None:
    """Partial-citation guard: 2+ atoms with no compound is a contract
    violation (backfill's prose rewrite would cite only atoms[0]) — degrade
    to NO-CLAIM so the chunk is retried whole rather than partially cited."""
    a1 = CanonicalClaim(sentence="x", scope={})
    a2 = CanonicalClaim(sentence="y", scope={})
    result = canon._coerce_extraction([a1, a2], None, [])
    assert result.is_empty
    assert result == ClaimExtraction(atoms=(), compound=None, not_claims=())


# ── dedup_judge / merge_confirm — bias-safe degrade ─────────────────────


def test_dedup_judge_parses_a_same_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={"verdict": "same", "confidence": 0.92, "rationale": "identical fact"}
        ),
    )
    v = dedup_judge("claim A", "claim B")
    assert v == {"verdict": "same", "confidence": 0.92, "rationale": "identical fact"}


def test_dedup_judge_degrades_to_different_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="boom"))
    v = dedup_judge("claim A", "claim B")
    assert v["verdict"] == "different"
    assert v["confidence"] == 0.0


def test_dedup_judge_degrades_to_different_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canon, "dispatch", lambda req: _result(data=None, text="prose, no json")
    )
    v = dedup_judge("claim A", "claim B")
    assert v["verdict"] == "different"
    assert v["confidence"] == 0.0


def test_dedup_judge_rejects_unrecognized_verdict_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed verdict (typo / hallucinated value) never silently
    becomes "same" — it degrades to "different"."""
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(data={"verdict": "kinda-same", "confidence": 0.99}),
    )
    v = dedup_judge("claim A", "claim B")
    assert v["verdict"] == "different"


def test_dedup_judge_clamps_confidence_to_unit_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(data={"verdict": "same", "confidence": 5.0}),
    )
    v = dedup_judge("a", "b")
    assert v["confidence"] == 1.0


def test_merge_confirm_degrades_to_different_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="down"))
    v = merge_confirm("a", "b")
    assert v["verdict"] == "different"
    assert v["confidence"] == 0.0


# ── place — deterministic branching ─────────────────────────────────────

_CLAIM = CanonicalClaim(sentence="MOFs have tunable pore geometry", scope={})
_CAND = Candidate(
    hub_ref_id=101, claim="MOFs have tunable pore geometry", distance=0.01
)
_CAND2 = Candidate(hub_ref_id=202, claim="unrelated claim", distance=0.5)


def test_place_attaches_on_high_confidence_same() -> None:
    judged = [(_CAND, _verdict("same", 0.95, "identical"))]
    result = place(_CLAIM, judged)
    assert result == Placement(
        action="attach", hub_ref_id=101, reason="confirmed same (high confidence)"
    )


def test_place_picks_the_first_high_confidence_same_when_several_match() -> None:
    """``judged`` is expected in block()'s distance order — first match wins."""
    judged = [
        (_CAND, _verdict("same", 0.9)),
        (_CAND2, _verdict("same", 0.99)),
    ]
    result = place(_CLAIM, judged)
    assert result.action == "attach"
    assert result.hub_ref_id == 101


def test_place_escalates_low_confidence_same_and_attaches_on_confirm() -> None:
    judged = [(_CAND, _verdict("same", 0.4, "unsure"))]

    def fake_confirm(a: str, b: str) -> Verdict:
        return _verdict("same", 0.9, "confirmed on review")

    result = place(_CLAIM, judged, merge_confirm_fn=fake_confirm)
    assert result == Placement(
        action="attach", hub_ref_id=101, reason="merge-confirmed: confirmed on review"
    )


def test_place_low_confidence_same_not_confirmed_needs_review() -> None:
    """Design #16: a risky merge that BIG doesn't confidently confirm is
    NOT auto-applied — it comes back needs_review, never attach or new."""
    judged = [(_CAND, _verdict("same", 0.4, "unsure"))]

    def fake_confirm(a: str, b: str) -> Verdict:
        return _verdict("different", 0.8, "actually distinct on closer look")

    result = place(_CLAIM, judged, merge_confirm_fn=fake_confirm)
    assert result.action == "needs_review"
    assert result.hub_ref_id == 101  # carries the candidate for the todo


def test_place_low_confidence_same_confirm_also_uncertain_needs_review() -> None:
    """merge_confirm agreeing "same" but still below threshold also
    doesn't auto-apply — still needs_review, not attach."""
    judged = [(_CAND, _verdict("same", 0.4))]

    def fake_confirm(a: str, b: str) -> Verdict:
        return _verdict("same", 0.5, "still not sure")

    result = place(_CLAIM, judged, merge_confirm_fn=fake_confirm)
    assert result.action == "needs_review"


def test_place_new_contradicts_on_contradiction() -> None:
    judged = [(_CAND, _verdict("contradicts", 0.8, "opposite polarity"))]
    result = place(_CLAIM, judged)
    assert result == Placement(
        action="new_contradicts",
        contradicts_hub_ref_id=101,
        reason="opposite polarity",
    )


def test_place_same_takes_priority_over_contradicts() -> None:
    judged = [
        (_CAND, _verdict("contradicts", 0.9)),
        (_CAND2, _verdict("same", 0.95)),
    ]
    result = place(_CLAIM, judged)
    assert result.action == "attach"
    assert result.hub_ref_id == 202


def test_place_new_on_all_different() -> None:
    judged = [
        (_CAND, _verdict("different", 0.9)),
        (_CAND2, _verdict("different", 0.7)),
    ]
    result = place(_CLAIM, judged)
    assert result.action == "new"


def test_place_new_on_no_candidates() -> None:
    result = place(_CLAIM, [])
    assert result == Placement(
        action="new", reason="no matching or contradicting candidate"
    )


# ── eval harness — label-collapse mapping ───────────────────────────────


@pytest.mark.parametrize(
    "relation,expected",
    [
        ("equivalent", "same"),
        ("broader", "different"),
        ("narrower", "different"),
        ("orthogonal", "different"),
        ("contradicts", "contradicts"),
    ],
)
def test_collapse_label_maps_fixture_relations(relation: str, expected: str) -> None:
    assert collapse_label(relation) == expected


def test_collapse_label_rejects_unknown_relation() -> None:
    with pytest.raises(ValueError, match="unrecognized fixture relation"):
        collapse_label("subsumes")


def test_eval_canonicalization_scores_a_perfect_judge(tmp_path: Any) -> None:
    fixture = tmp_path / "pairs.jsonl"
    fixture.write_text(
        '{"pair_id": 1, "claim_a": "A", "claim_b": "B", "relation": "equivalent"}\n'
        '{"pair_id": 2, "claim_a": "C", "claim_b": "D", "relation": "orthogonal"}\n'
        '{"pair_id": 3, "claim_a": "E", "claim_b": "F", "relation": "contradicts"}\n'
    )
    # A "perfect" stub judge: pair 1 -> same, pair 2 -> different, pair 3 -> contradicts.
    answers: dict[int, canon.Verdict3] = {1: "same", 2: "different", 3: "contradicts"}
    calls: list[tuple[str, str]] = []

    def stub_judge(a: str, b: str) -> Verdict:
        calls.append((a, b))
        idx = len(calls)
        return _verdict(answers[idx], 0.9)

    report = eval_canonicalization(fixture, dedup_judge_fn=stub_judge, progress=False)
    assert isinstance(report, Report)
    assert report.total == 3
    assert report.over_merges == []
    assert report.under_merges == []
    assert calls == [("A", "B"), ("C", "D"), ("E", "F")]


def test_eval_canonicalization_flags_an_over_merge(tmp_path: Any) -> None:
    fixture = tmp_path / "pairs.jsonl"
    fixture.write_text(
        '{"pair_id": 1, "claim_a": "A", "claim_b": "B", "relation": "orthogonal"}\n'
    )

    def bad_judge(a: str, b: str) -> Verdict:
        return _verdict("same", 0.9)  # wrong — should be "different"

    report = eval_canonicalization(fixture, dedup_judge_fn=bad_judge, progress=False)
    assert len(report.over_merges) == 1
    assert report.over_merges[0].pair_id == 1
    assert report.over_merge_rate == 1.0


def test_eval_canonicalization_flags_an_under_merge_as_tolerated(tmp_path: Any) -> None:
    fixture = tmp_path / "pairs.jsonl"
    fixture.write_text(
        '{"pair_id": 1, "claim_a": "A", "claim_b": "B", "relation": "equivalent"}\n'
    )

    def cautious_judge(a: str, b: str) -> Verdict:
        return _verdict("different", 0.9)  # under-merge — safe direction

    report = eval_canonicalization(
        fixture, dedup_judge_fn=cautious_judge, progress=False
    )
    assert report.over_merges == []
    assert len(report.under_merges) == 1


def test_report_format_renders_confusion_and_rates(tmp_path: Any) -> None:
    fixture = tmp_path / "pairs.jsonl"
    fixture.write_text(
        '{"pair_id": 1, "claim_a": "A", "claim_b": "B", "relation": "orthogonal"}\n'
    )
    report = eval_canonicalization(
        fixture, dedup_judge_fn=lambda a, b: _verdict("different", 0.9), progress=False
    )
    text = report.format()
    assert "over-merge" in text
    assert "under-merge" in text
    assert "1 pairs" in text


# ── eval_extraction — the AIDA-Atomic gate (offline, stub extractor) ────

_ATOM_A = CanonicalClaim(sentence="A", scope={})
_ATOM_B = CanonicalClaim(sentence="B", scope={})


def test_eval_extraction_scores_a_perfect_extractor(tmp_path: Any) -> None:
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 1, "expected_not_claims": []}\n'
        '{"id": 2, "passage": "p2", "expected_atom_count": 0, "expected_not_claims": []}\n'
    )
    answers: dict[int, ClaimExtraction] = {
        1: ClaimExtraction(atoms=(_ATOM_A,), compound=None, not_claims=()),
        2: ClaimExtraction(atoms=(), compound=None, not_claims=()),
    }
    calls: list[str] = []

    def stub_extract(text: str) -> ClaimExtraction:
        calls.append(text)
        return answers[len(calls)]

    report = eval_extraction(fixture, extract_fn=stub_extract, progress=False)
    assert isinstance(report, ExtractionReport)
    assert report.total == 2
    assert report.atom_count_matches == 2
    assert report.atom_count_agreement_rate == 1.0
    assert report.compound_without_atoms_violations == []
    assert report.conjunction_violations == []
    assert calls == ["p1", "p2"]


def test_eval_extraction_flags_a_compound_without_atoms_violation(
    tmp_path: Any,
) -> None:
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 0, "expected_not_claims": []}\n'
    )
    # A misbehaving stub that bypasses _coerce_extraction's invariant —
    # eval_extraction's hard gate should still catch it.
    bad = ClaimExtraction(
        atoms=(), compound=CanonicalClaim(sentence="bundle", scope={}), not_claims=()
    )
    report = eval_extraction(fixture, extract_fn=lambda t: bad, progress=False)
    assert len(report.compound_without_atoms_violations) == 1
    assert report.compound_without_atoms_violations[0].passage_id == 1


def test_eval_extraction_flags_a_residual_conjunction_atom(tmp_path: Any) -> None:
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 1, "expected_not_claims": []}\n'
    )
    unsplit = ClaimExtraction(
        atoms=(
            CanonicalClaim(
                sentence="X does A as well as B under the same conditions.",
                scope={},
            ),
        ),
        compound=None,
        not_claims=(),
    )
    report = eval_extraction(fixture, extract_fn=lambda t: unsplit, progress=False)
    assert len(report.conjunction_violations) == 1
    assert report.conjunction_violations[0].conjunction_atoms == [
        unsplit.atoms[0].sentence
    ]


def test_eval_extraction_bare_and_condition_list_is_not_flagged(tmp_path: Any) -> None:
    """Regression guard for the lexical heuristic: a legitimate condition
    list joined by a bare "and" must not be treated as an un-split atom."""
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 1, "expected_not_claims": []}\n'
    )
    clean = ClaimExtraction(
        atoms=(
            CanonicalClaim(
                sentence="The reaction ran at 300 K and 1 atm, giving 92% yield.",
                scope={},
            ),
        ),
        compound=None,
        not_claims=(),
    )
    report = eval_extraction(fixture, extract_fn=lambda t: clean, progress=False)
    assert report.conjunction_violations == []


def test_eval_extraction_disagreement_is_soft_not_a_violation(tmp_path: Any) -> None:
    """A different atom count than expected is tallied (soft metric) but
    never counted among either hard gate's violations."""
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 2, "expected_not_claims": []}\n'
    )
    one_atom = ClaimExtraction(atoms=(_ATOM_A,), compound=None, not_claims=())
    report = eval_extraction(fixture, extract_fn=lambda t: one_atom, progress=False)
    assert report.atom_count_matches == 0
    assert report.atom_count_agreement_rate == 0.0
    assert report.compound_without_atoms_violations == []
    assert report.conjunction_violations == []


def test_extraction_report_format_renders_gates_and_metric(tmp_path: Any) -> None:
    fixture = tmp_path / "passages.jsonl"
    fixture.write_text(
        '{"id": 1, "passage": "p1", "expected_atom_count": 1, "expected_not_claims": []}\n'
    )
    one_atom = ClaimExtraction(atoms=(_ATOM_A,), compound=None, not_claims=())
    report = eval_extraction(fixture, extract_fn=lambda t: one_atom, progress=False)
    text = report.format()
    assert "atom-count agreement" in text
    assert "compound-without-atoms" in text
    assert "residual conjunction" in text
    assert "1 passages" in text
