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

from typing import Any
from unittest.mock import Mock

import pytest
from psycopg.types.json import Jsonb

from precis.embedder import EmbedderUnavailable
from precis.store import Store
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
    def test_scores_and_abbrev_dedup_are_unchanged(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            Mock(return_value=["AB", "alpha batch", "zero", "opposite"]),
        )
        embedder = make_mock_bge_m3()
        embed = Mock(return_value=[[1.0, 0.0], [1.0, 0.0], [0.0, 0.0], [-1.0, 0.0]])
        monkeypatch.setattr(embedder, "embed", embed)
        assert extract_chunk_keywords(
            chunk_text="synthetic candidates",
            chunk_embedding=[1.0, 0.0],
            abbrevs={"AB": "alpha batch"},
            embedder=embedder,
        ) == [
            {"short": "AB", "long": "alpha batch", "score": 1.0},
            {"short": None, "long": "zero", "score": 0.0},
            {"short": None, "long": "opposite", "score": -1.0},
        ]
        embed.assert_called_once_with(
            ["alpha batch", "alpha batch", "zero", "opposite"]
        )

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
    @pytest.mark.parametrize("batch_size", [3, 32])
    def test_one_embed_call_matches_serial_keywords(
        self, store: Store, monkeypatch: Any, batch_size: int
    ) -> None:
        """Flattening preserves chunk slices and paper-local abbreviation scope."""
        refs = [seed_ref(store), seed_ref(store)]
        with store.pool.connection() as conn:
            for ref_id, long_form in zip(
                refs, ["alpha batch", "another batch"], strict=True
            ):
                conn.execute(
                    "UPDATE refs SET meta = %s WHERE ref_id = %s",
                    (Jsonb({"abbrevs": {"AB": long_form}}), ref_id),
                )
            conn.commit()
        empty_text = "the " * 60
        candidates = {
            _BODY_TEXT_1: ["AB", "alpha batch", "shared phrase"],
            empty_text: [],
            _BODY_TEXT_2: ["AB", "another batch", "shared phrase", "last phrase"],
        }
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            lambda text, *, max_keywords: candidates[text],
        )
        texts = [_BODY_TEXT_1, empty_text, _BODY_TEXT_2]
        for i in range(batch_size):
            _seed_long_chunk(
                store, ref_id=refs[i % 2], ord=i, text=texts[i % len(texts)]
            )
        embedder = make_mock_bge_m3()
        expected: dict[int, list[dict[str, Any]]] = {}
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=batch_size)
            for chunk_id, ref_id, text, vec_text in rows:
                assert vec_text is not None
                expected[chunk_id] = extract_chunk_keywords(
                    chunk_text=text,
                    chunk_embedding=[float(v) for v in vec_text.strip("[]").split(",")],
                    abbrevs=ensure_paper_abbrevs(conn, ref_id),
                    embedder=embedder,
                )
            conn.commit()
        embed = Mock(wraps=embedder.embed)
        monkeypatch.setattr(embedder, "embed", embed)

        result = run_chunk_keywords_pass(store, embedder, batch_size=batch_size)

        assert result == {"claimed": batch_size, "ok": batch_size, "failed": 0}
        embed.assert_called_once()
        # One call carrying each UNIQUE candidate text — the abbrev
        # mapping and shared phrases dedupe across chunks into one
        # embed slot per distinct text (was: every chunk's candidate
        # list flattened, duplicates embedded).
        assert sorted(embed.call_args.args[0]) == [
            "alpha batch",
            "another batch",
            "last phrase",
            "shared phrase",
        ]
        with store.pool.connection() as conn:
            written = conn.execute(
                "SELECT chunk_id, keywords, keywords_meta FROM chunks ORDER BY chunk_id"
            ).fetchall()
        assert len(written) == batch_size
        for chunk_id, canonical, meta in written:
            assert meta["keywords"] == expected[chunk_id]
            assert meta["version"] == KEYWORDS_VERSION
            assert meta["embedder"] == embedder.model
            assert canonical == [
                (k["short"] or k["long"]).lower() for k in expected[chunk_id]
            ]

    def test_all_empty_candidates_need_no_embed_call(
        self, store: Store, monkeypatch: Any
    ) -> None:
        ref_id = seed_ref(store)
        for i in range(2):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text="the " * 60)
        embedder = make_mock_bge_m3()
        embed = Mock(wraps=embedder.embed)
        monkeypatch.setattr(embedder, "embed", embed)

        assert run_chunk_keywords_pass(store, embedder) == {
            "claimed": 2,
            "ok": 2,
            "failed": 0,
        }
        embed.assert_not_called()
        with store.pool.connection() as conn:
            assert conn.execute("SELECT keywords FROM chunks").fetchall() == [
                ([],),
                ([],),
            ]

    @pytest.mark.parametrize("failure", ["unavailable", "short", "extra", "exception"])
    def test_failed_batch_writes_nothing_and_can_retry(
        self, store: Store, monkeypatch: Any, failure: str
    ) -> None:
        ref_id = seed_ref(store)
        for i in range(2):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
        embedder = make_mock_bge_m3()
        real_embed = embedder.embed

        def broken_embed(texts: list[str]) -> list[list[float]]:
            if failure == "unavailable":
                raise EmbedderUnavailable("temporarily busy")
            if failure == "exception":
                raise RuntimeError("bad embed response")
            vectors = real_embed(texts)
            return vectors[:-1] if failure == "short" else vectors + [vectors[0]]

        embed = Mock(side_effect=broken_embed)
        monkeypatch.setattr(embedder, "embed", embed)
        result = run_chunk_keywords_pass(store, embedder)
        assert result == (
            {"claimed": 0, "ok": 0, "failed": 0}
            if failure == "unavailable"
            else {"claimed": 2, "ok": 0, "failed": 2}
        )
        embed.assert_called_once()
        with store.pool.connection() as conn:
            assert conn.execute("SELECT keywords FROM chunks").fetchall() == [
                (None,),
                (None,),
            ]
        monkeypatch.setattr(embedder, "embed", real_embed)
        assert run_chunk_keywords_pass(store, embedder) == {
            "claimed": 2,
            "ok": 2,
            "failed": 0,
        }

    @pytest.mark.parametrize("stage", ["prepare", "score"])
    def test_bad_chunk_does_not_shift_later_slices(
        self, store: Store, monkeypatch: Any, stage: str
    ) -> None:
        ref_id = seed_ref(store)
        bad_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        good_id = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        embedder = make_mock_bge_m3()
        if stage == "prepare":

            def bad_candidates(text: str, *, max_keywords: int) -> list[str]:
                if text == _BODY_TEXT_1:
                    raise ValueError("bad candidates")
                return ["good phrase"]

            monkeypatch.setattr(
                "precis.workers.chunk_keywords._rake_phrases", bad_candidates
            )
        else:
            # The claim walks chunk_id DESC, so don't poison "the first
            # pooled vector" — poison the vector for a candidate only
            # the bad chunk produces.
            monkeypatch.setattr(
                "precis.workers.chunk_keywords._rake_phrases",
                lambda text, *, max_keywords: (
                    ["poison phrase"] if text == _BODY_TEXT_1 else ["good phrase"]
                ),
            )
            real_embed = embedder.embed

            def bad_vector(texts: list[str]) -> list[list[float]]:
                vectors = real_embed(texts)
                if "poison phrase" in texts:
                    vectors[texts.index("poison phrase")] = [1.0]
                return vectors

            monkeypatch.setattr(embedder, "embed", bad_vector)

        assert run_chunk_keywords_pass(store, embedder) == {
            "claimed": 2,
            "ok": 1,
            "failed": 1,
        }
        with store.pool.connection() as conn:
            written: dict[int, list[str] | None] = dict(
                conn.execute("SELECT chunk_id, keywords FROM chunks").fetchall()
            )
        assert written[bad_id] is None
        assert written[good_id]

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

    def test_db_level_failure_on_one_chunk_does_not_cascade(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A bad row aborts the bulk write, then the per-chunk fallback
        isolates it — the two good chunks still write.

        The bulk ``UPDATE ... FROM (VALUES ...)`` is one statement per
        slice: a genuine DB-level abort (division by zero at the SQL
        level — not a plain Python raise, this is what actually poisons
        a transaction) fails the slice, and the fallback's per-chunk
        ``write_chunk_keywords`` writes the survivors individually.
        """
        import precis.workers.chunk_keywords as chunk_keywords_mod

        ref_id = seed_ref(store)
        poison_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        good_id_1 = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        good_id_2 = _seed_long_chunk(store, ref_id=ref_id, ord=2, text=_BODY_TEXT_1)

        real_write_chunk_keywords = chunk_keywords_mod.write_chunk_keywords
        real_bulk = chunk_keywords_mod.write_chunk_keywords_bulk

        def _poisoning_write_chunk_keywords(
            conn: Any, chunk_id: int, *, keywords: Any, embedder_name: str
        ) -> None:
            if chunk_id == poison_id:
                conn.execute("SELECT 1 / 0")
            else:
                real_write_chunk_keywords(
                    conn, chunk_id, keywords=keywords, embedder_name=embedder_name
                )

        def _poisoning_bulk(conn: Any, items: Any, *, embedder_name: str) -> int:
            if any(chunk_id == poison_id for chunk_id, _ in items):
                conn.execute("SELECT 1 / 0")
                return 0
            return real_bulk(conn, items, embedder_name=embedder_name)

        monkeypatch.setattr(
            chunk_keywords_mod,
            "write_chunk_keywords",
            _poisoning_write_chunk_keywords,
        )
        monkeypatch.setattr(
            chunk_keywords_mod,
            "write_chunk_keywords_bulk",
            _poisoning_bulk,
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

    def test_embed_call_text_cap_chunks_one_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """``_EMBED_CALL_MAX_TEXTS`` bounds one call's texts; a batch
        bigger than the cap lands in a bounded few calls — never one
        per chunk or per phrase — and each text still embeds once."""
        import precis.workers.chunk_keywords as ck

        ref_id = seed_ref(store)
        bodies = [f"{_BODY_TEXT_1} Variant {i}." for i in range(4)]
        for i, text in enumerate(bodies):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=text)
        # Two fresh candidates per chunk → 8 unique texts; cap 2 forces
        # one flush per chunk.
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            lambda text, *, max_keywords: [f"{text[-9:]} a", f"{text[-9:]} b"],
        )
        monkeypatch.setattr(ck, "_EMBED_CALL_MAX_TEXTS", 2)
        embedder = make_mock_bge_m3()
        embed = Mock(wraps=embedder.embed)
        monkeypatch.setattr(embedder, "embed", embed)

        result = run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert result == {"claimed": 4, "ok": 4, "failed": 0}
        assert embed.call_count == 4
        seen: list[str] = []
        for call in embed.call_args_list:
            texts = call.args[0]
            assert len(texts) <= 2
            seen.extend(texts)
        assert len(seen) == len(set(seen)) == 8

    def test_wall_clock_cap_defers_rest_of_claim(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """The ``max_seconds`` deadline stops preparing new chunks mid-
        claim, so a big batch can't hold the advisory lock for minutes;
        the deferred tail re-claims cleanly next pass (gr549751)."""
        import precis.workers.chunk_keywords as ck

        ref_id = seed_ref(store)
        bodies = [f"{_BODY_TEXT_1} Variant {i}." for i in range(5)]
        for i, text in enumerate(bodies):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=text)
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            lambda text, *, max_keywords: [f"{text[-9:]} a", f"{text[-9:]} b"],
        )
        monkeypatch.setattr(ck, "_EMBED_CALL_MAX_TEXTS", 2)
        now = 0.0

        def _clock() -> float:
            return now

        embedder = make_mock_bge_m3()
        real_embed = embedder.embed

        def slow_embed(texts: list[str]) -> list[list[float]]:
            nonlocal now
            now += 30.0
            return real_embed(texts)

        monkeypatch.setattr(embedder, "embed", slow_embed)

        result = run_chunk_keywords_pass(
            store,
            embedder,
            batch_size=10,
            max_seconds=60.0,
            clock=_clock,
        )

        # deadline=60: chunk 1 flushes at t=30, chunk 2 at t=60 — the
        # third row sees clock >= deadline and the pass stops. The
        # remaining 3 are claimed but unwritten, so they stay claimable.
        assert result == {"claimed": 5, "ok": 2, "failed": 0}
        with store.pool.connection() as conn:
            assert conn.execute(
                "SELECT count(*) FROM chunks WHERE keywords IS NULL"
            ).fetchone() == (3,)

        drained = run_chunk_keywords_pass(
            store,
            embedder,
            batch_size=10,
            max_seconds=600.0,
            clock=_clock,
        )
        assert drained == {"claimed": 3, "ok": 3, "failed": 0}

    def test_zero_max_seconds_claims_nothing(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """``max_seconds=0`` parks the drain without touching service
        prio: nothing claims, no embed call fires."""
        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        embedder = make_mock_bge_m3()
        embed = Mock(wraps=embedder.embed)
        monkeypatch.setattr(embedder, "embed", embed)

        assert run_chunk_keywords_pass(store, embedder, max_seconds=0.0) == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
        }
        embed.assert_not_called()
        with store.pool.connection() as conn:
            assert conn.execute("SELECT keywords FROM chunks").fetchall() == [(None,)]

    def test_batch_size_env_is_the_passs_own_knob(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """``PRECIS_CHUNK_KEYWORDS_BATCH_SIZE`` sizes the claim when the
        caller passes ``batch_size=None`` — which the worker closure
        always does now that the shared ``--batch-size`` no longer
        leaks in (gr549751)."""
        ref_id = seed_ref(store)
        for i in range(5):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        monkeypatch.setenv("PRECIS_CHUNK_KEYWORDS_BATCH_SIZE", "2")
        assert run_chunk_keywords_pass(store, emb) == {
            "claimed": 2,
            "ok": 2,
            "failed": 0,
        }
        # Unset → the 256-chunk default claims whatever the cap admits.
        monkeypatch.delenv("PRECIS_CHUNK_KEYWORDS_BATCH_SIZE")
        assert run_chunk_keywords_pass(store, emb) == {
            "claimed": 3,
            "ok": 3,
            "failed": 0,
        }

    def test_max_seconds_env_parks_the_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        monkeypatch.setenv("PRECIS_CHUNK_KEYWORDS_MAX_SECONDS", "0")
        assert run_chunk_keywords_pass(store, emb) == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
        }
        monkeypatch.delenv("PRECIS_CHUNK_KEYWORDS_MAX_SECONDS")
        assert run_chunk_keywords_pass(store, emb) == {
            "claimed": 1,
            "ok": 1,
            "failed": 0,
        }


# ---------------------------------------------------------------------------
# Tiered claim — conv/draft first, fresh backlog DESC, stale last (gr520236)
# ---------------------------------------------------------------------------


def _mark_stale(store: Store, chunk_id: int) -> None:
    """Stamp ``chunk_id`` as a tier-3 stale re-derive: keywords present,
    version stale."""
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


class TestTieredClaim:
    @pytest.mark.parametrize("shard_count", [1, 2])
    def test_fresh_backlog_embedding_exists_keeps_planner_fence(
        self, shard_count: int
    ) -> None:
        """gr520236: tier 2 must not flatten EXISTS into a full embedding scan."""
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = []

        claim_chunks_without_keywords(conn, limit=500, shard_count=shard_count)

        queries = [
            " ".join(call.args[0].split()) for call in conn.execute.call_args_list
        ]
        assert len(queries) == 3
        tier1, tier2, tier3 = queries
        assert "OFFSET 0" not in tier1
        assert "OFFSET 0" not in tier3
        assert "c2.ref_id NOT IN (SELECT ref_id FROM kw_refs)" in tier2
        assert "AND c2.keywords IS NULL" in tier2
        candidate = tier2.split("kw_cand AS MATERIALIZED (", 1)[1]
        embedding_gate = candidate.split("AND EXISTS (", 1)[1].split(")", 1)[0]
        assert "FROM chunk_embeddings ce2" in embedding_gate
        assert embedding_gate.rstrip().endswith(
            "AND ce2.content_sha IS NOT DISTINCT FROM c2.content_sha OFFSET 0"
        )
        assert tier2.count("OFFSET 0") == 1

    def test_conv_and_draft_chunks_claim_ahead_of_papers(self, store: Store) -> None:
        """Tier 1 jumps the queue regardless of freshness: a conv/draft
        chunk wins over a NEWER paper chunk, and a stale conv chunk
        still claims in tier 1 (pending covers fresh and stale)."""
        conv_ref = seed_ref(store, kind="conv")
        paper_ref = seed_ref(store, kind="paper")
        # Conv chunk seeded FIRST — strictly older. Tier order, not
        # chunk_id, must pick it.
        conv_id = _seed_long_chunk(store, ref_id=conv_ref, ord=0, text=_BODY_TEXT_1)
        paper_id = _seed_long_chunk(store, ref_id=paper_ref, ord=0, text=_BODY_TEXT_2)
        assert conv_id < paper_id
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.rollback()
        assert [r[0] for r in rows] == [conv_id]
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=2)
            conn.rollback()
        assert {r[0] for r in rows} == {conv_id, paper_id}

        # A stale conv/draft chunk still claims via tier 1 — the
        # re-derive isn't pushed to tier 3 just because keywords exist.
        _mark_stale(store, conv_id)
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.rollback()
        assert [r[0] for r in rows] == [conv_id]

    def test_fresh_backlog_claims_newest_first(self, store: Store) -> None:
        """gr520236: fresh (keywords NULL) claims walk ``chunk_id DESC``
        so a fresh ingest is labelled ahead of the older backlog."""
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
        _mark_stale(store, stale_id)
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.rollback()
        assert [r[0] for r in rows] == [fresh_id]
        # limit 2 picks up the stale row too.
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=2)
            conn.rollback()
        assert {r[0] for r in rows} == {fresh_id, stale_id}

    def test_include_stale_false_skips_tier3(self, store: Store) -> None:
        ref_id = seed_ref(store)
        stale_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        _mark_stale(store, stale_id)
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10, include_stale=False)
            conn.rollback()
        assert stale_id not in [r[0] for r in rows]
        # … and with it on, the same call claims the stale chunk.
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=10, include_stale=True)
            conn.rollback()
        assert stale_id in [r[0] for r in rows]

    def test_claim_literals_match_pending_index_ddl(self) -> None:
        """The ops pre-built partial index only serves the DESC fresh
        claim when the claim's WHERE text matches its predicate — the
        claim inlines :func:`_eligibility_sql` literals, and
        :data:`PENDING_INDEX_DDL` must carry the same text (the migration
        that ships later builds exactly this statement)."""
        import precis.workers.chunk_keywords as chunk_keywords

        norm = " ".join(chunk_keywords.PENDING_INDEX_DDL.split())
        want = " ".join(
            f"keywords IS NULL AND {chunk_keywords._eligibility_sql('')}".split()
        )
        assert want in norm
        assert norm.startswith(
            "CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx "
            "ON chunks (chunk_id DESC) WHERE"
        )

    def test_shards_are_disjoint_and_covering(self, store: Store) -> None:
        """Lane ``i`` of ``N`` claims ``chunk_id % N = i`` only: two
        shards never see each other's chunks and together cover the
        queue."""
        ref_id = seed_ref(store)
        seeded = {
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
            for i in range(10)
        }
        with store.pool.connection() as conn:
            ids0 = {
                r[0]
                for r in claim_chunks_without_keywords(
                    conn, limit=100, shard_count=2, shard_index=0
                )
            }
            ids1 = {
                r[0]
                for r in claim_chunks_without_keywords(
                    conn, limit=100, shard_count=2, shard_index=1
                )
            }
            conn.rollback()
        assert ids0.isdisjoint(ids1)
        assert ids0 | ids1 == seeded
        assert all(i % 2 == 0 for i in ids0)
        assert all(i % 2 == 1 for i in ids1)

    def test_shard_index_must_be_below_count(self, store: Store) -> None:
        with store.pool.connection() as conn:
            with pytest.raises(ValueError, match="shard_index"):
                claim_chunks_without_keywords(
                    conn, limit=1, shard_count=2, shard_index=2
                )

    def test_four_way_shards_are_disjoint_and_covering(self, store: Store) -> None:
        """The global shard space (hosts × lanes): 2 hosts × 2 lanes =
        4 shards — distinct residue classes covering the queue, the
        math playbook 20g renders into ``--shard-count`` /
        ``--shard-index``."""
        ref_id = seed_ref(store)
        seeded = {
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
            for i in range(12)
        }
        claimed: list[set[int]] = []
        with store.pool.connection() as conn:
            for shard_index in range(4):
                claimed.append(
                    {
                        r[0]
                        for r in claim_chunks_without_keywords(
                            conn, limit=100, shard_count=4, shard_index=shard_index
                        )
                    }
                )
            conn.rollback()
        for i, ids in enumerate(claimed):
            assert all(c % 4 == i for c in ids)
        union = set().union(*claimed)
        assert union == seeded
        for i in range(4):
            for j in range(i + 1, 4):
                assert claimed[i].isdisjoint(claimed[j])

    def test_candidate_cap_counts_only_claimable_rows(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """The embedding gate is INSIDE ``kw_cand`` (0266 review): the
        cap counts claimable rows only, so a not-yet-embedded head
        block is walked past, never counted — the claim returns the
        embedded backlog deeper in the walk instead of starving."""
        import precis.workers.chunk_keywords as ck

        ref_id = seed_ref(store)
        # Two embedded chunks, then three NOT-yet-embedded at the head
        # (higher chunk_ids — the DESC walk sees them first).
        embedded = {
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
            for i in range(2)
        }
        for i in range(3):
            _seed_long_chunk(
                store,
                ref_id=ref_id,
                ord=10 + i,
                text=_BODY_TEXT_2,
                with_embedding=False,
            )
        # Cap at 1×limit: even the smallest window skips the
        # unembedded head and finds the two claimable rows.
        monkeypatch.setattr(ck, "_CANDIDATE_SCAN_MULT", 1)
        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=2)
            conn.rollback()
        assert {r[0] for r in rows} == embedded

    def test_deep_unembedded_head_does_not_starve_the_claim(self, store: Store) -> None:
        """3000 newest pending chunks with no embedding + 100 older
        embedded ones: a 500 claim returns the 100 embedded — the
        unembedded head is walked past, not counted toward the cap
        (0266 review: the outer-join-only shape returned 0 rows
        forever here)."""
        ref_id = seed_ref(store)
        vec = make_mock_bge_m3().embed_one("shared")
        with store.pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
                "SELECT %s, 'system', gs, 'paragraph', %s "
                "FROM generate_series(1, 100) gs RETURNING chunk_id",
                (ref_id, _BODY_TEXT_1),
            )
            emb_ids = {int(r[0]) for r in cur.fetchall()}
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, "
                "status) SELECT cid, 'bge-m3', %s, 'ok' "
                "FROM unnest(%s::bigint[]) AS cid",
                (vec, list(emb_ids)),
            )
            # 3000 newer pending chunks with NO embedding — the head
            # of the DESC walk.
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
                "SELECT %s, 'system', gs, 'paragraph', %s "
                "FROM generate_series(101, 3100) gs",
                (ref_id, _BODY_TEXT_2),
            )
            conn.commit()

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=500)
            conn.rollback()
        assert {r[0] for r in rows} == emb_ids


# ---------------------------------------------------------------------------
# write_chunk_keywords_bulk — UPDATE ... FROM (VALUES ...) (gr550973)
# ---------------------------------------------------------------------------


class TestBulkWrite:
    def test_bulk_write_matches_single_row_shape(self, store: Store) -> None:
        """The bulk write lands the same canonical TEXT[] + versioned
        envelope ``write_chunk_keywords`` produces."""
        from precis.workers.chunk_keywords import write_chunk_keywords_bulk

        ref_id = seed_ref(store)
        cid1 = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        cid2 = _seed_long_chunk(store, ref_id=ref_id, ord=1, text=_BODY_TEXT_2)
        keywords: list[dict[str, Any]] = [
            {"short": "MOF", "long": "metal-organic framework", "score": 0.82},
            {"short": None, "long": "porous crystalline materials", "score": 0.71},
        ]
        with store.pool.connection() as conn:
            n = write_chunk_keywords_bulk(
                conn,
                [(cid1, keywords), (cid2, keywords)],
                embedder_name="bge-m3",
            )
            conn.commit()
        assert n == 2
        with store.pool.connection() as conn:
            rows: dict[int, list[str]] = dict(
                conn.execute(
                    "SELECT chunk_id, keywords FROM chunks WHERE chunk_id IN (%s, %s)",
                    (cid1, cid2),
                ).fetchall()
            )
            metas: dict[int, dict[str, Any]] = dict(
                conn.execute(
                    "SELECT chunk_id, keywords_meta FROM chunks "
                    "WHERE chunk_id IN (%s, %s)",
                    (cid1, cid2),
                ).fetchall()
            )
        for cid in (cid1, cid2):
            assert rows[cid] == ["mof", "porous crystalline materials"]
            meta = metas[cid]
            assert meta["version"] == KEYWORDS_VERSION
            assert meta["embedder"] == "bge-m3"
            assert meta["keywords"] == keywords

    def test_bulk_write_stale_guard_skips_written(self, store: Store) -> None:
        """A second writer of an already-keyworded chunk is a no-op —
        the stale guard refuses rows whose version already matches."""
        from precis.workers.chunk_keywords import write_chunk_keywords_bulk

        ref_id = seed_ref(store)
        cid = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        first: list[dict[str, Any]] = [{"short": None, "long": "first", "score": 0.9}]
        second: list[dict[str, Any]] = [
            {"short": None, "long": "clobber", "score": 0.1}
        ]
        with store.pool.connection() as conn:
            assert (
                write_chunk_keywords_bulk(conn, [(cid, first)], embedder_name="bge-m3")
                == 1
            )
            # Same version, unchanged text → the guard refuses: the
            # duplicate claim window is a no-op, not a clobber.
            assert (
                write_chunk_keywords_bulk(conn, [(cid, second)], embedder_name="bge-m3")
                == 0
            )
            conn.commit()
            row = conn.execute(
                "SELECT keywords FROM chunks WHERE chunk_id = %s", (cid,)
            ).fetchone()
        assert row == (["first"],)


# ---------------------------------------------------------------------------
# run_dedicated_loop — the dedicated drain lane (gr520236 / gr550973)
# ---------------------------------------------------------------------------


class TestDedicatedLoop:
    def test_empty_queue_sleeps_then_rechecks(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """An empty claim rests ~idle_seconds then re-polls — never a
        hot loop."""
        import precis.workers.chunk_keywords as ck

        pass_calls: list[None] = []

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            pass_calls.append(None)
            return {"claimed": 0, "ok": 0, "failed": 0}

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        sleeps: list[float] = []
        ck.run_dedicated_loop(
            store,
            make_mock_bge_m3(),
            idle_seconds=60.0,
            should_stop=lambda: len(pass_calls) >= 3,
            sleep=sleeps.append,
        )
        assert len(pass_calls) == 3
        # Idle slicing slept at least once between re-checks.
        assert sleeps and all(0 < s <= 60.0 for s in sleeps)

    def test_claimed_work_loops_immediately(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A pass that claimed work loops at once — the 60 s rest is
        only for an empty queue."""
        import precis.workers.chunk_keywords as ck

        results = iter(
            [
                {"claimed": 2, "ok": 2, "failed": 0},
                {"claimed": 1, "ok": 1, "failed": 0},
                {"claimed": 0, "ok": 0, "failed": 0},
            ]
        )
        pass_calls: list[None] = []

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            pass_calls.append(None)
            return next(results)

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        sleeps: list[float] = []
        ck.run_dedicated_loop(
            store,
            make_mock_bge_m3(),
            idle_seconds=60.0,
            should_stop=lambda: len(pass_calls) >= 3,
            sleep=sleeps.append,
        )
        assert len(pass_calls) == 3
        # The empty third pass entered the rest loop, but the stop flag
        # landed before the first sleep slice — no sleep at all.
        assert sleeps == []

    def test_zero_yield_batch_backs_off(self, store: Store, monkeypatch: Any) -> None:
        """A pass that claimed rows but wrote none (ok == 0 — a poison
        head of queue: all failed, all deferred, or cut short) sleeps
        the idle interval instead of hot-looping the same batch."""
        import precis.workers.chunk_keywords as ck

        results = iter(
            [
                {"claimed": 2, "ok": 0, "failed": 2},
                {"claimed": 2, "ok": 0, "failed": 2},
            ]
        )
        pass_calls: list[None] = []

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            pass_calls.append(None)
            return next(results)

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        sleeps: list[float] = []
        ck.run_dedicated_loop(
            store,
            make_mock_bge_m3(),
            idle_seconds=0.3,
            should_stop=lambda: len(pass_calls) >= 2,
            sleep=sleeps.append,
        )
        assert len(pass_calls) == 2
        # Both passes backed off into the idle sleep — no hot re-poll.
        assert sleeps and all(0 < s <= 0.3 for s in sleeps)

    def test_half_yield_batch_still_loops(self, store: Store, monkeypatch: Any) -> None:
        """``ok >= claimed/2`` is progress — the loop continues without
        sleeping; below half is the poison back-off."""
        import precis.workers.chunk_keywords as ck

        results = iter(
            [
                {"claimed": 4, "ok": 2, "failed": 2},  # exactly half → loop
                {"claimed": 4, "ok": 1, "failed": 3},  # below half → back off
                {"claimed": 0, "ok": 0, "failed": 0},
            ]
        )
        events: list[str] = []

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            events.append("pass")
            return next(results)

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        ck.run_dedicated_loop(
            store,
            make_mock_bge_m3(),
            idle_seconds=0.3,
            should_stop=lambda: events.count("pass") >= 3,
            sleep=lambda s: events.append("sleep"),
        )
        # Pass 1's half yield continued straight into pass 2 — no
        # sleep; pass 2's below-half yield backed off before pass 3.
        assert events[:2] == ["pass", "pass"]
        assert events[2] == "sleep"

    def test_stop_flag_is_forwarded_into_the_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """``should_stop`` reaches ``run_chunk_keywords_pass`` — the
        mid-batch check lives inside the pass, not just the loop."""
        import precis.workers.chunk_keywords as ck

        captured: dict[str, Any] = {}
        pass_calls: list[None] = []

        def _stop() -> bool:
            return len(pass_calls) >= 1

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            pass_calls.append(None)
            captured.update(kwargs)
            return {"claimed": 0, "ok": 0, "failed": 0}

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        ck.run_dedicated_loop(
            store,
            make_mock_bge_m3(),
            should_stop=_stop,
            sleep=lambda s: None,
        )
        assert captured["should_stop"] is _stop

    def test_result_logged_as_runner_payload(
        self, store: Store, monkeypatch: Any, caplog: Any
    ) -> None:
        """The drain ledger stays the ``pass='runner'`` /
        ``payload.handler='chunk_keywords'`` shape the health probes
        read — logged through the runner logger with the run_loop
        payload keys."""
        import logging

        import precis.workers.chunk_keywords as ck

        pass_calls: list[None] = []

        def _fake_pass(*args: Any, **kwargs: Any) -> dict[str, int]:
            pass_calls.append(None)
            return {"claimed": 2, "ok": 2, "failed": 0}

        monkeypatch.setattr(ck, "run_chunk_keywords_pass", _fake_pass)
        with caplog.at_level(logging.INFO, logger="precis.workers.runner"):
            ck.run_dedicated_loop(
                store,
                make_mock_bge_m3(),
                should_stop=lambda: len(pass_calls) >= 1,
                sleep=lambda s: None,
            )
        rec = next(
            r
            for r in caplog.records
            if r.name == "precis.workers.runner" and "chunk_keywords" in r.message
        )
        assert rec.message == "worker: chunk_keywords claimed=2 ok=2 failed=0"
        payload = rec.payload
        assert payload["handler"] == "chunk_keywords"
        assert payload["ok"] == 2
        assert "duration_ms" in payload

    def test_shard_index_must_be_below_count(self, store: Store) -> None:
        import precis.workers.chunk_keywords as ck

        with pytest.raises(ValueError, match="shard_index"):
            ck.run_dedicated_loop(
                store,
                make_mock_bge_m3(),
                shard_count=2,
                shard_index=2,
            )


# ---------------------------------------------------------------------------
# Parallel-safe pass — lock-free dedicated path (gr520236 / gr550973)
# ---------------------------------------------------------------------------


class TestParallelPass:
    def test_two_shards_cover_without_overlap(self, store: Store) -> None:
        """Two dedicated lanes on N=2 write disjoint chunk sets and
        together drain the queue — no advisory lock needed."""
        ref_id = seed_ref(store)
        seeded = {
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
            for i in range(10)
        }
        emb = make_mock_bge_m3()
        r0 = run_chunk_keywords_pass(
            store,
            emb,
            batch_size=100,
            single_runner=False,
            shard_count=2,
            shard_index=0,
        )
        r1 = run_chunk_keywords_pass(
            store,
            emb,
            batch_size=100,
            single_runner=False,
            shard_count=2,
            shard_index=1,
        )
        assert r0["claimed"] + r1["claimed"] == len(seeded)
        with store.pool.connection() as conn:
            written = {
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE keywords IS NOT NULL"
                ).fetchall()
            }
        assert written == seeded
        assert {i for i in written if i % 2 == 0} <= {i for i in seeded if i % 2 == 0}
        # Nothing left for either shard.
        assert run_chunk_keywords_pass(
            store,
            emb,
            batch_size=100,
            single_runner=False,
            shard_count=2,
            shard_index=0,
        ) == {"claimed": 0, "ok": 0, "failed": 0}

    def test_no_connection_open_during_embed(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """No transaction is open across the embedder call: when
        ``embedder.embed`` runs, every pool connection is back in the
        pool (claim commits before the slow HTTP work)."""
        ref_id = seed_ref(store)
        for i in range(2):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1)
        emb = make_mock_bge_m3()
        real_embed = emb.embed
        checked: list[bool] = []

        def embed_checking_pool(texts: list[str]) -> list[list[float]]:
            stats = store.pool.get_stats()
            checked.append(stats["pool_available"] == stats["pool_size"])
            return real_embed(texts)

        monkeypatch.setattr(emb, "embed", embed_checking_pool)
        result = run_chunk_keywords_pass(store, emb, batch_size=10, single_runner=False)
        assert result == {"claimed": 2, "ok": 2, "failed": 0}
        assert checked and all(checked)

    def test_stop_flag_breaks_the_batch_row_loop(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """``should_stop`` lands mid-batch: the flag is checked per
        claimed row (between embed sub-batches and writes), so a stop
        after the second row leaves the rest unwritten — re-claimable
        next pass, not lost."""
        import precis.workers.chunk_keywords as ck

        ref_id = seed_ref(store)
        for i in range(4):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=f"{_BODY_TEXT_1} v{i}")
        # One unique candidate text per chunk, one text per embed call
        # — each row is its own sub-batch, so the stop fires between
        # real embed/write units.
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            lambda text, *, max_keywords: [text[-8:]],
        )
        monkeypatch.setattr(ck, "_EMBED_CALL_MAX_TEXTS", 1)
        checks = {"n": 0}

        def _stop() -> bool:
            checks["n"] += 1
            # Row-top and write-slice checks alternate (row1, write1,
            # row2, write2, row3…): the flag goes true on the fifth
            # call — after row 2's write, before row 3 starts.
            return checks["n"] > 4

        result = run_chunk_keywords_pass(
            store,
            make_mock_bge_m3(),
            batch_size=10,
            single_runner=False,
            should_stop=_stop,
        )
        # Two chunks written; the rest of the claim abandoned
        # mid-batch — unwritten, not lost.
        assert result == {"claimed": 4, "ok": 2, "failed": 0}
        with store.pool.connection() as conn:
            assert conn.execute(
                "SELECT count(*) FROM chunks WHERE keywords IS NULL"
            ).fetchone() == (2,)
        # The abandoned two re-claim cleanly on the next pass.
        assert run_chunk_keywords_pass(
            store, make_mock_bge_m3(), batch_size=10, single_runner=False
        ) == {"claimed": 2, "ok": 2, "failed": 0}

    def test_dedicated_path_ignores_a_held_advisory_lock(self, store: Store) -> None:
        """``single_runner=False`` doesn't take the fleet lock — a lock
        held elsewhere (a shared-tick pass, an operator session) can't
        starve the lane."""
        import precis.workers.chunk_keywords as ck
        from precis.store.advisory import try_xact_advisory_lock

        ref_id = seed_ref(store)
        _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        assert store.dsn
        skip_if_backends_multiplexed(store.dsn)
        emb = make_mock_bge_m3()
        with try_xact_advisory_lock(store.dsn, ck._LOCK_KEY) as held:
            assert held is True
            result = run_chunk_keywords_pass(
                store, emb, batch_size=10, single_runner=False
            )
        assert result == {"claimed": 1, "ok": 1, "failed": 0}


# ---------------------------------------------------------------------------
# Stale-claim pacing — tier 3 seq-scans, so it runs on a cadence
# ---------------------------------------------------------------------------


class TestStalePacing:
    def test_stale_claim_runs_every_nth_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """Off-cadence passes skip the seq-scanning tier-3 claim; the
        Nth pass runs it and the stale chunk is re-derived."""
        import precis.workers.chunk_keywords as ck

        monkeypatch.setattr(ck, "_pass_count", 0)
        monkeypatch.setattr(ck, "_stale_draining", False)
        ref_id = seed_ref(store)
        stale_id = _seed_long_chunk(store, ref_id=ref_id, ord=0, text=_BODY_TEXT_1)
        _mark_stale(store, stale_id)
        emb = make_mock_bge_m3()
        # Pass 1: _pass_count→1, 1 % 10 != 0 — tier 3 doesn't run.
        assert run_chunk_keywords_pass(store, emb, batch_size=10) == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
        }
        # Next pass lands on the cadence multiple → tier 3 claims it.
        monkeypatch.setattr(ck, "_pass_count", ck._STALE_CLAIM_EVERY - 1)
        result = run_chunk_keywords_pass(store, emb, batch_size=10)
        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT keywords_meta->>'version' FROM chunks WHERE chunk_id = %s",
                (stale_id,),
            ).fetchone()
        assert row == (KEYWORDS_VERSION,)

    def test_stale_claim_drains_every_pass_until_empty(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """While the previous tier-3 claim returned rows the pacing is
        suspended — a live stale backlog drains every pass, not every
        Nth."""
        import precis.workers.chunk_keywords as ck

        monkeypatch.setattr(ck, "_pass_count", 0)
        monkeypatch.setattr(ck, "_stale_draining", True)
        ref_id = seed_ref(store)
        for i in range(2):
            _mark_stale(
                store,
                _seed_long_chunk(store, ref_id=ref_id, ord=i, text=_BODY_TEXT_1),
            )
        emb = make_mock_bge_m3()
        # Both off-cadence passes still claim — _stale_draining holds
        # until a claim comes back empty.
        assert run_chunk_keywords_pass(store, emb, batch_size=1)["ok"] == 1
        assert ck._stale_draining is True
        assert run_chunk_keywords_pass(store, emb, batch_size=1)["ok"] == 1
        # Queue empty → the flag clears, back to the paced cadence.
        run_chunk_keywords_pass(store, emb, batch_size=10)
        assert ck._stale_draining is False


# ---------------------------------------------------------------------------
# numpy scoring — identical output to the pure-Python reference (golden)
# ---------------------------------------------------------------------------


class TestNumpyScoring:
    def test_matches_pure_python_reference(self) -> None:
        """Top-8 order is identical and scores match the old
        ``sum(a*b)/norms`` loop to float tolerance — the METHOD did not
        change, only the matrix algebra."""
        import math

        from precis.workers.chunk_keywords import _score_keyword_candidates

        def reference(
            chunk_embedding: list[float],
            candidate_meta: list[tuple[str, str | None]],
            cand_vecs: list[list[float]],
        ) -> list[dict[str, Any]]:
            def _norm(v: list[float]) -> float:
                return math.sqrt(sum(x * x for x in v))

            chunk_norm = _norm(chunk_embedding)
            scored: list[tuple[float, str, str | None]] = []
            for (long_form, cand_short), vec in zip(candidate_meta, cand_vecs):
                v_norm = _norm(vec)
                score = (
                    0.0
                    if chunk_norm == 0.0 or v_norm == 0.0
                    else sum(a * b for a, b in zip(chunk_embedding, vec))
                    / (chunk_norm * v_norm)
                )
                scored.append((float(score), long_form, cand_short))
            scored.sort(key=lambda t: t[0], reverse=True)
            seen: set[str] = set()
            kept: list[dict[str, Any]] = []
            for score, long_form, cand_short in scored:
                key = (cand_short or long_form).lower()
                if key in seen:
                    continue
                seen.add(key)
                kept.append({"short": cand_short, "long": long_form, "score": score})
                if len(kept) >= 8:
                    break
            return kept

        emb = make_mock_bge_m3()
        chunk_vec = emb.embed_one(_BODY_TEXT_1)
        candidate_meta: list[tuple[str, str | None]] = [
            ("metal-organic framework", "MOF"),
            ("gas storage", None),
            ("catalysis", None),
            ("separation applications", None),
            ("pore geometry", None),
            ("organic linkers", None),
            ("high surface area", None),
            ("crystalline materials", None),
            ("tunable pore", None),
            ("metal nodes", None),
            ("zero-vector filler", None),
        ]
        cand_vecs = emb.embed([long_form for long_form, _ in candidate_meta])
        cand_vecs[-1] = [0.0] * len(cand_vecs[0])  # zero vector → score 0.0

        got = _score_keyword_candidates(chunk_vec, candidate_meta, cand_vecs)
        want = reference(chunk_vec, candidate_meta, cand_vecs)

        assert [k["long"] for k in got] == [k["long"] for k in want]
        assert [k["short"] for k in got] == [k["short"] for k in want]
        for g, w in zip(got, want):
            assert g["score"] == pytest.approx(w["score"], abs=1e-12)


# ---------------------------------------------------------------------------
# Embed-call budget — one pass, bounded calls (gr520236 / gr550973)
# ---------------------------------------------------------------------------


class TestEmbedCallBudget:
    def test_calls_bounded_by_unique_texts(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """One pass makes <= ceil(unique candidate texts /
        _EMBED_CALL_MAX_TEXTS) embed calls — never one per chunk."""
        import math

        import precis.workers.chunk_keywords as ck

        ref_id = seed_ref(store)
        for i in range(30):
            _seed_long_chunk(store, ref_id=ref_id, ord=i, text=f"{_BODY_TEXT_1} v{i}")
        # Two shared phrases + one per-chunk unique phrase → 32 unique
        # candidate texts across 30 chunks.
        monkeypatch.setattr(
            "precis.workers.chunk_keywords._rake_phrases",
            lambda text, *, max_keywords: [
                "shared a",
                "shared b",
                f"unique {text[-2:]}",
            ],
        )
        monkeypatch.setattr(ck, "_EMBED_CALL_MAX_TEXTS", 10)
        emb = make_mock_bge_m3()
        embed = Mock(wraps=emb.embed)
        monkeypatch.setattr(emb, "embed", embed)

        result = run_chunk_keywords_pass(store, emb, batch_size=100, max_seconds=3600)

        assert result == {"claimed": 30, "ok": 30, "failed": 0}
        assert embed.call_count <= math.ceil(32 / 10)
        seen: list[str] = []
        for call in embed.call_args_list:
            seen.extend(call.args[0])
        assert len(seen) == len(set(seen))
