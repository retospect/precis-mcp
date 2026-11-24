"""Scenario tests for ``precis.workers.inbound_ground`` (taproot inbound
grounding, part b: a newly ingested paper is checked against the claim set).

Real DB ``store`` fixture, same idiom as ``tests/workers/test_chase_trigger.py``:
a hub is a real ``mint_hub``-minted ``TAPROOT:claim``/``STATUS:canonical``
finding, a paper is a real ``paper`` ref with embedded body chunks
(``MockEmbedder`` -- deterministic by exact text: identical text embeds to
distance 0, materially different sentences land beyond the 0.45 floor). The
verifier is always a local stub -- never a live LLM.

The acceptance test from the spec: a paper known to support an existing hub
is ingested; an evidence edge appears with NO ``cites`` path between them.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import META_REJECTED, mint_hub
from precis.utils.llm import router as llm_router
from precis.workers import _grounding_budget as grounding_budget
from precis.workers._grounding_budget import GroundingBudget
from precis.workers.inbound_ground import (
    INBOUND_GROUND_VERSION,
    classify_verdict,
    run_inbound_ground_pass,
)
from precis.workers.inbound_ground_backfill import run_inbound_ground_backfill_pass
from tests.workers._helpers import make_mock_bge_m3

_MIN_SIM = 0.45
#: Eight-plus lowercase words with a terminator: passes ``has_grounding_prose``.
_CLAIM = "The annealed membrane rejects ninety-nine percent of divalent ions at pH 7."
_FAR = "Zebra migration patterns across the equatorial rainforest canopy were logged."


# ── seeding helpers ─────────────────────────────────────────────────────


def _seed_hub(store: Any, sentence: str = _CLAIM) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _seed_paper(
    store: Any,
    embedder: Any,
    *,
    slug: str,
    texts: list[str],
    embed: bool = True,
) -> tuple[int, list[int]]:
    """Mint a paper with one body chunk per text, each embedded (unless
    ``embed=False`` -- an ingest still in flight). Returns ``(ref_id,
    chunk_ids)``."""
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}", meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=i, text=t, meta={}) for i, t in enumerate(texts)]
    )
    chunk_ids: list[int] = []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s ORDER BY ord",
            (ref.id,),
        ).fetchall()
        for chunk_id, text in rows:
            chunk_ids.append(int(chunk_id))
            if embed:
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                    "VALUES (%s, %s, %s, 'ok')",
                    (int(chunk_id), embedder.model, embedder.embed_one(str(text))),
                )
        conn.commit()
    return ref.id, chunk_ids


def _run(store: Any, embedder: Any, verify_fn: Any, **kw: Any) -> dict[str, int]:
    kw.setdefault("limit", 10)
    kw.setdefault("topk", 5)
    kw.setdefault("max_llm", 5)
    kw.setdefault("min_sim", _MIN_SIM)
    kw.setdefault("max_age_days", 30)
    return run_inbound_ground_pass(store, embedder=embedder, verify_fn=verify_fn, **kw)


def _edges(store: Any, *, paper: int, hub: int, relation: str) -> list[Any]:
    return [
        link
        for link in store.links_for(hub, direction="in", relation=relation)
        if link.src_ref_id == paper
    ]


def _marked(store: Any, paper: int) -> bool:
    return store.has_tag(paper, "INBOUND_GROUND", INBOUND_GROUND_VERSION)


def _hub_meta(store: Any, hub: int) -> dict[str, Any]:
    return dict(store.fetch_refs_by_ids([hub])[hub].meta or {})


def _verdict(supports: str, **extra: Any) -> dict[str, Any]:
    base = {
        "supports": supports,
        "support_reason": f"stub says {supports}",
        "caveats": [],
        "contradicts": False,
        "same_setup": True,
        "terminal": True,
    }
    base.update(extra)
    return base


class _Verifier:
    """Records every call; answers from ``verdict`` (or raises/None)."""

    def __init__(self, verdict: dict[str, Any] | None) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> dict[str, Any] | None:
        self.calls.append(kw)
        return self.verdict


# ── classify_verdict ─────────────────────────────────────────────────────


def test_classify_verdict_three_way() -> None:
    assert classify_verdict(_verdict("yes")) == "support"
    assert classify_verdict(_verdict("partial")) == "support"
    assert classify_verdict(_verdict("partial", contradicts=True)) == "deny"
    assert classify_verdict(_verdict("no", contradicts=True)) == "deny"
    assert classify_verdict(_verdict("no")) == "neutral"


# ── degrade ─────────────────────────────────────────────────────────────


def test_no_embedder_degrades_to_a_no_op(store: Any) -> None:
    _seed_hub(store)
    result = run_inbound_ground_pass(store, embedder=None)
    assert result["claimed"] == 0 and result["attached"] == 0


# ── acceptance: support lands an evidence edge with no citation path ────


def test_supporting_paper_attaches_certified_evidence_without_a_cites_path(
    store: Any,
) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, chunk_ids = _seed_paper(store, embedder, slug="supporter", texts=[_CLAIM])
    verifier = _Verifier(_verdict("yes", caveats=["pH 7 only"]))

    # No citation edge of any kind between the paper and the hub.
    assert store.links_for(paper, relation="cites") == []

    result = _run(store, embedder, verifier)
    assert result["claimed"] == 1 and result["ok"] == 1 and result["failed"] == 0
    assert result["hubs_matched"] == 1
    assert result["verified"] == 1
    assert result["attached"] == 1

    # The verifier saw the paper's own passage against the hub's sentence.
    (call,) = verifier.calls
    assert call["claim"] == _CLAIM
    assert call["target_chunk_text"] == _CLAIM
    assert call["source_kind"] == "paper"

    (edge,) = _edges(store, paper=paper, hub=hub, relation="corroborates")
    # Chunk-grounded, born certified (not withheld): the full verified stamp.
    assert edge.src_chunk_id == chunk_ids[0]
    assert edge.meta["support"] == "yes"
    assert edge.meta["caveats"] == ["pH 7 only"]
    assert edge.meta["verified_by"] == "inbound-ground"
    assert edge.meta["verified_claim_sha"]
    assert edge.meta["inbound_ground"]["version"] == INBOUND_GROUND_VERSION
    assert store.links_for(paper, relation="cites") == []

    # Done-marker + event, so the paper is never re-claimed.
    assert _marked(store, paper)
    again = _run(store, embedder, verifier)
    assert again["claimed"] == 0
    assert len(verifier.calls) == 1


# ── deny / neutral ───────────────────────────────────────────────────────


def test_denying_paper_files_disputes_and_memoes_the_hub(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="denier", texts=[_CLAIM])
    verifier = _Verifier(_verdict("no", contradicts=True))

    result = _run(store, embedder, verifier)
    assert result["attached"] == 0 and result["disputes"] == 1

    assert _edges(store, paper=paper, hub=hub, relation="corroborates") == []
    (edge,) = _edges(store, paper=paper, hub=hub, relation="disputes")
    assert edge.meta["support"] == "no"
    assert edge.meta["via"] == "inbound_ground"

    memo = _hub_meta(store, hub)[META_REJECTED]
    assert memo[str(paper)]["verdict"] == "deny"
    assert memo[str(paper)]["via"] == "inbound_ground"
    assert _marked(store, paper)


def test_cross_setup_contradiction_is_memo_only(store: Any) -> None:
    """A ``contradicts`` verdict without same-setup + terminal never files a
    ``disputes`` edge (the widen arm's own gate) -- memo only."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="cross", texts=[_CLAIM])
    verifier = _Verifier(_verdict("no", contradicts=True, same_setup=False))

    result = _run(store, embedder, verifier)
    assert result["disputes"] == 0
    assert _edges(store, paper=paper, hub=hub, relation="disputes") == []
    assert _hub_meta(store, hub)[META_REJECTED][str(paper)]["verdict"] == "deny"


def test_neutral_verdict_memoes_without_an_edge(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="neutral", texts=[_CLAIM])
    verifier = _Verifier(_verdict("no"))

    result = _run(store, embedder, verifier)
    assert result["verified"] == 1
    assert result["attached"] == 0 and result["disputes"] == 0
    assert _edges(store, paper=paper, hub=hub, relation="corroborates") == []
    assert _edges(store, paper=paper, hub=hub, relation="disputes") == []
    assert _hub_meta(store, hub)[META_REJECTED][str(paper)]["verdict"] == "neutral"
    assert _marked(store, paper)


# ── matching + filters ──────────────────────────────────────────────────


def test_far_paper_matches_nothing_and_is_still_marked(store: Any) -> None:
    embedder = make_mock_bge_m3()
    _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="far", texts=[_FAR])
    verifier = _Verifier(_verdict("yes"))

    result = _run(store, embedder, verifier)
    assert result["claimed"] == 1 and result["hubs_matched"] == 0
    assert verifier.calls == []
    assert _marked(store, paper)


def test_already_memoed_pair_spends_no_llm(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="memoed", texts=[_CLAIM])
    store.update_ref(
        hub,
        meta_patch={
            META_REJECTED: {str(paper): {"supports": "no", "contradicts": False}}
        },
    )
    verifier = _Verifier(_verdict("yes"))

    result = _run(store, embedder, verifier)
    assert result["hubs_matched"] == 1 and result["verified"] == 0
    assert verifier.calls == []
    assert _marked(store, paper)


def test_paper_still_embedding_is_not_claimed(store: Any) -> None:
    """ "Finished ingest" means every embeddable body chunk has a vector --
    a paper with one chunk still pending waits for the next pass."""
    embedder = make_mock_bge_m3()
    _seed_hub(store)
    paper, chunk_ids = _seed_paper(
        store, embedder, slug="inflight", texts=[_CLAIM, _FAR], embed=False
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_ids[0], embedder.model, embedder.embed_one(_CLAIM)),
        )
        conn.commit()
    verifier = _Verifier(_verdict("yes"))

    assert _run(store, embedder, verifier)["claimed"] == 0
    assert not _marked(store, paper)

    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (chunk_ids[1], embedder.model, embedder.embed_one(_FAR)),
        )
        conn.commit()
    assert _run(store, embedder, verifier)["claimed"] == 1
    assert _marked(store, paper)


def test_old_paper_is_outside_the_inbound_window(store: Any) -> None:
    embedder = make_mock_bge_m3()
    _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="old", texts=[_CLAIM])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '40 days' WHERE ref_id = %s",
            (paper,),
        )
        conn.commit()
    verifier = _Verifier(_verdict("yes"))

    assert _run(store, embedder, verifier, max_age_days=30)["claimed"] == 0
    assert _run(store, embedder, verifier, max_age_days=60)["claimed"] == 1


# ── cost bounds ─────────────────────────────────────────────────────────


def test_topk_and_max_llm_bound_the_verifier_calls(store: Any) -> None:
    embedder = make_mock_bge_m3()
    sentences = [
        f"Sample {w} shows a reproducible ten percent gain in ionic conductivity "
        f"after annealing at three hundred kelvin for one hour."
        for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
    ]
    hubs = [_seed_hub(store, s) for s in sentences]
    # One chunk per hub sentence: every hub sits at distance 0 to one chunk.
    paper, _ = _seed_paper(store, embedder, slug="many", texts=sentences)

    verifier = _Verifier(_verdict("yes"))
    result = _run(store, embedder, verifier, topk=4, max_llm=5)
    assert result["hubs_matched"] == 4
    assert result["verified"] == 4 and len(verifier.calls) == 4

    # Fresh paper, tighter LLM cap than top-k: the cap wins.
    paper2, _ = _seed_paper(store, embedder, slug="many2", texts=sentences)
    verifier2 = _Verifier(_verdict("yes"))
    result2 = _run(store, embedder, verifier2, topk=6, max_llm=2)
    assert result2["hubs_matched"] == 6
    assert result2["verified"] == 2 and len(verifier2.calls) == 2
    attached = sum(
        1 for h in hubs if _edges(store, paper=paper2, hub=h, relation="corroborates")
    )
    assert attached == 2
    assert _marked(store, paper) and _marked(store, paper2)


# ── failure handling ────────────────────────────────────────────────────


def test_verifier_failure_leaves_the_paper_unmarked_for_retry(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="retry", texts=[_CLAIM])

    failing = _Verifier(None)
    result = _run(store, embedder, failing)
    assert result["ok"] == 1 and result["llm_errors"] == 1
    assert not _marked(store, paper)
    assert _edges(store, paper=paper, hub=hub, relation="corroborates") == []

    # Next pass re-claims it and the recovered verifier lands the edge.
    ok = _Verifier(_verdict("yes"))
    result = _run(store, embedder, ok)
    assert result["claimed"] == 1 and result["attached"] == 1
    assert _marked(store, paper)


def test_backfill_resumes_after_interruption_and_skips_completed_claim(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store)
    old = [
        _seed_paper(store, embedder, slug=f"backfill-old-{i}", texts=[_CLAIM])[0]
        for i in range(2)
    ]
    _seed_paper(store, embedder, slug="backfill-recent", texts=[_CLAIM])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '31 days' "
            "WHERE ref_id = ANY(%s)",
            (old,),
        )
        conn.commit()
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_TOPK", "5")
    calls: list[int] = []

    class Interrupted(BaseException):
        pass

    def verify(**kwargs: Any) -> dict[str, Any]:
        calls.append(int(kwargs["target_cite_key"].split(":")[1]))
        if len(calls) == 2:
            raise Interrupted
        if len(calls) == 1:
            return _verdict("yes")
        return _verdict("no")

    with pytest.raises(Interrupted):
        run_inbound_ground_backfill_pass(store, embedder=embedder, verify_fn=verify)

    assert _edges(store, paper=old[0], hub=hub, relation="corroborates")
    resumed = run_inbound_ground_backfill_pass(
        store, embedder=embedder, verify_fn=verify
    )
    assert resumed["pairs_checked"] == 1
    assert calls == [old[0], old[1], old[1]]

    run_inbound_ground_backfill_pass(store, embedder=embedder, verify_fn=verify)
    done = run_inbound_ground_backfill_pass(store, embedder=embedder, verify_fn=verify)
    assert done["claims_visited"] == done["pairs_checked"] == done["tokens"] == 0
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT paper_ref_id, verdict FROM inbound_grounded "
            "WHERE claim_ref_id = %s ORDER BY paper_ref_id",
            (hub,),
        ).fetchall() == [(old[0], "support"), (old[1], "no-relation")]


def test_grounding_token_reservations_share_the_daily_ceiling(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_DAILY_TOKENS", "10")
    recent = GroundingBudget(store)
    backfill = GroundingBudget(store, local=True)

    assert recent._reserve(6)
    assert not backfill._reserve(5)
    assert GroundingBudget(store)._reserve(4)
    assert not GroundingBudget(store)._reserve(1)
    assert recent.tokens == 6
    assert backfill.tokens == 0


# ── the shared budget, end to end minus the wire ────────────────────────


def test_budget_verify_reserves_and_caps_through_dispatch(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production seam with the wire faked: ``budget.verify`` composes
    the verifier prompt, and the dispatch forces the completion cap, pins
    local placement and attaches the admission the FailoverProvider
    consults before every attempt."""
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_DAILY_TOKENS", "1000000")
    seen: dict[str, Any] = {}

    def fake_route(request: Any) -> Any:
        seen["request"] = request
        return llm_router.LlmResult(
            text="{}",
            cost_usd=None,
            turns_used=None,
            model="m",
            tier=llm_router.Tier.BIG,
            data={"supports": "yes"},
        )

    monkeypatch.setattr(grounding_budget, "route", fake_route)
    budget = GroundingBudget(store, local=True)

    out = budget.verify(
        claim=_CLAIM,
        scope={},
        target_cite_key="paper:1",
        target_chunk_ord=0,
        target_chunk_text=_CLAIM,
    )
    assert out == {"supports": "yes"}
    request = seen["request"]
    assert request.max_tokens == 2048
    assert request.placement == "local"
    assert request.admit_attempt is not None
    assert request.admit_attempt()  # the reservation lands in the ledger
    assert budget.tokens > 0


def test_budget_refusal_folds_to_none_without_dispatching(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling below one reservation refuses the attempt BEFORE any
    provider runs; the verifier's ``res.error`` contract folds the
    ``budget``-class refusal to ``None``, so a pass counts the pair as
    failed and retries it next run."""
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_DAILY_TOKENS", "100")
    dispatched: list[Any] = []

    def counting_route(request: Any) -> Any:
        dispatched.append(request)
        return llm_router.FailoverProvider(
            [llm_router.Rung(llm_router.Transport.OPENAI_TOOLS)]
        ).run(request, model="oss-model")

    monkeypatch.setattr(grounding_budget, "route", counting_route)
    budget = GroundingBudget(store)

    out = budget.verify(
        claim=_CLAIM,
        scope={},
        target_cite_key="paper:1",
        target_chunk_ord=0,
        target_chunk_text=_CLAIM,
    )
    assert out is None
    assert len(dispatched) == 1
    assert budget.exhausted


# ── memory claims ────────────────────────────────────────────────────────


def _seed_memory_claim(store: Any, sentence: str = _CLAIM) -> int:
    """A live ``TAPROOT:claim`` memory ref — the backfill's non-finding target."""
    ref = store.insert_ref(kind="memory", slug=None, title=sentence, meta={})
    store.add_tag(ref.id, Tag.closed("TAPROOT", "claim"), set_by="system")
    return ref.id


def _age_paper(store: Any, paper: int, *, days: int = 31) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(days => %s) "
            "WHERE ref_id = %s",
            (days, paper),
        )
        conn.commit()


def test_backfill_grounds_a_memory_claim_through_a_bare_link(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TAPROOT:claim`` memory ref is the backfill's own target: no
    finding-hub gate, no retraction trigger — the verdict lands as a bare
    ``corroborates`` link stamped with the backfill's verifier fingerprint,
    and a fresh support is counted as new."""
    embedder = make_mock_bge_m3()
    claim = _seed_memory_claim(store)
    paper, chunk_ids = _seed_paper(store, embedder, slug="mem-old", texts=[_CLAIM])
    _age_paper(store, paper)
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_TOPK", "5")
    verifier = _Verifier(_verdict("yes"))

    result = run_inbound_ground_backfill_pass(
        store, embedder=embedder, verify_fn=verifier
    )

    assert result["claims_visited"] == 1
    assert result["supports"] == 1
    (call,) = verifier.calls
    assert call["claim"] == _CLAIM
    (edge,) = _edges(store, paper=paper, hub=claim, relation="corroborates")
    assert edge.src_chunk_id == chunk_ids[0]
    assert edge.meta["verified_by"] == "inbound-ground-backfill"
    assert edge.meta["inbound_ground"]["distance"] == 0.0
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT verdict FROM inbound_grounded WHERE claim_ref_id = %s "
            "AND paper_ref_id = %s",
            (claim, paper),
        ).fetchone()
    assert row == ("support",)


def test_backfill_retries_a_pair_whose_verifier_keeps_failing(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently failing verifier writes nothing, so the pair is never
    watermarked: each run re-ranks the same top-K and re-spends on it
    (bounded by the per-run pair cap), and the claim is never marked
    backfilled — pinned so the retry contract is explicit, not accidental."""
    embedder = make_mock_bge_m3()
    _seed_hub(store)
    paper, _ = _seed_paper(store, embedder, slug="stuck", texts=[_CLAIM])
    _age_paper(store, paper)
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("PRECIS_INBOUND_GROUND_TOPK", "5")
    calls: list[int] = []

    def failing(**kwargs: Any) -> None:
        calls.append(int(kwargs["target_cite_key"].split(":")[1]))
        return None

    for _ in range(2):
        result = run_inbound_ground_backfill_pass(
            store, embedder=embedder, verify_fn=failing
        )
        assert result["failed"] == 1 and result["pairs_checked"] == 0

    assert calls == [paper, paper]  # retried each run, never advanced
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM inbound_grounded").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM inbound_ground_backfilled"
        ).fetchone() == (0,)
