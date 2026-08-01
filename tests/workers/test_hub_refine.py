"""Scenario tests for ``precis.workers.hub_refine`` (docs/proposals/
taproot-hub-refine.md).

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
from precis.taproot.hub import mint_hub
from precis.workers.hub_refine import hub_refine_enabled, run_hub_refine_pass
from tests.workers._helpers import make_mock_bge_m3

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
