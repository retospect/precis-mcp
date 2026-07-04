"""``view='dreamable'`` — the focus region (#5 core, no clustering dep).

Two layers, mirroring the angle tests:

- store engine (``Store.dreamable_region``): seed-by-salience + ANN ring,
  card-inclusive, kind-filtered, side-effect-free.
- dispatch (``search(view='dreamable')``): routing, rendering, and the
  ``last_dreamt`` rotation stamp — end to end through ``runtime.dispatch``.

The salience score is ``last_seen - last_dreamt`` (both default to
``created_at`` → score 0). Tests set scores explicitly so seed choice is
deterministic rather than racing ``now()``.
"""

from __future__ import annotations

import re

from precis.embedder import MockEmbedder
from precis.runtime import PrecisRuntime
from precis.store import Store

_EMB = MockEmbedder(dim=1024)
_DEFAULT_EMBEDDER = "bge-m3"


def _embed_chunk(store: Store, ref_id: int, ord_: int, text: str) -> int:
    vec = _EMB.embed_one(text)
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
        assert row is not None
        cid = int(row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _DEFAULT_EMBEDDER, vec),
        )
    return cid


def _bare_chunk(store: Store, ref_id: int, ord_: int, text: str) -> int:
    """A chunk with no embedding (worker hasn't run)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _set_score(store: Store, chunk_id: int, score_seconds: float) -> None:
    """Make ``last_seen - last_dreamt = score_seconds`` for one chunk."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET last_seen = now(), "
            "  last_dreamt = now() - make_interval(secs => %s) WHERE chunk_id = %s",
            (score_seconds, chunk_id),
        )


# ── store engine: dreamable_region ──────────────────────────────────


def test_dreamable_empty_corpus(store: Store) -> None:
    assert store.dreamable_region() == (None, [])


def test_dreamable_seed_is_most_due(store: Store) -> None:
    p1 = store.insert_ref(kind="paper", slug="p1", title="P1", meta={})
    p2 = store.insert_ref(kind="paper", slug="p2", title="P2", meta={})
    seed = _embed_chunk(store, p1.id, 0, "copper nitrate reduction")
    _embed_chunk(store, p2.id, 0, "palladium hydrogen absorption")
    _set_score(store, seed, 100)  # most due

    seed_id, region = store.dreamable_region(n=5)
    assert seed_id == seed
    assert region  # non-empty
    # the seed itself is its own nearest neighbour → first member
    assert region[0][0].id == seed


def test_dreamable_excludes_non_target_kinds(store: Store) -> None:
    paper = store.insert_ref(kind="paper", slug="pk", title="K", meta={})
    seed = _embed_chunk(store, paper.id, 0, "shared text")
    _set_score(store, seed, 100)
    todo = store.insert_ref(kind="todo", slug=None, title="t", meta={})
    _embed_chunk(store, todo.id, 0, "shared text")  # near-identical vec

    _seed_id, region = store.dreamable_region(n=10)
    assert all(ref.kind in ("paper", "memory") for _b, ref, _c in region)


def test_dreamable_seed_skips_unembedded_most_due(store: Store) -> None:
    # gr48249: on a partially-embedded corpus the dream seed must skip the
    # most-due UN-embedded chunk and land on an embedded one, so the region
    # is non-empty whenever any target chunk is embedded (patents mid-backfill
    # kept seeding a bare chunk and losing the whole patent dream anchor).
    paper = store.insert_ref(kind="paper", slug="pn", title="N", meta={})
    bare = _bare_chunk(store, paper.id, 0, "unembedded but most due")
    _set_score(store, bare, 100)
    other = store.insert_ref(kind="paper", slug="po", title="O", meta={})
    embedded = _embed_chunk(store, other.id, 0, "embedded but less due")
    _set_score(store, embedded, 50)

    seed_id, region = store.dreamable_region()
    assert seed_id == embedded  # skipped the bare most-due chunk
    assert region  # non-empty
    assert region[0][0].id == embedded


def test_dreamable_n_limits_region(store: Store) -> None:
    paper = store.insert_ref(kind="paper", slug="pm", title="M", meta={})
    seed = _embed_chunk(store, paper.id, 0, "anchor")
    for i in range(1, 6):
        _embed_chunk(store, paper.id, i, f"neighbour {i}")
    _set_score(store, seed, 100)

    _seed_id, region = store.dreamable_region(n=2)
    assert len(region) == 2


def test_dreamable_region_is_side_effect_free(store: Store) -> None:
    paper = store.insert_ref(kind="paper", slug="ps", title="S", meta={})
    seed = _embed_chunk(store, paper.id, 0, "anchor")
    _set_score(store, seed, 100)

    store.dreamable_region()  # pure read — must not stamp last_dreamt
    # seed still most-due (score unchanged) → still selected
    assert store.select_dream_seed() == seed


# ── dispatch: search(view='dreamable') ──────────────────────────────


def _put_memory(rt: PrecisRuntime, text: str) -> int:
    out = rt.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    return int(m.group(1))


def _embed_card(rt: PrecisRuntime, ref_id: int, text: str) -> int:
    store = rt.hub.store
    assert store is not None
    (cid,) = store.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, rt.hub.embedder.embed_one(text)),
        )
    return cid


def test_dreamable_empty_renders_message(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"view": "dreamable"})
    assert "no dreamable region" in out


def test_dreamable_renders_region(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    a = _put_memory(rt, "copper catalyses nitrate reduction")
    cid = _embed_card(rt, a, "copper catalyses nitrate reduction")
    _set_score(rt.hub.store, cid, 100)

    out = rt.dispatch("search", {"view": "dreamable"})
    assert "region member" in out
    assert f"#{a}" in out


def test_dreamable_stamps_rotation(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    a = _put_memory(rt, "copper catalyses nitrate reduction")
    b = _put_memory(rt, "palladium absorbs hydrogen gas")
    ca = _embed_card(rt, a, "copper catalyses nitrate reduction")
    cb = _embed_card(rt, b, "palladium absorbs hydrogen gas")
    _set_score(store, ca, 100)  # a most due
    _set_score(store, cb, 50)  # b runner-up

    # n=1 → region is just the seed (a); surfacing stamps a.last_dreamt
    out = rt.dispatch("search", {"view": "dreamable", "n": 1})
    assert f"#{a}" in out
    # a now dreamt → its score drops below b → next seed rotates to b
    assert store.select_dream_seed() == cb
