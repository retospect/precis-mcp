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

from precis.store.types import ChunkInsert
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import META_REJECTED, mint_hub
from precis.workers.inbound_ground import (
    INBOUND_GROUND_VERSION,
    classify_verdict,
    run_inbound_ground_pass,
)
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
