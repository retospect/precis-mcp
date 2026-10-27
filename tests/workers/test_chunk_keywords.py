"""Tests for ``precis.workers.chunk_keywords``.

The chunk_keywords worker is F20's replacement for the persistent
discovery layer. It runs *after* ``embed:bge-m3`` populates
``chunk_embeddings`` and produces ``chunks.keywords`` (canonical
TEXT[]) + ``chunks.keywords_meta`` (versioned JSONB) for every body
chunk above :data:`MIN_CHUNK_CHARS` that isn't on the skip-kind list.

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

from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from precis.store import Store
from precis.workers import chunk_keywords
from precis.workers.chunk_keywords import (
    KEYWORDS_VERSION,
    claim_chunks_without_keywords,
    ensure_paper_abbrevs,
    extract_chunk_keywords,
    run_chunk_keywords_pass,
    write_chunk_keywords,
)
from tests.workers._helpers import (
    make_mock_bge_m3,
    seed_chunk,
    seed_ref,
    skip_if_backends_multiplexed,
)

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


def _mark_stale(store: Store, chunk_id: int) -> None:
    """Stamp ``chunk_id`` as a tier-3 stale re-derive: keywords present,
    ``keywords_meta.version`` behind :data:`KEYWORDS_VERSION`."""
    stale_meta = {
        "version": "0.0-stale",
        "embedder": "bge-m3",
        "keywords": [{"short": None, "long": "stale", "score": 1.0}],
    }
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s, keywords_meta = %s WHERE chunk_id = %s",
            (["stale"], Jsonb(stale_meta), chunk_id),
        )
        conn.commit()


# Sentence long enough to clear ``MIN_CHUNK_CHARS=150`` and dense
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
        short_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text="too short")

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10)
            conn.commit()
        assert short_id not in [r[0] for r in rows]

    def test_excludes_skip_kinds(self, store: Store) -> None:
        ref_id = seed_ref(store)
        body_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
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
        chunk_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
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
        chunk_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
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

    def test_fresh_backlog_claims_newest_first(self, store: Store) -> None:
        """gr520236: fresh (keywords NULL) claims walk ``chunk_id DESC``
        so a fresh ingest is labelled ahead of the older backlog — and the
        pending partial index makes the walk cheap."""
        ref_id = seed_ref(store)
        old_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        new_id = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.rollback()
        assert [r[0] for r in rows] == [new_id]
        assert new_id > old_id

    def test_fresh_tier_beats_stale_rederive(self, store: Store) -> None:
        """The stale re-derive tier only fills what the fresh tiers left:
        a version-stale chunk waits behind unkeyworded ones."""
        ref_id = seed_ref(store)
        stale_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        fresh_id = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        stale_meta = {
            "version": "0.0-stale",
            "embedder": "bge-m3",
            "keywords": [{"short": None, "long": "stale", "score": 1.0}],
        }
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET keywords = %s, keywords_meta = %s "
                "WHERE chunk_id = %s",
                (["stale"], Jsonb(stale_meta), stale_id),
            )
            conn.commit()
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.rollback()
        assert [r[0] for r in rows] == [fresh_id]
        # limit 2 picks up the stale row too.
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=2)
            conn.rollback()
        assert {r[0] for r in rows} == {fresh_id, stale_id}

    def test_pending_partial_index_exists(self, store: Store) -> None:
        """The DESC fresh claim's cheapness depends on migration 0261's
        partial index existing and being valid."""
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'chunks_keywords_pending_idx'"
            ).fetchone()
        assert row == (True,)

    def test_claim_literals_match_index_predicate(self) -> None:
        """gr520236 review: the partial index only helps when the
        planner can prove the claim WHERE implies its predicate — the
        claim inlines :func:`_eligibility_sql` literals, and the
        migration's predicate must be the same text, including inside
        the ops hints (the wrong-predicate guard refuses with a
        DROP + runnable CREATE INDEX CONCURRENTLY rebuild)."""
        mig = (
            Path(chunk_keywords.__file__).resolve().parent.parent
            / "migrations"
            / "0261_chunks_keywords_pending_idx.sql"
        ).read_text(encoding="utf-8")
        # Unescape SQL '' -> ' so literals embedded in HINT strings
        # compare against the claim's own text; collapse whitespace so
        # statements split across lines still match.
        norm = " ".join(mig.replace("''", "'").split())
        want = " ".join(
            f"keywords IS NULL AND {chunk_keywords._eligibility_sql('')}".split()
        )
        assert want in norm
        # The guard's rebuild hints carry the same literals as a
        # runnable statement.
        assert (
            "CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx "
            f"ON chunks (chunk_id DESC) WHERE {want}"
        ) in norm
        # … and the predicate check itself compares in Postgres's
        # deparsed form (NOT IN -> <> ALL over an ARRAY[...] literal,
        # explicit ::text casts).
        kinds = ", ".join(f"'{k}'::text" for k in sorted(chunk_keywords.SKIP_KINDS))
        deparse = (
            f"((keywords IS NULL) AND (chunk_kind <> ALL (ARRAY[{kinds}])) "
            f"AND (length(text) >= {chunk_keywords.MIN_CHUNK_CHARS}) "
            "AND ((meta ->> 'no_index'::text) IS DISTINCT FROM 'true'::text))"
        )
        assert deparse in norm

    def test_index_predicate_matches_postgres_deparse(self, store: Store) -> None:
        """The 0261 guard compares the recorded indexdef to a literal
        deparse — pin that literal to what this Postgres actually
        prints for the live index."""
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'chunks_keywords_pending_idx'"
            ).fetchone()
        assert row is not None
        kinds = ", ".join(f"'{k}'::text" for k in sorted(chunk_keywords.SKIP_KINDS))
        assert row[0] == (
            f"((keywords IS NULL) AND (chunk_kind <> ALL (ARRAY[{kinds}])) "
            f"AND (length(text) >= {chunk_keywords.MIN_CHUNK_CHARS}) "
            "AND ((meta ->> 'no_index'::text) IS DISTINCT FROM 'true'::text))"
        )


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
        seed_chunk(store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=body)

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
                    Jsonb(
                        {
                            "abbrevs": {
                                "MOF": {
                                    "long": "metal-organic framework",
                                    "first_at": 12,
                                },
                                "ETC": "etcetera",
                            }
                        }
                    ),
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
        chunk_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
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
                "SELECT keywords, keywords_meta FROM chunks WHERE chunk_id = %s",
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
        chunk_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        with store.pool.connection() as conn:
            write_chunk_keywords(conn, chunk_id, keywords=[], embedder_name="bge-m3")
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
    def test_ok_path_writes_keywords_and_returns_counts(self, store: Store) -> None:
        ref_id = seed_ref(store)
        cid1 = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        cid2 = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)

        result = run_chunk_keywords_pass(store, make_mock_bge_m3(), batch_size=10)
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

    def test_lock_held_elsewhere_skips_the_pass(self, store: Store) -> None:
        """A held single-runner lock => graceful ``claimed=0`` skip, and the
        work is picked up once the holder releases (gr463966)."""
        import precis.workers.chunk_keywords as ck
        from precis.store.advisory import try_xact_advisory_lock

        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        assert store.dsn
        skip_if_backends_multiplexed(store.dsn)
        emb = make_mock_bge_m3()
        with try_xact_advisory_lock(store.dsn, ck._LOCK_KEY) as held:
            assert held is True
            skipped = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert skipped == {"claimed": 0, "ok": 0, "failed": 0}
        # Lock released on exit: the same chunk is now processed.
        done = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert done == {"claimed": 1, "ok": 1, "failed": 0}

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
        result = run_chunk_keywords_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

    def test_pooled_candidate_embeds(self, store: Store, monkeypatch: Any) -> None:
        """gr520236: the batch pools all chunks' RAKE candidates into
        shared ``embed`` calls instead of one call per chunk."""
        ref_id = seed_ref(store)
        for i in range(3):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        calls: list[int] = []
        real_embed = emb.embed

        def counting(texts: list[str]) -> list[list[float]]:
            calls.append(len(texts))
            return real_embed(texts)

        monkeypatch.setattr(emb, "embed", counting)
        result = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert result == {"claimed": 3, "ok": 3, "failed": 0}
        assert sum(calls) > 0
        assert len(calls) <= 1, f"expected one pooled call, got {calls}"

    def test_pooled_failure_falls_back_per_chunk(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A non-retryable pooled-embed failure must not sink the batch:
        the pass drops to per-chunk calls so the fault stays scoped."""
        ref_id = seed_ref(store)
        for i in range(2):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        real_embed = emb.embed

        def picky(texts: list[str]) -> list[list[float]]:
            if len(texts) > 45:
                raise RuntimeError("too many texts")
            return real_embed(texts)

        monkeypatch.setattr(emb, "embed", picky)
        result = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert result == {"claimed": 2, "ok": 2, "failed": 0}

    def test_stale_claim_runs_every_nth_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr520236 review: with no stale backlog draining, the tier-3
        stale re-derive claim — a ``chunk_embeddings`` seq-scan on every
        underfilled pass — only runs when the pass count hits
        :data:`_STALE_CLAIM_EVERY`."""
        ref_id = seed_ref(store)
        _mark_stale(
            store, _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        )
        # An off-multiple pass skips the stale claim entirely.
        monkeypatch.setattr(chunk_keywords, "_pass_count", 0)
        monkeypatch.setattr(chunk_keywords, "_stale_draining", False)
        result = run_chunk_keywords_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        # The _STALE_CLAIM_EVERY-th pass picks it up.
        monkeypatch.setattr(
            chunk_keywords,
            "_pass_count",
            chunk_keywords._STALE_CLAIM_EVERY - 1,
        )
        result = run_chunk_keywords_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result["claimed"] == 1 and result["ok"] == 1

    def test_stale_claim_drains_every_pass_until_empty(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr520236 follow-up: a live stale backlog (a KEYWORDS_VERSION
        re-derive drains only through tier 3) runs on EVERY pass while
        the previous tier-3 claim returned rows; the first empty claim
        drops the pace back to :data:`_STALE_CLAIM_EVERY`."""
        ref_id = seed_ref(store)
        emb = make_mock_bge_m3()
        monkeypatch.setattr(chunk_keywords, "_pass_count", 0)
        monkeypatch.setattr(chunk_keywords, "_stale_draining", False)

        _mark_stale(
            store, _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        )
        # Off-multiple, not draining: the stale claim is skipped.
        assert run_chunk_keywords_pass(store, emb, batch_size=10)["claimed"] == 0
        # The paced pass claims the stale chunk — a non-empty tier-3
        # claim latches draining on.
        monkeypatch.setattr(
            chunk_keywords,
            "_pass_count",
            chunk_keywords._STALE_CLAIM_EVERY - 1,
        )
        r = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert r["claimed"] == 1 and r["ok"] == 1
        # The very next pass is off-multiple but still runs tier 3.
        _mark_stale(
            store, _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        )
        r = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert r["claimed"] == 1 and r["ok"] == 1
        # Tier 3 runs once more, returns empty, and the pace falls back:
        # a fresh stale chunk now waits for the next multiple again.
        assert run_chunk_keywords_pass(store, emb, batch_size=10)["claimed"] == 0
        _mark_stale(
            store, _seed_long_chunk(store, ref_id=ref_id, ord=2, text=_BODY_TEXT_1)
        )
        assert run_chunk_keywords_pass(store, emb, batch_size=10)["claimed"] == 0
        monkeypatch.setattr(
            chunk_keywords,
            "_pass_count",
            chunk_keywords._STALE_CLAIM_EVERY - 1,
        )
        assert run_chunk_keywords_pass(store, emb, batch_size=10)["claimed"] == 1

    def test_fallback_skips_embed_for_empty_candidates(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr520236 review: a chunk whose RAKE pass produced no
        candidates costs no embed call on the per-chunk fallback
        path — an empty slice is scored as [] instead."""
        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        real_candidates = chunk_keywords._keyword_candidates

        def sparse(
            text: str, abbrevs: dict[str, str]
        ) -> tuple[list[str], list[tuple[str, str | None]]]:
            if text == _BODY_TEXT_2:
                return [], []
            return real_candidates(text, abbrevs)

        monkeypatch.setattr(chunk_keywords, "_keyword_candidates", sparse)
        emb = make_mock_bge_m3()
        seen: list[int] = []

        def boom(texts: list[str]) -> list[list[float]]:
            seen.append(len(texts))
            raise RuntimeError("embed down")

        monkeypatch.setattr(emb, "embed", boom)
        result = run_chunk_keywords_pass(store, emb, batch_size=10)
        # Chunk A fails on the falling embedder; chunk B scores [].
        assert result == {"claimed": 2, "ok": 1, "failed": 1}
        assert seen and all(n > 0 for n in seen)

    def test_db_level_failure_on_one_chunk_does_not_cascade(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A per-chunk savepoint isolates a genuine DB-statement abort.

        Before the per-chunk ``with conn.transaction():`` savepoint, a
        chunk that aborted the shared transaction (e.g. a failed SQL
        execute) poisoned the connection for every *subsequent* chunk in
        the batch — each of those then died with
        ``InFailedSqlTransaction`` even though nothing was wrong with
        them. The claim query orders by ``c.chunk_id`` ascending, so
        seeding the poison chunk first (lowest chunk_id) reproduces the
        worst case: it's processed before the two good chunks.
        """
        import precis.workers.chunk_keywords as chunk_keywords_mod

        ref_id = seed_ref(store)
        # Seeded first => lowest chunk_id => claimed/processed first.
        poison_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        good_id_1 = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        good_id_2 = _seed_long_chunk(store, ref_id=ref_id, ord=2, text=_BODY_TEXT_1)

        real_write_chunk_keywords = chunk_keywords_mod.write_chunk_keywords

        def _poisoning_write_chunk_keywords(
            conn: Any, chunk_id: int, *, keywords: Any, embedder_name: str
        ) -> None:
            if chunk_id == poison_id:
                # A genuine DB-level statement error (division by zero at
                # the SQL level) aborts the current transaction — unlike a
                # plain Python raise, this is what actually poisons a
                # shared connection/transaction pre-fix.
                conn.execute("SELECT 1 / 0")
            else:
                real_write_chunk_keywords(
                    conn, chunk_id, keywords=keywords, embedder_name=embedder_name
                )

        monkeypatch.setattr(
            chunk_keywords_mod,
            "write_chunk_keywords",
            _poisoning_write_chunk_keywords,
        )

        result = run_chunk_keywords_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result == {"claimed": 3, "ok": 2, "failed": 1}

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, keywords_meta->>'version' "
                "FROM chunks WHERE chunk_id IN (%s, %s, %s)",
                (poison_id, good_id_1, good_id_2),
            ).fetchall()
        versions: dict[int, str | None] = dict(rows)
        assert versions[good_id_1] == KEYWORDS_VERSION
        assert versions[good_id_2] == KEYWORDS_VERSION
        assert versions[poison_id] is None
