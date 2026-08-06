"""Tests for the bib_retag pass — Layer-2 corpus remediation of mis-typed
bibliography chunks (gripe 196447, ``workers/bib_retag.py``).

End-to-end against real PG (the ``store`` fixture); no network, no LLM (the
pass is pure regex detection reusing ``bib_parse``'s shared detector). The
retype-mechanism assertions turn on the append-only body-text trigger NOT
firing for a ``chunk_kind``-only UPDATE (migration 0068) and on the derived
``chunk_embeddings`` / ``chunk_summaries`` being deleted, not orphaned.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.workers.bib_retag as bib_retag_mod
from precis.workers.bib_retag import BIB_RETAG_VERSION, run_bib_retag_pass
from tests.workers._helpers import seed_chunk, seed_ref

#: Three marker-shaped lines — a chunk that content-detects as bibliography
#: regardless of its (mis-typed) ``chunk_kind``.
_BIB_TEXT = "\n".join(
    [
        "- [1] A. One, B. Two, Nature 2018, 5, 100.",
        "- [2] C. Three, D. Four, Science 2019, 8, 200.",
        "- [3] E. Five, F. Six, Chem. Rev. 2021, 121, 50.",
    ]
)

#: Ordinary prose that merely mentions one bracketed citation — the negative /
#: precision case: a single ``[3]`` line is outvoted by prose, so the detector
#: must NOT flag it (retyping real body content would silently drop it from
#: search).
_PROSE_WITH_MARKER = (
    "As shown previously [3] the catalyst is stable under load. We describe "
    "the reactor geometry and the measurement protocol in the next section, "
    "then turn to the kinetics of the observed decomposition."
)


def _seed_embedding(store: Any, chunk_id: int) -> None:
    """Give ``chunk_id`` a ``chunk_embeddings`` row (as the embed worker would)
    so the pass has something stale to delete. ``bge-m3`` is the seeded default
    embedder; the vector is left NULL (only presence/absence matters here)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status) "
            "VALUES (%s, 'bge-m3', 'ok')",
            (chunk_id,),
        )
        conn.commit()


def _chunk_kind(store: Any, chunk_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_kind FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _embedding_count(store: Any, chunk_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _meta_version(store: Any, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT (meta->>'bib_retag_version')::int FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return None if row[0] is None else int(row[0])


class TestRetype:
    def test_retypes_mistyped_bibliography_chunks(self, store: Any) -> None:
        ref_id = seed_ref(store, title="a paper with a mis-typed bibliography")
        body = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text="Body prose."
        )
        bib = seed_chunk(
            store, ref_id=ref_id, ord=1, chunk_kind="paragraph", text=_BIB_TEXT
        )
        _seed_embedding(store, body)
        _seed_embedding(store, bib)

        result = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        assert result["claimed"] == 1
        assert result["ok"] == 1
        assert result["failed"] == 0
        assert result["papers_retagged"] == 1
        assert result["chunks_retyped"] == 1
        assert result["embeddings_deleted"] == 1

        # The bibliography chunk is now 'references' with its embedding gone;
        # the real body chunk is untouched (kind + embedding intact).
        assert _chunk_kind(store, bib) == "references"
        assert _embedding_count(store, bib) == 0
        assert _chunk_kind(store, body) == "paragraph"
        assert _embedding_count(store, body) == 1

        assert _meta_version(store, ref_id) == BIB_RETAG_VERSION

    def test_summaries_also_deleted(self, store: Any) -> None:
        # The derived chunk_summaries row must be torn down too (else it
        # describes a chunk that no longer participates in search).
        ref_id = seed_ref(store, title="paper needing summary cleanup")
        bib = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=_BIB_TEXT
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
                "VALUES (%s, 'rake-lemma', 'stale summary', 'ok')",
                (bib,),
            )
            conn.commit()

        run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunk_summaries WHERE chunk_id = %s", (bib,)
            ).fetchone()
        assert row is not None and int(row[0]) == 0

    def test_already_references_untouched_but_stamped(self, store: Any) -> None:
        ref_id = seed_ref(store, title="a paper already correctly tagged")
        # A references chunk correctly typed at ingest — no marker lines even.
        refs = seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="references",
            text="[1] A. One. [2] B. Two.",
        )

        result = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        assert result["claimed"] == 1
        assert result["papers_retagged"] == 0
        assert result["chunks_retyped"] == 0
        assert _chunk_kind(store, refs) == "references"
        assert _meta_version(store, ref_id) == BIB_RETAG_VERSION

    def test_prose_with_incidental_marker_not_retyped(self, store: Any) -> None:
        ref_id = seed_ref(store, title="an ordinary prose paper")
        prose = seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            chunk_kind="paragraph",
            text=_PROSE_WITH_MARKER,
        )
        _seed_embedding(store, prose)

        result = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        assert result["claimed"] == 1
        assert result["chunks_retyped"] == 0
        assert result["embeddings_deleted"] == 0
        # Body prose stays a paragraph with its embedding — NOT dropped.
        assert _chunk_kind(store, prose) == "paragraph"
        assert _embedding_count(store, prose) == 1
        assert _meta_version(store, ref_id) == BIB_RETAG_VERSION


class TestConvergence:
    def test_idempotent_second_run_is_a_noop(self, store: Any) -> None:
        ref_id = seed_ref(store, title="a paper retagged once")
        bib = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=_BIB_TEXT
        )
        _seed_embedding(store, bib)

        first = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])
        assert first["claimed"] == 1
        assert first["chunks_retyped"] == 1

        # Second run: the paper is stamped, so it isn't even re-claimed.
        second = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])
        assert second["claimed"] == 0
        assert second["chunks_retyped"] == 0
        assert _chunk_kind(store, bib) == "references"

    def test_version_bump_resweeps(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = seed_ref(store, title="a plain paper")
        seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text="plain prose."
        )

        first = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])
        assert first["claimed"] == 1
        assert _meta_version(store, ref_id) == BIB_RETAG_VERSION

        still = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])
        assert still["claimed"] == 0

        monkeypatch.setattr(bib_retag_mod, "BIB_RETAG_VERSION", BIB_RETAG_VERSION + 1)
        bumped = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])
        assert bumped["claimed"] == 1
        assert _meta_version(store, ref_id) == BIB_RETAG_VERSION + 1


class TestDryRun:
    def test_dry_run_detects_but_mutates_nothing(self, store: Any) -> None:
        ref_id = seed_ref(store, title="a paper counted in dry-run")
        bib = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=_BIB_TEXT
        )
        _seed_embedding(store, bib)

        result = run_bib_retag_pass(
            store, batch_size=10, ref_ids=[ref_id], dry_run=True
        )

        assert result["dry_run"] == 1
        assert result["claimed"] == 1
        # It reports what WOULD be retyped ...
        assert result["chunks_retyped"] == 1
        # ... but nothing is actually mutated: kind, embedding, and (crucially)
        # the version stamp are all unchanged, so the real sweep re-claims it.
        assert _chunk_kind(store, bib) == "paragraph"
        assert _embedding_count(store, bib) == 1
        assert _meta_version(store, ref_id) is None

    def test_dry_run_env_flag_is_honoured(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_BIB_RETAG_DRY_RUN", "1")
        ref_id = seed_ref(store, title="a paper counted via env dry-run")
        bib = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=_BIB_TEXT
        )

        result = run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        assert result["dry_run"] == 1
        assert _chunk_kind(store, bib) == "paragraph"
        assert _meta_version(store, ref_id) is None


class TestEmbedSkip:
    def test_retyped_chunk_lands_in_the_embed_skip_list(self, store: Any) -> None:
        # Ties the remediation to its purpose: the embed claim query excludes
        # chunks whose kind is in ``EmbedHandler.skip_chunk_kinds``
        # (``chunk_kind <> ALL(skip_chunk_kinds)`` in embed.py). A mis-typed
        # 'paragraph' bibliography chunk is NOT skipped (it gets a vector and
        # pollutes search); after retyping, its kind IS in that skip-list, so
        # it can never re-enter the claim — proven against the same constant
        # the query reads, so this can't drift out of agreement with it.
        from precis.workers.embed import EmbedHandler

        assert "paragraph" not in EmbedHandler.skip_chunk_kinds

        ref_id = seed_ref(store, title="a paper for the embed-skip assertion")
        bib = seed_chunk(
            store, ref_id=ref_id, ord=0, chunk_kind="paragraph", text=_BIB_TEXT
        )

        run_bib_retag_pass(store, batch_size=10, ref_ids=[ref_id])

        assert _chunk_kind(store, bib) in EmbedHandler.skip_chunk_kinds
