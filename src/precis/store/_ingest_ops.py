"""Paper bundle ingest. Mixin on :class:`precis.store.Store`.

Single entry point — :meth:`IngestMixin.ingest_bundle` — that reads
a ``.acatome`` file, dedupes against existing papers via
``(doi, pdf_hash, arxiv_id)``, mints a slug, and writes ``refs`` +
``blocks`` + provenance tags under one transaction.

Split out of the main Store because the ingest path is the only
place that depends on the top-level :mod:`precis.ingest` module;
keeping it in the base class would force every ``Store`` import to
pull the bundle parser.

Mixin assumes the concrete Store provides:

* ``self.pool``
* ``self.tx()``
* ``self.ensure_corpus(slug)``           — corpus lifecycle
* ``self.insert_ref(...)``               — RefsMixin
* ``self.insert_blocks(...)``            — BlocksMixin
* ``self.count_blocks(ref_id)``          — BlocksMixin
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store.types import Block, BlockInsert, Ref

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.ingest import IngestResult


class IngestMixin:
    """``.acatome`` paper-bundle ingest with DOI/hash-based dedupe."""

    pool: ConnectionPool

    # Provided by the concrete Store / sibling mixins. These stubs
    # document the dependency for readers; MRO resolves the real
    # implementations at runtime when the mixin is composed into
    # :class:`Store`. Calling them on a bare ``IngestMixin`` raises.
    def tx(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def ensure_corpus(self, slug: str, *, title: str | None = None) -> int:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def insert_ref(
        self,
        *,
        corpus_id: int,
        kind: str,
        slug: str | None,
        title: str,
        provider: str | None = None,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Ref:
        raise NotImplementedError  # pragma: no cover — overridden by RefsMixin

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        raise NotImplementedError  # pragma: no cover — overridden by BlocksMixin

    def count_blocks(self, ref_id: int) -> int:
        raise NotImplementedError  # pragma: no cover — overridden by BlocksMixin

    def ingest_bundle(
        self,
        path: Path,
        *,
        embedder: Embedder,
        corpus_slug: str = "default",
    ) -> IngestResult:
        """Read a ``.acatome`` bundle and write it into the v2 schema.

        Idempotency: if a paper with the same DOI, pdf_hash, or
        arxiv_id is already present (``kind='paper'``, matching the
        first available key), the call is a no-op —
        ``IngestResult.inserted=False``. Re-extract is a separate
        operation handled by a future ``--force`` flag.

        Block embeddings:

        * Bundle blocks carrying a vector matching the embedder's
          dim are inserted as-is (no re-embed cost).
        * Anything else gets re-embedded by ``embedder``.

        All work runs in one transaction.
        """
        from precis.ingest import (
            IngestResult,
            fill_embeddings,
            mint_paper_slug,
            parse_bundle,
            read_bundle,
        )

        raw = read_bundle(Path(path))
        parsed = parse_bundle(raw, embedding_dim=embedder.dim)

        # Identity dedupe: short-circuit if we already have this paper
        # under any stable content key. Checked in order of strength:
        #   1. DOI          — canonical publication identifier
        #   2. pdf_hash     — sha256 of source PDF, exact content match
        #   3. arxiv_id     — preprint identifier (DOI-less preprints)
        # Rescued bundles (acatome-extract `text_rescue` path) often
        # lack a DOI but still carry pdf_hash, which is what used to
        # trigger silent duplicate re-ingest on each directory walk.
        dedupe_keys: list[tuple[str, str]] = []
        if parsed.doi:
            dedupe_keys.append(("doi", parsed.doi))
        if parsed.pdf_hash:
            dedupe_keys.append(("pdf_hash", parsed.pdf_hash))
        if parsed.arxiv_id:
            dedupe_keys.append(("arxiv_id", parsed.arxiv_id))

        for meta_key, value in dedupe_keys:
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT id, slug FROM refs "
                    "WHERE kind = 'paper' AND deleted_at IS NULL "
                    "AND meta->>%s = %s "
                    "LIMIT 1",
                    (meta_key, value),
                ).fetchone()
            if row is not None:
                ref_id, slug = row[0], row[1]
                return IngestResult(
                    ref_id=ref_id,
                    slug=slug,
                    block_count=self.count_blocks(ref_id),
                    inserted=False,
                    embedding_dim=embedder.dim,
                )

        # Embed any block missing a usable vector.
        blocks = fill_embeddings(parsed.blocks, embedder=embedder)

        with self.tx() as conn:
            cid = self.ensure_corpus(corpus_slug)

            # Slug minting: probe via the same connection so the dedup
            # sees this transaction's writes. We're inside ``tx()``, so
            # other concurrent ingests can't observe our partial state
            # until commit — collisions resolve by suffixing.
            def _slug_taken(s: str) -> bool:
                row = conn.execute(
                    "SELECT 1 FROM refs WHERE kind='paper' AND slug=%s",
                    (s,),
                ).fetchone()
                return row is not None

            slug = mint_paper_slug(parsed, _slug_taken)

            ref = self.insert_ref(
                corpus_id=cid,
                kind="paper",
                slug=slug,
                title=parsed.title,
                provider=parsed.provider,
                meta=dict(parsed.raw_meta),
                conn=conn,
            )

            # Block insert payloads. Slug minting per block is
            # deferred — phase 3 doesn't need stable per-block citation
            # handles for the agent surface to work; pos is enough.
            inserts = [
                BlockInsert(
                    pos=i,
                    text=b.text,
                    embedding=b.embedding,
                    density=b.density,
                    token_count=len(b.text.split()),
                )
                for i, b in enumerate(blocks)
            ]
            self.insert_blocks(ref.id, inserts, conn=conn)

            # Provenance + density tags. Closed-namespace SRC tag is
            # writable by the system actor only.
            conn.execute(
                "INSERT INTO ref_closed_tags (ref_id, prefix, value, set_by) "
                "VALUES (%s, 'SRC', 'bundle', 'system') "
                "ON CONFLICT DO NOTHING",
                (ref.id,),
            )

        return IngestResult(
            ref_id=ref.id,
            slug=slug,
            block_count=len(blocks),
            inserted=True,
            embedding_dim=embedder.dim,
        )


__all__ = ["IngestMixin"]
