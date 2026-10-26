"""``get(kind='paper', view='ops')`` + ``precis paper dump`` — one ref's
full stored state as a ``precis-paper-dump/1`` JSON block (gr517423,
slice 1 of docs/backlog/paper-backup-pdf-transfer.md).

Before ``drop_body``/``clear_abstract``/``split_body``/``move_body_to``
ran on 25 refs (gr513878), the executor had to write hand-built SQL
through ``prod-psql --ro`` to capture undo state — no paper view dumped a
ref. This is the read side of the dump/restore pair: every destructive
door's dry run now points at it with an ``undo:`` line, and slice 2's
``put(args={'restore': …})`` consumes what it emits.

A slice's fenced block is ``{"format": "precis-paper-dump/1",
"manifest": {…}, …}`` — paged over ALL the ref's chunks (card chunks
``ord < 0`` and body chunks alike, ordered by ``ord``) the same way the
``se`` kind's ``view='ops'`` windows its ops list
(:mod:`precis_se.ops_export`, :func:`precis.handlers._slice_args.slice_window`).
Only the slice whose window starts at 0 carries ``ref``, ``identifiers``,
``tags``, ``paper_authors``, ``bib_entries`` and the link lists; later
slices carry ``manifest`` + ``chunks`` only, so a reader concatenates
``chunks`` arrays across slices.

``manifest.body_sha`` is the round-trip checksum slice 2 verifies: a
sha256 over ``(ord, chunk_kind, text)`` for every chunk of the ref in ord
order — one compact JSON triple per line, UTF-8 — so a restored body is
provably the dumped one.

What the dump deliberately does NOT carry (:data:`NOT_CARRIED`, printed
in the rendered header — a stated partial copy beats a silent one):

* ``chunks.tsv`` — a GENERATED ALWAYS column; Postgres recomputes it on
  insert.
* ``chunks.keywords`` / ``chunks.keywords_meta`` — derived annotations a
  worker pass rewrites; a restored chunk re-earns them.
* The chunk-keyed derived tables — ``chunk_embeddings``,
  ``chunk_summaries``, ``chunk_citations``, ``chunk_blobs``,
  ``chunk_tags``, ``chunk_events``, ``chunk_review``. Embeddings come
  back ``NULL`` on restore and the workers refill them, as after ingest.
* ``ref_tags.tag_id`` — a database-local FK; the dump carries the tag's
  ``(namespace, value)``, which is the identity a restore re-resolves.

Read-only: no store writes, no events, no migration.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.handlers._slice_args import slice_window
from precis.store._identifiers_ops import (
    _HUMAN_EDIT_SOURCES,
    _cleared_schemes,
    _dropped_pairs,
    _normalise_identifier,
    _unstamp_cleared,
)
from precis.store.revision_context import trust_door
from precis.utils import handle_registry

#: The fenced block's ``format`` marker — slice 2's restore refuses
#: anything else.
FORMAT = "precis-paper-dump/1"

#: Page knob: the ``undo:`` line's assumed page size and the view's
#: default when ``args`` names none.
DEFAULT_PAGE_SIZE = 100
#: Hard cap — a bigger page_size refuses rather than silently clamping,
#: so a caller never mistakes a truncated slice for a whole ref.
MAX_PAGE_SIZE = 500

#: The stated gaps, in the order the rendered header prints them. The
#: verdict test pins every excluded column against this list.
NOT_CARRIED: tuple[str, ...] = (
    "chunks.tsv — a generated tsvector column, recomputed by Postgres on insert",
    "chunks.keywords / chunks.keywords_meta — derived annotations; a worker "
    "pass rewrites them on the restored rows",
    "the chunk-keyed derived tables (chunk_embeddings, chunk_summaries, "
    "chunk_citations, chunk_blobs, chunk_tags, chunk_events, chunk_review) — "
    "restored chunks carry embedding NULL and the workers refill them",
    "ref_tags.tag_id — a database-local FK; a tag carries its "
    "(namespace, value) instead, which a restore re-resolves",
)

#: Columns each table's dump rows carry, in SELECT order. The verdict
#: test asserts the union of these and the :data:`NOT_CARRIED` gaps is
#: exactly the table's ``information_schema`` column set — a schema
#: addition fails loudly here instead of quietly dropping out of dumps.
REF_COLUMNS: tuple[str, ...] = (
    "ref_id",
    "kind",
    "set_by",
    "title",
    "authors",
    "year",
    "provider",
    "human_verified_at",
    "human_verified_by",
    "human_verified_note",
    "retraction_status",
    "retracted_at",
    "retraction_reason",
    "retraction_url",
    "retraction_checked_at",
    "pdf_sha256",
    "pdf_pages",
    "pdf_role",
    "meta",
    "retired_at",
    "created_at",
    "updated_at",
    "auto_refresh_days",
    "refreshed_at",
    "parent_id",
    "prio",
    "handle",
    "last_viewed_at",
    "alert_source",
    "fingerprint",
    "resolved_at",
    "doi_status",
    "doi_validated_at",
    "owner_login",
    "last_recalled_at",
    "trust_origin",
    "trust_confidence",
    "trust_write_version",
)

CHUNK_COLUMNS: tuple[str, ...] = (
    "chunk_id",
    "ref_id",
    "set_by",
    "ord",
    "chunk_kind",
    "text",
    "block_ids",
    "token_count",
    "section_path",
    "page_first",
    "page_last",
    "meta",
    "created_at",
    "numerics",
    "accesses",
    "last_dreamt",
    "last_seen",
    "last_watched",
    "handle",
    "pos",
    "parent_chunk_id",
    "content_sha",
    "retired_at",
)

IDENTIFIER_COLUMNS: tuple[str, ...] = (
    "id_kind",
    "id_value",
    "ref_id",
    "source",
    "created_at",
)

#: ``ref_tags`` minus ``tag_id`` (carried as ``namespace`` + ``value``
#: through the ``tags`` join — see :data:`NOT_CARRIED`).
TAG_COLUMNS: tuple[str, ...] = (
    "namespace",
    "value",
    "set_by",
    "created_at",
    "expires_at",
)

AUTHOR_COLUMNS: tuple[str, ...] = (
    "ref_id",
    "position",
    "given",
    "middle",
    "family",
    "name_raw",
    "orcid",
    "openalex_author_id",
    "person_ref_id",
    "source",
    "verified_at",
    "updated_at",
    "full_name",
)

BIB_COLUMNS: tuple[str, ...] = (
    "id",
    "ref_id",
    "marker",
    "raw_text",
    "authors",
    "journal",
    "year",
    "volume",
    "first_page",
    "doi",
    "s2_id",
    "openalex_id",
    "held_ref_id",
    "parse_conf",
    "match_conf",
    "parse_version",
    "source",
    "created_at",
)

#: ``links`` plus the two ``src_chunk_ord``/``dst_chunk_ord`` lookups and
#: the far ref's handle — chunk_ids do not survive a restore, ``(ref,
#: ord)`` does, so both are emitted.
LINK_COLUMNS: tuple[str, ...] = (
    "link_id",
    "src_ref_id",
    "src_chunk_id",
    "dst_ref_id",
    "dst_chunk_id",
    "relation",
    "set_by",
    "meta",
    "created_at",
    "trust_origin",
    "trust_confidence",
    "trust_write_version",
)


def _jsonable(value: Any) -> Any:
    """One stored value → its JSON spelling: timestamps UTC ``Z``,
    ``int4range`` a ``[lo, hi)`` pair, ``numeric[]`` floats, bytea hex."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # psycopg Range (refs.pdf_pages is int4range): a [lo, hi) pair.
    if hasattr(value, "isempty") and hasattr(value, "lower"):
        if value.isempty:
            return None
        return [_jsonable(value.lower), _jsonable(value.upper)]
    return value


def _row_dict(columns: tuple[str, ...], row: Any) -> dict[str, Any]:
    return {col: _jsonable(val) for col, val in zip(columns, row, strict=True)}


def _fetch_all_chunks(conn: Any, ref_id: int) -> list[dict[str, Any]]:
    """Every chunk of the ref — cards (``ord < 0``) and body, live and
    retired — in the window's ``(ord, chunk_id)`` order."""
    rows = conn.execute(
        f"SELECT {', '.join(CHUNK_COLUMNS)} FROM chunks WHERE ref_id = %s "
        "ORDER BY ord, chunk_id",
        (ref_id,),
    ).fetchall()
    return [_row_dict(CHUNK_COLUMNS, r) for r in rows]


def body_sha(chunks: list[dict[str, Any]]) -> str:
    """sha256 over ``(ord, chunk_kind, text)`` in ord order — one compact
    JSON triple per line. The equality slice 2's restore asserts after
    re-inserting: identical text under identical ords."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(
            json.dumps(
                [c["ord"], c["chunk_kind"], c["text"]], separators=(",", ":")
            ).encode("utf-8")
        )
        h.update(b"\n")
    return h.hexdigest()


def chunk_total(conn: Any, ref_id: int) -> int:
    """How many chunks a dump of this ref pages over — the ``undo:``
    line's slice count and the manifest's ``chunks_total``."""
    row = conn.execute(
        "SELECT count(*) FROM chunks WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    return int((row or (0,))[0])


def undo_line(conn: Any, ref_id: int, handle: str) -> str:
    """The dry-run pointer every destructive door prints: name the export
    and how many default-size slices saving it takes."""
    n_slices = max(1, math.ceil(chunk_total(conn, ref_id) / DEFAULT_PAGE_SIZE))
    return (
        f"undo: save get(kind='paper', id='{handle}', view='ops') first "
        f"({n_slices} slice(s) at page_size {DEFAULT_PAGE_SIZE})"
    )


def _fetch_ref(conn: Any, ref_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(REF_COLUMNS)} FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    return None if row is None else _row_dict(REF_COLUMNS, row)


def _fetch_side_rows(conn: Any, ref_id: int) -> dict[str, Any]:
    """The first-slice companions: identifiers, tags, byline, bib and
    both link directions."""
    identifiers = [
        _row_dict(IDENTIFIER_COLUMNS, r)
        for r in conn.execute(
            f"SELECT {', '.join(IDENTIFIER_COLUMNS)} FROM ref_identifiers "
            "WHERE ref_id = %s ORDER BY id_kind, id_value",
            (ref_id,),
        ).fetchall()
    ]
    tags = [
        _row_dict(TAG_COLUMNS, r)
        for r in conn.execute(
            "SELECT t.namespace, t.value, rt.set_by, rt.created_at, "
            "       rt.expires_at "
            "FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s ORDER BY rt.created_at, t.tag_id",
            (ref_id,),
        ).fetchall()
    ]
    authors = [
        _row_dict(AUTHOR_COLUMNS, r)
        for r in conn.execute(
            f"SELECT {', '.join(AUTHOR_COLUMNS)} FROM paper_authors "
            "WHERE ref_id = %s ORDER BY position",
            (ref_id,),
        ).fetchall()
    ]
    bib = [
        _row_dict(BIB_COLUMNS, r)
        for r in conn.execute(
            f"SELECT {', '.join(BIB_COLUMNS)} FROM paper_bib_entries "
            "WHERE ref_id = %s ORDER BY marker",
            (ref_id,),
        ).fetchall()
    ]
    link_cols = ", ".join(f"l.{c}" for c in LINK_COLUMNS)
    links_out = [
        _link_dict(LINK_COLUMNS, r, far="dst")
        for r in conn.execute(
            f"SELECT {link_cols}, sc.ord, dc.ord, dr.kind "
            "FROM links l "
            "LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id "
            "LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id "
            "LEFT JOIN refs dr ON dr.ref_id = l.dst_ref_id "
            "WHERE l.src_ref_id = %s ORDER BY l.link_id",
            (ref_id,),
        ).fetchall()
    ]
    links_in = [
        _link_dict(LINK_COLUMNS, r, far="src")
        for r in conn.execute(
            f"SELECT {link_cols}, sc.ord, dc.ord, sr.kind "
            "FROM links l "
            "LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id "
            "LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id "
            "LEFT JOIN refs sr ON sr.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s ORDER BY l.link_id",
            (ref_id,),
        ).fetchall()
    ]
    return {
        "ref": _fetch_ref(conn, ref_id),
        "identifiers": identifiers,
        "tags": tags,
        "paper_authors": authors,
        "bib_entries": bib,
        "links_out": links_out,
        "links_in": links_in,
    }


def _link_dict(columns: tuple[str, ...], row: Any, *, far: str) -> dict[str, Any]:
    """A links row plus its chunk-ord anchors and the far ref's handle —
    the last three select items are ``sc.ord``, ``dc.ord`` and the far
    ref's ``kind`` (for ``format_handle``). ``far`` names which endpoint
    is not the dumped ref: ``'dst'`` on links_out, ``'src'`` on
    links_in."""
    n = len(columns)
    out = _row_dict(columns, row[:n])
    src_ord, dst_ord, far_kind = row[n], row[n + 1], row[n + 2]
    out["src_chunk_ord"] = None if src_ord is None else int(src_ord)
    out["dst_chunk_ord"] = None if dst_ord is None else int(dst_ord)
    far_id = out["dst_ref_id" if far == "dst" else "src_ref_id"]
    out[f"{far}_ref_handle"] = (
        handle_registry.try_format(far_kind, far_id) if far_kind is not None else None
    )
    return out


def dump_ref(
    conn: Any, ref_id: int, *, offset: int, size: int | None
) -> dict[str, Any]:
    """One ``precis-paper-dump/1`` slice for ``ref_id``.

    ``offset`` is the 0-based chunk index the window starts at; ``size``
    the cap, ``0``/``None`` meaning every chunk from ``offset`` on. Only
    the ``offset == 0`` slice carries ``ref``/``identifiers``/``tags``/
    ``paper_authors``/``bib_entries``/``links_*`` — later slices are
    ``format`` + ``manifest`` + ``chunks`` only.
    """
    chunks = _fetch_all_chunks(conn, ref_id)
    window = chunks[offset:] if not size else chunks[offset : offset + size]
    ref_row = _fetch_ref(conn, ref_id)
    if ref_row is None:
        raise BadInput(f"ref_id {ref_id} does not exist")
    manifest = {
        "ref_id": ref_id,
        "handle": handle_registry.format_handle(str(ref_row["kind"]), ref_id),
        "chunks_total": len(chunks),
        "body_sha": body_sha(chunks),
        "window": [offset, offset + len(window)],
        "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    payload: dict[str, Any] = {"format": FORMAT, "manifest": manifest}
    if offset == 0:
        payload.update(_fetch_side_rows(conn, ref_id))
    payload["chunks"] = window
    return payload


def slice_args(
    args: dict[str, Any] | None, *, where: str
) -> tuple[int, int, list[str]]:
    """``view='ops'``'s window: canonical ``page``/``page_size`` plus the
    deprecated ``offset``/``limit`` aliases, the se view's semantics —
    default 100, cap 500, ``0`` means everything from the offset on.
    Returns ``(offset, size, readings)``."""
    offset, size, readings = slice_window(
        args, where=where, default_size=DEFAULT_PAGE_SIZE
    )
    if size is not None and size > MAX_PAGE_SIZE:
        raise BadInput(
            f"{where} 'page_size' must be <= {MAX_PAGE_SIZE}; got {size}",
            next=f"{where} slices with args={{'page': 1, 'page_size': 500}}",
        )
    return offset, DEFAULT_PAGE_SIZE if size is None else size, readings


def render_ops(
    conn: Any,
    *,
    ref_id: int,
    title: str,
    handle: str,
    args: dict[str, Any] | None = None,
) -> str:
    """The view body: header, the stated gaps, one fenced
    ``precis-paper-dump/1`` block, and a ``next slice:`` line while more
    chunks remain — the se ``view='ops'`` contract transferred to papers
    (:func:`precis_se.ops_export.render_ops`)."""
    where = "get(kind='paper', view='ops')"
    offset, size, readings = slice_args(args, where=where)
    payload = dump_ref(conn, ref_id, offset=offset, size=size)
    manifest = payload["manifest"]
    total = manifest["chunks_total"]
    window = payload["chunks"]
    sliced = offset > 0 or len(window) < total
    lead = (
        f"{total} chunk(s) in this ref's full dump "
        "(precis-paper-dump/1) — ref row, identifiers, tags, byline, bib "
        "entries, links and every chunk incl. cards. Concatenate every "
        "slice's `chunks` array for the complete body."
    )
    if sliced:
        lo, hi = manifest["window"]
        lead = (
            f"{total} chunk(s) in this ref's full dump "
            "(precis-paper-dump/1) — this is a slice (chunks "
            f"{lo}–{hi}). Later slices carry `manifest` + `chunks` only; "
            "concatenate every slice's `chunks` array — a partial list "
            "restores a partial body."
        )
        if not window:
            lead += f" (offset {offset} is past the end — nothing to show)"
    lines = [
        f"# {title} — ops export",
        "",
        lead,
        "",
        "Not carried by the dump:",
        "",
    ]
    lines += [f"- {gap}" for gap in NOT_CARRIED]
    lines += ["", "```json", _dump_json(payload), "```"]
    remaining = total - offset - len(window)
    if remaining > 0:
        nxt_off = offset + len(window)
        nxt_args = (
            f"{{'page': {nxt_off // size + 1}, 'page_size': {size}}}"
            if size and nxt_off % size == 0
            else f"{{'offset': {nxt_off}, 'limit': {size}}}"
        )
        lines += [
            "",
            f"next slice: get(kind='paper', id='{handle}', view='ops', "
            f"args={nxt_args})",
        ]
    body = "\n".join(lines)
    if readings:
        from precis.handlers._interpret import PREFIX as _INTERPRETED

        body = f"{_INTERPRETED}{'; '.join(readings)}\n{body}"
    return body


def _dump_json(payload: dict[str, Any]) -> str:
    """The fenced block: every top-level key compact, one chunk object
    per line — the same size pressure as the se export's one-op-per-line
    layout (``_ops_json``): ``indent=2`` on a few hundred chunks would
    blow the response frame, and a truncated dump still parses halfway."""
    head = {k: v for k, v in payload.items() if k != "chunks"}
    chunks = payload.get("chunks") or []
    parts = [
        f"{json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}"
        for k, v in head.items()
    ]
    out = "{" + ",".join(parts)
    if not chunks:
        return out + ',"chunks":[]}'
    body = ",\n ".join(json.dumps(c, separators=(",", ":")) for c in chunks)
    return out + ',\n "chunks": [\n ' + body + "\n ]}"


# ── restore door (slice 2) ──────────────────────────────────────────
#
# ``restore_slice`` is the write half of the pair: it consumes one dump
# slice per call inside the caller's transaction and rebuilds the ref's
# chunks (INSERT-only — the append-only invariant forbids UPDATE on body
# rows), stashes in-flight progress on the target's ``meta.restore``,
# and on the completing slice recomputes :func:`body_sha` and rewrites
# the link lists, remapping chunk anchors through ``(ref, ord)``.
#
# Progress record on ``refs.meta.restore`` (set by the first slice,
# cleared by the completing one)::
#
#     {dump_ref_id, dump_body_sha, chunks_total, chunks_done,
#      started_at, links_out, links_in, mode, slug, cards_done,
#      identifiers_done, chunk_ords, parents, inserted_ords}
#
# ``mode`` ('same'|'new') and ``slug`` pin which in-flight run a
# continuation joins — a 'new' call never hijacks the original ref's
# 'same' stamp; ``cards_done``/``identifiers_done`` accumulate so the
# completing event counts the whole restore; ``chunk_ords`` +
# ``parents`` carry the dump's chunk_id→ord map and parent edges so
# ``parent_chunk_id`` resolves across slices at completion.
#
# The frontier rule: a fresh call must start at window[0] == 0; a
# continuation may overlap (per-ord idempotence) but never jump ahead of
# ``chunks_done``. Re-sending a finished restore is a true no-op: the
# stored ``body_sha`` already equals the manifest's, so the call
# verifies and reports completion without re-stamping.


@dataclass
class RestoreReport:
    """What one slice did — or, with ``dry``, would have done before the
    savepoint rolled back. The handler renders this into the ack."""

    ref_id: int
    handle: str
    window: tuple[int, int]
    chunks_done: int  # frontier after this slice
    chunks_total: int
    inserted: int = 0  # fresh chunk rows
    kept: int = 0  # identical rows already in place
    replaced: int = 0  # card/retired rows swapped DELETE+INSERT
    cards: int = 0  # of inserted+replaced, the ord<0 card rows
    identifiers: int = 0  # ref_identifiers rows restored
    unretired: bool = False
    minted: bool = False  # a fresh ref row was INSERTed (target='new' / gone)
    slug: str | None = None  # the minted ref's cite_key
    completed: bool = False  # body_sha verified, links written, meta cleared
    links_written: int = 0
    links_kept: int = 0  # already present (ON CONFLICT DO NOTHING)
    links_skipped: list[dict[str, Any]] = field(default_factory=list)
    links_restamped: list[dict[str, Any]] = field(default_factory=list)
    skipped_identifiers: list[dict[str, Any]] = field(default_factory=list)
    not_carried: list[str] = field(default_factory=list)
    cards_total: int = 0  # cards over the whole restore, at completion
    identifiers_total: int = 0  # identifiers over the whole restore
    dry: bool = False


#: Chunk columns a restore writes — :data:`CHUNK_COLUMNS` minus the
#: identity (``chunk_id``, ``ref_id``) and the :data:`NOT_CARRIED` gaps
#: (``tsv`` is generated; ``keywords``/``keywords_meta`` are left NULL for
#: the worker pass). ``created_at``/the ``last_*`` timestamps and
#: ``retired_at`` round-trip so a restored body is the dumped one —
#: including retired rows, which the sha counts.
_RESTORE_CHUNK_COLUMNS: tuple[str, ...] = (
    "set_by",
    "ord",
    "chunk_kind",
    "text",
    "block_ids",
    "token_count",
    "section_path",
    "page_first",
    "page_last",
    "meta",
    "created_at",
    "numerics",
    "accesses",
    "last_dreamt",
    "last_seen",
    "last_watched",
    "handle",
    "pos",
    "parent_chunk_id",
    "content_sha",
    "retired_at",
)

#: The four timestamps that are ``NOT NULL DEFAULT now()``: an honest dump
#: always carries them, but a hand-built slice may not — COALESCE keeps the
#: default instead of forcing a NULL violation.
_RESTORE_TS_COALESCE = {"created_at", "last_dreamt", "last_seen", "last_watched"}

_INSERT_CHUNK_SQL = (
    f"INSERT INTO chunks (ref_id, {', '.join(_RESTORE_CHUNK_COLUMNS)}) "
    "VALUES ("
    + ", ".join(
        ["%s"]
        + [
            "COALESCE(%s::timestamptz, now())" if c in _RESTORE_TS_COALESCE else "%s"
            for c in _RESTORE_CHUNK_COLUMNS
        ]
    )
    + ") RETURNING chunk_id"
)

#: Links unique key — the ``links_endpoints_relation_idx`` NULLS NOT
#: DISTINCT index (``_links_ops``'s own upsert names the same five).
_LINK_CONFLICT = (
    "ON CONFLICT (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
    "relation) DO NOTHING"
)


def _restore_manifest(
    dump: Any, *, where: str
) -> tuple[dict[str, Any], int, int, list[dict[str, Any]]]:
    """Shape-check one slice: format marker, a manifest whose ``window``
    brackets exactly ``chunks``, and the fields restore needs."""
    if not isinstance(dump, dict) or dump.get("format") != FORMAT:
        raise BadInput(
            f"{where} takes one {FORMAT} slice (a fenced block from view='ops')",
            next="get(kind='paper', id=<pa>, view='ops')",
        )
    manifest = dump.get("manifest")
    if not isinstance(manifest, dict):
        raise BadInput(f"{where} slice is missing 'manifest'")
    try:
        lo, hi = (int(x) for x in manifest["window"])
        chunks = dump["chunks"]
        dump_ref_id = int(manifest["ref_id"])
        str(manifest["body_sha"])
        chunks_total = int(manifest["chunks_total"])
        if lo < 0 or hi < lo or hi - lo != len(chunks) or chunks_total < hi:
            raise ValueError(
                f"window [{lo}, {hi}) vs {len(chunks)} chunks / {chunks_total} total"
            )
        for c in chunks:
            int(c["ord"])
            str(c["chunk_kind"])
            str(c["text"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadInput(f"{where} slice is malformed: {exc}") from exc
    manifest["ref_id"] = dump_ref_id
    manifest["chunks_total"] = chunks_total
    return manifest, lo, hi, chunks


def _insert_restore_chunk(
    conn: Any,
    ref_id: int,
    c: dict[str, Any],
    *,
    minted: bool,
    chunk_ords: dict[str, int],
) -> None:
    """One INSERT carrying every dump column but the ids — embedding stays
    NULL (no chunk_embeddings row) for the worker to refill, ``tsv``
    recomputes. ``parent_chunk_id`` remaps through the run's accumulated
    ``chunk_id -> ord`` map to the restored row's new id (the completion
    fix-up catches parents a later slice carries). ``handle`` is dropped
    on a minted target — the globally-unique handle stays with the
    original's rows."""
    parent_id = None
    parent_ord = (
        chunk_ords.get(str(c["parent_chunk_id"]))
        if c.get("parent_chunk_id") is not None
        else None
    )
    if parent_ord is not None:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s "
            "AND retired_at IS NULL ORDER BY chunk_id LIMIT 1",
            (ref_id, parent_ord),
        ).fetchone()
        parent_id = int(row[0]) if row else None
    conn.execute(
        _INSERT_CHUNK_SQL,
        (
            ref_id,
            c.get("set_by"),
            int(c["ord"]),
            c["chunk_kind"],
            c["text"],
            c.get("block_ids") or [],
            c.get("token_count"),
            c.get("section_path") or [],
            c.get("page_first"),
            c.get("page_last"),
            Jsonb(c.get("meta") or {}),
            c.get("created_at"),
            c.get("numerics") or [],
            c.get("accesses") or 0,
            c.get("last_dreamt"),
            c.get("last_seen"),
            c.get("last_watched"),
            None if minted else c.get("handle"),
            c.get("pos"),
            parent_id,
            c.get("content_sha"),
            c.get("retired_at"),
        ),
    )


def _write_restore_links(
    conn: Any, target_ref_id: int, dump_ref_id: int, links: list[dict[str, Any]]
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Rewrite the dump's link lists at completion. ``dump_ref_id``
    endpoints re-point at the target; far refs must still be live (a
    retired or missing far end skips the link). Chunk anchors remap
    through ``(ref, ord)`` — an ord with no live row skips too. The
    ``trust_*`` columns are never copied: raw stamps are refused by the
    links guard and the ``trust_door`` context restamps instead
    (computed/hunch v1 on converted links, unknown v0 otherwise) —
    ``restamped`` records every stamp that changed so nothing is
    silently lost."""
    written = kept = 0
    skipped: list[dict[str, Any]] = []
    restamped: list[dict[str, Any]] = []
    live: dict[int, bool] = {}

    def _live(rid: int) -> bool:
        if rid == target_ref_id:
            return True
        if rid not in live:
            row = conn.execute(
                "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
            live[rid] = bool(row and row[0])
        return live[rid]

    def _chunk_at(rid: int, ord_: int) -> int | None:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s "
            "AND retired_at IS NULL ORDER BY chunk_id LIMIT 1",
            (rid, ord_),
        ).fetchone()
        return int(row[0]) if row else None

    for lnk in links:
        src = int(lnk["src_ref_id"])
        dst = int(lnk["dst_ref_id"])
        src = target_ref_id if src == dump_ref_id else src
        dst = target_ref_id if dst == dump_ref_id else dst
        src_ord = lnk.get("src_chunk_ord")
        dst_ord = lnk.get("dst_chunk_ord")
        src_chunk = _chunk_at(src, int(src_ord)) if src_ord is not None else None
        dst_chunk = _chunk_at(dst, int(dst_ord)) if dst_ord is not None else None
        reason = None
        if not _live(src) or not _live(dst):
            reason = "far ref retired or missing"
        elif src_ord is not None and src_chunk is None:
            reason = f"no live chunk at (pa{src}, ord {src_ord})"
        elif dst_ord is not None and dst_chunk is None:
            reason = f"no live chunk at (pa{dst}, ord {dst_ord})"
        if reason is not None:
            skipped.append(
                {
                    "src_ref_id": int(lnk["src_ref_id"]),
                    "dst_ref_id": int(lnk["dst_ref_id"]),
                    "relation": lnk.get("relation"),
                    "reason": reason,
                }
            )
            continue
        cur = conn.execute(
            "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, "
            "dst_chunk_id, relation, set_by, meta, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "COALESCE(%s::timestamptz, now())) " + _LINK_CONFLICT + " "
            "RETURNING link_id, trust_origin, trust_confidence, "
            "trust_write_version",
            (
                src,
                src_chunk,
                dst,
                dst_chunk,
                lnk["relation"],
                lnk.get("set_by"),
                Jsonb(lnk.get("meta") or {}),
                lnk.get("created_at"),
            ),
        )
        landed = cur.fetchone()
        if landed is None:
            kept += 1
            landed = conn.execute(
                "SELECT link_id, trust_origin, trust_confidence, "
                "trust_write_version FROM links "
                "WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = %s "
                "AND src_chunk_id IS NOT DISTINCT FROM %s "
                "AND dst_chunk_id IS NOT DISTINCT FROM %s",
                (src, dst, lnk["relation"], src_chunk, dst_chunk),
            ).fetchone()
        else:
            written += 1
        if landed is not None:
            was = (
                str(lnk.get("trust_origin") or "unknown"),
                str(lnk.get("trust_confidence") or "unknown"),
                int(lnk.get("trust_write_version") or 0),
            )
            now = (str(landed[1]), str(landed[2]), int(landed[3]))
            if was != now:
                restamped.append(
                    {
                        "link_id": int(landed[0]),
                        "dump_link_id": lnk.get("link_id"),
                        "was": f"{was[0]}/{was[1]}/v{was[2]}",
                        "now": f"{now[0]}/{now[1]}/v{now[2]}",
                    }
                )
    return written, kept, skipped, restamped


@trust_door("computed")
def restore_slice(
    conn: Any,
    dump: dict[str, Any],
    *,
    target: str = "same",
    handle: str | None = None,
    kind: str = "paper",
    expected_ref_id: int | None = None,
    dry: bool = False,
) -> RestoreReport:
    """Restore one ``precis-paper-dump/1`` slice into its target ref.

    ``target='same'`` restores into the dump's own ``manifest.ref_id``:
    a live ref must be bodiless (or already carry this exact body — an
    idempotent re-send), a retired ref is un-retired, and a merge
    tombstone (``meta.superseded_by``) refuses. ``dry=True`` runs every
    check and write under a savepoint, then rolls it back — the report
    is real, nothing persists.
    """
    where = "put(args={'restore': …})"
    manifest, lo, hi, chunks = _restore_manifest(dump, where=where)
    dump_ref_id = int(manifest["ref_id"])
    if target not in ("same", "new"):
        raise BadInput(
            f"{where} 'target' must be 'same' or 'new'; got {target!r}",
            options=["same", "new"],
        )
    if expected_ref_id is not None and expected_ref_id != dump_ref_id:
        raise BadInput(
            f"{where} the dump is for pa{dump_ref_id} but the call pins "
            f"pa{expected_ref_id}",
            next=f"resend on pa{dump_ref_id}, or mint a fresh ref with "
            "args={'target': 'new'}",
        )
    if dry:
        conn.execute("SAVEPOINT precis_restore_dry")
    try:
        report = _restore_dispatch(
            conn,
            dump,
            manifest,
            lo,
            hi,
            chunks,
            target=target,
            handle=handle,
            kind=kind,
            where=where,
        )
    except Exception:
        if dry:
            conn.execute("ROLLBACK TO SAVEPOINT precis_restore_dry")
        raise
    if dry:
        conn.execute("ROLLBACK TO SAVEPOINT precis_restore_dry")
        report.dry = True
    return report


def restore_abort(
    conn: Any, ref_id: int, *, kind: str = "paper", dry: bool = False
) -> dict[str, Any]:
    """The escape hatch for an in-flight restore: clears the target's
    ``meta.restore`` so an abandoned run — or a source that was
    re-exported under a new ``body_sha`` — can start over from slice 0,
    and DELETEs the chunk rows the run inserted (the stamp's
    ``inserted_ords`` — ords with no row before the run; kept and
    replaced rows predate it and stay). A retired target stays retired —
    and a minted one was never live to begin with — so an abort can
    never leave a live ref wearing a truncated body (N1 ruling). ``dry``
    reports what would be cleared and removed without writing."""
    row = conn.execute(
        "SELECT kind, retired_at, meta FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
        (ref_id,),
    ).fetchone()
    if row is None:
        raise BadInput(f"pa{ref_id} does not exist — nothing to abort")
    ref_kind, retired_at, meta = str(row[0]), row[1], dict(row[2] or {})
    handle = handle_registry.format_handle(ref_kind, ref_id)
    if ref_kind != kind:
        raise BadInput(
            f"{handle} is a {ref_kind!r} ref, not {kind!r}",
            next=f"restore it through kind='{ref_kind}'",
        )
    progress = meta.get("restore")
    if not isinstance(progress, dict):
        raise BadInput(
            f"{handle} has no restore in flight — nothing to abort",
            next="to rebuild it anyway, send slice 0 of its dump: "
            f"put(kind='{kind}', id='{handle}', "
            "args={'restore': <slice>, 'reason': '…'})",
        )
    inserted = sorted(int(o) for o in (progress.get("inserted_ords") or []))
    doomed = 0
    if inserted:
        doomed = int(
            (
                conn.execute(
                    "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord = ANY(%s)",
                    (ref_id, inserted),
                ).fetchone()
                or (0,)
            )[0]
        )
    if not dry:
        if inserted:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord = ANY(%s)",
                (ref_id, inserted),
            )
        conn.execute(
            "UPDATE refs SET meta = meta - 'restore', updated_at = now() "
            "WHERE ref_id = %s",
            (ref_id,),
        )
    live = conn.execute(
        "SELECT count(*) FROM chunks WHERE ref_id = %s AND retired_at IS NULL",
        (ref_id,),
    ).fetchone()
    live_n = int((live or (0,))[0])
    return {
        "handle": handle,
        "retired": retired_at is not None,
        "progress": progress,
        # For a dry run nothing was deleted, so the stayers are the
        # live count minus what would go.
        "chunks_removed": doomed,
        "chunks_live": live_n if not dry else live_n - doomed,
    }


def _restore_dispatch(
    conn: Any,
    dump: dict[str, Any],
    manifest: dict[str, Any],
    lo: int,
    hi: int,
    chunks: list[dict[str, Any]],
    *,
    target: str,
    handle: str | None,
    kind: str,
    where: str,
) -> RestoreReport:
    dump_ref_id = int(manifest["ref_id"])
    # Target resolution — the row lock (FOR NO KEY UPDATE, the dedup
    # pattern) serializes concurrent slice-0 calls and a racing
    # merge_into's tombstone check. 'same' is the dump's own ref (minted
    # fresh when it is gone); 'new' always mints. An in-flight restore
    # is found by the progress stamp's ``dump_ref_id`` AND ``mode`` — a
    # 'new' call never joins the original ref's 'same' stamp, and the
    # stamp's ``slug`` makes a mismatched handle= refuse instead of
    # silently continuing into a ref minted under another name.
    row = (
        conn.execute(
            "SELECT ref_id, kind, retired_at, meta FROM refs WHERE ref_id = %s "
            "FOR NO KEY UPDATE",
            (dump_ref_id,),
        ).fetchone()
        if target == "same"
        else None
    )
    if row is None:
        row = conn.execute(
            "SELECT ref_id, kind, retired_at, meta FROM refs "
            "WHERE meta->'restore'->>'dump_ref_id' = %s "
            "AND COALESCE(meta->'restore'->>'mode', 'same') = %s "
            "ORDER BY ref_id LIMIT 1 FOR NO KEY UPDATE",
            (str(dump_ref_id), target),
        ).fetchone()
    report = RestoreReport(
        ref_id=0,
        handle="",
        window=(lo, hi),
        chunks_done=0,
        chunks_total=int(manifest["chunks_total"]),
    )
    minted_slug: str | None = None
    if row is None:
        if lo != 0:
            raise BadInput(
                f"pa{dump_ref_id} is gone and no restore is in flight — a "
                "mint starts at window [0, …)",
                next="send the slice whose window starts at 0 (it carries "
                "the ref row the mint needs)",
            )
        target_id, slug = _mint_restore_ref(
            conn, dump, handle=handle, kind=kind, report=report, where=where
        )
        # _mint_restore_ref inserts the ref retired; it un-retires at
        # completion through the same was_retired path a 'same' restore
        # of a retired ref uses.
        ref_kind, retired_at, meta = kind, datetime.now(UTC), {}
        report.minted = True
        report.slug = slug
        minted_slug = slug
    else:
        target_id = int(row[0])
        ref_kind, retired_at, meta = str(row[1]), row[2], dict(row[3] or {})
        if ref_kind != kind:
            raise BadInput(
                f"{handle_registry.format_handle(ref_kind, target_id)} is a "
                f"{ref_kind!r} ref, not {kind!r}",
                next=f"restore it through kind='{ref_kind}'",
            )
        if target == "new" and isinstance(meta.get("restore"), dict):
            have = meta["restore"].get("slug")
            want = _normalise_identifier("cite_key", handle) if handle else None
            if want and have and want != have:
                raise BadInput(
                    f"a restore into "
                    f"{handle_registry.format_handle(ref_kind, target_id)} "
                    f"is already in flight under slug {have!r} — "
                    f"args['handle'] names {want!r}",
                    next=f"resend with args={{'handle': {have!r}}}, or "
                    "abandon it: "
                    f"put(kind='{kind}', "
                    f"id='{handle_registry.format_handle(ref_kind, target_id)}', "
                    "args={'restore_abort': True, 'reason': '…'})",
                )
            report.slug = str(have or want) if (have or want) else None
    report.ref_id = target_id
    report.handle = handle_registry.format_handle(ref_kind, target_id)
    was_retired = retired_at is not None
    if was_retired and meta.get("superseded_by"):
        raise BadInput(
            f"pa{target_id} is a merge tombstone — it was merged "
            f"into pa{meta['superseded_by']}; resurrecting it would "
            "duel the survivor",
            next=f"restore into pa{meta['superseded_by']} instead "
            "(export it first if it has a body)",
        )
    # The un-retire lands only with the completing slice: a half-restored
    # retired ref stays retired, so an abandoned restore can't leave a
    # live ref with a truncated body (gr517423 review).
    progress = meta.get("restore") if isinstance(meta.get("restore"), dict) else None
    if progress is None:
        # Re-sending slice 0 of a *finished* restore is a true no-op:
        # the stored body already is the dump's (the same sha the
        # completing slice verified), so re-stamping meta.restore —
        # and with it the meta merge and identifier re-adds — would
        # undo edits made since the dump. Verify and report, write
        # nothing; a retired ref is still un-retired by the call.
        # Never on a mint: an empty stored body hashes equal to a
        # chunkless dump's sha and would skip the identifier/link work
        # entirely (N2).
        stored = _fetch_all_chunks(conn, target_id)
        if not report.minted and body_sha(stored) == manifest["body_sha"]:
            report.completed = True
            report.kept = len(chunks)
            report.chunks_done = int(manifest["chunks_total"])
            if was_retired:
                conn.execute(
                    "UPDATE refs SET retired_at = NULL, updated_at = now() "
                    "WHERE ref_id = %s",
                    (target_id,),
                )
                report.unretired = True
            return report
        _restore_frontier_fresh(
            conn,
            dump,
            manifest,
            lo,
            target_id,
            kind,
            report,
            target=target,
            was_retired=was_retired,
            where=where,
        )
        progress = {
            "dump_ref_id": dump_ref_id,
            "dump_body_sha": manifest["body_sha"],
            "chunks_total": int(manifest["chunks_total"]),
            "chunks_done": 0,
            "mode": target,
            "slug": minted_slug,
            "cards_done": 0,
            "identifiers_done": 0,
            "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "links_out": dump.get("links_out") or [],
            "links_in": dump.get("links_in") or [],
            "chunk_ords": {},
            "parents": {},
            "inserted_ords": [],
        }
    else:
        if (
            progress.get("dump_ref_id") != dump_ref_id
            or progress.get("dump_body_sha") != manifest["body_sha"]
        ):
            raise BadInput(
                f"a different restore is already in flight on "
                f"{report.handle} (dump {progress.get('dump_ref_id')}) — "
                "this usually means the source was re-exported and the "
                "body_sha changed",
                next="finish the in-flight restore, or abandon it: "
                f"put(kind='{kind}', id='{report.handle}', "
                "args={'restore_abort': True, 'reason': '…'}), then "
                "resend the new dump's slice 0",
            )
        done = int(progress.get("chunks_done") or 0)
        if lo > done:
            raise BadInput(
                f"out of order: {report.handle} has restored "
                f"{done}/{progress.get('chunks_total')} chunks; this "
                f"slice starts at {lo}",
                next=f"send the slice whose window starts at {done}",
            )
    # Fold this slice's chunk_id→ord map and parent edges into the
    # accumulated record: a parent_chunk_id resolves through ords, and a
    # parent can sit in a slice this one hasn't seen yet.
    ords = {str(k): int(v) for k, v in (progress.get("chunk_ords") or {}).items()}
    parents = {str(k): int(v) for k, v in (progress.get("parents") or {}).items()}
    for c in chunks:
        if c.get("chunk_id") is not None:
            ords[str(c["chunk_id"])] = int(c["ord"])
        if c.get("parent_chunk_id") is not None:
            parents[str(c["chunk_id"])] = int(c["parent_chunk_id"])
    progress["chunk_ords"] = ords
    progress["parents"] = parents
    # ``inserted_ords`` records which ords this run created fresh — the
    # abort door deletes exactly those rows so nothing half-restored
    # stays behind (N1). Kept/replaced ords predate the run and stay.
    inserted_ords = {int(o) for o in (progress.get("inserted_ords") or [])}
    _restore_chunks(
        conn,
        target_id,
        chunks,
        report,
        minted=report.minted,
        chunk_ords=ords,
        inserted_ords=inserted_ords,
    )
    progress["inserted_ords"] = sorted(inserted_ords)
    report.chunks_done = max(int(progress.get("chunks_done") or 0), hi)
    progress["chunks_done"] = report.chunks_done
    progress["cards_done"] = int(progress.get("cards_done") or 0) + report.cards
    progress["identifiers_done"] = (
        int(progress.get("identifiers_done") or 0) + report.identifiers
    )
    if report.chunks_done >= report.chunks_total:
        stored = _fetch_all_chunks(conn, target_id)
        if body_sha(stored) != manifest["body_sha"]:
            raise BadInput(
                f"restored body_sha does not match the manifest's "
                f"({manifest['body_sha'][:12]}…) — the transaction is "
                "rolled back, nothing was written",
                next="re-export the ref and diff the chunk lists; for a "
                "stale dump, abandon this restore "
                f"(put(kind='{kind}', id='{report.handle}', "
                "args={'restore_abort': True, 'reason': '…'})) and "
                "drop_body before resending",
            )
        _restore_parent_fixup(conn, target_id, ords, parents)
        written, kept_l, skipped, restamped = _write_restore_links(
            conn,
            target_id,
            dump_ref_id,
            (progress.get("links_out") or []) + (progress.get("links_in") or []),
        )
        report.links_written = written
        report.links_kept = kept_l
        report.links_skipped = skipped
        report.links_restamped = restamped
        report.cards_total = int(progress["cards_done"])
        report.identifiers_total = int(progress["identifiers_done"])
        if was_retired:
            conn.execute(
                "UPDATE refs SET retired_at = NULL, updated_at = now() "
                "WHERE ref_id = %s",
                (target_id,),
            )
            report.unretired = True
        conn.execute(
            "UPDATE refs SET meta = meta - 'restore', updated_at = now() "
            "WHERE ref_id = %s",
            (target_id,),
        )
        report.completed = True
    else:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb({"restore": progress}), target_id),
        )
    return report


def _restore_parent_fixup(
    conn: Any, ref_id: int, chunk_ords: dict[str, int], parents: dict[str, int]
) -> None:
    """Last-mile parent wiring at completion: every dumped
    ``parent_chunk_id`` resolves by ord once the whole body is in place
    — children whose parent sat in a later slice landed NULL, and a kept
    row's pointer may name a pre-restore chunk_id. The UPDATE touches
    only ``parent_chunk_id``, outside the append-only trigger's column
    set — a structural pointer, not body text (accepted under the
    gr517423 re-review, N3)."""
    for child_cid, parent_cid in parents.items():
        child_ord = chunk_ords.get(str(child_cid))
        parent_ord = chunk_ords.get(str(parent_cid))
        if child_ord is None or parent_ord is None:
            continue
        conn.execute(
            "UPDATE chunks child SET parent_chunk_id = p.chunk_id "
            "FROM chunks p "
            "WHERE child.ref_id = %s AND child.ord = %s "
            "AND child.retired_at IS NULL "
            "AND p.ref_id = %s AND p.ord = %s AND p.retired_at IS NULL "
            "AND child.parent_chunk_id IS DISTINCT FROM p.chunk_id",
            (ref_id, child_ord, ref_id, parent_ord),
        )


def _suggest_free_slug(conn: Any, slug: str) -> str:
    """``<slug>-restored`` / ``<slug>-restored<N>`` — the first cite_key /
    refs.handle pair nobody owns, for the taken-handle refusal's ``next=``."""
    for n in range(0, 20):
        candidate = f"{slug}-restored" if n == 0 else f"{slug}-restored{n}"
        taken = conn.execute(
            "SELECT 1 FROM ref_identifiers WHERE id_kind = 'cite_key' "
            "AND id_value = %s "
            "UNION ALL SELECT 1 FROM refs WHERE handle = %s LIMIT 1",
            (candidate, candidate),
        ).fetchone()
        if taken is None:
            return candidate
    return slug  # a refusal naming no alternative is still honest


def _restore_pdf_gate(conn: Any, dumped_ref: dict[str, Any]) -> tuple[Any, Any, Any]:
    """``(pdf_sha256, pdf_pages, pdf_role)`` only while the ``pdfs`` row
    still exists — a drop whose bytes are gone must not re-point the ref
    at nothing. ``pdf_pages`` arrives as the dump's ``[lo, hi)`` pair."""
    sha = dumped_ref.get("pdf_sha256")
    if not sha:
        return None, None, None
    if not conn.execute(
        "SELECT 1 FROM pdfs WHERE pdf_sha256 = %s", (str(sha),)
    ).fetchone():
        return None, None, None
    pages = dumped_ref.get("pdf_pages")
    pages_range = (
        f"[{pages[0]},{pages[1]})"
        if isinstance(pages, list) and len(pages) == 2
        else None
    )
    return sha, pages_range, dumped_ref.get("pdf_role")


def _pdfs_has(conn: Any, id_kind: str, value: str) -> bool:
    """Whether the corpus still holds the PDF an identifier names —
    ``pdf_sha256`` / ``content_hash`` rows are restored only then."""
    col = "pdf_sha256" if id_kind == "pdf_sha256" else "content_hash"
    return bool(
        conn.execute(f"SELECT 1 FROM pdfs WHERE {col} = %s", (value,)).fetchone()
    )


def _mint_restore_ref(
    conn: Any,
    dump: dict[str, Any],
    *,
    handle: str | None,
    kind: str,
    report: RestoreReport,
    where: str,
) -> tuple[int, str]:
    """Mint the replacement ref for a restore whose original is gone
    (``target='new'``, or ``target='same'`` with no row left). Returns
    ``(ref_id, slug)``: slug is ``args['handle']`` else the dump's
    ``cite_key``; a live owner refuses with a suggested free slug, a
    retired owner's row is reclaimed. Attestation state does not
    transfer: human_verified_*, doi_status/dates, prio, fingerprint and
    the refresh/owner columns all stay at defaults — the minted ref
    re-earns them like a fresh ingest. The retraction state, byline and
    tags *do* carry (a minted copy that silently lost a retraction would
    read as clean); ``bib_entries`` are listed as not carried."""
    dumped_ref = dump.get("ref")
    if not isinstance(dumped_ref, dict) or not dumped_ref:
        raise BadInput(
            "minting a replacement ref needs the dump's slice 0 — the "
            "only slice carrying the ref row",
            next="send the slice whose window starts at 0",
        )
    if dumped_ref.get("kind") != kind:
        raise BadInput(
            f"the dump is a kind={dumped_ref.get('kind')!r} ref, not {kind!r}",
            next=f"restore it through kind='{dumped_ref.get('kind')}'",
        )
    cite_key = next(
        (
            str(i.get("id_value") or "")
            for i in dump.get("identifiers") or []
            if str(i.get("id_kind") or "").strip().lower() == "cite_key"
        ),
        "",
    )
    slug = _normalise_identifier("cite_key", (handle or "").strip() or cite_key)
    if not slug:
        raise BadInput(
            "the dump carries no cite_key and args['handle'] names none "
            "— the mint has no slug",
            next="resend with args={'handle': '<slug>'}",
        )
    owner = conn.execute(
        "SELECT ri.ref_id, r.retired_at IS NULL, r.meta ? 'restore' "
        "FROM ref_identifiers ri "
        "LEFT JOIN refs r ON r.ref_id = ri.ref_id "
        "WHERE ri.id_kind = 'cite_key' AND ri.id_value = %s",
        (slug,),
    ).fetchone()
    if owner is not None and owner[1]:
        free = _suggest_free_slug(conn, slug)
        raise BadInput(
            f"slug {slug!r} is already taken by live pa{int(owner[0])}",
            next=f"resend the same call with args={{'handle': {free!r}}}",
        )
    if owner is not None and owner[2]:
        # A retired owner still holding a restore stamp is a minted ref
        # mid-flight (mints land retired until the completing slice) —
        # stealing its cite_key would strand it.
        raise BadInput(
            f"slug {slug!r} is held by pa{int(owner[0])}'s in-flight restore",
            next="let it finish, or abandon it first: "
            f"put(kind='{kind}', id='pa{int(owner[0])}', "
            "args={'restore_abort': True, 'reason': '…'})",
        )
    if owner is not None:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE id_kind = 'cite_key' AND id_value = %s",
            (slug,),
        )
    dumped_meta = {
        k: v
        for k, v in dict(dumped_ref.get("meta") or {}).items()
        if k not in ("restore", "superseded_by")
    }
    pdf_sha, pdf_pages, pdf_role = _restore_pdf_gate(conn, dumped_ref)
    rhandle = dumped_ref.get("handle")
    if (
        rhandle
        and conn.execute("SELECT 1 FROM refs WHERE handle = %s", (rhandle,)).fetchone()
    ):
        rhandle = None
    parent = dumped_ref.get("parent_id")
    if (
        parent is not None
        and not conn.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (int(parent),),
        ).fetchone()
    ):
        parent = None
    # The minted ref lands RETIRED and is un-retired only by the
    # completing slice (the was_retired path in _restore_dispatch) — a
    # half-restored mint can never read as a live ref with a truncated
    # body, and an abort leaves a retired stub instead (N1 ruling).
    row = conn.execute(
        "INSERT INTO refs (kind, set_by, title, authors, year, provider, "
        " meta, parent_id, pdf_sha256, pdf_pages, pdf_role, handle, "
        " retraction_status, retracted_at, retraction_reason, "
        " retraction_url, retraction_checked_at, retired_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::int4range, %s, %s, "
        " %s, %s, %s, %s, %s, now()) "
        "RETURNING ref_id",
        (
            kind,
            dumped_ref.get("set_by"),
            str(dumped_ref.get("title") or slug),
            Jsonb(dumped_ref["authors"])
            if dumped_ref.get("authors") is not None
            else None,
            dumped_ref.get("year"),
            dumped_ref.get("provider"),
            Jsonb(dumped_meta),
            parent,
            pdf_sha,
            pdf_pages,
            pdf_role,
            rhandle,
            dumped_ref.get("retraction_status"),
            dumped_ref.get("retracted_at"),
            dumped_ref.get("retraction_reason"),
            dumped_ref.get("retraction_url"),
            dumped_ref.get("retraction_checked_at"),
        ),
    ).fetchone()
    assert row is not None
    new_id = int(row[0])
    try:
        # Savepoint: two fresh mints of the same dump can both pass the
        # slug check before either commits — the loser maps to a named
        # refusal instead of a raw unique_violation.
        with conn.transaction():
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('cite_key', %s, %s, 'restore')",
                (slug, new_id),
            )
    except UniqueViolation as exc:
        free = _suggest_free_slug(conn, slug)
        raise BadInput(
            f"slug {slug!r} was taken by a mint that committed after "
            "this restore checked it — the mint rolled back, nothing "
            "was written",
            next=f"resend the same call with args={{'handle': {free!r}}}",
        ) from exc
    _restore_minted_side_rows(conn, new_id, dump, report)
    return new_id, slug


def _restore_minted_side_rows(
    conn: Any, ref_id: int, dump: dict[str, Any], report: RestoreReport
) -> None:
    """The rows a minted ref inherits from the dump: byline
    (``person_ref_id`` only while the person ref still exists — the FK
    is ON DELETE SET NULL but a hard-gone one would violate it at
    insert) and tags (re-resolved by ``(namespace, value)``, the
    identity the dump carries). ``bib_entries`` are deliberately not
    carried — a copy's bibliography is its own to re-derive; the ack
    names them."""
    for a in dump.get("paper_authors") or []:
        person = a.get("person_ref_id")
        if (
            person is not None
            and not conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s", (int(person),)
            ).fetchone()
        ):
            person = None
        conn.execute(
            "INSERT INTO paper_authors (ref_id, position, given, middle, "
            "family, name_raw, orcid, openalex_author_id, person_ref_id, "
            "source, verified_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "COALESCE(%s::timestamptz, now()))",
            (
                ref_id,
                a["position"],
                a.get("given") or "",
                a.get("middle") or "",
                a.get("family") or "",
                a.get("name_raw") or "",
                a.get("orcid"),
                a.get("openalex_author_id"),
                person,
                a.get("source") or "pdf",
                a.get("verified_at"),
                a.get("updated_at"),
            ),
        )
    from precis.store._mappers import _upsert_tag

    for t in dump.get("tags") or []:
        tag_id = _upsert_tag(conn, str(t["namespace"]), str(t["value"]))
        conn.execute(
            "INSERT INTO ref_tags (ref_id, tag_id, set_by, created_at, "
            "expires_at) VALUES (%s, %s, %s, "
            "COALESCE(%s::timestamptz, now()), %s) "
            "ON CONFLICT (ref_id, tag_id) DO NOTHING",
            (
                ref_id,
                tag_id,
                t.get("set_by") or "restore",
                t.get("created_at"),
                t.get("expires_at"),
            ),
        )
    bib = dump.get("bib_entries") or []
    if bib:
        report.not_carried.append(
            f"{len(bib)} bib_entr{'y' if len(bib) == 1 else 'ies'}"
        )


def _restore_identifiers(
    conn: Any,
    target_id: int,
    dump: dict[str, Any],
    *,
    minted: bool,
    for_new: bool,
    report: RestoreReport,
    where: str,
) -> None:
    """Carry the dump's ``ref_identifiers`` onto the target — checked
    before the first write so a live-foreign value refuses the whole
    slice rather than half-landing. Already-owned rows skip; a retired
    owner's row is reclaimed; a scheme the target's meta cleared by a
    human edit stays cleared (gr515246); ``pdf_sha256``/``content_hash``
    land only while the ``pdfs`` row exists. A minted ref's cite_key is
    the slug the mint already wrote — the dumped row is skipped.

    Two copy-aware skips land in ``report.skipped_identifiers`` instead
    of writing: on ``target='new'`` a value another *live* ref owns (a
    copy cannot hold the original's identifiers — refusing pointed at
    'new' again, a dead end, R5); and on ``'same'`` the dumped cite_key
    when the ref has since been renamed — a second live cite_key makes
    every ``LIMIT 1`` slug lookup nondeterministic (R7)."""
    idents = dump.get("identifiers") or []
    if not idents:
        return
    cleared = _cleared_schemes(conn, target_id)
    dropped = _dropped_pairs(conn, target_id)
    own_cite_keys: set[str] | None = None
    for ident in idents:
        s = str(ident.get("id_kind") or "").strip().lower()
        raw_v = str(ident.get("id_value") or "")
        if not s or not raw_v.strip():
            continue
        if minted and s == "cite_key":
            continue
        v = _normalise_identifier(s, raw_v)
        if not v:
            continue
        if s in ("pdf_sha256", "content_hash") and not _pdfs_has(conn, s, v):
            continue
        source = str(ident.get("source") or "").strip().lower()
        if s in cleared and source not in _HUMAN_EDIT_SOURCES:
            continue
        if v in dropped.get(s, ()):
            # The pair was deliberately dropped at the drop_identifier
            # door (gr517591) — a dump restore must not resurrect it,
            # whatever source the dumped row carries: a restore is not
            # an edit. Only a live edit/mcp-edit/web-edit on the ref
            # lifts the pair (ruling: the dump predates the drop, its
            # recorded source describes the ORIGINAL add, not this one).
            report.skipped_identifiers.append(
                {
                    "id_kind": s,
                    "id_value": v,
                    "reason": "suppressed in meta.dropped_identifiers "
                    "— an explicit add lifts the pair",
                }
            )
            continue
        if s == "cite_key" and not minted:
            if own_cite_keys is None:
                own_cite_keys = {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT id_value FROM ref_identifiers "
                        "WHERE ref_id = %s AND id_kind = 'cite_key'",
                        (target_id,),
                    ).fetchall()
                }
            if v in own_cite_keys:
                continue
            if own_cite_keys:
                report.skipped_identifiers.append(
                    {
                        "id_kind": s,
                        "id_value": v,
                        "reason": f"{report.handle} already has cite_key "
                        f"{sorted(own_cite_keys)[0]!r} — the dump's slug "
                        "stays off a renamed ref",
                    }
                )
                continue
        owner = conn.execute(
            "SELECT ri.ref_id, r.retired_at IS NULL FROM ref_identifiers ri "
            "LEFT JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = %s AND ri.id_value = %s",
            (s, v),
        ).fetchone()
        if owner is not None:
            if int(owner[0]) == target_id:
                continue
            if owner[1]:
                owner_handle = handle_registry.format_handle("paper", int(owner[0]))
                if for_new:
                    report.skipped_identifiers.append(
                        {
                            "id_kind": s,
                            "id_value": v,
                            "reason": f"already belongs to live "
                            f"{owner_handle} — a copy cannot hold the "
                            "original's identifiers",
                        }
                    )
                    continue
                raise BadInput(
                    f"identifier {s}={v!r} already belongs to live "
                    f"{owner_handle} — restoring it would fork the "
                    "identifier",
                    next=f"get(kind='paper', id='{owner_handle}')",
                    options=[f"edit(args={{'merge_into': '{owner_handle}'}})"],
                )
            conn.execute(
                "DELETE FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
                (s, v),
            )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source, "
            "created_at) VALUES (%s, %s, %s, %s, "
            "COALESCE(%s::timestamptz, now()))",
            (
                s,
                v,
                target_id,
                source or "restore",
                ident.get("created_at"),
            ),
        )
        report.identifiers += 1
        if s in cleared:
            # A human-sourced row lands and lifts the clear mark — the
            # operator re-added it by hand (the _identifiers_ops rule).
            _unstamp_cleared(conn, target_id, s)


def _restore_frontier_fresh(
    conn: Any,
    dump: dict[str, Any],
    manifest: dict[str, Any],
    lo: int,
    ref_id: int,
    kind: str,
    report: RestoreReport,
    *,
    target: str,
    was_retired: bool,
    where: str,
) -> None:
    """First-slice gates: the window must start at 0, a live ref's body
    must not diverge from the dump's — an identical ord keeps (the
    idempotent re-send), a different one refuses — and the dump's own
    meta merges in under the existing keys (``dumped || meta``, minus
    ``restore``/``superseded_by`` — a merge-tombstone stamp never rides
    onto a live ref). A live body that is merely a *subset* of the dump
    — the clear_abstract shape, body intact but a card gone — restores
    through: the per-ord rules fill the hole and the completing slice's
    sha proves it. Ords this slice doesn't carry can't be checked yet;
    a later slice's per-ord rule or the completion sha catches a real
    divergence."""
    if lo != 0:
        raise BadInput(
            f"no restore is in flight on pa{ref_id}; a restore starts "
            f"at window [0, …), this slice starts at {lo}",
            next="send the slice whose window starts at 0 (it also "
            "carries ref/identifiers/links)",
        )
    dumped_ref = dump.get("ref")
    if dumped_ref is not None and dumped_ref.get("kind") != kind:
        raise BadInput(
            f"the dump is a kind={dumped_ref.get('kind')!r} ref, not {kind!r}",
            next=f"restore it through kind='{dumped_ref.get('kind')}'",
        )
    # The link lists stash on meta.restore for the whole run — shape-
    # check them now so a malformed row refuses on slice 0 instead of
    # landing a KeyError on the completing slice.
    for side in ("links_out", "links_in"):
        for i, lnk in enumerate(dump.get(side) or []):
            try:
                int(lnk["src_ref_id"])
                int(lnk["dst_ref_id"])
                str(lnk["relation"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BadInput(
                    f"{where} {side}[{i}] is malformed: {exc}",
                    next=f"re-export the ref: get(kind='{kind}', "
                    f"id='pa{ref_id}', view='ops')",
                ) from exc
    if not was_retired:
        dumped = {int(c["ord"]): c for c in dump.get("chunks") or []}
        whole = lo == 0 and int(manifest["window"][1]) == int(manifest["chunks_total"])
        live = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id = %s "
            "AND ord >= 0 AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
        conflict: int | None = None
        extras = 0
        for r_ord, r_kind, r_text in live:
            c = dumped.get(int(r_ord))
            if c is None:
                if whole:
                    extras += 1
                continue
            if str(c["chunk_kind"]) != str(r_kind) or str(c["text"]) != str(r_text):
                conflict = int(r_ord)
                break
        if conflict is not None or extras:
            detail = (
                f"ord {conflict} differs"
                if conflict is not None
                else f"{extras} chunk(s) the dump doesn't carry"
            )
            raise BadInput(
                f"pa{ref_id} still has a body ({len(live)} live chunk(s), "
                f"{detail}) — restoring over it would fork the history",
                next=f"undo: save get(kind='paper', id='pa{ref_id}', "
                "view='ops') first, then edit(kind='paper', "
                f"id='pa{ref_id}', args={{'drop_body': True, "
                "'reason': …}})",
            )
    if dumped_ref is not None:
        dumped_meta = {
            k: v
            for k, v in dict(dumped_ref.get("meta") or {}).items()
            if k not in ("restore", "superseded_by")
        }
        conn.execute(
            "UPDATE refs SET meta = (%s::jsonb || refs.meta) WHERE ref_id = %s",
            (Jsonb(dumped_meta), ref_id),
        )
        # The dump's pdf pointer comes back through the same gate a mint
        # uses — only while the pdfs row still exists, and only onto a
        # ref drop_body NULLed (a live pointer always wins).
        pdf_sha, pdf_pages, pdf_role = _restore_pdf_gate(conn, dumped_ref)
        if pdf_sha is not None:
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s, pdf_pages = %s::int4range, "
                "pdf_role = %s, updated_at = now() WHERE ref_id = %s "
                "AND pdf_sha256 IS NULL",
                (pdf_sha, pdf_pages, pdf_role, ref_id),
            )
    _restore_identifiers(
        conn,
        ref_id,
        dump,
        minted=report.minted,
        for_new=target == "new",
        report=report,
        where=where,
    )


def _restore_chunks(
    conn: Any,
    ref_id: int,
    chunks: list[dict[str, Any]],
    report: RestoreReport,
    *,
    minted: bool,
    chunk_ords: dict[str, int],
    inserted_ords: set[int],
) -> None:
    """The per-ord rules: an identical live row keeps, a different live
    body row refuses, a retired row DELETEs+INSERTs, a card row swaps on
    any difference (the card exception to append-only). ``chunk_ords``
    is the run's accumulated dump-chunk_id→ord map — a parent can sit in
    a slice this one hasn't seen; whatever doesn't resolve yet lands
    NULL and the completion fix-up wires it. ``inserted_ords`` collects
    the ords this run created fresh (no prior row) — the abort door
    deletes exactly those."""
    for c in sorted(chunks, key=lambda x: int(x["ord"])):
        ord_ = int(c["ord"])
        existing = conn.execute(
            "SELECT chunk_id, chunk_kind, text, retired_at FROM chunks "
            "WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_),
        ).fetchone()
        if existing is not None and existing[3] is None:
            if str(existing[1]) == c["chunk_kind"] and str(existing[2]) == c["text"]:
                report.kept += 1
                continue
            if ord_ >= 0:
                raise BadInput(
                    f"pa{ref_id} ord {ord_} already holds a different "
                    f"{existing[1]!r} chunk — the ref changed since the "
                    "dump",
                    next="export the ref (view='ops') to see what it holds "
                    "now; to force the dump's body, drop_body first and "
                    "resend",
                )
        if existing is not None:
            conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (int(existing[0]),))
            report.replaced += 1
        else:
            report.inserted += 1
            inserted_ords.add(ord_)
        if ord_ < 0:
            report.cards += 1
        _insert_restore_chunk(conn, ref_id, c, minted=minted, chunk_ords=chunk_ords)
