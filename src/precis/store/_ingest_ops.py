"""Paper bundle ingest. Mixin on :class:`precis.store.Store`.

Single entry point — :meth:`IngestMixin.ingest_bundle` — that reads
a ``.acatome`` file, dedupes against existing papers via the
``ref_identifiers`` alias index, mints a slug, and writes ``refs`` +
``blocks`` + ``ref_identifiers`` rows + provenance tags under one
transaction.

Dedup: every identifier the bundle carries is checked against
``ref_identifiers`` in a single query. Hits short-circuit the ingest
(``IngestResult.inserted=False``); misses fall through to the
write path. After a successful insert, the ingest writes one row
per known ``(scheme, value)`` so future ingests of the same paper —
even via different identifiers from a different lookup path —
collapse to the same ref id.

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
* ``self.find_ref_by_identifier(...)``   — IdentifiersMixin
* ``self.insert_ref_identifiers(...)``   — IdentifiersMixin
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
    from precis.ingest import IngestResult, ParsedBundle


# ---------------------------------------------------------------------------
# Bundle -> identifier triple extraction
# ---------------------------------------------------------------------------

# Map S2's external_ids dict keys to our normalised scheme names.
# S2's keys are case-sensitive ('DOI', 'ArXiv', 'PubMed', ...); our
# schemes are lowercased. Unknown keys are forwarded under their
# lowercased form so the table accepts them — adding a new S2 scheme
# requires no code change here.
_S2_EXTERNAL_KEY_MAP: dict[str, str] = {
    "DOI": "doi",
    "ArXiv": "arxiv",
    "PubMed": "pubmed",
    "PubMedCentralID": "pmc",
    "MAG": "mag",
    "DBLP": "dblp",
    "CorpusId": "corpusid",
    "OpenAlex": "openalex",
}


def _identifier_triples(parsed: ParsedBundle) -> list[tuple[str, str, str]]:
    """Yield ``(scheme, value, source)`` triples for a bundle.

    Used both for dedup lookup (we hit ``ref_identifiers`` with the
    same set we'd write on insert) and for the post-insert write.
    Output is deduplicated so the same identifier doesn't appear twice
    even when the bundle reports it via two paths (e.g. ``header.doi``
    AND ``header.external_ids['DOI']`` for a paper resolved via S2).
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []
    provider = parsed.provider or "manual"

    def _push(scheme: str, value: str | None, source: str) -> None:
        if not value:
            return
        key = (scheme, value.strip().lower())
        if key in seen:
            return
        seen.add(key)
        out.append((scheme, value, source))

    # The four primary keys.
    _push("doi", parsed.doi, provider)
    _push("arxiv", parsed.arxiv_id, provider)
    _push("s2", parsed.s2_id, provider)
    _push("pdfsha256", parsed.pdf_hash, "local")

    # Anything S2 also told us about. Unknown keys forward under
    # lowercased name so the table grows organically as S2 adds
    # new external schemes.
    for raw_key, raw_val in (parsed.external_ids or {}).items():
        if not raw_val:
            continue
        scheme = _S2_EXTERNAL_KEY_MAP.get(raw_key, raw_key.lower())
        # Skip schemes whose value we already emitted under a primary key
        # (e.g. external_ids['DOI'] duplicating header.doi).
        _push(scheme, raw_val, "s2")

    return out


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

    def find_ref_by_identifier(
        self,
        scheme: str,
        value: str,
        *,
        kind: str | None = None,
    ) -> int | None:
        raise NotImplementedError  # pragma: no cover — overridden by IdentifiersMixin

    def insert_ref_identifiers(
        self,
        ref_id: int,
        identifiers: Any,  # Iterable[tuple[str, str, str]]
        *,
        conn: Connection | None = None,
    ) -> int:
        raise NotImplementedError  # pragma: no cover — overridden by IdentifiersMixin

    def ingest_bundle(
        self,
        path: Path,
        *,
        embedder: Embedder,
        corpus_slug: str = "default",
    ) -> IngestResult:
        """Read a ``.acatome`` bundle and write it into the v2 schema.

        Idempotency: every identifier the bundle carries (DOI,
        arXiv id, S2 paperId, pdf_hash, plus any extras from S2's
        ``externalIds`` like PubMed / MAG / OpenAlex) is checked
        against the ``ref_identifiers`` alias index in a single
        query. A hit short-circuits to
        ``IngestResult.inserted=False``. Re-extract / replace is a
        separate operation (future ``--force`` flag).

        Aliases-on-insert: after a successful write, the same
        identifier set is INSERTed into ``ref_identifiers`` (with
        ``ON CONFLICT DO NOTHING`` so a concurrent ingest can't
        race us). This lets future ingests of the same paper —
        even via different identifiers from a different lookup
        path — collapse to the same ref id without scanning
        ``refs.meta``.

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

        # Identity dedupe: short-circuit if any of the bundle's
        # identifiers already point at a live paper. The alias
        # index does the heavy lifting — one indexed lookup per
        # scheme, sequential. We stop on the first hit.
        #
        # Scheme order matters for diagnostic purposes only: a hit
        # via 'doi' is the strongest signal; 'pdfsha256' catches
        # rescued bundles without a DOI; 'arxiv' / 's2' / extras
        # cover the long tail.
        triples = _identifier_triples(parsed)
        for scheme, value, _source in triples:
            existing_ref_id = self.find_ref_by_identifier(
                scheme, value, kind="paper"
            )
            if existing_ref_id is not None:
                with self.pool.connection() as conn:
                    row = conn.execute(
                        "SELECT slug FROM refs WHERE id = %s",
                        (existing_ref_id,),
                    ).fetchone()
                slug = row[0] if row is not None else ""
                # Opportunistic enrichment: if we matched on (e.g.)
                # 'doi' but the bundle also carries an 'arxiv' id we
                # didn't have on file, INSERT the missing alias
                # rows. This is exactly the "cross-DOI dedup"
                # promise — once any path identifies this ref, we
                # absorb the rest of the cluster into the alias
                # index so subsequent lookups by *any* member hit
                # in O(1) without re-querying S2.
                self.insert_ref_identifiers(existing_ref_id, triples)
                return IngestResult(
                    ref_id=existing_ref_id,
                    slug=slug,
                    block_count=self.count_blocks(existing_ref_id),
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

            # Write identifier aliases inside the same transaction so
            # the ref + its aliases land atomically — a partial-state
            # window where the ref exists but isn't yet alias-indexed
            # would let a concurrent ingest of the same paper insert
            # a duplicate ref before our aliases hit the table.
            self.insert_ref_identifiers(ref.id, triples, conn=conn)

        return IngestResult(
            ref_id=ref.id,
            slug=slug,
            block_count=len(blocks),
            inserted=True,
            embedding_dim=embedder.dim,
        )


__all__ = ["IngestMixin"]
