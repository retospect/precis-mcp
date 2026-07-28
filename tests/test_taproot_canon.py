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
    Placement,
    Verdict,
    dedup_judge,
    extract_claim,
    merge_confirm,
    place,
)
from precis.taproot.eval_canon import Report, collapse_label, eval_canonicalization


def _result(
    *, data: dict[str, Any] | None = None, text: str = "", error: str | None = None
) -> Any:
    """A stand-in for ``LlmResult`` — dispatch() callers only read
    ``.error``, ``.data``, and ``.text``."""
    return SimpleNamespace(text=text, data=data, error=error)


def _verdict(v: canon.Verdict3, confidence: float, rationale: str = "") -> Verdict:
    return Verdict(verdict=v, confidence=confidence, rationale=rationale)


# ── extract_claim — NO-CLAIM detection ──────────────────────────────────


def test_extract_claim_returns_canonical_claim_on_a_real_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canon,
        "dispatch",
        lambda req: _result(
            data={
                "claim": "Pd/C catalyzes Suzuki coupling at RT with mild base",
                "method": "Suzuki coupling",
                "regime": "RT",
            }
        ),
    )
    result = extract_claim("Pd/C catalyzes Suzuki coupling at room temperature...")
    assert result == CanonicalClaim(
        sentence="Pd/C catalyzes Suzuki coupling at RT with mild base",
        scope={"method": "Suzuki coupling", "regime": "RT"},
    )


def test_extract_claim_returns_none_on_pure_pointer_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "See [12]" / Related-Work chunk -> the model says claim=null ->
    NO-CLAIM (taproot.md Axis A stage 0')."""
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(data={"claim": None}))
    assert extract_claim("As shown in prior work [12], ...") is None


def test_extract_claim_returns_none_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "dispatch", lambda req: _result(error="transport down"))
    assert extract_claim("some passage") is None


def test_extract_claim_returns_none_on_empty_input() -> None:
    assert extract_claim("") is None
    assert extract_claim("   ") is None


def test_extract_claim_returns_none_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canon, "dispatch", lambda req: _result(data=None, text="not json at all")
    )
    assert extract_claim("some passage") is None


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

    report = eval_canonicalization(fixture, dedup_judge_fn=stub_judge)
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

    report = eval_canonicalization(fixture, dedup_judge_fn=bad_judge)
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

    report = eval_canonicalization(fixture, dedup_judge_fn=cautious_judge)
    assert report.over_merges == []
    assert len(report.under_merges) == 1


def test_report_format_renders_confusion_and_rates(tmp_path: Any) -> None:
    fixture = tmp_path / "pairs.jsonl"
    fixture.write_text(
        '{"pair_id": 1, "claim_a": "A", "claim_b": "B", "relation": "orthogonal"}\n'
    )
    report = eval_canonicalization(
        fixture, dedup_judge_fn=lambda a, b: _verdict("different", 0.9)
    )
    text = report.format()
    assert "over-merge" in text
    assert "under-merge" in text
    assert "1 pairs" in text
