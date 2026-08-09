"""Scenario tests for ``precis.workers.hub_refine`` (docs/backlog/taproot-hub-refine.md).

Real DB ``store`` fixture (mirrors ``test_taproot_chase_bridge.py``): a
hub is a real ``mint_hub``-minted ``TAPROOT:claim``/``STATUS:canonical``
finding, a candidate is a real ``paper`` ref with an embedded body chunk
(``MockEmbedder`` — deterministic, no live model). The verifier hook
(``precis.workers.hub_refine._verify_support_with_caveats``) is always
mocked — a *counting* mock so the idempotence / rejection-memo
acceptance criteria can assert call counts, not just outcomes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from precis.store.types import BlockInsert, Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis.workers._chase_llm import is_corroborating
from precis.workers.bib_mark import run_bib_mark_pass
from precis.workers.hub_refine import hub_refine_enabled, run_hub_refine_pass
from precis_web.claim_render import render_claim_evidence
from tests.workers._helpers import make_mock_bge_m3


def test_is_corroborating_gate() -> None:
    """The shared attach gate: yes attaches; partial attaches only without
    contradicts; a contradicting partial and any 'no' do not."""
    assert is_corroborating({"supports": "yes"}) is True
    assert is_corroborating({"supports": "yes", "contradicts": True}) is True
    assert is_corroborating({"supports": "partial"}) is True
    assert is_corroborating({"supports": "partial", "contradicts": False}) is True
    assert is_corroborating({"supports": "partial", "contradicts": True}) is False
    assert is_corroborating({"supports": "no"}) is False
    assert is_corroborating({"supports": None}) is False
    assert is_corroborating({}) is False


_VERIFY_PATH = "precis.workers.hub_refine._verify_support_with_caveats"

_VERIFY_YES = {
    "supports": "yes",
    "support_reason": "direct measurement statement",
    "caveats": [],
    "cited_others": [],
    "terminal": True,
}
_VERIFY_PARTIAL = {
    "supports": "partial",
    "support_reason": "supports under a narrower regime",
    "caveats": ["only tested at room temperature"],
    "cited_others": [],
    "terminal": True,
}
_VERIFY_NO = {
    "supports": "no",
    "support_reason": "chunk does not corroborate the claim",
    "caveats": [],
    "cited_others": [],
    "terminal": True,
}
_VERIFY_PARTIAL_CONTRADICTS = {
    "supports": "partial",
    "support_reason": "on-topic but the result runs counter to the claim",
    "caveats": ["reports larger crystallites, contradicting the small-domain claim"],
    "contradicts": True,
    "cited_others": [],
    "terminal": True,
}


# ── seeding helpers ──────────────────────────────────────────────────


def _seed_hub(store: Any, *, sentence: str, scope: dict[str, str] | None = None) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope=scope or {}))


def _seed_paper_chunk(
    store: Any, embedder: Any, *, cite_key: str, text: str
) -> tuple[int, int]:
    """Mint a paper with one embedded body chunk. Returns ``(ref_id, chunk_id)``."""
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text=text, meta={})])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (ref.id,)
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_id, embedder.model, embedder.embed_one(text)),
        )
        conn.commit()
    return ref.id, chunk_id


def _seed_patent_block(
    store: Any,
    embedder: Any,
    *,
    cite_key: str,
    text: str,
    block_meta: dict[str, Any],
) -> tuple[int, int]:
    """Mint a patent with one embedded body chunk carrying ``block_meta``
    (the ``patent_block`` section marker minted by
    ``handlers/_patent_claims.py`` — ``"description"`` or ``"claim"``).
    Returns ``(ref_id, chunk_id)``."""
    ref = store.insert_ref(
        kind="patent", slug=cite_key, title=f"Test patent {cite_key}", meta={}
    )
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text=text, meta=block_meta)])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (ref.id,)
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_id, embedder.model, embedder.embed_one(text)),
        )
        conn.commit()
    return ref.id, chunk_id


def _hub_meta(store: Any, hub_ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (hub_ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _edges_from(store: Any, src: int) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id, relation, meta FROM links WHERE src_ref_id = %s",
            (src,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def _edge_src_chunk_id(store: Any, src: int) -> int | None:
    """The evidence edge's ``src_chunk_id`` -- ``None`` for a (regression)
    ref-level edge, an int when the edge is grounded at a specific passage."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id FROM links WHERE src_ref_id = %s",
            (src,),
        ).fetchone()
    assert row is not None
    return None if row[0] is None else int(row[0])


# ── hub_refine_enabled ───────────────────────────────────────────────


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_TAPROOT_REFINE_ENABLED", raising=False)
    assert hub_refine_enabled() is False


def test_enabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_TAPROOT_REFINE_ENABLED", "1")
    assert hub_refine_enabled() is True


# ── embedder-unavailable degrade ─────────────────────────────────────


def test_no_embedder_degrades_to_a_no_op(store: Any) -> None:
    """A due hub sitting in the corpus is never even claimed when no
    embedder is wired — a logged no-op, not a crash or a lexical-only
    silent quality drop."""
    hub = _seed_hub(store, sentence="Pd/C catalyzes Suzuki coupling at RT.")

    result = run_hub_refine_pass(store, limit=10, embedder=None)
    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert _hub_meta(store, hub).get("last_refined_at") is None


# ── (a) fresh under-covered hub gains a corroborator ─────────────────


def test_fresh_hub_gains_a_verified_corroborator(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The device sustains 2.4 kV without breakdown.")
    paper, chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="primary", text="A direct measurement statement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 1

    edges = _edges_from(store, paper)
    assert len(edges) == 1
    dst, relation, meta = edges[0]
    assert dst == hub
    assert relation == "corroborates"
    assert meta["support"] == "yes"
    assert meta["source_handle"] == f"pc{chunk_id}"

    assert _hub_meta(store, hub).get("last_refined_at") is not None


def test_partial_support_carries_caveats_onto_the_edge(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Cu electrodes hold off 2.4 kV under bias.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="partial", text="A conditional measurement statement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_PARTIAL):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    edges = _edges_from(store, paper)
    assert len(edges) == 1
    _dst, _relation, meta = edges[0]
    assert meta["support"] == "partial"
    assert meta["caveats"] == ["only tested at room temperature"]


def test_contradicting_partial_is_memoed_not_attached(store: Any) -> None:
    """A ``partial`` flagged ``contradicts`` (on-topic but runs counter to /
    does not substantiate the claim) is NOT attached as corroboration -- it
    lands in the rejection memo like a ``no``, so it never dilutes the living
    cite and is judged once."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="MOF crystallites are ~7 nm single-crystal cubes.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="contra", text="A conflicting measurement statement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_PARTIAL_CONTRADICTS):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    assert _edges_from(store, paper) == []
    rejected = _hub_meta(store, hub).get("taproot_rejected") or {}
    assert str(paper) in rejected
    assert rejected[str(paper)]["supports"] == "partial"
    assert rejected[str(paper)]["contradicts"] is True


def test_contradicting_partial_is_not_reverified_next_pass(store: Any) -> None:
    """Convergence: a memoed contradicting ``partial`` is precheck-skipped on
    the next (DUE-retriggered) pass -- never a repeat LLM verify, never a
    late attach."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A contradicting-partial convergence probe.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="contra-conv", text="A conflicting statement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_PARTIAL_CONTRADICTS) as mv:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert mv.call_count == 1
    assert _edges_from(store, paper) == []

    # Re-trigger via a fresh DUE tag; the memoed paper must not re-verify.
    store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
    with patch(_VERIFY_PATH, return_value=_VERIFY_PARTIAL_CONTRADICTS) as mv2:
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second["claimed"] == 1
    assert mv2.call_count == 0  # precheck-skipped via the memo
    assert _edges_from(store, paper) == []


def test_empty_pass_still_stamps_last_refined_at(store: Any) -> None:
    """A hub with zero candidate chunks in the corpus (or none survive the
    verifier) still stamps ``last_refined_at`` so cadence holds."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A claim nothing in the corpus supports.")

    result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _hub_meta(store, hub).get("last_refined_at") is not None


# ── (b) idempotence — 0 new attachments, 0 verify calls on a re-run ──


def test_idempotent_second_pass_over_same_corpus_attaches_nothing(store: Any) -> None:
    """A second pass forced by a fresh ``TAPROOT_DUE`` tag (the real
    re-trigger mechanism -- ``workers/chase_trigger.py``) re-discovers the
    same candidate but attaches nothing new: the edge-exists precheck
    skips it before ever reaching the LLM hook."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Graphene mobility exceeds 10,000 cm2/Vs at RT.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="idem", text="A direct measurement statement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        assert mock_verify.call_count == 1
        assert len(_edges_from(store, paper)) == 1

        # Not due again on its own (sha matches, no tag, within backstop).
        baseline = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert baseline == {"claimed": 0, "ok": 0, "failed": 0}

        # Simulate the trigger pass marking a new near paper -- forces the
        # hub back into the claim pool, proving idempotence of the
        # *discovery+verify* pipeline itself.
        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert second == {"claimed": 1, "ok": 1, "failed": 0}
        # No new verify call: the edge-exists precheck skips the
        # already-attached candidate before ever reaching the LLM hook.
        assert mock_verify.call_count == 1

    assert len(_edges_from(store, paper)) == 1  # still exactly one edge


# ── (c) due-set gating — TAPROOT_DUE / sha-reopen / backstop ─────────


def test_due_tag_reclaims_a_refined_hub_and_is_popped_at_claim_time(store: Any) -> None:
    """A ``TAPROOT_DUE`` tag (the trigger pass's marker) reclaims an
    already-refined, otherwise-not-due hub -- and the tag is popped in the
    same unit that claims it."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A due-tag reclaim probe claim.")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert first["claimed"] == 1

    # Baseline: not due again (no tag, sha matches, within the backstop).
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        baseline = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert baseline == {"claimed": 0, "ok": 0, "failed": 0}
    assert mock_verify.call_count == 0

    store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
    assert store.has_tag(hub, "TAPROOT_DUE", "1") is True

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second["claimed"] == 1
    assert store.has_tag(hub, "TAPROOT_DUE", "1") is False  # popped at claim time


def test_sha_reopen_reclaims_and_clears_the_rejection_memo_before_discovery(
    store: Any,
) -> None:
    """An edited claim (title changed since last refine) reopens the hub
    even with a recent ``last_refined_at`` and no due tag -- and the old
    rejection memo is cleared *before* discovery, so a candidate rejected
    under the previous wording is re-verified rather than silently
    skipped."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Original claim wording about a device.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="reopen-reject", text="An unrelated measurement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert first["claimed"] == 1
    rejected = _hub_meta(store, hub).get("taproot_rejected") or {}
    assert str(paper) in rejected

    # Baseline: not due again (sha matches, no tag, within the backstop).
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        baseline = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert baseline == {"claimed": 0, "ok": 0, "failed": 0}
    assert mock_verify.call_count == 0

    # The claim's wording changes -- a sha-reopen.
    store.update_ref(hub, title="A materially edited claim about the same device.")

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify2:
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second["claimed"] == 1
    # The memo was cleared BEFORE discovery: the previously-rejected paper
    # is re-verified, not silently skipped by the (now-stale) memo.
    assert mock_verify2.call_count == 1


def test_backstop_hub_not_reclaimed_within_the_window(store: Any) -> None:
    embedder = make_mock_bge_m3()
    _seed_hub(store, sentence="BN nanotubes show negative differential resistance.")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert first["claimed"] == 1

    # No due tag, matching sha, last_refined_at just stamped -- well within
    # the (huge, 90d-default) backstop window -- so it drains, not reclaimed.
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second == {"claimed": 0, "ok": 0, "failed": 0}
    assert mock_verify.call_count == 0


def test_backstop_elapsed_reclaims_a_hub(store: Any, monkeypatch) -> None:
    """The long backstop (``PRECIS_TAPROOT_REFINE_BACKSTOP_H``) is the
    safety net for a lost due-tag -- forcing it to ``0`` reclaims a hub
    with no tag and a matching sha purely off elapsed time."""
    embedder = make_mock_bge_m3()
    _seed_hub(store, sentence="A backstop-elapsed reclaim probe claim.")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert first["claimed"] == 1

    monkeypatch.setenv("PRECIS_TAPROOT_REFINE_BACKSTOP_H", "0")
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second["claimed"] == 1


# ── (d) rejection memo — a supports=no candidate is verified once ───


def test_rejection_memo_verifies_a_no_candidate_exactly_once(store: Any) -> None:
    """Same wording across passes (no sha-reopen) -- the memo persists and
    the precheck skips the candidate on a re-trigger."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="YBCO shows zero resistance above 90K.")
    paper, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="reject", text="An unrelated measurement."
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        assert mock_verify.call_count == 1
        assert _edges_from(store, paper) == []  # no edge for a NO verdict

        rejected = _hub_meta(store, hub).get("taproot_rejected") or {}
        assert str(paper) in rejected
        assert rejected[str(paper)]["supports"] == "no"

        # Re-trigger (a new near paper -- same claim wording, no reopen).
        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert second == {"claimed": 1, "ok": 1, "failed": 0}
        # The memo precheck skips the candidate before verify runs again.
        assert mock_verify.call_count == 1

    assert _edges_from(store, paper) == []


# ── edge-exists precheck runs before any LLM spend ───────────────────


def test_edge_exists_precheck_skips_verify_for_a_pre_attached_paper(store: Any) -> None:
    """A paper that already carries a ``corroborates`` edge on this hub
    (e.g. seeded by ``precis taproot mint``, or a prior chase bridge run)
    must never be re-verified by hub-refine."""
    from precis.taproot.hub import attach_evidence

    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="MoS2 monolayers exhibit direct bandgap emission.")
    paper, chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="pre-attached", text="A direct measurement statement."
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"pc{chunk_id}"},
        set_by="agent",
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 0
    assert len(_edges_from(store, paper)) == 1  # still exactly one edge


# ── verifier error -> candidate simply retried later, no crash ───────


def test_verify_dispatch_error_skips_the_candidate_without_failing_the_hub(
    store: Any,
) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="InGaAs HEMTs switch below 1 ps.")
    _seed_paper_chunk(
        store, embedder, cite_key="dispatcherr", text="A direct measurement statement."
    )

    with patch(_VERIFY_PATH, return_value=None):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _hub_meta(store, hub).get("last_refined_at") is not None


# ── claim-time attempt lease braking a mid-loop raise (OPEN-ITEMS
# "Unbraked LLM-pass cluster") ────────────────────────────────────────


def test_raise_in_verify_loop_is_not_reclaimed_by_an_immediately_following_sweep(
    store: Any,
) -> None:
    """A raise inside the discover/verify loop (a DB error in
    ``attach_evidence``, here) rolls back the whole per-hub transaction, so
    ``last_refined_at`` never advances -- but the claim-time attempt lease
    written at claim time (BEFORE the loop ran, already committed) must
    still brake the hub from an immediately-following sweep, rather than
    re-verifying every candidate against the LLM again next tick."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A mid-loop-raise attempt-lease probe.")
    _seed_paper_chunk(
        store, embedder, cite_key="midraise", text="A direct measurement statement."
    )

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db error attaching evidence")

    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES),
        patch("precis.workers.hub_refine.attach_evidence", side_effect=_boom),
    ):
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    # The transaction rolled back -- no stamp landed.
    assert _hub_meta(store, hub).get("last_refined_at") is None

    # Immediately-following sweep: NOT re-claimed, NOT re-verified.
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify2:
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second == {"claimed": 0, "ok": 0, "failed": 0}
    assert mock_verify2.call_count == 0


# ── HUBS_PER_PASS / limit is honoured ─────────────────────────────────


def test_limit_caps_hubs_claimed_per_pass(store: Any) -> None:
    embedder = make_mock_bge_m3()
    for i in range(3):
        _seed_hub(store, sentence=f"Distinct claim number {i} about a device.")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        result = run_hub_refine_pass(store, limit=2, embedder=embedder, topk=8)
    assert result["claimed"] == 2


def test_verify_call_uses_a_mock_object_not_the_real_router(store: Any) -> None:
    """Belt-and-suspenders: the counting fake really is a
    ``unittest.mock`` object, not accidentally the live LLM dispatch."""
    embedder = make_mock_bge_m3()
    _seed_hub(store, sentence="A claim used only to check the mock plumbing.")
    _seed_paper_chunk(store, embedder, cite_key="mockcheck", text="Some chunk text.")
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert isinstance(mock_verify, MagicMock)
    assert mock_verify.call_count == 1


# ── citation-following discover source (AC 3, AC 4) ──────────────────
#
# docs/backlog/citation-taproot-resolve.md: a second Discover source
# inside _refine_one_hub follows the hub's OWN evidence citations —
# grounding chunk -> chunk_citations -> resolve_citation -> held cited
# paper -> scoped verify — sharing the single Filter/Verify/Write tail and
# the loop-local seen_papers dedup with the semantic source.


def _seed_bib_entry(store: Any, ref_id: int, marker: int, *, held_ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, held_ref_id, doi, parse_version) "
            "VALUES (%s, %s, %s, %s, %s, 1) RETURNING id",
            (ref_id, marker, f"cited {marker}", held_ref_id, f"10.1/{ref_id}.{marker}"),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _seed_citation_scenario(
    store: Any, embedder: Any, *, claim: str, tag: str, marker: int = 126
) -> tuple[int, int, int, int, int]:
    """A hub whose EXISTING evidence grounds in a citing-paper chunk that
    carries ``[marker]``, which resolves (via the real bib_mark sweep +
    paper_bib_entries) to a HELD cited paper with a supporting passage.

    Returns ``(hub, citing_paper, citing_chunk, cited_paper, cited_chunk)``.
    """
    hub = _seed_hub(store, sentence=claim)
    citing, citing_chunk = _seed_paper_chunk(
        store,
        embedder,
        cite_key=f"{tag}-citing",
        text=f"Prior work established the effect [{marker}].",
    )
    cited, cited_chunk = _seed_paper_chunk(
        store,
        embedder,
        cite_key=f"{tag}-cited",
        text="A direct measurement statement supporting the claim.",
    )
    _seed_bib_entry(store, citing, marker, held_ref_id=cited)
    # Populate chunk_citations end-to-end via the real sweep (not a direct
    # insert) so AC 3 exercises the actual population path.
    run_bib_mark_pass(store, batch_size=100)
    # The hub's pre-existing evidence edge, grounded at the citing chunk.
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=citing,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"pc{citing_chunk}"},
        set_by="system",
    )
    return hub, citing, citing_chunk, cited, cited_chunk


def test_ac3_citation_reached_paper_is_discovered_and_attached(store: Any) -> None:
    """AC 3 (load-bearing): driven from ``run_hub_refine_pass`` (not the
    internals), a paper reached only by following the claim's own inline
    citation is verified and attached — with a ``source_handle`` that is a
    passage INSIDE the cited paper."""
    embedder = make_mock_bge_m3()
    hub, citing, _citing_chunk, cited, cited_chunk = _seed_citation_scenario(
        store, embedder, claim="The catalyst sustains high turnover.", tag="ac3yes"
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    # The already-attached citing paper is precheck-skipped; only the
    # citation-reached cited paper is verified.
    assert mock_verify.call_count == 1
    corro = [e for e in _edges_from(store, cited) if e[0] == hub]
    assert len(corro) == 1
    _dst, relation, meta = corro[0]
    assert relation == "corroborates"
    # Grounded at a passage INSIDE the cited paper, not the citing chunk.
    assert meta["source_handle"] == f"pc{cited_chunk}"


def test_ac3_citation_miss_records_flag_and_renders_red(store: Any) -> None:
    """AC 3 companion: the cited held paper does NOT contain the content →
    no edge, a rejection memo entry marked ``via: 'citation'``, a
    ``meta.citation_misses`` record on the hub, and a red miss line on the
    claim page."""
    embedder = make_mock_bge_m3()
    hub, _citing, citing_chunk, cited, _cited_chunk = _seed_citation_scenario(
        store, embedder, claim="The catalyst sustains high turnover.", tag="ac3no"
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    # No corroborating edge from the cited paper.
    assert [e for e in _edges_from(store, cited) if e[0] == hub] == []

    meta = _hub_meta(store, hub)
    rejected = meta.get("taproot_rejected") or {}
    assert rejected[str(cited)]["via"] == "citation"
    misses = meta.get("citation_misses") or []
    assert {"marker": 126, "cited_ref": cited, "from_chunk": citing_chunk} in misses

    # The claim page renders the red miss line.
    head = handle_registry.format_handle("finding", hub)
    data = render_claim_evidence(store, head)
    assert data is not None
    rows = data["citation_misses"]
    assert any(r["marker"] == 126 and r["cited_ref_id"] == cited for r in rows)


def test_ac4_paper_reached_by_both_sources_is_verified_once(store: Any) -> None:
    """AC 4 (intra-pass): a paper surfaced by BOTH the citation source and
    the semantic ANN within one ``_refine_one_hub`` gets exactly ONE verify
    call (the shared ``seen_papers`` dedup)."""
    embedder = make_mock_bge_m3()
    claim = "A dual-reachable claim about sustained turnover."
    hub, _citing, _citing_chunk, cited, _cited_chunk = _seed_citation_scenario(
        store, embedder, claim=claim, tag="ac4"
    )

    # Precondition: the SEMANTIC source alone ALSO surfaces the cited paper,
    # so a single verify proves the shared dedup did the work — not that the
    # semantic source simply missed it.
    sem = store.search_blocks(
        q=claim,
        query_vec=embedder.embed_one(claim),
        mode="semantic",
        kind="paper",
        limit=8,
    )
    assert cited in {int(ref.id) for _b, ref, _s in sem}

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    assert mock_verify.call_count == 1
    assert len([e for e in _edges_from(store, cited) if e[0] == hub]) == 1


def test_ac4_citation_source_wins_the_shared_slot(store: Any) -> None:
    """When both sources surface the same paper, the citation candidate wins
    the single verify slot — proven on a ``no`` verdict, which only the
    citation source marks ``via: 'citation'`` + records as a miss."""
    embedder = make_mock_bge_m3()
    claim = "Another dual-reachable claim about turnover stability."
    hub, _citing, _citing_chunk, cited, _cited_chunk = _seed_citation_scenario(
        store, embedder, claim=claim, tag="ac4b"
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    assert mock_verify.call_count == 1
    meta = _hub_meta(store, hub)
    assert (meta.get("taproot_rejected") or {})[str(cited)]["via"] == "citation"
    assert any(m["cited_ref"] == cited for m in (meta.get("citation_misses") or []))


def test_ac4_second_pass_makes_no_new_verify_for_same_pair(store: Any) -> None:
    """Cross-pass convergence for the citation source: a re-trigger over the
    same state re-discovers the cited paper but attaches nothing new and
    makes zero new verify calls (edge-exists precheck)."""
    embedder = make_mock_bge_m3()
    claim = "A convergence claim reached by citation."
    hub, _citing, _citing_chunk, cited, _cited_chunk = _seed_citation_scenario(
        store, embedder, claim=claim, tag="ac4conv"
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        assert mock_verify.call_count == 1
        assert len([e for e in _edges_from(store, cited) if e[0] == hub]) == 1

        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
        # No new verify: the cited paper is already an attached supporter.
        assert mock_verify.call_count == 1
    assert len([e for e in _edges_from(store, cited) if e[0] == hub]) == 1


def test_citation_contradicting_partial_also_records_a_miss(store: Any) -> None:
    """A citation-reached ``partial`` flagged ``contradicts`` (a stronger
    miss than a plain ``no``) is NOT attached and DOES land in
    ``meta.citation_misses`` — the deliberate inclusion beyond the AC's
    literal ``supports=no`` (a cited source that contradicts the claim is a
    red flag too)."""
    embedder = make_mock_bge_m3()
    hub, _citing, citing_chunk, cited, _cited_chunk = _seed_citation_scenario(
        store, embedder, claim="A claim its cited source contradicts.", tag="ac3contra"
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_PARTIAL_CONTRADICTS):
        run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)

    # Contradicting partial → not corroboration, so no edge.
    assert [e for e in _edges_from(store, cited) if e[0] == hub] == []

    meta = _hub_meta(store, hub)
    rejected = meta.get("taproot_rejected") or {}
    assert rejected[str(cited)]["via"] == "citation"
    assert rejected[str(cited)]["contradicts"] is True
    misses = meta.get("citation_misses") or []
    assert {"marker": 126, "cited_ref": cited, "from_chunk": citing_chunk} in misses


# ── patent discovery leg (docs/backlog/patent-evidence-parity.md,
# Phase 1) ─────────────────────────────────────────────────────────
#
# The semantic-ANN discover source now runs a second, patent-scoped leg
# beside the paper leg (two ``store.search_blocks`` calls, merged by score
# and truncated back to ``topk`` -- the wrapper takes one ``kind=`` string,
# not a list). Grounding policy: a patent's *claims*-section blocks are
# legal scope, not empirical support, and are dropped before Verify ever
# sees them -- only description/abstract blocks are eligible.


def test_patent_description_block_gains_a_verified_corroborator(store: Any) -> None:
    """A patent whose description block semantically matches the world-claim
    is discovered via the patent leg and, on a supporting verdict, attaches
    as an evidence edge exactly like a paper would."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The catalyst sustains 400C without deactivation.")
    patent, chunk_id = _seed_patent_block(
        store,
        embedder,
        cite_key="ep1-desc",
        text="A direct measurement statement from the description.",
        block_meta={"patent_block": "description"},
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 1
    # The verify prompt is told this candidate's source is a patent.
    assert mock_verify.call_args.kwargs["source_kind"] == "patent"

    edges = _edges_from(store, patent)
    assert len(edges) == 1
    dst, relation, edge_meta = edges[0]
    assert dst == hub
    assert relation == "corroborates"
    assert edge_meta["support"] == "yes"
    # Patent chunks mint "pk" handles, not the paper "pc" code -- a
    # mis-kinded handle fails resolve_handle's kind cross-check and the
    # edge silently degrades to ref-level (no src_chunk_id).
    assert edge_meta["source_handle"] == f"pk{chunk_id}"
    # The edge itself is chunk-grounded, not just the meta blob: a
    # regression back to a mis-kinded handle would leave this NULL.
    assert _edge_src_chunk_id(store, patent) == chunk_id


def test_patent_claims_only_block_yields_no_candidate(store: Any) -> None:
    """A patent whose ONLY block sits in the claims section is never a
    grounding candidate -- filtered out of the patent leg's hits before
    Filter/Verify, even though it semantically matches (it's the only
    embedded chunk in the corpus, so an unfiltered ANN would surely surface
    it). Never verified, never attached."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Claims-only patent probe: sustained turnover.")
    patent, _chunk_id = _seed_patent_block(
        store,
        embedder,
        cite_key="ep1-claim",
        text="1. A method comprising sustained turnover at high temperature.",
        block_meta={
            "patent_block": "claim",
            "claim_number": 1,
            "claim_independent": True,
        },
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 0
    assert _edges_from(store, patent) == []
    assert _hub_meta(store, hub).get("last_refined_at") is not None


def test_rejected_patent_is_not_reverified_next_pass(store: Any) -> None:
    """A patent judged ``no`` lands in the rejection memo like a paper would
    -- the precheck skips it on a re-trigger without a repeat LLM call."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A patent-rejection-memo convergence probe.")
    patent, _chunk_id = _seed_patent_block(
        store,
        embedder,
        cite_key="ep1-reject",
        text="An unrelated description passage.",
        block_meta={"patent_block": "description"},
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify:
        first = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert first == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 1
    rejected = _hub_meta(store, hub).get("taproot_rejected") or {}
    assert str(patent) in rejected

    store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
    with patch(_VERIFY_PATH, return_value=_VERIFY_NO) as mock_verify2:
        second = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert second["claimed"] == 1
    assert mock_verify2.call_count == 0  # precheck-skipped via the memo
    assert _edges_from(store, patent) == []


def test_attached_patent_is_not_reverified_next_pass(store: Any) -> None:
    """A patent already carrying a ``corroborates`` edge on this hub (e.g.
    seeded by ``precis taproot mint``) is precheck-skipped -- the
    ``_attached_source_ids`` query covers patent sources exactly like
    papers (``taproot.hub._EVIDENCE_SRC_KINDS`` admits both kinds)."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A patent already-attached precheck probe.")
    patent, chunk_id = _seed_patent_block(
        store,
        embedder,
        cite_key="ep1-attached",
        text="A direct measurement statement from the description.",
        block_meta={"patent_block": "description"},
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=patent,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"pc{chunk_id}"},
        set_by="agent",
    )

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES) as mock_verify:
        result = run_hub_refine_pass(store, limit=10, embedder=embedder, topk=8)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert mock_verify.call_count == 0
    assert len(_edges_from(store, patent)) == 1  # still exactly one edge
