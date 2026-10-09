"""Scenario tests for ``precis.workers.conflict_search`` (claim-conflict-
search slice 1).

Real DB ``store`` fixture (mirrors ``tests/workers/test_hub_refine.py``): a
hub is a real ``mint_hub``-minted ``TAPROOT:claim``/``STATUS:canonical``
finding, a candidate is a real ``paper`` ref with an embedded body chunk
(``MockEmbedder`` — deterministic, no live model). ``negate_fn``/``verify_fn``
are always local stubs — the paid calls are deterministic Python functions,
never a live LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.store.types import ChunkInsert
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.workers.conflict_search import (
    CONFLICT_SEARCH_FRESH_DAYS,
    CONFLICT_SEARCH_VERSION,
    CONFLICT_SWEEP_JOB_TYPE,
    _discover,
    coverage_counts,
    coverage_status,
    enqueue_conflict_sweep,
    run_conflict_search_pass,
    sweep_one_hub,
)
from precis.workers.service_config import ALL_HOSTS, set_service_prio
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_ref

_SENTENCE = "Pd/C catalyzes Suzuki coupling at room temperature."


# ── seeding helpers ──────────────────────────────────────────────────


def _seed_hub(store: Any, sentence: str = _SENTENCE) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _seed_paper_chunk(
    store: Any, embedder: Any, *, cite_key: str, text: str, kind: str = "paper"
) -> tuple[int, int]:
    """Mint a source with one embedded body chunk. Returns ``(ref_id, chunk_id)``."""
    ref = store.insert_ref(
        kind=kind, slug=cite_key, title=f"Test {kind} {cite_key}", meta={}
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text, meta={})])
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


def _embed_hub_body_chunk(store: Any, embedder: Any, hub_ref_id: int) -> int:
    """Embed a ``mint_hub``-created hub's ``ord=0`` ``finding_body`` chunk
    (``mint_hub`` itself doesn't embed -- that's the worker's job in prod).
    Returns the ``chunk_id``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND ord = 0",
            (hub_ref_id,),
        ).fetchone()
        assert row is not None
        chunk_id, text = int(row[0]), str(row[1])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_id, embedder.model, embedder.embed_one(text)),
        )
        conn.commit()
    return chunk_id


def _seed_bare_finding_chunk(
    store: Any, embedder: Any, *, text: str
) -> tuple[int, int]:
    """A plain ``kind='finding'`` ref (no ``TAPROOT:claim``/
    ``STATUS:canonical`` tags -- not a claim hub) with one embedded body
    chunk. Returns ``(ref_id, chunk_id)``."""
    ref_id = seed_ref(store, kind="finding", title="bare finding, not a claim hub")
    chunk_id = seed_chunk(store, ref_id=ref_id, text=text, ord=0)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_id, embedder.model, embedder.embed_one(text)),
        )
        conn.commit()
    return ref_id, chunk_id


def _set_read_first(store: Any, ref_id: int, value: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'paper_rank', jsonb_build_object('read_first', %s)) "
            "WHERE ref_id = %s",
            (value, ref_id),
        )
        conn.commit()


def _hub_meta(store: Any, hub_ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (hub_ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _links_between(store: Any, src: int, dst: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT relation, meta FROM links "
            "WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchall()
    return [{"relation": r[0], **dict(r[1] or {})} for r in rows]


def _reset_watermark(store: Any, hub_ref_id: int) -> None:
    """Hand-write a stale ``conflict_search.version`` (0), simulating a
    hub swept under an older method version."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{conflict_search,version}', '0') "
            "WHERE ref_id = %s",
            (hub_ref_id,),
        )
        conn.commit()


class _RiggedEmbedder:
    """Wraps a real embedder, but pins specific query texts to a target
    text's embedding — lets a test force which query "finds" a given
    planted passage without depending on the mock's (semantically inert)
    hash-based vectors lining up by chance."""

    def __init__(self, base: Any, pins: dict[str, str]) -> None:
        self._base = base
        self._pins = pins

    @property
    def model(self) -> str:
        return str(self._base.model)

    def embed_one(self, text: str) -> list[float]:
        target = self._pins.get(text, text)
        return list(self._base.embed_one(target))


def _no_negate(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
    return {"paraphrases": []}


def _never_negate(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
    raise AssertionError("negate_fn must not be called on an already-swept hub")


def _no_verdict(**kwargs: Any) -> dict[str, Any] | None:
    return {"supports": "no", "contradicts": False, "caveats": []}


def _never_verify(**kwargs: Any) -> dict[str, Any] | None:
    raise AssertionError("verify_fn must not be called")


def _recording_verify(
    calls: list[dict[str, Any]], verdict: dict[str, Any] | None = None
) -> Any:
    def fn(**kwargs: Any) -> dict[str, Any] | None:
        calls.append(kwargs)
        return verdict if verdict is not None else _no_verdict(**kwargs)

    return fn


# ── (1) fresh sweep + watermark honored on immediate re-run ─────────────


def test_fresh_hub_swept_and_watermark_honored(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)

    result = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    assert result["hubs_swept"] == 1
    assert result["llm_errors"] == 0

    meta = _hub_meta(store, hub)
    cs = meta["conflict_search"]
    assert cs["version"] == CONFLICT_SEARCH_VERSION
    assert cs["at"]
    assert cs["candidates_checked"] == 0
    assert cs["disputes_filed"] == 0
    assert meta.get("conflict_search_claimed_at") is None

    result2 = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_never_negate, verify_fn=_never_verify
    )
    assert result2["hubs_swept"] == 0


# ── (2) watermark staleness re-sweeps ────────────────────────────────────


def test_stale_watermark_is_reswept(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    _reset_watermark(store, hub)

    result = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    assert result["hubs_swept"] == 1


# ── (3) confirmed contradicts files a disputes link; re-sweep is idempotent ──


def test_confirmed_contradicts_files_a_disputes_link(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, chunk_id = _seed_paper_chunk(
        store,
        embedder,
        cite_key="opposer",
        text="Pd/C does NOT catalyze Suzuki coupling at room temperature.",
    )

    # ``supports="partial"`` alongside ``contradicts=True`` is a reachable
    # verify-prompt output (the two fields are judged independently) --
    # pins that the filed edge's ``meta.support`` is hardcoded "no"
    # (hub_refine._attach_disputes's convention) rather than mirrored off
    # ``supports``, which would otherwise produce a self-contradictory edge.
    verdict = {
        "supports": "partial",
        "support_reason": "reports the opposite outcome",
        "caveats": ["only tested at RT"],
        "contradicts": True,
    }
    calls: list[dict[str, Any]] = []
    result = run_conflict_search_pass(
        store,
        embedder=embedder,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls, verdict),
    )
    assert result["hubs_swept"] == 1
    assert result["disputes_filed"] == 1
    assert len(calls) == 1

    links = _links_between(store, paper, hub)
    assert len(links) == 1
    assert links[0]["relation"] == "disputes"
    assert links[0]["support"] == "no"
    assert links[0]["via"] == "conflict_search"
    assert links[0]["source_handle"] == f"pc{chunk_id}"

    meta = _hub_meta(store, hub)
    assert meta["conflict_search"]["disputes_filed"] == 1

    # Re-sweep after a version bump: the disputed source is now excluded
    # from discovery (already-adjudicated opposition), so verify is never
    # re-called and the link is never duplicated.
    _reset_watermark(store, hub)
    result2 = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    assert result2["hubs_swept"] == 1
    assert result2["disputes_filed"] == 0
    assert result2["candidates_checked"] == 0

    links_after = _links_between(store, paper, hub)
    assert len(links_after) == 1

    meta2 = _hub_meta(store, hub)
    assert meta2["conflict_search"]["disputes_filed"] == 0


# ── (4) floor rule: a below-median candidate still gets verify spend ────


def test_floor_reserves_a_slot_for_below_median_read_first(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)

    low_ref, _low_chunk = _seed_paper_chunk(
        store, embedder, cite_key="low", text="Low-rank opposing passage alpha."
    )
    _set_read_first(store, low_ref, 10.0)
    for i in range(6):
        high_ref, _chunk = _seed_paper_chunk(
            store, embedder, cite_key=f"high{i}", text=f"High-rank passage number {i}."
        )
        _set_read_first(store, high_ref, 90.0)

    calls: list[dict[str, Any]] = []
    result = run_conflict_search_pass(
        store,
        embedder=embedder,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls),
    )
    assert result["hubs_swept"] == 1
    # Default verify budget (6) < the 7-candidate pool, so the floor rule
    # is the only thing that can save the below-median candidate.
    checked_refs = {c["target_cite_key"] for c in calls}
    assert f"paper:{low_ref}" in checked_refs
    assert len(calls) == 6


# ── (5) paraphrase retrieval extends recall past the claim's own phrasing ──


def test_paraphrase_query_finds_what_the_claim_query_misses(
    store: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("PRECIS_CONFLICT_SEARCH_TOPK", "1")
    base = make_mock_bge_m3()
    _seed_hub(store)

    decoy_text = "An unrelated passage about something else entirely."
    opposing_text = "Pd/C does NOT catalyze Suzuki coupling at room temperature."
    paraphrase = "Suzuki coupling proceeds without Pd/C catalysis at room temperature."

    _seed_paper_chunk(store, base, cite_key="decoy", text=decoy_text)
    opposing_ref, _opposing_chunk = _seed_paper_chunk(
        store, base, cite_key="opposer", text=opposing_text
    )

    rigged = _RiggedEmbedder(
        base,
        pins={
            # The claim-sentence query resolves to the decoy's own vector
            # (distance 0) -- with topk=1 the decoy alone wins that leg.
            _SENTENCE: decoy_text,
            # The paraphrase query resolves to the opposer's own vector --
            # topk=1 picks it up on that leg regardless of the decoy.
            paraphrase: opposing_text,
        },
    )

    calls: list[dict[str, Any]] = []
    result = run_conflict_search_pass(
        store,
        embedder=rigged,
        negate_fn=lambda sentence, scope: {"paraphrases": [paraphrase]},
        verify_fn=_recording_verify(calls),
    )
    assert result["hubs_swept"] == 1
    checked_refs = {c["target_cite_key"] for c in calls}
    assert f"paper:{opposing_ref}" in checked_refs


def test_discover_records_which_query_found_each_candidate(store: Any) -> None:
    """Unit-level pin on ``_Candidate.found_by``: a candidate reachable
    only via a paraphrase carries ``["paraphrase:0"]``, never ``"claim"``
    — the bookkeeping the floor/paraphrase acceptance criteria rely on."""
    base = make_mock_bge_m3()
    hub = _seed_hub(store)

    decoy_text = "An unrelated passage about something else entirely."
    opposing_text = "Pd/C does NOT catalyze Suzuki coupling at room temperature."
    paraphrase = "Suzuki coupling proceeds without Pd/C catalysis at room temperature."

    _seed_paper_chunk(store, base, cite_key="decoy2", text=decoy_text)
    opposing_ref, opposing_chunk = _seed_paper_chunk(
        store, base, cite_key="opposer2", text=opposing_text
    )

    rigged = _RiggedEmbedder(
        base, pins={_SENTENCE: decoy_text, paraphrase: opposing_text}
    )

    candidates = _discover(
        store,
        rigged,
        hub_ref_id=hub,
        claim_sentence=_SENTENCE,
        paraphrases=[paraphrase],
        topk=1,
    )
    by_chunk = {c.chunk_id: c for c in candidates}
    assert by_chunk[opposing_chunk].ref_id == opposing_ref
    assert by_chunk[opposing_chunk].found_by == ["paraphrase:0"]
    assert "claim" not in by_chunk[opposing_chunk].found_by


# ── (6) negate failure leaves the watermark unstamped, retried next pass ──


def test_negate_failure_leaves_watermark_unstamped_and_counts_llm_error(
    store: Any,
) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)

    def down(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
        return None

    result = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=down, verify_fn=_never_verify
    )
    assert result["hubs_swept"] == 0
    assert result["llm_errors"] == 1

    meta = _hub_meta(store, hub)
    assert "conflict_search" not in meta
    assert meta.get("conflict_search_claimed_at") is None

    # Lease was cleared (not just left to expire) -- an immediately
    # following pass with a working negate_fn picks the hub right back up.
    result2 = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    assert result2["hubs_swept"] == 1


# ── (7) an already evidence-linked source is excluded from discovery ────


def test_already_evidence_linked_ref_is_excluded_from_discovery(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    ref, _chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="corroborator", text=_SENTENCE
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=ref,
        role="corroborates",
        meta={},
        set_by="system",
    )

    result = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_never_verify
    )
    assert result["hubs_swept"] == 1
    assert result["candidates_checked"] == 0


# ── (8) 'finding'-kind candidates are filtered to live canonical hubs ───


def test_finding_candidates_filtered_to_live_canonical_hubs(store: Any) -> None:
    """The finding-kind ANN leg surfaces every embedded ``finding_body``
    chunk -- including a bare, mid-lifecycle finding that carries neither
    ``TAPROOT:claim`` nor ``STATUS:canonical``. Only a live canonical claim
    hub may become a verify candidate (and, downstream, the src of a filed
    disputes edge); a bare finding must never reach ``verify_fn``."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)

    other_hub = _seed_hub(store, sentence="Ru catalyzes olefin metathesis at 60 C.")
    _embed_hub_body_chunk(store, embedder, other_hub)

    bare_ref, _bare_chunk = _seed_bare_finding_chunk(
        store,
        embedder,
        text="Pd/C does NOT catalyze Suzuki coupling at room temperature.",
    )

    # limit=1: ``other_hub`` is itself a fresh, due canonical hub -- cap
    # the sweep to just the hub under test (lower ref_id, claimed first)
    # so this test isolates its discovery, not a second hub's own sweep.
    calls: list[dict[str, Any]] = []
    result = run_conflict_search_pass(
        store,
        embedder=embedder,
        limit=1,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls),
    )
    assert result["hubs_swept"] == 1
    assert _hub_meta(store, hub).get("conflict_search") is not None
    checked_refs = {c["target_cite_key"] for c in calls}
    assert f"finding:{other_hub}" in checked_refs
    assert f"finding:{bare_ref}" not in checked_refs


# ── no embedder degrades to a no-op ──────────────────────────────────────


def test_no_embedder_degrades_to_a_no_op(store: Any) -> None:
    _seed_hub(store)
    result = run_conflict_search_pass(
        store, embedder=None, negate_fn=_never_negate, verify_fn=_never_verify
    )
    assert result == {
        "hubs_claimed": 0,
        "hubs_swept": 0,
        "candidates_checked": 0,
        "disputes_filed": 0,
        "llm_errors": 0,
        "skipped": 0,
    }


# ── slice 2: the covered ledger, one-hub door, enqueue + mint trigger ──


def _job_rows(store: Any, hub: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.meta FROM refs r WHERE r.kind = 'job' "
            "AND r.retired_at IS NULL AND r.meta->>'job_type' = %s "
            "AND (r.meta->'params'->>'hub_id')::int = %s ORDER BY r.ref_id",
            (CONFLICT_SWEEP_JOB_TYPE, hub),
        ).fetchall()
    return [{"ref_id": int(r[0]), **dict(r[1] or {})} for r in rows]


def _enable_service(store: Any) -> None:
    set_service_prio(store, ALL_HOSTS, "conflict_search", 5)


def test_ledger_records_every_verified_passage_with_its_verdict(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    opposer, opp_chunk = _seed_paper_chunk(
        store, embedder, cite_key="opp", text="Pd/C does NOT catalyze Suzuki coupling."
    )
    neutral, neu_chunk = _seed_paper_chunk(
        store, embedder, cite_key="neu", text="Pd/C is a common hydrogenation catalyst."
    )

    def verify(**kw: Any) -> dict[str, Any]:
        return {
            "supports": "no",
            "contradicts": kw["target_cite_key"] == f"paper:{opposer}",
            "caveats": [],
        }

    result = run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=verify
    )
    assert result["hubs_swept"] == 1

    cs = _hub_meta(store, hub)["conflict_search"]
    by_chunk = {int(r["chunk_id"]): r for r in cs["covered"]}
    assert by_chunk[opp_chunk]["verdict"] == "disputes"
    assert by_chunk[opp_chunk]["handle"] == f"pc{opp_chunk}"
    assert by_chunk[opp_chunk]["ref_id"] == opposer
    assert by_chunk[neu_chunk]["verdict"] == "no-conflict"
    assert by_chunk[neu_chunk]["ref_id"] == neutral
    assert cs["candidates_checked"] == len(cs["covered"]) == 2
    assert cs["disputes_filed"] == 1


def test_same_version_refresh_skips_covered_chunks_and_merges(store: Any) -> None:
    """The "never repeat work" rule: a refresh sweep spends verify budget
    only on chunks the ledger has not seen, and the ledger grows rather
    than resets."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    _seed_paper_chunk(
        store,
        embedder,
        cite_key="first",
        text="Pd/C is a common hydrogenation catalyst.",
    )
    calls: list[dict[str, Any]] = []
    first = sweep_one_hub(
        store,
        embedder=embedder,
        hub_ref_id=hub,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls),
    )
    assert first is not None and first.swept and first.checked == 1
    assert first.skipped_covered == 0

    # Not due anymore: without refresh the one-hub door is a no-op.
    assert (
        sweep_one_hub(
            store,
            embedder=embedder,
            hub_ref_id=hub,
            negate_fn=_never_negate,
            verify_fn=_never_verify,
        )
        is None
    )

    # The corpus grows; a refresh verifies only the new passage.
    later, later_chunk = _seed_paper_chunk(
        store,
        embedder,
        cite_key="later",
        text="Suzuki coupling needs elevated temperature.",
    )
    calls.clear()
    second = sweep_one_hub(
        store,
        embedder=embedder,
        hub_ref_id=hub,
        refresh=True,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls),
    )
    assert second is not None and second.swept
    assert second.skipped_covered == 1
    assert [c["target_cite_key"] for c in calls] == [f"paper:{later}"]

    cs = _hub_meta(store, hub)["conflict_search"]
    assert {int(r["chunk_id"]) for r in cs["covered"]} >= {later_chunk}
    assert cs["candidates_checked"] == 2


def test_version_bump_discards_the_old_ledger(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    _seed_paper_chunk(store, embedder, cite_key="p", text="Pd/C hydrogenation.")
    run_conflict_search_pass(
        store, embedder=embedder, negate_fn=_no_negate, verify_fn=_no_verdict
    )
    assert _hub_meta(store, hub)["conflict_search"]["candidates_checked"] == 1
    _reset_watermark(store, hub)
    calls: list[dict[str, Any]] = []
    run_conflict_search_pass(
        store,
        embedder=embedder,
        negate_fn=_no_negate,
        verify_fn=_recording_verify(calls),
    )
    # Re-verified under the new method, ledger rebuilt from scratch.
    assert len(calls) == 1
    assert _hub_meta(store, hub)["conflict_search"]["candidates_checked"] == 1


def test_sweep_one_hub_ignores_a_non_claim_ref(store: Any) -> None:
    embedder = make_mock_bge_m3()
    paper = seed_ref(store, kind="paper", title="not a hub")
    assert (
        sweep_one_hub(
            store,
            embedder=embedder,
            hub_ref_id=paper,
            refresh=True,
            negate_fn=_never_negate,
            verify_fn=_never_verify,
        )
        is None
    )


def test_enqueue_is_dark_until_the_service_row_lights_it(store: Any) -> None:
    hub = _seed_hub(store)
    assert enqueue_conflict_sweep(store, hub, reason="test") is None
    assert _job_rows(store, hub) == []

    _enable_service(store)
    job_id = enqueue_conflict_sweep(store, hub, reason="test", refresh=True)
    assert job_id is not None
    rows = _job_rows(store, hub)
    assert len(rows) == 1
    assert rows[0]["ref_id"] == job_id
    assert rows[0]["executor"] == "claude_inproc"
    assert rows[0]["params"] == {"hub_id": hub, "refresh": True}
    assert rows[0]["idem_key"] == (
        f"{CONFLICT_SWEEP_JOB_TYPE}:{hub}:v{CONFLICT_SEARCH_VERSION}:test"
    )
    # Idempotent on the key: same reason returns the live job, mints nothing.
    assert enqueue_conflict_sweep(store, hub, reason="test") == job_id
    assert len(_job_rows(store, hub)) == 1
    # A different reason is a different job.
    assert enqueue_conflict_sweep(store, hub, reason="other") not in (None, job_id)
    assert len(_job_rows(store, hub)) == 2


def test_mint_hub_queues_the_sweep_when_the_service_is_lit(store: Any) -> None:
    """The mint trigger: a freshly minted claim hub carries a queued
    ``conflict_sweep`` job without any nanopub intent; a converge-to-
    existing mint queues nothing new."""
    _enable_service(store)
    hub = _seed_hub(store, "Mint-trigger claim sentence one.")
    rows = _job_rows(store, hub)
    assert len(rows) == 1
    assert rows[0]["idem_key"].endswith(":mint")
    assert rows[0]["params"] == {"hub_id": hub, "refresh": False}

    again = _seed_hub(store, "Mint-trigger claim sentence one.")
    assert again == hub
    assert len(_job_rows(store, hub)) == 1


def test_mint_hub_queues_nothing_while_dark(store: Any) -> None:
    hub = _seed_hub(store, "Dark-service claim sentence.")
    assert _job_rows(store, hub) == []


def test_coverage_status_reads_the_ledger_as_a_statement() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert coverage_status(None).status == "missing"
    assert coverage_status({"conflict_search": "garbage"}).status == "missing"
    fresh = coverage_status(
        {
            "conflict_search": {
                "version": CONFLICT_SEARCH_VERSION,
                "at": (now - timedelta(days=1)).isoformat(),
                "candidates_checked": 3,
                "disputes_filed": 1,
                "covered": [{"ref_id": 1, "chunk_id": 2, "verdict": "disputes"}],
            }
        },
        now=now,
    )
    assert fresh.status == "fresh" and fresh.fresh
    assert (fresh.candidates_checked, fresh.disputes_filed) == (3, 1)
    assert len(fresh.covered) == 1
    old = coverage_status(
        {
            "conflict_search": {
                "version": CONFLICT_SEARCH_VERSION,
                "at": (
                    now - timedelta(days=CONFLICT_SEARCH_FRESH_DAYS + 1)
                ).isoformat(),
            }
        },
        now=now,
    )
    assert old.status == "stale-age"
    older_method = coverage_status(
        {"conflict_search": {"version": 0, "at": now.isoformat()}}, now=now
    )
    assert older_method.status == "stale-version"
    # A current-version ledger whose ``at`` is unreadable is stale, not fresh.
    assert (
        coverage_status(
            {"conflict_search": {"version": CONFLICT_SEARCH_VERSION, "at": "??"}}
        ).status
        == "stale-age"
    )


def test_coverage_counts_is_one_query_over_live_hubs(store: Any) -> None:
    embedder = make_mock_bge_m3()
    a = _seed_hub(store, "Coverage claim A.")
    _seed_hub(store, "Coverage claim B.")
    before = coverage_counts(store)
    assert before["total"] >= 2 and before["version"] == CONFLICT_SEARCH_VERSION
    swept_before = before["swept"]
    assert sweep_one_hub(
        store,
        embedder=embedder,
        hub_ref_id=a,
        negate_fn=_no_negate,
        verify_fn=_no_verdict,
    )
    after = coverage_counts(store)
    assert after["swept"] == swept_before + 1
    _reset_watermark(store, a)
    assert coverage_counts(store)["swept"] == swept_before
