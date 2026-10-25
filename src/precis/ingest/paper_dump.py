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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from precis.errors import BadInput
from precis.handlers._slice_args import slice_window
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
