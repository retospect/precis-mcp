"""The method-gap arm of ``hub_refine`` (claims-and-evidence thread, grounding
build 2 D1): a hub whose evidence passages lack a term its sentence names gets
that term searched in its evidence papers' chunks (and SI), the best chunks
judged by the widen verifier and attached when they verify. The verifier is a
counting mock, as in ``test_hub_refine.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from precis.nanopub import method_gap
from precis.nanopub.term_coverage import PaperChunk
from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import claim_sha
from precis.taproot.hub import attach_evidence
from precis.workers import hub_refine
from precis.workers.hub_refine import (
    RegroundConfig,
    StrictVerdict,
    run_hub_refine_pass,
)
from tests.workers._helpers import make_mock_bge_m3
from tests.workers.test_hub_refine import (
    _VERIFY_NO,
    _VERIFY_PATH,
    _VERIFY_YES,
    _hub_meta,
    _seed_hub,
    _seed_paper_chunk,
)

_PAPER_CHUNKS_PATH = "precis.workers.hub_refine.paper_chunks"
_ARM_PATH = "precis.workers.hub_refine._method_gap_arm"
_SENTENCE = "TEM shows the crystals are anisotropic."
_PASSAGE = "The crystals are anisotropic."  # names no method


def _add_chunks(
    store: Any, ref_id: int, texts: list[tuple[int, str]]
) -> dict[int, int]:
    """Insert ``(ord, text)`` body chunks; returns ``{ord: chunk_id}``."""
    store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=o, text=t, meta={}) for o, t in texts]
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_id FROM chunks WHERE ref_id = %s", (ref_id,)
        ).fetchall()
    return {int(o): int(c) for o, c in rows}


def _attach_verified(store: Any, hub: int, paper: int, chunk_id: int) -> None:
    """Attach a passage already carrying a current verdict, so the
    publish-gate re-verify arm has nothing to judge and every verifier call a
    test counts belongs to the method-gap arm."""
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={
            "source_handle": f"pc{chunk_id}",
            "support": "yes",
            "support_reason": "seeded",
            "caveats": [],
            "verified_by": "hub-refine",
            "verified_at": datetime.now(UTC).isoformat(),
            "verified_claim_sha": claim_sha(_SENTENCE),
        },
        check_retraction=False,
    )


def _gap_hub(store: Any, embedder: Any, extra: list[tuple[int, str]]) -> dict[str, Any]:
    """A hub grounded on one passage of a paper whose other chunks are
    ``extra``; returns ids."""
    hub = _seed_hub(store, sentence=_SENTENCE)
    paper, passage_chunk = _seed_paper_chunk(
        store, embedder, cite_key="gap", text=_PASSAGE
    )
    chunks = _add_chunks(store, paper, extra)
    _attach_verified(store, hub, paper, passage_chunk)
    return {"hub": hub, "paper": paper, "passage": passage_chunk, "chunks": chunks}


def _gap_calls(mock: Any) -> list[Any]:
    """Verifier calls that targeted a chunk other than the seeded passage
    (``ord`` 0) — i.e. the method-gap arm's."""
    return [c for c in mock.call_args_list if c.kwargs["target_chunk_ord"] > 0]


def _links(store: Any, hub: int) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_chunk_id, relation, meta FROM links WHERE dst_ref_id = %s "
            "AND src_chunk_id IS NOT NULL ORDER BY src_chunk_id",
            (hub,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def test_caption_with_the_missing_term_is_judged_and_attached(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(
        store,
        embedder,
        [(1, "Figure 3 TEM image of the crystals along the [001] axis.")],
    )
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    calls = _gap_calls(mock_verify)
    assert len(calls) == 1
    call = calls[0].kwargs
    assert "TEM image" in call["target_chunk_text"]
    # Full workspace, like the widen arm: identity + the claim's own source
    # passage reach the verifier (section/neighbours are the same helpers).
    assert call["source_identity"].title == "Test paper gap"
    assert call["neighbours"] == [_PASSAGE]
    assert call["with_request_hash"] is True

    caption = g["chunks"][1]
    links = {cid: (rel, meta) for cid, rel, meta in _links(store, g["hub"])}
    assert set(links) == {g["passage"], caption}
    relation, meta = links[caption]
    assert relation == "corroborates"
    assert meta["support"] == "yes"
    assert meta["source_handle"] == f"pc{caption}"
    assert meta["verified_by"] == "hub-refine"
    assert meta["verified_claim_sha"] == claim_sha(_SENTENCE)
    assert meta["widen"]["via"] == "method-gap"
    assert meta["widen"]["terms"] == ["TEM"]


def test_rejected_chunk_is_memoed_and_never_rejudged(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(store, embedder, [(1, "Figure 3 TEM image of the crystals.")])
    from precis.store.types import Tag

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert len(_gap_calls(mock_verify)) == 1
        # Force a second pass (the trigger pass's re-mark): nothing new to judge.
        store.add_tag(g["hub"], Tag.closed("TAPROOT_DUE", "1"), set_by="system")
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert len(_gap_calls(mock_verify)) == 1

    # Passage-grained memo, never the source-grained rejection (which would
    # evict a paper the hub is already grounded on).
    meta = _hub_meta(store, g["hub"])
    key = f"{g['paper']}:{g['chunks'][1]}"
    assert meta["reground_seen"][key]["via"] == "method-gap"
    assert meta["reground_seen"][key]["terms"] == ["TEM"]
    assert str(g["paper"]) not in (meta.get("taproot_rejected") or {})
    assert [cid for cid, _r, _m in _links(store, g["hub"])] == [g["passage"]]


def test_per_term_cap_judges_one_of_many(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(
        store,
        embedder,
        [(o, f"Figure {o} TEM micrograph number {o}.") for o in range(1, 7)],
    )
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert len(_gap_calls(mock_verify)) == method_gap.PER_TERM == 1
    assert len(_hub_meta(store, g["hub"])["reground_seen"]) == 1


def test_covered_claim_makes_no_llm_call(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence=_SENTENCE)
    paper, chunk = _seed_paper_chunk(
        store, embedder, cite_key="cov", text="TEM shows anisotropic crystals."
    )
    _add_chunks(store, paper, [(1, "Figure 3 TEM image of the crystals.")])
    _attach_verified(store, hub, paper, chunk)
    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify,
        patch(_PAPER_CHUNKS_PATH, wraps=hub_refine.paper_chunks) as spy,
    ):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert mock_verify.call_count == 0
    # The cheap pre-check returned before any chunk was loaded.
    assert spy.call_count == 0


def test_number_only_gap_makes_no_llm_call(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The crystals are 400 nm wide.")
    paper, chunk = _seed_paper_chunk(
        store, embedder, cite_key="num", text="The crystals are wide."
    )
    _add_chunks(store, paper, [(1, "The crystals measure 400 nm across.")])
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={
            "source_handle": f"pc{chunk}",
            "support": "yes",
            "support_reason": "seeded",
            "caveats": [],
            "verified_by": "hub-refine",
            "verified_at": datetime.now(UTC).isoformat(),
            "verified_claim_sha": claim_sha("The crystals are 400 nm wide."),
        },
        check_retraction=False,
    )
    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify,
        patch(_PAPER_CHUNKS_PATH, wraps=hub_refine.paper_chunks) as spy,
    ):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert mock_verify.call_count == 0
    assert spy.call_count == 0


def test_si_chunk_of_an_evidence_paper_is_not_searched_in_v1(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(store, embedder, [])
    si = store.insert_ref(
        kind="paper", slug="gap-si", title="Supporting information", meta={}
    )
    si_chunks = _add_chunks(store, si.id, [(1, "Figure S2 TEM image of the crystals.")])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET pdf_role = 'supplement' WHERE ref_id = %s", (si.id,)
        )
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, meta, set_by) "
            "VALUES (%s, %s, 'part-of', '{\"role\": \"supplement\"}', 'system')",
            (si.id, g["paper"]),
        )
        conn.commit()
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert mock_verify.call_count == 0
    attached = {cid for cid, _r, _m in _links(store, g["hub"])}
    assert si_chunks[1] not in attached


def _due(store: Any, hub: int) -> None:
    store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")


def test_two_attempts_per_term_per_claim_version(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(
        store,
        embedder,
        [(o, f"Figure {o} TEM micrograph number {o}.") for o in range(1, 5)],
    )
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        counts = []
        for _ in range(3):
            run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
            counts.append(len(_gap_calls(mock_verify)))
            _due(store, g["hub"])
    assert counts == [1, 2, 2]  # a third pass for the same term makes no call
    memos = _hub_meta(store, g["hub"])["reground_seen"]
    assert len(memos) == 2
    assert {m["verdict"] for m in memos.values()} == {"NO-CORROBORATION"}


def test_persistent_none_verdict_stops_after_two_attempts(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(store, embedder, [(1, "Figure 3 TEM image of the crystals.")])
    with patch(_VERIFY_PATH, return_value=None) as mock_verify:
        counts = []
        for _ in range(4):
            run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
            counts.append(len(_gap_calls(mock_verify)))
            _due(store, g["hub"])
    # Same chunk retried once (unjudged does not exclude it), then spent.
    assert counts == [1, 2, 2, 2]
    (memo,) = _hub_meta(store, g["hub"])["reground_seen"].values()
    assert (memo["verdict"], memo["attempts"]) == ("unjudged", 2)


def test_arm_failure_leaves_the_discovery_attach_committed(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence=_SENTENCE)
    paper, chunk = _seed_paper_chunk(
        store, embedder, cite_key="iso", text="A direct measurement statement."
    )
    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES),
        patch(
            "precis.workers.hub_refine.method_gap.select_candidates",
            side_effect=RuntimeError("boom"),
        ) as boom,
    ):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert boom.call_count == 1  # the arm ran and raised
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert [cid for cid, _r, _m in _links(store, hub)] == [chunk]  # widen attach kept
    assert _hub_meta(store, hub).get("last_refined_at") is not None


def test_arm_is_skipped_under_a_reground_plan(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(store, embedder, [(1, "Figure 3 TEM image of the crystals.")])

    def judge(**_kw: Any) -> StrictVerdict:
        return StrictVerdict(verdict="KEEP", reason="ok")

    cfg = RegroundConfig(prune=False, judge_fn=judge, deeper_topk=8)
    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify,
        patch(_ARM_PATH, wraps=hub_refine._method_gap_arm) as arm,
    ):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8, reground=cfg)
        assert arm.call_count == 0
        assert not _gap_calls(mock_verify)
        _due(store, g["hub"])
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8, reground=None)
        assert arm.call_count == 1  # control: the same hub does reach the arm


def test_same_setup_contradiction_files_disputes_and_is_memoed(store: Any) -> None:
    embedder = make_mock_bge_m3()
    g = _gap_hub(store, embedder, [(1, "Figure 3 TEM image of the crystals.")])
    verdict = {
        **_VERIFY_NO,
        "contradicts": True,
        "same_setup": True,
        "terminal": True,
    }
    with (
        patch(_VERIFY_PATH, return_value=verdict),
        patch("precis.workers.hub_refine.run_demotions", return_value=[]),
    ):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM links WHERE dst_ref_id = %s AND relation = 'disputes'",
            (g["hub"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0]["widen"]["via"] == "method-gap"
    assert (
        f"{g['paper']}:{g['chunks'][1]}" in _hub_meta(store, g["hub"])["reground_seen"]
    )


# ── DB-free core ─────────────────────────────────────────────────────


def _chunk(chunk_id: int, ord_: int, text: str, ref_id: int = 1) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        ref_id=ref_id,
        handle=f"pc{chunk_id}",
        ord=ord_,
        kind="paragraph",
        section_path=["Results"],
        text=text,
    )


def test_per_hub_cap_and_per_term_quota() -> None:
    sentence = "TEM and STS and XRD and AFM and SEM show the crystals are anisotropic."
    names = ["TEM", "STS", "XRD", "AFM", "SEM"]
    chunks = [
        _chunk(10 + i, 2 + i, f"Figure {i} {term} data {i}.")
        for i, term in enumerate(t for t in names for _ in range(3))
    ]
    out = method_gap.select_candidates(
        sentence,
        ["The crystals are anisotropic."],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids=set(),
    )
    assert method_gap.PER_TERM == 1
    assert len(out) == method_gap.PER_HUB == 4
    terms = [t for c in out for t in c.terms]
    assert terms == ["TEM", "STS", "XRD", "AFM"]  # SEM starved by the hub cap
    assert len({c.chunk.chunk_id for c in out}) == 4


def test_per_term_quota_override_takes_two_per_term() -> None:
    chunks = [_chunk(10 + i, 2 + i, f"Figure {i} TEM data {i}.") for i in range(4)]
    out = method_gap.select_candidates(
        _SENTENCE,
        [_PASSAGE],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids=set(),
        per_term=2,
    )
    assert len(out) == 2


def test_skipped_chunks_are_never_offered() -> None:
    chunks = [
        _chunk(10, 2, "Figure 1 TEM image."),
        _chunk(11, 3, "Figure 2 TEM image."),
    ]
    out = method_gap.select_candidates(
        _SENTENCE,
        [_PASSAGE],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids={10},
    )
    assert [c.chunk.chunk_id for c in out] == [11]


def test_numbers_are_not_search_terms() -> None:
    chunks = [_chunk(10, 2, "The crystals measure 400 nm across.")]
    out = method_gap.select_candidates(
        "The crystals are 400 nm wide.",
        ["The crystals are wide."],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids=set(),
    )
    assert out == []


def test_chunk_carrying_two_terms_is_one_candidate() -> None:
    chunks = [_chunk(10, 2, "Figure 1 TEM and STS data of the crystals.")]
    out = method_gap.select_candidates(
        "TEM and STS show anisotropic crystals.",
        [_PASSAGE],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids=set(),
    )
    assert len(out) == 1
    assert out[0].terms == ("TEM", "STS")


def test_acronym_is_found_by_its_in_paper_expansion() -> None:
    chunks = [
        _chunk(9, 1, "We use transmission electron microscopy (TEM) throughout."),
        _chunk(
            10,
            2,
            "Transmission electron microscopy of the crystals shows lattice fringes.",
        ),
    ]
    out = method_gap.select_candidates(
        _SENTENCE,
        [_PASSAGE],
        chunks,
        {},
        evidence_refs={1},
        skip_chunk_ids=set(),
        per_term=2,
    )
    assert {c.chunk.chunk_id for c in out} == {9, 10}


def test_has_gap_precheck() -> None:
    assert method_gap.has_gap(_SENTENCE, [_PASSAGE]) is True
    assert method_gap.has_gap(_SENTENCE, ["TEM shows anisotropic crystals."]) is False
    # A passage that spells the method out covers its acronym via its own map.
    assert (
        method_gap.has_gap(
            _SENTENCE,
            ["Transmission electron microscopy (TEM) shows anisotropic crystals."],
        )
        is False
    )
    # Numbers alone never open the search.
    assert (
        method_gap.has_gap("The crystals are 400 nm wide.", ["Wide crystals."]) is False
    )
