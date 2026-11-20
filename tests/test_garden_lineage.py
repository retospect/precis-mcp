"""Premise lineage for grounding audits (gr634486 slice 1).

Pure checks over a claim set: support cycles ground nothing, shared
roots are not independent corroboration, one unsupported premise cannot
hide behind one sourced premise, and deep inference chains flag for
source review without losing their verdict.
"""

from __future__ import annotations

from typing import Any

from precis.graph.garden import lineage


def _claim(claim_id: str, **kw: Any) -> lineage.LineageClaim:
    base: dict[str, Any] = {"kind": "empirical", "status": "supported"}
    base.update(kw)
    return lineage.LineageClaim(claim_id=claim_id, **base)


def test_cycle_not_grounded() -> None:
    claims = [
        _claim("A", premises=["B"], evidence=["s1"]),
        _claim("B", kind="inference", premises=["A"], evidence=["s1"]),
    ]
    report = lineage.analyze_lineage(claims)
    assert report.cycles == [["A", "B"]]
    assert report.verdicts["A"].verdict == "unsupported"
    assert report.verdicts["B"].verdict == "unsupported"
    assert "support cycle" in report.verdicts["A"].reasons[0]
    assert "support cycle" in report.verdicts["B"].reasons[0]


def test_two_claims_sharing_one_source_are_not_independent() -> None:
    claims = [
        _claim("A", evidence=["s1"]),
        _claim("B", premises=["A"], evidence=["s1"]),
    ]
    report = lineage.analyze_lineage(claims)
    assert report.shared_sources == {"s1": ["A", "B"]}
    assert report.verdicts["A"].verdict == "supported"
    verdict_b = report.verdicts["B"]
    assert verdict_b.verdict == "partial"
    assert verdict_b.independent_evidence == []
    assert any("independent" in reason for reason in verdict_b.reasons)


def test_one_unsupported_premise_hidden_by_sourced_one_is_partial() -> None:
    claims = [
        _claim("A", evidence=["s1"]),
        _claim("B", status="unsupported"),
        _claim("C", kind="inference", premises=["A", "B"], evidence=["s2"]),
    ]
    report = lineage.analyze_lineage(claims)
    verdict_c = report.verdicts["C"]
    assert verdict_c.verdict == "partial"
    assert any("B" in reason for reason in verdict_c.reasons)
    assert report.verdicts["A"].verdict == "supported"
    assert report.verdicts["B"].verdict == "unsupported"


def test_multi_step_supported_inference_stays_supported() -> None:
    claims = [
        _claim("E", evidence=["s1"]),
        _claim("I1", kind="inference", premises=["E"]),
        _claim("I2", kind="inference", premises=["I1"]),
    ]
    report = lineage.analyze_lineage(claims)
    verdict = report.verdicts["I2"]
    assert verdict.verdict == "supported"
    assert verdict.inference_depth == 2
    assert not verdict.needs_source_review


def test_deep_inference_chain_needs_source_review() -> None:
    claims = [_claim("E", evidence=["s1"])]
    prior = "E"
    for i in range(1, lineage.INFERENCE_REVIEW_DEPTH + 1):
        claims.append(
            _claim(f"I{i}", kind="inference", premises=[prior], status="supported")
        )
        prior = f"I{i}"
    report = lineage.analyze_lineage(claims)
    deepest = report.verdicts[f"I{lineage.INFERENCE_REVIEW_DEPTH}"]
    assert deepest.verdict == "supported"
    assert deepest.needs_source_review
    assert deepest.inference_depth == lineage.INFERENCE_REVIEW_DEPTH


def test_definition_premise_never_supports_existence() -> None:
    claims = [
        _claim(
            "D",
            kind="definition",
            evidence=["s1"],
            status="provisional",
        ),
        _claim("X", kind="empirical", premises=["D"], evidence=["s2"]),
    ]
    report = lineage.analyze_lineage(claims)
    verdict_x = report.verdicts["X"]
    assert verdict_x.verdict == "partial"
    assert any("definition" in reason for reason in verdict_x.reasons)


def test_unresolvable_premise_is_recorded() -> None:
    claims = [_claim("A", premises=["me999"], evidence=["s1"])]
    report = lineage.analyze_lineage(claims, resolve_source=lambda _h: False)
    assert report.verdicts["A"].verdict == "partial"
    assert report.unresolved_premises == {"A": ["me999"]}


def test_stale_source_sha_detected() -> None:
    claims = [_claim("A", evidence=["pc1"])]
    report = lineage.analyze_lineage(
        claims,
        source_sha=lambda h: {"pc1": "sha-now"}.get(h),
        audited_sha={"pc1": "sha-then"},
    )
    assert report.stale_sources == ["pc1"]
    fresh = lineage.analyze_lineage(
        claims,
        source_sha=lambda h: {"pc1": "sha-now"}.get(h),
        audited_sha={"pc1": "sha-now"},
    )
    assert fresh.stale_sources == []


def test_stale_source_caps_supported_and_flags_review() -> None:
    claims = [_claim("A", evidence=["pc1"])]
    report = lineage.analyze_lineage(
        claims,
        source_sha=lambda h: {"pc1": "sha-now"}.get(h),
        audited_sha={"pc1": "sha-then"},
    )
    verdict = report.verdicts["A"]
    assert verdict.verdict == "partial"
    assert verdict.stale_evidence == ["pc1"]
    assert verdict.needs_source_review


def test_external_premise_resolves_to_its_current_judgment() -> None:
    supported = lineage.ClaimLineageVerdict(lineage.VERDICT_SUPPORTED)
    partial = lineage.ClaimLineageVerdict(
        lineage.VERDICT_PARTIAL, reasons=["premise me2 audit rollup: partial"]
    )
    unresolved = lineage.ClaimLineageVerdict(
        lineage.VERDICT_UNRESOLVED, reasons=["no current audit judgment"]
    )
    claims = [
        _claim("A", kind="inference", premises=["me1"]),
        _claim("B", kind="inference", premises=["me2"]),
        _claim("C", kind="inference", premises=["me3"]),
    ]
    report = lineage.analyze_lineage(
        claims,
        premise_verdict=lambda h: {
            "me1": supported,
            "me2": partial,
            "me3": unresolved,
        }.get(h),
    )
    assert report.verdicts["A"].verdict == "supported"
    verdict_b = report.verdicts["B"]
    assert verdict_b.verdict == "partial"
    assert any(
        "partially supported" in r or "rollup: partial" in r for r in verdict_b.reasons
    )
    verdict_c = report.verdicts["C"]
    assert verdict_c.verdict == "unresolved"
    assert report.unresolved_premises == {"C": ["me3"]}
    # Resolving a live citation supplies no judgment.
    both = lineage.analyze_lineage(
        [_claim("D", kind="inference", premises=["me9"])],
        premise_verdict=lambda _h: None,
        resolve_source=lambda _h: True,
    )
    assert both.verdicts["D"].verdict == "unresolved"
    assert both.unresolved_premises == {"D": ["me9"]}


def test_chunk_handles_of_one_paper_are_one_shared_source() -> None:
    # Two chunk handles of the same paper are one root: shared lineage,
    # not independent corroboration.
    claims = [
        _claim("A", evidence=["pc1"]),
        _claim("B", premises=["A"], evidence=["pc2"]),
    ]
    report = lineage.analyze_lineage(claims, source_key=lambda h: "paper:1")
    assert report.shared_sources == {"paper:1": ["A", "B"]}
    assert report.verdicts["B"].verdict == "partial"
    assert report.verdicts["B"].independent_evidence == []
