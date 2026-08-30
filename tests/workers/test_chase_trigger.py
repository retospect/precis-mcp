"""Scenario tests for ``precis.workers.chase_trigger`` (transient-napping-
parrot Phase 1 -- the taproot chase incremental trigger).

Real DB ``store`` fixture, same idiom as ``test_hub_refine.py``: a hub is a
real ``mint_hub``-minted ``TAPROOT:claim``/``STATUS:canonical`` finding, a
candidate is a real ``paper`` ref with an embedded body chunk
(``MockEmbedder`` -- deterministic, no live model). The match step needs
real pgvector ``<=>`` distance, which isn't meaningfully fakeable without
reimplementing cosine distance in Python, so this stays on the real test DB
rather than the FakeStore/FakeConn idiom used for the pure-branching
``slice_refine_eval`` harness tests.

``MockEmbedder`` is deterministic by exact text: identical text embeds to
the identical vector (distance 0, always "near"), and two materially
different sentences land far apart in the 1024-dim space (cosine distance
close to 1.0, always beyond the 0.45 default floor). Tests exploit that to
control "near" vs "far" without needing a real model.
"""

from __future__ import annotations

from typing import Any

from precis.store.types import ChunkInsert
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import link_claims, mint_hub
from precis.workers.chase_trigger import (
    _near_claims,
    run_chase_trigger_pass,
)
from tests.workers._helpers import make_mock_bge_m3

_MIN_SIM = 0.45


# ── seeding helpers (mirror test_hub_refine.py) ───────────────────────


def _seed_hub(store: Any, *, sentence: str, scope: dict[str, str] | None = None) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope=scope or {}))


def _seed_paper_chunk(
    store: Any, embedder: Any, *, cite_key: str, text: str
) -> tuple[int, int]:
    """Mint a paper with one embedded body chunk. Returns ``(ref_id, chunk_id)``."""
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
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


def _claim_embedding_sha(
    store: Any, hub_ref_id: int, embedder_model: str
) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT claim_sha FROM claim_embeddings "
            "WHERE claim_ref_id = %s AND embedder = %s",
            (hub_ref_id, embedder_model),
        ).fetchone()
    return None if row is None else str(row[0])


def _chunk_swept(store: Any, ref_id: int, ord_: int) -> bool:
    return any(
        t.namespace == "closed" and t.prefix == "CHASETRIG"
        for t in store.tags_for(ref_id, pos=ord_)
    )


# ── embedder-unavailable degrade ───────────────────────────────────────


def test_no_embedder_degrades_to_a_no_op(store: Any) -> None:
    _seed_hub(store, sentence="Pd/C catalyzes Suzuki coupling at RT.")

    result = run_chase_trigger_pass(store, embedder=None)
    assert result == {
        "claim_embeds": 0,
        "chunks_swept": 0,
        "due_marked": 0,
        "failed": 0,
    }


# ── (a) claim_embeddings refresh -- only on missing/stale sha ─────────


def test_claim_embeddings_refreshed_only_when_the_sha_changes(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A sha-refresh probe claim.")

    first = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert first["claim_embeds"] == 1
    sha_after_first = _claim_embedding_sha(store, hub, embedder.model)
    assert sha_after_first is not None

    # Unchanged title -- the sha already matches, no re-embed.
    second = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert second["claim_embeds"] == 0
    assert _claim_embedding_sha(store, hub, embedder.model) == sha_after_first

    # Edited title -- sha now differs -- re-embedded.
    store.update_ref(hub, title="A materially edited sha-refresh probe claim.")
    third = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert third["claim_embeds"] == 1
    assert _claim_embedding_sha(store, hub, embedder.model) != sha_after_first


# ── (b)/(c)/(d) sweep: near match marks due, far match doesn't, both swept ─


def test_near_chunk_marks_the_claim_due_far_chunk_does_not_both_swept(
    store: Any,
) -> None:
    embedder = make_mock_bge_m3()
    claim_sentence = "The device sustains 2.4 kV without breakdown."
    hub = _seed_hub(store, sentence=claim_sentence)

    # Identical text -> identical MockEmbedder vector -> distance 0 (near).
    near_ref, _near_chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="near", text=claim_sentence
    )
    # A materially different sentence -> far apart in the mock embedding
    # space -> beyond the 0.45 floor.
    far_ref, _far_chunk_id = _seed_paper_chunk(
        store,
        embedder,
        cite_key="far",
        text="Zebra migration patterns in equatorial rainforest canopies.",
    )

    result = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert result["claim_embeds"] == 1
    assert result["chunks_swept"] == 2
    assert result["due_marked"] == 1
    assert result["failed"] == 0

    assert store.has_tag(hub, "TAPROOT_DUE", "1") is True
    # Every claimed chunk is marked swept, matched or not -- convergence.
    assert _chunk_swept(store, near_ref, 0) is True
    assert _chunk_swept(store, far_ref, 0) is True


def test_convergence_second_pass_resweeps_nothing(store: Any) -> None:
    """A chunk already carrying the current-version CHASETRIG marker is
    never re-claimed -- the queue drains."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A convergence probe claim.")
    _seed_paper_chunk(
        store,
        embedder,
        cite_key="conv",
        text="Something wholly unrelated to the claim.",
    )

    first = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert first["chunks_swept"] == 1
    assert first["due_marked"] == 0

    second = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert second["chunks_swept"] == 0
    assert second["due_marked"] == 0
    # unrelated to the sweep, but confirms step (a) is independently idle too
    assert second["claim_embeds"] == 0
    assert store.has_tag(hub, "TAPROOT_DUE", "1") is False


def test_batch_size_caps_chunks_claimed_per_pass(store: Any) -> None:
    embedder = make_mock_bge_m3()
    _seed_hub(store, sentence="A batch-size cap probe claim.")
    for i in range(3):
        _seed_paper_chunk(
            store,
            embedder,
            cite_key=f"batch-{i}",
            text=f"Unrelated sentence number {i}.",
        )

    result = run_chase_trigger_pass(
        store, embedder=embedder, batch_size=2, min_sim=_MIN_SIM, claim_refresh_limit=10
    )
    assert result["chunks_swept"] == 2


# ── self-match exclusion (defense-in-depth; see chase_trigger._near_claims) ─


def test_near_claims_excludes_a_claim_matching_its_own_source_chunk(store: Any) -> None:
    """A claim can never be marked due by a chunk that belongs to its own
    ref -- exercised directly against ``_near_claims`` since the sweep's
    own kind filter (paper/patent only) never lets a real ``finding`` hub
    chunk reach this path in production."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A self-match exclusion probe claim.")

    with store.pool.connection() as conn:
        # Seed a claim_embeddings row directly, and a chunk_embeddings row
        # on a chunk that (unrealistically) belongs to the hub's own
        # ref_id, to exercise the self-exclusion guard in isolation.
        vec = embedder.embed_one("A self-match exclusion probe claim.")
        conn.execute(
            "INSERT INTO claim_embeddings (claim_ref_id, embedder, claim_sha, vector) "
            "VALUES (%s, %s, 'deadbeef', %s)",
            (hub, embedder.model, vec),
        )
        chunk_row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', -2, 'card_combined', 'self') RETURNING chunk_id",
            (hub,),
        ).fetchone()
        assert chunk_row is not None
        self_chunk_id = int(chunk_row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (self_chunk_id, embedder.model, vec),
        )
        conn.commit()

        near = _near_claims(
            conn,
            [self_chunk_id],
            embedder_model=embedder.model,
            floor=_MIN_SIM,
            chunk_ref_map={self_chunk_id: hub},
        )
    assert near == set()


# ── compound-hub exclusion (docs/backlog/taproot-atomic-claims.md) ──────
#
# A compound claim hub (a live inbound `conjunct-of` edge from a live
# finding) must never be embedded/probed/marked TAPROOT_DUE here -- a
# due-mark hub_refine's own due-set query would never claim (it excludes
# compounds too) would just accumulate, an unpopped tag with no consumer.


def _link_conjunct(store: Any, *, atom: int, compound: int) -> None:
    assert link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )


def test_compound_hub_excluded_from_claim_embeddings_refresh(store: Any) -> None:
    """A compound claim hub never gets a ``claim_embeddings`` row -- (a)'s
    hub query excludes it outright, so it never becomes a probe target
    either."""
    embedder = make_mock_bge_m3()
    atom = _seed_hub(store, sentence="A compound-exclusion probe atomic claim.")
    compound = _seed_hub(store, sentence="A compound-exclusion probe compound claim.")
    _link_conjunct(store, atom=atom, compound=compound)

    result = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert result["claim_embeds"] == 1  # the atom only
    assert _claim_embedding_sha(store, atom, embedder.model) is not None
    assert _claim_embedding_sha(store, compound, embedder.model) is None


def test_compound_hub_excluded_from_due_marking_even_with_a_stale_embedding_row(
    store: Any,
) -> None:
    """Belt-and-suspenders: even if a compound hub already carries a
    ``claim_embeddings`` row (seeded directly here, simulating a hub that
    became compound AFTER an earlier refresh already wrote one), the probe
    query's own exclusion still keeps it from being marked ``TAPROOT_DUE``."""
    embedder = make_mock_bge_m3()
    claim_sentence = "A due-marking exclusion probe compound claim."
    atom = _seed_hub(store, sentence="A due-marking exclusion probe atomic claim.")
    compound = _seed_hub(store, sentence=claim_sentence)
    _link_conjunct(store, atom=atom, compound=compound)

    with store.pool.connection() as conn:
        vec = embedder.embed_one(claim_sentence)
        conn.execute(
            "INSERT INTO claim_embeddings (claim_ref_id, embedder, claim_sha, vector) "
            "VALUES (%s, %s, 'deadbeef', %s)",
            (compound, embedder.model, vec),
        )
        conn.commit()

    # Identical text -> identical MockEmbedder vector -> distance 0 (near).
    _seed_paper_chunk(store, embedder, cite_key="due-exclusion", text=claim_sentence)

    result = run_chase_trigger_pass(
        store,
        embedder=embedder,
        batch_size=10,
        min_sim=_MIN_SIM,
        claim_refresh_limit=10,
    )
    assert store.has_tag(compound, "TAPROOT_DUE", "1") is False
    assert result["due_marked"] == 0


def test_near_claims_excludes_a_compound_claim(store: Any) -> None:
    """Direct check of the probe query's own exclusion, isolated from the
    refresh step: a ``claim_embeddings`` row for a compound hub, seeded
    directly, never surfaces from ``_near_claims`` even at distance 0."""
    embedder = make_mock_bge_m3()
    claim_sentence = "A direct near_claims compound-exclusion probe."
    atom = _seed_hub(store, sentence="A direct near_claims atomic conjunct probe.")
    compound = _seed_hub(store, sentence=claim_sentence)
    _link_conjunct(store, atom=atom, compound=compound)
    near_ref, near_chunk_id = _seed_paper_chunk(
        store, embedder, cite_key="near-compound", text=claim_sentence
    )

    with store.pool.connection() as conn:
        vec = embedder.embed_one(claim_sentence)
        conn.execute(
            "INSERT INTO claim_embeddings (claim_ref_id, embedder, claim_sha, vector) "
            "VALUES (%s, %s, 'deadbeef', %s)",
            (compound, embedder.model, vec),
        )
        conn.commit()

        near = _near_claims(
            conn,
            [near_chunk_id],
            embedder_model=embedder.model,
            floor=_MIN_SIM,
            chunk_ref_map={near_chunk_id: near_ref},
        )
    assert near == set()
