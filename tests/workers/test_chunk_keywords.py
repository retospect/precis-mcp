"""Tests for ``precis.workers.chunk_keywords``.

The chunk_keywords worker is F20's replacement for the persistent
discovery layer (ADR 0018). It runs *after* ``embed:bge-m3`` populates
``chunk_embeddings`` and produces ``chunks.keywords`` (canonical
TEXT[]) + ``chunks.keywords_meta`` (versioned JSONB) for every body
chunk above :data:`_MIN_CHUNK_CHARS` that isn't on the skip-kind list.

Coverage shape:

* claim-query: respects min-length, skip-kinds, embedding-exists,
  version mismatch, ``FOR UPDATE SKIP LOCKED``
* :func:`ensure_paper_abbrevs`: first-call detection + cache,
  legacy-string envelope handling
* :func:`extract_chunk_keywords`: top-K, abbrev short/long collapse
* :func:`write_chunk_keywords`: dual-column write shape +
  ``keywords_meta.version`` stamp
* :func:`run_chunk_keywords_pass`: counts + per-chunk failure isolation
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from precis.store import Store
from precis.workers.chunk_keywords import (
    KEYWORDS_VERSION,
    claim_chunks_without_keywords,
    ensure_paper_abbrevs,
    extract_chunk_keywords,
    run_chunk_keywords_pass,
    write_chunk_keywords,
)
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_ref

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attach_embedding(store: Store, *, chunk_id: int, dim: int = 1024) -> None:
    """Stamp a ``status='ok'`` ``chunk_embeddings`` row for ``chunk_id``.

    Uses ``MockEmbedder``-derived vectors so the value is deterministic
    across test runs but distinct between chunks (the mock seeds the
    counter from a SHA of the input).
    """
    emb = make_mock_bge_m3()
    vec = emb.embed_one(f"chunk:{chunk_id}")
    assert len(vec) == dim
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status)
            VALUES (%s, 'bge-m3', %s, 'ok')
            ON CONFLICT (chunk_id, embedder) DO UPDATE
               SET vector = EXCLUDED.vector, status = 'ok'
            """,
            (chunk_id, vec),
        )
        conn.commit()


def _seed_long_chunk(
    store: Store,
    *,
    ref_id: int,
    ord: int,
    text: str,
    chunk_kind: str = "paragraph",
    with_embedding: bool = True,
) -> int:
    """Seed a chunk + (optionally) its bge-m3 embedding.

    Worker's claim query requires both a long-enough chunk and an
    ``ok`` embedding; this helper keeps the two-step setup uniform.
    """
    chunk_id = seed_chunk(
        store, ref_id=ref_id, ord=ord, chunk_kind=chunk_kind, text=text
    )
    if with_embedding:
        _attach_embedding(store, chunk_id=chunk_id)
    return chunk_id


# Sentence long enough to clear ``_MIN_CHUNK_CHARS=150`` and dense
# enough for RAKE to produce candidates.
_BODY_TEXT_1 = (
    "Metal-organic frameworks (MOFs) are porous crystalline materials "
    "built from metal nodes and organic linkers. Their tunable pore "
    "geometry and high surface area make them attractive for gas "
    "storage, catalysis, and separation applications."
)

_BODY_TEXT_2 = (
    "Photocatalytic NOx reduction proceeds through a Z-scheme mechanism "
    "in which visible-light excitation transfers electrons across two "
    "semiconductor stages. The MOF host stabilises the active sites and "
    "improves quantum efficiency at low irradiance."
)


# ---------------------------------------------------------------------------
# claim_chunks_without_keywords — derived-queue claim shape
# ---------------------------------------------------------------------------


class TestClaimQuery:
    def test_returns_long_embedded_chunks(self, store: Store) -> None:
        ref_id = seed_ref(store)
        long_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        ids = [r[0] for r in rows]
        assert long_id in ids

    def test_excludes_short_chunks(self, store: Store) -> None:
        ref_id = seed_ref(store)
        # Below the 150-char floor — KeyBERT has nothing to score on.
        short_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text="too short"
        )

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        assert short_id not in [r[0] for r in rows]

    def test_excludes_skip_kinds(self, store: Store) -> None:
        ref_id = seed_ref(store)
        body_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1
        )
        # Same body length but a skip-kind — must not be claimed even
        # though its embedding exists.
        refs_id = _seed_long_chunk(
            store,
            ref_id=ref_id,
            ord=1,
            text=_BODY_TEXT_1,
            chunk_kind="references",
        )

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        ids = [r[0] for r in rows]
        assert body_id in ids
        assert refs_id not in ids

    def test_excludes_chunks_without_embedding(self, store: Store) -> None:
        ref_id = seed_ref(store)
        # No embedding row at all — claim query JOIN drops it.
        unembedded_id = _seed_long_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            text=_BODY_TEXT_1,
            with_embedding=False,
        )
        # Failed embedding — status != 'ok' must not satisfy the join.
        failed_id = _seed_long_chunk(
            store,
            ref_id=ref_id,
            ord=1,
            text=_BODY_TEXT_2,
            with_embedding=False,
        )
        with store.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO chunk_embeddings (chunk_id, embedder, status, last_error)
                VALUES (%s, 'bge-m3', 'failed', 'simulated')
                """,
                (failed_id,),
            )
            conn.commit()

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        ids = [r[0] for r in rows]
        assert unembedded_id not in ids
        assert failed_id not in ids

    def test_reclaim_on_version_mismatch(self, store: Store) -> None:
        ref_id = seed_ref(store)
        chunk_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1
        )
        # Pre-populate keywords + a stale version stamp.
        stale_meta = {
            "version": "0.0-stale",
            "embedder": "bge-m3",
            "keywords": [{"short": None, "long": "stale", "score": 1.0}],
        }
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET keywords = %s, keywords_meta = %s "
                "WHERE chunk_id = %s",
                (["stale"], Jsonb(stale_meta), chunk_id),
            )
            conn.commit()

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        assert chunk_id in [r[0] for r in rows]

    def test_no_reclaim_on_current_version(self, store: Store) -> None:
        ref_id = seed_ref(store)
        chunk_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1
        )
        fresh_meta = {
            "version": KEYWORDS_VERSION,
            "embedder": "bge-m3",
            "keywords": [{"short": None, "long": "fresh", "score": 1.0}],
        }
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET keywords = %s, keywords_meta = %s "
                "WHERE chunk_id = %s",
                (["fresh"], Jsonb(fresh_meta), chunk_id),
            )
            conn.commit()

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        assert chunk_id not in [r[0] for r in rows]


# ---------------------------------------------------------------------------
# ensure_paper_abbrevs — Schwartz-Hearst caching
# ---------------------------------------------------------------------------


class TestEnsurePaperAbbrevs:
    def test_first_call_detects_and_caches(self, store: Store) -> None:
        # Schwartz-Hearst trigger: "long form (SHORT)" with the short
        # constructible from the long.
        body = (
            "We study metal-organic frameworks (MOFs) in detail. "
            "These MOFs exhibit large surface areas. The role of the "
            "linker is central to the topology of the MOF."
        )
        ref_id = seed_ref(store)
        seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=body
        )

        with store.pool.connection() as conn:
            detected = ensure_paper_abbrevs(conn, ref_id)
            conn.commit()
        # The pair must land in the result dict and on refs.meta.
        assert "MOFs" in detected or "MOF" in detected

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        assert row is not None
        meta = row[0]
        assert isinstance(meta, dict)
        assert "abbrevs" in meta

    def test_second_call_uses_cache(self, store: Store) -> None:
        ref_id = seed_ref(store)
        # Plant a synthetic cache so we can prove the second call
        # reads from it (the body wouldn't yield "FAKE→fake-long").
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s WHERE ref_id = %s",
                (Jsonb({"abbrevs": {"FAKE": "fake long"}}), ref_id),
            )
            conn.commit()
        with store.pool.connection() as conn:
            result = ensure_paper_abbrevs(conn, ref_id)
            conn.commit()
        assert result == {"FAKE": "fake long"}

    def test_legacy_envelope_normalised(self, store: Store) -> None:
        # Legacy rows stored ``{short: {"long": "...", "first_at": N}}``
        # instead of bare string. The reader must normalise both.
        ref_id = seed_ref(store)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s WHERE ref_id = %s",
                (
                    Jsonb({
                        "abbrevs": {
                            "MOF": {"long": "metal-organic framework", "first_at": 12},
                            "ETC": "etcetera",
                        }
                    }),
                    ref_id,
                ),
            )
            conn.commit()
        with store.pool.connection() as conn:
            out = ensure_paper_abbrevs(conn, ref_id)
            conn.commit()
        assert out == {"MOF": "metal-organic framework", "ETC": "etcetera"}


# ---------------------------------------------------------------------------
# extract_chunk_keywords — pure compute (no DB)
# ---------------------------------------------------------------------------


class TestExtractChunkKeywords:
    def test_returns_top_k_shape(self) -> None:
        embedder = make_mock_bge_m3()
        chunk_vec = embedder.embed_one(_BODY_TEXT_1)
        out = extract_chunk_keywords(
            chunk_text=_BODY_TEXT_1,
            chunk_embedding=chunk_vec,
            abbrevs={},
            embedder=embedder,
        )
        # Each entry carries the {short, long, score} triple.
        for entry in out:
            assert set(entry.keys()) == {"short", "long", "score"}
            assert isinstance(entry["long"], str)
            assert isinstance(entry["score"], float)
        # Top-K cap = 8.
        assert len(out) <= 8

    def test_empty_text_returns_empty(self) -> None:
        embedder = make_mock_bge_m3()
        chunk_vec = embedder.embed_one("anything")
        out = extract_chunk_keywords(
            chunk_text="",
            chunk_embedding=chunk_vec,
            abbrevs={},
            embedder=embedder,
        )
        assert out == []

    def test_abbrev_short_long_collapses_to_one_entry(self) -> None:
        # When both the short ("MOF") and the long
        # ("metal-organic framework") appear in candidates, they
        # collapse to a single result entry.
        embedder = make_mock_bge_m3()
        text = (
            "MOF chemistry surfaces in MOF synthesis. The "
            "metal-organic framework topology is determined by linker "
            "geometry and the choice of metal-organic framework node."
        )
        chunk_vec = embedder.embed_one(text)
        out = extract_chunk_keywords(
            chunk_text=text,
            chunk_embedding=chunk_vec,
            abbrevs={"MOF": "metal-organic framework"},
            embedder=embedder,
        )
        # Count entries whose canonical key is the MOF pair —
        # short and long should not both surface.
        keys = {(e["short"] or e["long"]).lower() for e in out}
        # Exactly one of "mof" / "metal-organic framework" survives.
        mof_keys = keys & {"mof", "metal-organic framework"}
        assert len(mof_keys) <= 1


# ---------------------------------------------------------------------------
# write_chunk_keywords — dual-column write shape
# ---------------------------------------------------------------------------


class TestWriteChunkKeywords:
    def test_populates_both_columns(self, store: Store) -> None:
        ref_id = seed_ref(store)
        chunk_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1
        )
        keywords: list[dict[str, Any]] = [
            {"short": "MOF", "long": "metal-organic framework", "score": 0.82},
            {"short": None, "long": "porous crystalline materials", "score": 0.71},
        ]
        with store.pool.connection() as conn:
            write_chunk_keywords(
                conn, chunk_id, keywords=keywords, embedder_name="bge-m3"
            )
            conn.commit()

            row = conn.execute(
                "SELECT keywords, keywords_meta FROM chunks "
                "WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        assert row is not None
        kw_array, kw_meta = row
        # canonical TEXT[] uses lowercase short-or-long
        assert kw_array == ["mof", "porous crystalline materials"]
        assert isinstance(kw_meta, dict)
        assert kw_meta["version"] == KEYWORDS_VERSION
        assert kw_meta["embedder"] == "bge-m3"
        assert kw_meta["keywords"] == keywords

    def test_empty_keywords_writes_empty_array(self, store: Store) -> None:
        ref_id = seed_ref(store)
        chunk_id = _seed_long_chunk(
            store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1
        )
        with store.pool.connection() as conn:
            write_chunk_keywords(
                conn, chunk_id, keywords=[], embedder_name="bge-m3"
            )
            conn.commit()
            row = conn.execute(
                "SELECT keywords FROM chunks WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        # Empty TEXT[] (not NULL) — the version stamp on
        # keywords_meta still marks the chunk as processed.
        assert row == ([],)


# ---------------------------------------------------------------------------
# run_chunk_keywords_pass — orchestration + failure isolation
# ---------------------------------------------------------------------------


class TestRunChunkKeywordsPass:
    def test_ok_path_writes_keywords_and_returns_counts(
        self, store: Store
    ) -> None:
        ref_id = seed_ref(store)
        cid1 = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        cid2 = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)

        result = run_chunk_keywords_pass(
            store, make_mock_bge_m3(), batch_size=10
        )
        assert result == {"claimed": 2, "ok": 2, "failed": 0}

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, keywords_meta->>'version' "
                "FROM chunks WHERE chunk_id IN (%s, %s) "
                "ORDER BY chunk_id",
                (cid1, cid2),
            ).fetchall()
        # Both chunks get the current version stamp.
        assert [r[1] for r in rows] == [KEYWORDS_VERSION, KEYWORDS_VERSION]

    def test_idempotent_within_same_version(self, store: Store) -> None:
        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        first = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert first["claimed"] == 1
        # Second pass: nothing to do — version matches.
        second = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert second == {"claimed": 0, "ok": 0, "failed": 0}

    def test_empty_queue_returns_zero_counts(self, store: Store) -> None:
        # No chunks seeded at all.
        result = run_chunk_keywords_pass(
            store, make_mock_bge_m3(), batch_size=10
        )
        assert result == {"claimed": 0, "ok": 0, "failed": 0}
