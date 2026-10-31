"""``scripts/_search_floor_calibrate.py`` — the gr514956 floor probe.

Percentile math and the floor proposal on synthetic distributions, plus
an end-to-end pass against the seeded test DB pinning the relevant/noise
SQL: the query chunk excludes itself, cards (``ord < 0``) and the probe's
``exclude_target_kinds`` never count as targets, and other-kind refs
never reach the noise pool.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

from precis.store import ChunkInsert, Store, Tag

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_search_floor_calibrate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_search_floor_calibrate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves annotations
    # through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _vec_at(dim: int, distance: float) -> list[float]:
    """Unit vector at cosine ``distance`` from the query vector e0."""
    c = 1.0 - distance
    v = [0.0] * dim
    v[0] = c
    v[1] = math.sqrt(1.0 - c * c)
    return v


def _e0(dim: int) -> list[float]:
    return _vec_at(dim, 0.0)


# -- pure functions ----------------------------------------------------------


def test_quantile_interpolates() -> None:
    mod = _load()
    assert mod.quantile([0.3, 0.1, 0.2], 0.5) == pytest.approx(0.2)
    assert mod.quantile([0.0, 1.0], 0.9) == pytest.approx(0.9)
    assert mod.quantile([0.4], 0.95) == pytest.approx(0.4)


def test_share_above_is_strict() -> None:
    mod = _load()
    # A hit at exactly the floor survives (the fused leg keeps d <= floor).
    assert mod.share_above([0.42, 0.50], 0.42) == pytest.approx(50.0)
    assert mod.share_above([], 0.5) == 0.0


def test_propose_floor_clean_separation() -> None:
    mod = _load()
    floor, why = mod.propose_floor([0.10, 0.20, 0.30], [0.50, 0.60], corpus_floor=0.65)
    assert floor == 0.40  # midpoint of relevant max .30 and noise min .50
    assert "separation" in why


def test_propose_floor_overlap_takes_rel_quantile() -> None:
    mod = _load()
    rel = [0.30 + 0.01 * i for i in range(20)]  # .30 … .49, overlapping noise
    floor, why = mod.propose_floor(rel, [0.40], corpus_floor=0.65, max_rel_loss=0.05)
    assert floor == pytest.approx(mod.quantile(rel, 0.95), abs=0.005)
    assert "overlap" in why


def test_propose_floor_capped_under_corpus_floor() -> None:
    mod = _load()
    # Bimodal web shape: a quarter of relevant sits past the corpus floor.
    rel = [0.30] * 15 + [0.92] * 5
    floor, why = mod.propose_floor(rel, [0.45], corpus_floor=0.65)
    assert floor == 0.55  # corpus − 0.10 cap, paper/web's landed shape
    assert "capped" in why


def test_propose_floor_no_relevant() -> None:
    mod = _load()
    floor, why = mod.propose_floor([], [0.4], corpus_floor=0.65)
    assert floor is None and "no relevant" in why


# -- end-to-end against the seeded test DB ------------------------------------


def _ref_with(
    store: Store, kind: str, slug: str | None, chunks: list[ChunkInsert]
) -> int:
    ref = store.insert_ref(kind=kind, slug=slug, title=slug or kind)
    store.chunks.insert_chunks(ref.id, chunks)
    return ref.id


def _embedder_name(store: Store) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_measure_kind_end_to_end(store: Store) -> None:
    mod = _load()
    dim = store.embedding_dim()
    q = _e0(dim)

    # ref A: a card_combined query chunk (e0) + body chunks at .20 / .30,
    # a card_title card at .02 — cards (ord < 0) are never targets — and
    # an unembedded body chunk that must be skipped.
    a = _ref_with(
        store,
        "memory",
        None,
        [
            ChunkInsert(
                ord=-1,
                text="card",
                meta={"chunk_kind": "card_combined"},
                embedding=q,
            ),
            ChunkInsert(
                ord=-2,
                text="title card",
                meta={"chunk_kind": "card_title"},
                embedding=_vec_at(dim, 0.02),
            ),
            ChunkInsert(
                ord=0,
                text="body near",
                meta={"chunk_kind": "memory_body"},
                embedding=_vec_at(dim, 0.20),
            ),
            ChunkInsert(
                ord=1,
                text="body far",
                meta={"chunk_kind": "memory_body"},
                embedding=_vec_at(dim, 0.30),
            ),
            ChunkInsert(
                ord=2,
                text="unembedded body",
                meta={"chunk_kind": "memory_body"},
            ),
        ],
    )
    # ref B (same kind): its card (the query vector is e0 like A's) +
    # body chunks at .35/.55.
    b = _ref_with(
        store,
        "memory",
        None,
        [
            ChunkInsert(
                ord=-1,
                text="card",
                meta={"chunk_kind": "card_combined"},
                embedding=q,
            ),
            ChunkInsert(
                ord=0,
                text="body",
                meta={"chunk_kind": "memory_body"},
                embedding=_vec_at(dim, 0.35),
            ),
            ChunkInsert(
                ord=1,
                text="body",
                meta={"chunk_kind": "memory_body"},
                embedding=_vec_at(dim, 0.55),
            ),
        ],
    )
    # A different-kind ref at .05 — must not reach memory's noise pool.
    _ref_with(
        store,
        "paper",
        "pa-c",
        [ChunkInsert(ord=0, text="body", embedding=_vec_at(dim, 0.05))],
    )

    probe = mod.PROBES["memory"]
    embedder = _embedder_name(store)
    with store.pool.connection() as conn:
        sampled = mod.sample_queries(
            conn, kind="memory", probe=probe, embedder=embedder, sample=100
        )
        assert {rid for _cid, rid, _v in sampled} == {a, b}
        queries = [(vec, rid, [cid]) for cid, rid, vec in sampled]
        m = mod.measure(
            conn,
            kind="memory",
            queries=queries,
            embedder=embedder,
            exclude_kinds=probe.exclude_target_kinds,
            noise_k=10,
        )
    # A's own best body is .20 (its .02 card_title would beat it if
    # cards counted); B's own best body is .35.
    assert sorted(m.rel) == pytest.approx([0.20, 0.35], abs=1e-4)
    # Noise pools the other ref's body chunks — never the cards nor
    # the .05 paper chunk.
    assert sorted(m.noise) == pytest.approx([0.20, 0.30, 0.35, 0.55], abs=1e-4)
    assert m.n_no_body == 0 and m.n_queries == 2


def test_comment_kinds_are_never_targets(store: Store) -> None:
    mod = _load()
    dim = store.embedding_dim()
    q = _e0(dim)

    # One gripe: two comments (queries in the method, both at e0) + the
    # body. If comments counted as targets each query's "relevant" would
    # be the sibling comment at 0.0, not the .25 body. The DB requires a
    # gripe to commit carrying exactly one STATUS tag — the check runs at
    # commit, so the tag must ride in the insert's own transaction.
    with store.pool.connection() as conn:
        g = store.insert_ref(kind="gripe", slug=None, title="gr-1", conn=conn).id
        store.chunks.insert_chunks(
            g,
            [
                ChunkInsert(
                    ord=0,
                    text="first comment",
                    meta={"chunk_kind": "gripe_comment"},
                    embedding=q,
                ),
                ChunkInsert(
                    ord=1,
                    text="second comment",
                    meta={"chunk_kind": "gripe_comment"},
                    embedding=q,
                ),
                ChunkInsert(
                    ord=2,
                    text="the gripe body",
                    meta={"chunk_kind": "gripe_body"},
                    embedding=_vec_at(dim, 0.25),
                ),
            ],
            conn=conn,
        )
        store.add_tag(g, Tag.closed("STATUS", "open"), set_by="agent", conn=conn)

    probe = mod.PROBES["gripe"]
    embedder = _embedder_name(store)
    with store.pool.connection() as conn:
        sampled = mod.sample_queries(
            conn, kind="gripe", probe=probe, embedder=embedder, sample=100
        )
        assert len(sampled) == 2
        queries = [(vec, rid, [cid]) for cid, rid, vec in sampled]
        m = mod.measure(
            conn,
            kind="gripe",
            queries=queries,
            embedder=embedder,
            exclude_kinds=probe.exclude_target_kinds,
            noise_k=10,
        )
    assert {rid for _cid, rid, _v in sampled} == {g}
    # Each comment's only valid target is the .25 body — not the sibling.
    assert sorted(m.rel) == pytest.approx([0.25, 0.25], abs=1e-4)
    assert m.noise == []  # no other gripe refs exist


def test_tsv_query_shape_no_self_exclusion(store: Store) -> None:
    """Free-text mode: ``Query`` with no chunk exclusion hits the whole ref."""
    mod = _load()
    dim = store.embedding_dim()
    q = _e0(dim)
    a = _ref_with(
        store,
        "memory",
        None,
        [
            ChunkInsert(
                ord=0,
                text="body",
                meta={"chunk_kind": "memory_body"},
                embedding=_vec_at(dim, 0.15),
            )
        ],
    )
    embedder = _embedder_name(store)
    with store.pool.connection() as conn:
        m = mod.measure(
            conn,
            kind="memory",
            queries=[(str(q), a, [])],
            embedder=embedder,
            exclude_kinds=(),
            noise_k=5,
        )
    assert m.rel == pytest.approx([0.15], abs=1e-4)


def test_report_renders_candidate_table() -> None:
    mod = _load()
    m = mod.KindMeasurement(kind="paper", n_queries=2, rel=[0.3, 0.4], noise=[0.5])
    out = mod.report(m, current=0.55)
    assert "paper" in out and "p10/p50/p90/max" in out
    assert "<- current" in out and "proposed" in out
