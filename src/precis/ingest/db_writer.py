"""v2 INSERT cascade for the ingest pipeline.

This is a private module — only ``precis.ingest.add`` (B4d) and the
ingest tests should import it directly. The Store mixins are kept
out of the loop on purpose: they currently target the v1 schema
(B7 will rewrite them) and routing v2 writes through them would
double the deletion blast radius.

Public surface (in dependency order):

* :class:`ChunkToWrite`, :class:`PaperToWrite` — the data the
  pipeline assembles before calling the writer.
* :class:`WriteResult` — what the writer returns on a successful
  insert.
* :func:`probe_existing` — idempotency check; the caller calls
  this *before* :func:`write_paper` and short-circuits on a hit.
* :func:`resolve_cite_key` — collision-resolution helper used
  internally by :func:`write_paper`; exported for tests.
* :func:`write_paper` — the atomic INSERT cascade. Caller owns
  the transaction boundary.

See ``docs/design/b4-precis-add.md`` for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

# ---------------------------------------------------------------------------
# Identifier kinds — pinned to the migration's ref_identifiers comment.
# ---------------------------------------------------------------------------

# Order matters: ``probe_existing`` queries them in this priority so the
# first-match short-circuit (LIMIT 1) is deterministic across callers.
_IDENTIFIER_KINDS: tuple[str, ...] = (
    "paper_id",
    "doi",
    "arxiv",
    "s2",
    "pubmed",
    "openalex",
    "pdf_sha256",
    "content_hash",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkToWrite:
    """One chunk row to insert. ``ord`` and ``chunk_kind`` are subject
    to the schema's CHECK: cards (``card_*``) get ``ord < 0``, body
    chunks get ``ord >= 0``.
    """

    ord: int
    chunk_kind: str
    text: str
    section_path: list[str] = field(default_factory=list)
    page_first: int | None = None
    page_last: int | None = None
    token_count: int | None = None
    meta: dict[str, Any] | None = None
    #: Denormalized lexical numeric-token index — every
    #: ``<number> <unit>`` token detected in ``text``. Populated by
    #: the ingest pipeline via :func:`precis.utils.numerics.extract_numerics`.
    #: Empty for cards and structural blocks.
    numerics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PaperToWrite:
    """Everything ``write_paper`` needs to assemble a single ref's
    rows across ``refs``, ``ref_identifiers``, ``pdfs``, and ``chunks``.

    The ``cite_key_prefix`` is the un-suffixed surname+year string
    produced by :func:`precis.identity.make_cite_key` with no
    ``taken=`` set. The writer probes ``ref_identifiers`` for taken
    suffixes and produces the final cite_key inside the transaction.
    """

    # Core ref columns
    title: str
    authors: list[dict[str, Any]]
    year: int | None
    kind: str = "paper"

    # Provenance
    provider: str | None = None  # 'crossref' | 's2' | 'arxiv' | 'embedded'
    set_by: str = "system"

    # Identity (computed by precis.identity)
    paper_id: str = ""
    pub_id: str | None = None
    cite_key_prefix: str = ""

    # PDF (None for metadata-only ingests)
    pdf_sha256: str | None = None
    content_hash: str | None = None
    pdf_pages_first: int | None = None
    pdf_pages_last: int | None = None
    pdf_role: str | None = None
    pdf_storage_path: str | None = None
    pdf_page_count: int | None = None
    pdf_size_bytes: int | None = None
    # Historical pdf_sha256 values for the same physical file — e.g.
    # the pre-patch hash when ``precis.ingest.pdf_writer`` rewrote the
    # PDF's Info dict to embed the resolved DOI. Each entry becomes an
    # extra ``ref_identifiers`` row of kind ``pdf_sha256`` pointing at
    # this ref, so re-ingesting *either* byte sequence short-circuits
    # the fast path. See ADR 0014.
    pdf_sha256_aliases: list[str] = field(default_factory=list)

    # External IDs (any combination; missing ones are skipped)
    doi: str | None = None
    arxiv_id: str | None = None
    s2_id: str | None = None
    pubmed_id: str | None = None
    openalex_id: str | None = None

    # Free-form bookkeeping
    meta: dict[str, Any] = field(default_factory=dict)

    # Chunks (cards + body)
    chunks: list[ChunkToWrite] = field(default_factory=list)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a successful :func:`write_paper` call."""

    ref_id: int
    cite_key: str
    chunks_written: int
    identifiers_written: dict[str, str]


# ---------------------------------------------------------------------------
# probe_existing — idempotency check
# ---------------------------------------------------------------------------


def probe_existing(
    *,
    paper_id: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_id: str | None = None,
    pubmed_id: str | None = None,
    openalex_id: str | None = None,
    pdf_sha256: str | None = None,
    content_hash: str | None = None,
    conn: Connection,
) -> int | None:
    """Return ``ref_id`` if any of the given identifiers already
    points at a live ref, else ``None``.

    Looks up the ``ref_identifiers`` index — single round-trip.
    First match wins; ordering follows :data:`_IDENTIFIER_KINDS`.
    """
    candidates: list[tuple[str, str]] = []
    by_kind = {
        "paper_id": paper_id,
        "doi": doi,
        "arxiv": arxiv_id,
        "s2": s2_id,
        "pubmed": pubmed_id,
        "openalex": openalex_id,
        "pdf_sha256": pdf_sha256,
        "content_hash": content_hash,
    }
    for kind in _IDENTIFIER_KINDS:
        value = by_kind.get(kind)
        if value:
            candidates.append((kind, value))
    if not candidates:
        return None

    # Build a parameterised IN-list. Each candidate becomes two
    # placeholders — one for id_kind, one for id_value.
    placeholders = ", ".join(["(%s, %s)"] * len(candidates))
    # Join refs and exclude soft-deleted rows: a reconciled/merged
    # stub keeps its identifiers (they migrate to the survivor, but
    # content-derived ids like cite_key may stay behind on the retired
    # row). Returning a soft-deleted ref here would resurrect a merged
    # duplicate on the next ingest. ``ri.ref_id`` is the PK side, so the
    # join is index-only on ``refs_pkey``.
    sql = (
        "SELECT ri.ref_id, ri.id_kind FROM ref_identifiers ri "
        "JOIN refs r ON r.ref_id = ri.ref_id "
        f"WHERE (ri.id_kind, ri.id_value) IN ({placeholders}) "
        "AND r.deleted_at IS NULL "
        "LIMIT 1"
    )
    flat_params: list[str] = []
    for kind, value in candidates:
        flat_params.extend([kind, value])

    row = conn.execute(sql, flat_params).fetchone()
    if row is None:
        return None
    ref_id_value = row[0]
    assert isinstance(ref_id_value, int)
    return ref_id_value


# ---------------------------------------------------------------------------
# resolve_cite_key — collision-resolution helper
# ---------------------------------------------------------------------------


def resolve_cite_key(
    cite_key_prefix: str,
    *,
    conn: Connection,
) -> str:
    """Pick the next free ``cite_key`` for a given prefix.

    Probes ``ref_identifiers`` for keys starting with ``cite_key_prefix``
    and asks :func:`precis.identity.make_cite_key` to choose the next
    free suffix.

    The probe is cheap thanks to the ``cite_key_trgm`` index on
    ``ref_identifiers (id_value)``. The set of "taken" keys is
    typically small (papers from the same first-author + year).
    """
    if not cite_key_prefix:
        raise ValueError("cite_key_prefix must be non-empty")

    sql = (
        "SELECT id_value FROM ref_identifiers "
        "WHERE id_kind = 'cite_key' AND id_value LIKE %s"
    )
    rows = conn.execute(sql, (cite_key_prefix + "%",)).fetchall()
    taken = {row[0] for row in rows}

    # ``make_cite_key`` re-derives the prefix from authors+year. We
    # already have the prefix string; the cleanest path is to call
    # the helper with the raw inputs, but since the prefix is what
    # we have, we simulate the suffix progression here.
    return _next_cite_key(cite_key_prefix, taken)


def _next_cite_key(prefix: str, taken: set[str]) -> str:
    """Given a prefix and the set of already-taken cite_keys
    starting with that prefix, return the next free key.

    Mirrors :func:`precis.identity.make_cite_key`'s suffix policy:
    bare prefix first, then ``a``..``z``, then ``aa``..``zz``, …
    """
    if prefix not in taken:
        return prefix
    for suffix in _suffix_progression():
        candidate = prefix + suffix
        if candidate not in taken:
            return candidate
    # _suffix_progression is unbounded in practice; this is here for
    # mypy's benefit.
    raise RuntimeError("cite_key suffix progression exhausted")  # pragma: no cover


def _suffix_progression():
    """Yield 'a', 'b', …, 'z', 'aa', 'ab', …, 'zz', 'aaa', …

    Matches :func:`precis.identity.make_cite_key`'s suffix order so
    re-running the resolver on different subsets of ``taken`` yields
    consistent allocations.
    """
    import string

    alphabet = string.ascii_lowercase
    width = 1
    while True:
        # base-26 counter at the given width
        for n in range(26**width):
            digits: list[str] = []
            x = n
            for _ in range(width):
                digits.append(alphabet[x % 26])
                x //= 26
            yield "".join(reversed(digits))
        width += 1


# ---------------------------------------------------------------------------
# write_paper — the atomic INSERT cascade
# ---------------------------------------------------------------------------


def write_paper(paper: PaperToWrite, *, conn: Connection) -> WriteResult:
    """Insert ``paper`` into the v2 schema.

    Caller owns the transaction. This function does not COMMIT; on
    success the caller commits, on exception the caller rolls back.

    Order of operations:

    1. Resolve the final ``cite_key`` (probe ref_identifiers).
    2. INSERT INTO pdfs ON CONFLICT DO NOTHING (if PDF metadata).
    3. INSERT INTO refs RETURNING ref_id.
    4. INSERT INTO ref_identifiers (paper_id, pub_id, cite_key, doi,
       arxiv, s2, pubmed, openalex, pdf_sha256, content_hash) ON
       CONFLICT (id_kind, id_value) DO NOTHING.
    5. INSERT INTO chunks (one row per ``ChunkToWrite``).

    Raises :class:`ValueError` if the paper is missing required
    identity fields (``paper_id``, ``cite_key_prefix``).
    """
    if not paper.paper_id:
        raise ValueError("paper.paper_id is required")
    if not paper.cite_key_prefix:
        raise ValueError("paper.cite_key_prefix is required")

    # 1. Resolve cite_key
    cite_key = resolve_cite_key(paper.cite_key_prefix, conn=conn)

    # 2. PDF row (if any)
    if paper.pdf_sha256 is not None:
        conn.execute(
            "INSERT INTO pdfs "
            "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (
                paper.pdf_sha256,
                paper.content_hash or paper.pdf_sha256,
                paper.pdf_page_count or 0,
                paper.pdf_size_bytes or 0,
                paper.pdf_storage_path or "",
            ),
        )

    # 3. refs row
    pdf_pages: str | None = None
    if paper.pdf_pages_first is not None and paper.pdf_pages_last is not None:
        # INT4RANGE literal — inclusive on both ends matches Marker's
        # 1-indexed page numbers.
        pdf_pages = f"[{paper.pdf_pages_first},{paper.pdf_pages_last}]"

    row = conn.execute(
        "INSERT INTO refs "
        "(kind, set_by, title, authors, year, provider, "
        " pdf_sha256, pdf_pages, pdf_role, meta) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, %s) "
        "RETURNING ref_id",
        (
            paper.kind,
            paper.set_by,
            paper.title,
            Jsonb(paper.authors),
            paper.year,
            paper.provider,
            paper.pdf_sha256,
            pdf_pages,
            paper.pdf_role,
            Jsonb(paper.meta or {}),
        ),
    ).fetchone()
    assert row is not None
    ref_id_value = row[0]
    assert isinstance(ref_id_value, int)
    ref_id: int = ref_id_value

    # 4. ref_identifiers rows
    identifiers: dict[str, str] = {}
    identifiers["paper_id"] = paper.paper_id
    identifiers["cite_key"] = cite_key
    if paper.pub_id:
        identifiers["pub_id"] = paper.pub_id
    if paper.doi:
        identifiers["doi"] = paper.doi
    if paper.arxiv_id:
        identifiers["arxiv"] = paper.arxiv_id
    if paper.s2_id:
        identifiers["s2"] = paper.s2_id
    if paper.pubmed_id:
        identifiers["pubmed"] = paper.pubmed_id
    if paper.openalex_id:
        identifiers["openalex"] = paper.openalex_id
    if paper.pdf_sha256:
        identifiers["pdf_sha256"] = paper.pdf_sha256
    if paper.content_hash:
        identifiers["content_hash"] = paper.content_hash

    # ON CONFLICT DO NOTHING is race-safe but also defends against
    # the rare case where two different identifiers carry the same
    # value (e.g. paper_id == content_hash on a pathological input).
    for id_kind, id_value in identifiers.items():
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id_kind, id_value) DO NOTHING",
            (id_kind, id_value, ref_id, paper.provider),
        )

    # Historical pdf_sha256 aliases — one extra row each. These reuse
    # the ``pdf_sha256`` id_kind so the same probe path picks them up.
    for alias in paper.pdf_sha256_aliases:
        if not alias or alias == paper.pdf_sha256:
            continue
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id_kind, id_value) DO NOTHING",
            ("pdf_sha256", alias, ref_id, paper.provider),
        )

    # 5. chunks rows — with text-hash dedup
    #
    # Marker occasionally emits two consecutive blocks with byte-identical
    # text (tables on transition pages are the common trigger; see the
    # deng10 MTV-MOF case where ord=28 and ord=29 had the same 1561-char
    # table). The unique (ref_id, ord) constraint doesn't catch these
    # because the ords differ. Skip on first-seen-wins so the corpus
    # stays clean instead of inflating retrieval results with
    # near-identical hits.
    import hashlib

    seen_text_hashes: set[str] = set()
    chunks_skipped_dup = 0
    chunk_rows: list[tuple[Any, ...]] = []
    for chunk in paper.chunks:
        text_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        if text_hash in seen_text_hashes:
            chunks_skipped_dup += 1
            continue
        seen_text_hashes.add(text_hash)
        chunk_rows.append(
            (
                ref_id,
                paper.set_by,
                chunk.ord,
                chunk.chunk_kind,
                chunk.text,
                chunk.section_path,
                chunk.page_first,
                chunk.page_last,
                chunk.token_count,
                Jsonb(chunk.meta or {}),
                chunk.numerics or [],
            )
        )
    # One round-trip for the whole chunk batch instead of
    # len(paper.chunks) per-row INSERTs — a 200-chunk paper used to
    # pay 200x the connection RTT cost on the watcher path.
    if chunk_rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks "
                "(ref_id, set_by, ord, chunk_kind, text, section_path, "
                " page_first, page_last, token_count, meta, numerics) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                chunk_rows,
            )
    chunks_written = len(chunk_rows)

    return WriteResult(
        ref_id=ref_id,
        cite_key=cite_key,
        chunks_written=chunks_written,
        identifiers_written=identifiers,
    )


def register_aliases_and_maybe_upgrade(
    existing_ref_id: int,
    paper: PaperToWrite,
    *,
    conn: Connection,
) -> int:
    """Apply a fresh ingest's new info to an existing ref.

    Called from ``_ingest_pdf`` when ``probe_existing`` finds a match.
    Two distinct jobs, performed in one transaction:

    1. **Alias registration (always).** Every PDF representation of a
       paper is a real fact about it. The new ``pdf_sha256`` and
       ``content_hash`` from this ingest are inserted into
       ``ref_identifiers`` (``ON CONFLICT DO NOTHING``); if the hash
       isn't already in ``pdfs``, that row lands too. Subsequent
       ingests probe the same identifiers and short-circuit to the
       same ``ref_id``.

    2. **Stub upgrade (conditional).** When the existing ref has
       ``pdf_sha256 IS NULL`` — the stub-state predicate per the
       finding-chase design — *and* this ingest brings PDF bytes, we
       upgrade the row by setting the canonical ``pdf_sha256``,
       ``pdf_pages``, ``pdf_role``, and writing the chunks the
       pipeline extracted. The derived queue (embed / summarize)
       picks the new chunks up on its next pass; any finding waiting
       on this stub resumes naturally.

    Returns the number of chunks written (zero on the alias-only
    path, ``len(paper.chunks)`` on the stub-upgrade path).

    Caller owns the transaction — this function does not COMMIT.
    """
    # ── 1. Always register the new pdf_sha256 / content_hash aliases.
    if paper.pdf_sha256:
        conn.execute(
            "INSERT INTO pdfs "
            "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (
                paper.pdf_sha256,
                paper.content_hash or paper.pdf_sha256,
                paper.pdf_page_count or 0,
                paper.pdf_size_bytes or 0,
                paper.pdf_storage_path or "",
            ),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id_kind, id_value) DO NOTHING",
            ("pdf_sha256", paper.pdf_sha256, existing_ref_id, paper.provider),
        )
    if paper.content_hash:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id_kind, id_value) DO NOTHING",
            ("content_hash", paper.content_hash, existing_ref_id, paper.provider),
        )
    # Historical aliases the patch-write-back path collected (ADR 0014).
    for alias in paper.pdf_sha256_aliases:
        if not alias or alias == paper.pdf_sha256:
            continue
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id_kind, id_value) DO NOTHING",
            ("pdf_sha256", alias, existing_ref_id, paper.provider),
        )

    # ── 2. Stub upgrade — only if the ref currently has no canonical PDF.
    existing_pdf_row = conn.execute(
        "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
        (existing_ref_id,),
    ).fetchone()
    if existing_pdf_row is None or existing_pdf_row[0] is not None:
        # Either the ref vanished (soft-deleted between probe and now,
        # rare race) or it already carries a canonical PDF (this
        # ingest is a multi-hash alias for a known paper). Either
        # way, no upgrade work — return alias-only count.
        return 0
    if not paper.pdf_sha256:
        # Caller hit a stub but provided no PDF bytes (e.g. a DOI
        # path landing on a chase-created stub). Aliases registered;
        # nothing else to do.
        return 0

    # Promote the canonical PDF + run the chunk insert. Mirror the
    # write_paper §3 + §5 code path so the upgraded row looks
    # identical to one written fresh.
    pdf_pages: str | None = None
    if paper.pdf_pages_first is not None and paper.pdf_pages_last is not None:
        pdf_pages = f"[{paper.pdf_pages_first},{paper.pdf_pages_last}]"
    conn.execute(
        "UPDATE refs SET "
        "  pdf_sha256 = %s, "
        "  pdf_pages  = COALESCE(%s::int4range, pdf_pages), "
        "  pdf_role   = COALESCE(%s, pdf_role), "
        "  updated_at = now() "
        "WHERE ref_id = %s AND deleted_at IS NULL",
        (paper.pdf_sha256, pdf_pages, paper.pdf_role, existing_ref_id),
    )
    chunk_rows: list[tuple[Any, ...]] = [
        (
            existing_ref_id,
            paper.set_by,
            chunk.ord,
            chunk.chunk_kind,
            chunk.text,
            chunk.section_path,
            chunk.page_first,
            chunk.page_last,
            chunk.token_count,
            Jsonb(chunk.meta or {}),
            chunk.numerics or [],
        )
        for chunk in paper.chunks
    ]
    # One round-trip for the whole chunk batch — see write_paper for
    # the same pattern. Stub-upgrade ingests typically hand in
    # 50-300 chunks and used to pay the RTT cost per row.
    if chunk_rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks "
                "(ref_id, set_by, ord, chunk_kind, text, section_path, "
                " page_first, page_last, token_count, meta, numerics) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                chunk_rows,
            )
    return len(chunk_rows)


__all__ = [
    "ChunkToWrite",
    "PaperToWrite",
    "WriteResult",
    "probe_existing",
    "register_aliases_and_maybe_upgrade",
    "resolve_cite_key",
    "write_paper",
]
