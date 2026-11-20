"""Bounded conflict probe for the grounding audit (gr634486 slice 3):
the conflict-search negate+verify surface over an explicit paper
budget, with ``none_found`` recorded as a scoped statement — never
proof of support. All seams are stubs; no LLM, no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from precis.graph.garden import grounding as g
from precis.taproot.reground import PaperChunk

CLAIM = "The catalyst doubles the yield under standard conditions"
NOW = datetime(2026, 11, 18, tzinfo=UTC)


def _negate(*_args: Any) -> dict[str, Any]:
    return {"paraphrases": ["The catalyst does not double the yield"]}


def _candidates(*chunks: PaperChunk) -> g.ProbeSearchFn:
    return lambda _query: list(chunks)


C1 = PaperChunk(
    chunk_id=101, chunk_ord=0, section_path=None, text="opposing passage one"
)
C2 = PaperChunk(
    chunk_id=102, chunk_ord=1, section_path=None, text="opposing passage two"
)


def test_no_conflict_found_is_not_supported() -> None:
    checked: list[int] = []

    def verify(chunk: PaperChunk) -> dict[str, Any]:
        checked.append(chunk.chunk_id)
        return {"contradicts": False}

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=verify,
        now=NOW,
    )
    assert probe.outcome == "none_found"
    assert probe.checked == 2 and checked == [101, 102]
    assert probe.scope["at"] == NOW.isoformat()
    assert probe.scope["queries"] == [CLAIM, "The catalyst does not double the yield"]
    assert probe.scope["version"] == g.CONFLICT_PROBE_VERSION
    # The load-bearing half: a none_found probe never upgrades a claim.
    claim = g.ClaimRecord(
        span=[0, 1], text=CLAIM, kind="empirical", status="unresolved"
    )
    verdict, _reason = g.rollup_claim(claim, support=None, probe=probe)
    assert verdict == "unresolved"
    assert verdict != "supported"


def test_contradicted_probe_rolls_claim_up_to_contradicted() -> None:
    def verify(chunk: PaperChunk) -> dict[str, Any]:
        return {"contradicts": chunk.chunk_id == 102}

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=verify,
        now=NOW,
    )
    assert probe.outcome == "contradicted"
    assert probe.checked == 2  # the budget was spent finding the hit
    claim = g.ClaimRecord(span=[0, 1], text=CLAIM, kind="empirical", status="supported")
    verdict, reason = g.rollup_claim(claim, support=None, probe=probe)
    assert verdict == "contradicted"
    assert "contradicts" in reason


def test_negate_failure_is_unknown_never_none_found() -> None:
    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=lambda *_args: None,
        search_fn=_candidates(C1),
        verify_fn=lambda _c: {"contradicts": False},
        now=NOW,
    )
    assert probe.outcome == "unknown"
    assert probe.unknowns == ["negate unavailable"]
    assert probe.checked == 0


def test_verify_failures_are_recorded_as_unknowns() -> None:
    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=lambda _c: None,
        now=NOW,
    )
    # Every verification failed — the probe knows nothing; none_found
    # after a total verify outage would be a false negative.
    assert probe.outcome == "unknown"
    assert probe.checked == 2
    assert probe.unknowns == ["verify unavailable", "verify unavailable"]
    assert [p["verdict"] for p in probe.checked_passages] == [
        "verify_unavailable",
        "verify_unavailable",
    ]


def test_partial_verify_failure_still_allows_none_found() -> None:
    checked: list[int] = []

    def verify(chunk: PaperChunk) -> dict[str, Any] | None:
        checked.append(chunk.chunk_id)
        return None if chunk.chunk_id == 101 else {"contradicts": False}

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=verify,
        now=NOW,
    )
    assert probe.outcome == "none_found"
    assert probe.unknowns == ["verify unavailable"]
    assert [p["verdict"] for p in probe.checked_passages] == [
        "verify_unavailable",
        "no-conflict",
    ]


def test_total_retrieval_failure_is_unknown_never_none_found() -> None:
    def search_raises(_query: str) -> list[PaperChunk]:
        raise RuntimeError("embedder down")

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=search_raises,
        verify_fn=lambda _c: {"contradicts": False},
        now=NOW,
    )
    assert probe.outcome == "unknown"
    assert probe.unknowns == ["retrieval unavailable"]
    assert probe.checked == 0


def test_checked_passage_identities_are_preserved() -> None:
    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=lambda c: {"contradicts": c.chunk_id == 102},
        now=NOW,
    )
    assert probe.outcome == "contradicted"
    assert probe.checked_passages == [
        {"chunk_id": 101, "chunk_ord": 0, "verdict": "no-conflict"},
        {"chunk_id": 102, "chunk_ord": 1, "verdict": "disputes"},
    ]


def test_rank_fn_selects_which_candidates_spend_the_budget() -> None:
    checked: list[int] = []

    def verify(chunk: PaperChunk) -> dict[str, Any]:
        checked.append(chunk.chunk_id)
        return {"contradicts": False}

    # The live path's rank seam reuses the conflict sweep's ranking; a
    # ranking that prefers chunk 102 must decide which candidate the
    # single budget slot verifies.
    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=verify,
        budget=1,
        now=NOW,
        rank_fn=lambda chunks: [chunks[1], chunks[0]],
    )
    assert probe.outcome == "none_found"
    assert checked == [102]
    assert [p["chunk_id"] for p in probe.checked_passages] == [102]


def test_empty_candidate_pool_is_none_found_with_unknown() -> None:
    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(),
        verify_fn=lambda _c: {"contradicts": False},
        now=NOW,
    )
    assert probe.outcome == "none_found"
    assert probe.checked == 0
    assert probe.unknowns == ["no candidates retrieved"]


def test_budget_bounds_the_verify_spend() -> None:
    checked: list[int] = []

    def verify(chunk: PaperChunk) -> dict[str, Any]:
        checked.append(chunk.chunk_id)
        return {"contradicts": False}

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=_candidates(C1, C2),
        verify_fn=verify,
        budget=1,
        now=NOW,
    )
    assert probe.checked == 1 and checked == [101]
    assert probe.budget == 1
    assert probe.outcome == "none_found"


def test_retrieval_seams_run_per_query() -> None:
    queries: list[str] = []

    def search(q: str) -> list[PaperChunk]:
        queries.append(q)
        return [C1]

    probe = g.probe_claim_conflicts(
        CLAIM,
        negate_fn=_negate,
        search_fn=search,
        verify_fn=lambda _c: {"contradicts": False},
        now=NOW,
    )
    assert probe.outcome == "none_found"
    assert queries == [CLAIM, "The catalyst does not double the yield"]
