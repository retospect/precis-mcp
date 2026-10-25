"""Paper body repair primitives — detach, re-home or re-parent a body.

The agent-facing body doors behind ``edit(kind='paper', args={…})``
(``drop_body`` / ``move_body_to`` / ``split_body`` / ``clear_abstract``,
gr514995) share the SQL here — and since gr515106 so does dedup's
:func:`precis.ingest.dedup._move_body_to_survivor`, which delegates its
body move to :func:`move_body` (there is exactly one body-move
implementation; dedup only keeps the pdf-only branch it can express
without copying a body).

Two hard rules the module holds:

- **Body chunks are append-only** (``docs/conventions/invariants.md``):
  a body row is DELETEd or INSERTed, never UPDATEd. A move INSERTs
  copies under the target ref, then DELETEs the donor's rows — the
  ``(ref_id, ord)`` uniqueness is what re-links the derived rows, so the
  copies must land before the delete.
- **Derived rows follow the body**: ``chunk_embeddings`` /
  ``chunk_summaries`` / ``chunk_citations`` / ``chunk_blobs`` /
  ``chunk_tags`` / ``chunk_review`` are copied onto the new chunk ids;
  ``links`` chunk anchors (``src_chunk_id`` / ``dst_chunk_id`` — the
  agent/user edges to ``paper:slug~N``) are re-pointed onto the copies
  through the same ``(ref_id, ord)`` join before the donor delete, or
  counted when they cascade away on a drop. Anchors on already-retired
  donor chunks have no copy to land on and cascade away — reported as
  ``chunk_links_removed``. Worker bookkeeping
  (``chase_coverage``) and the chunk audit log (``chunk_events``)
  cascade away and re-derive; ``chunk_claims`` has no FK, so its rows
  are orphaned and expire under the claim cooldown. The
  ``chunk_embeddings`` trigger fills the denormalised ``ref_id``/``kind``
  from the new parent chunk itself (migration 0226).

The mutating doors run under ``@trust_door("computed")`` — dedup's
choice for its own ref/link moves (``merge_duplicate``,
``store.migrate_links``). The links trust guard
(``links_trust_guard``, migration 0214) refuses an endpoint change on
a ``trust_write_version = 1`` link from an unconverted writer, which
this would otherwise be under the MCP edit context; under the door
the re-point restamps the link ``computed/hunch`` and records a
``revise`` act. ``migrate_links``' insert+``carry`` copy is not an
option — it mints a new ``link_id``, and the id is the edge's
identity (``measures.primary_link_id``, the revisions log).

gr514995 ruling (option A): a re-anchor *keeps* the link's stamp
when the text it was anchored on is provably unchanged.
:func:`_restore_link_stamp` re-applies the stamp the ``revise`` act
just recorded — the link's own prior, so the door can set no other
value and none higher — through the schema's internal door
(``precis.trust_internal``), only while every moved anchor's old and
new chunk text hash identically, only inside the re-point's own
transaction, and writes its own audit act (``via: body-anchor``). A
re-point whose anchored text changed keeps the demotion. The helper
is module-private: no MCP, verb or ``args=`` path reaches it. The
same write context keeps a version-1 ref's body/meta writes from
refusing (``precis_trust_body_guard`` / the refs guard restamp
instead).

Everything runs inside the caller's transaction (``conn=``) — the doors
get one atomic commit per call.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.ingest.db_writer import _BODY_OWNED_META_KEYS, _merge_ingest_meta
from precis.ingest.dedup import (
    _PDF_ID_KINDS,
    _SINGLE_ID_KINDS,
    _rehome_content_identifiers,
)
from precis.store.revision_context import trust_door

#: chunk_id-keyed tables copied onto the moved chunks' new ids, as
#: ``(table, column list minus chunk_id)``. Order is irrelevant — the
#: ``(ref_id, ord)`` join re-links each row; anything not listed is the
#: deliberate cascade set named in the module docstring.
_DERIVED_COPIES: tuple[tuple[str, str], ...] = (
    (
        "chunk_embeddings",
        "embedder, vector, status, attempts, last_error, created_at, content_sha",
    ),
    (
        "chunk_summaries",
        "summarizer, text, prompt_hash, token_count, status, attempts, "
        "last_error, created_at, content_sha",
    ),
    ("chunk_citations", "marker, bib_entry_id, created_at"),
    (
        "chunk_blobs",
        "bytes, mime, sha256, size_bytes, width, height, created_at",
    ),
    ("chunk_tags", "tag_id, set_by, created_at"),
    ("chunk_review", "checker, approved_sha, verdict, at"),
)

#: ``chunks`` columns the copy reproduces verbatim — the same list
#: dedup's move uses, plus ``keywords``/``keywords_meta`` (the keyword
#: worker's output would otherwise be silently dropped with the donor
#: row). ``handle``/``pos``/``parent_chunk_id``/``content_sha`` are
#: draft/filesystem fields, NULL on every paper body chunk.
_CHUNK_COPY_COLS = (
    "set_by, ord, chunk_kind, text, section_path, page_first, page_last, "
    "token_count, meta, numerics, keywords, keywords_meta"
)


@dataclass(frozen=True)
class BodyReport:
    """What a body op did — or, from :func:`probe_body`, would do.

    ``chunks`` counts the ref's live ``ord >= 0`` rows (what a move
    copies); ``retired_chunks`` counts the already-retired ``ord >= 0``
    rows — the ops' DELETE takes them too, so they are reported. A link
    whose ``src_chunk_id`` or ``dst_chunk_id`` sits on a *live* body
    chunk counts in ``chunk_links`` — what a move re-points onto the
    copies. One anchored on a *retired* body chunk has no copy to land
    on and counts in ``chunk_links_removed`` — a move loses it to the
    delete's cascade, and ``detach_body`` widens the field to the full
    union since a drop loses every chunk-anchored link
    (``chunk_links_total``). ``embeddings`` counts ``chunk_embeddings``
    rows on the live chunks; ``identifiers`` / ``meta_keys`` name the
    content-derived alias rows and extraction-describing meta keys the
    op drops or moves. These are the figures the door's ack and
    ref_event report.
    """

    chunks: int = 0
    retired_chunks: int = 0
    embeddings: int = 0
    chunk_links: int = 0
    chunk_links_removed: int = 0
    chunk_links_total: int = 0
    pdf: bool = False
    identifiers: list[str] = field(default_factory=list)
    meta_keys: list[str] = field(default_factory=list)
    #: The pdf sha a ``keep_pdf`` drop retained (``None`` = pointer
    #: cleared as usual); set by :func:`detach_body`, never the probe.
    kept_pdf: str | None = None
    #: True when ``kept_pdf`` re-pointed ``refs.pdf_sha256`` (and cleared
    #: ``pdf_pages``, the dropped file's span).
    pdf_repointed: bool = False

    @property
    def is_stub(self) -> bool:
        """No body and no pdf — nothing a body op would change."""
        return self.chunks == 0 and not self.pdf


def probe_body(conn: Any, ref_id: int) -> BodyReport:
    """Read-only snapshot of a ref's body-bearing state (dry-run probe)."""
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE retired_at IS NULL), "
        "       count(*) FILTER (WHERE retired_at IS NOT NULL) "
        "FROM chunks WHERE ref_id = %s AND ord >= 0",
        (ref_id,),
    ).fetchone()
    n_chunks = int(row[0]) if row else 0
    n_retired = int(row[1]) if row else 0
    row = conn.execute(
        "SELECT count(*) FROM chunk_embeddings e "
        "JOIN chunks c ON c.chunk_id = e.chunk_id "
        "WHERE c.ref_id = %s AND c.ord >= 0 AND c.retired_at IS NULL",
        (ref_id,),
    ).fetchone()
    n_embeddings = int(row[0]) if row else 0
    # Chunk-anchored links, split by the chunk's state: a move re-points
    # the live-anchored ones and loses the retired-anchored ones to the
    # delete's cascade; a drop loses all of them. A link anchored on a
    # live *and* a retired chunk counts in each filtered column and once
    # in the union.
    row = conn.execute(
        "SELECT count(DISTINCT l.link_id) "
        "       FILTER (WHERE c.retired_at IS NULL), "
        "       count(DISTINCT l.link_id) "
        "       FILTER (WHERE c.retired_at IS NOT NULL), "
        "       count(DISTINCT l.link_id) "
        "FROM links l "
        "JOIN chunks c ON c.chunk_id = l.src_chunk_id "
        "            OR c.chunk_id = l.dst_chunk_id "
        "WHERE c.ref_id = %s AND c.ord >= 0",
        (ref_id,),
    ).fetchone()
    n_links = int(row[0]) if row else 0
    n_links_removed = int(row[1]) if row else 0
    n_links_total = int(row[2]) if row else 0
    row = conn.execute(
        "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    has_pdf = row is not None and row[0] is not None
    return BodyReport(
        chunks=n_chunks,
        retired_chunks=n_retired,
        embeddings=n_embeddings,
        chunk_links=n_links,
        chunk_links_removed=n_links_removed,
        chunk_links_total=n_links_total,
        pdf=has_pdf,
        identifiers=_content_ids(conn, ref_id),
        meta_keys=sorted(_body_owned_meta(conn, ref_id)),
    )


def _body_owned_meta(conn: Any, ref_id: int) -> dict[str, Any]:
    """The donor's extraction-describing meta keys (the subset of
    ``_BODY_OWNED_META_KEYS`` it actually carries)."""
    row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    meta = row[0] if row and isinstance(row[0], dict) else {}
    return {k: v for k, v in meta.items() if k in _BODY_OWNED_META_KEYS}


def _strip_body_owned_meta(conn: Any, ref_id: int) -> None:
    """Remove a ref's extraction-describing meta keys."""
    keys = sorted(_body_owned_meta(conn, ref_id))
    if keys:
        conn.execute(
            "UPDATE refs SET meta = meta - %s::text[] WHERE ref_id = %s",
            (keys, ref_id),
        )


def _content_ids(conn: Any, ref_id: int) -> list[str]:
    """``id_kind:id_value`` of the ref's content-derived identifiers —
    the kinds that describe the attached file/body, not the paper."""
    kinds = list(_PDF_ID_KINDS) + list(_SINGLE_ID_KINDS)
    rows = conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind = ANY(%s) ORDER BY id_kind",
        (ref_id, kinds),
    ).fetchall()
    return [f"{k}:{v}" for k, v in rows]


def _attached_pdf_shas(conn: Any, ref_id: int) -> tuple[str | None, list[str]]:
    """``(refs.pdf_sha256, sorted pdf_sha256 identifier values)`` — every
    file the ref is attached to."""
    row = conn.execute(
        "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    pointer = str(row[0]).strip() if row and row[0] else None
    rows = conn.execute(
        "SELECT id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind = 'pdf_sha256' ORDER BY id_value",
        (ref_id,),
    ).fetchall()
    return pointer, [str(r[0]).strip() for r in rows]


def resolve_kept_pdf(conn: Any, ref_id: int, *, named: str | None) -> tuple[str, bool]:
    """The pdf sha a ``keep_pdf`` drop retains — and whether the pointer
    moves (gr513878).

    "Attached" is the union of ``refs.pdf_sha256`` and the ref's
    ``pdf_sha256`` identifier rows — a merge can leave a second file's
    row behind while the pointer still names the ref's own attachment.
    ``named`` (the door's ``args['pdf_sha256']``) must name one of them;
    with no ``named`` a single distinct attached sha keeps it and
    several refuse — the caller names which file survives. Returns
    ``(sha, switch)`` where ``switch`` says the kept sha is not the
    ref's current pointer, so the door must re-point
    ``refs.pdf_sha256`` and clear ``pdf_pages`` (the stored span was
    measured on the dropped extraction, not the surviving file).

    A switch also needs the file itself: ``refs.pdf_sha256`` FKs to
    ``pdfs``, so re-pointing at an identifier whose bytes the corpus
    ledger doesn't hold refuses — the heal could never find the file
    anyway.
    """
    named_sha = (named or "").strip().lower() or None
    pointer, id_shas = _attached_pdf_shas(conn, ref_id)
    attached = sorted({*id_shas, *([pointer] if pointer else [])})
    sha: str | None
    if named_sha is not None:
        if named_sha not in attached:
            raise BadInput(
                f"pdf_sha256={named_sha!r} is not attached to ref {ref_id}",
                next=(
                    f"attached pdf_sha256 values: {', '.join(attached) or 'none'} "
                    "— pass one of them as args['pdf_sha256'], or drop "
                    "keep_pdf to clear the pdf too"
                ),
            )
        sha = named_sha
    elif not attached:
        raise BadInput(
            f"ref {ref_id} holds no pdf — keep_pdf has nothing to keep",
            next="run the plain drop_body (no keep_pdf), or attach a pdf "
            "first with put(args={'pdf_url': ...})",
        )
    elif len(attached) > 1:
        raise BadInput(
            f"ref {ref_id} is attached to {len(attached)} pdfs — "
            "keep_pdf needs args['pdf_sha256'] to name the one to keep",
            next=f"attached pdf_sha256 values: {', '.join(attached)}; "
            "re-run with 'pdf_sha256': '<sha>' in args",
        )
    else:
        sha = attached[0]
    switch = sha != pointer
    if (
        switch
        and conn.execute("SELECT 1 FROM pdfs WHERE pdf_sha256 = %s", (sha,)).fetchone()
        is None
    ):
        raise BadInput(
            f"pdf_sha256={sha!r} is on ref {ref_id} but the corpus holds "
            "no pdfs row for it — there is no stored file to point at",
            next=f"re-point at the attached sha that IS stored "
            f"({pointer or 'none held'}), or re-fetch the file first",
        )
    return sha, switch


@trust_door("computed")
def detach_body(
    *,
    ref_id: int,
    conn: Any,
    keep_pdf: bool = False,
    pdf_sha256: str | None = None,
) -> BodyReport:
    """Strip ``ref_id`` back to a stub: body chunks, pdf pointers,
    content ids and body-owned meta keys all go; bibliographic metadata,
    identifiers and cards stay. The FK cascade clears every derived row
    with the chunk delete — including ``links`` chunk anchors, which is
    intended here and reported via ``report.chunk_links_removed`` (the
    union over live and retired chunks: every anchored link dies).
    Retired ``ord >= 0`` rows delete too (``report.retired_chunks``).

    ``keep_pdf=True`` keeps the attachment instead of clearing it
    (gr513878): ``refs.pdf_sha256``/``pdf_pages`` and the matching
    ``pdf_sha256`` identifier row survive, so the bodiless ref is a
    ``paper_reconcile`` / ``precis bodiless-heal`` candidate (the heal's
    predicate is exactly ``pdf_sha256 IS NOT NULL AND NOT has_body``) and
    the stored file re-extracts with no refetch. ``pdf_sha256=`` names
    the file to keep when the ref is attached to several — naming one
    that is not the current pointer re-points ``refs.pdf_sha256`` at it
    and clears ``pdf_pages`` (the span belonged to the dropped
    extraction); every other attached sha's identifier row drops with
    ``content_hash`` — but ``paper_id``/``pub_id`` stay under keep_pdf:
    they name the paper, not the dropped bytes, and nothing re-mints
    them.
    """
    report = probe_body(conn, ref_id)
    # A stub still runs when it carries stale content ids or body meta —
    # the early return must not strand a chunkless, pdf-less ref that has
    # junk only this op removes (gr515106 LOW).
    if report.is_stub and not report.identifiers and not report.meta_keys:
        return report
    kept: str | None = None
    switch = False
    if keep_pdf:
        kept, switch = resolve_kept_pdf(conn, ref_id, named=pdf_sha256)
    conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (ref_id,))
    if report.pdf and kept is None:
        conn.execute(
            "UPDATE refs SET pdf_sha256 = NULL, pdf_pages = NULL, "
            "updated_at = now() WHERE ref_id = %s",
            (ref_id,),
        )
    elif switch:
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_pages = NULL, "
            "updated_at = now() WHERE ref_id = %s",
            (kept, ref_id),
        )
    # The content ids describe the dropped artifact; leaving pdf_sha256 /
    # content_hash behind would probe a re-ingest of the same wrong file
    # straight back onto this stub. cite_key / doi / arxiv / s2 stay —
    # they name the paper, not the bytes. keep_pdf spares exactly the
    # kept sha's pdf_sha256 row (the other attachments' rows drop — the
    # door named the survivor) plus paper_id/pub_id, which describe the
    # paper and which the heal never re-mints (Opus review, gr513878).
    if kept is None:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = ANY(%s)",
            (ref_id, list(_PDF_ID_KINDS) + list(_SINGLE_ID_KINDS)),
        )
    else:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s AND "
            "(id_kind = 'content_hash' OR "
            " (id_kind = 'pdf_sha256' AND id_value <> %s))",
            (ref_id, kept),
        )
    _strip_body_owned_meta(conn, ref_id)
    # A drop removes every chunk-anchored link — the union, not just the
    # retired-anchored subset the field holds off the probe.
    return replace(
        report,
        chunk_links_removed=report.chunk_links_total,
        kept_pdf=kept,
        pdf_repointed=switch,
    )


def _copy_body_chunks(conn: Any, *, src_ref_id: int, dst_ref_id: int) -> int:
    """INSERT the donor's live body chunks under the target; returns the
    count. Derived rows are re-linked by :func:`_copy_derived_rows`
    *before* the donor rows are deleted."""
    cur = conn.execute(
        f"INSERT INTO chunks (ref_id, {_CHUNK_COPY_COLS}) "
        f"SELECT %s, {_CHUNK_COPY_COLS} FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL "
        " ORDER BY ord",
        (dst_ref_id, src_ref_id),
    )
    return cur.rowcount


def _copy_derived_rows(
    conn: Any, *, src_ref_id: int, dst_ref_id: int
) -> dict[str, int]:
    """Re-attach each derived row to the copy of its chunk — the ord join
    works because ``(ref_id, ord)`` is unique and the copies landed with
    the donor rows still in place."""
    copied: dict[str, int] = {}
    for table, cols in _DERIVED_COPIES:
        # Every column is qualified — chunks carries created_at too.
        e_cols = ", ".join(f"e.{c.strip()}" for c in cols.split(","))
        cur = conn.execute(
            f"INSERT INTO {table} (chunk_id, {cols}) "
            f"SELECT n.chunk_id, {e_cols} FROM {table} e "
            "JOIN chunks o ON o.chunk_id = e.chunk_id "
            " AND o.ref_id = %s AND o.ord >= 0 AND o.retired_at IS NULL "
            "JOIN chunks n ON n.ref_id = %s AND n.ord = o.ord "
            " AND n.retired_at IS NULL",
            (src_ref_id, dst_ref_id),
        )
        if cur.rowcount:
            copied[table] = cur.rowcount
    return copied


def _repoint_chunk_links(conn: Any, *, src_ref_id: int, dst_ref_id: int) -> int:
    """Re-anchor links whose chunk endpoint sits on a donor body chunk.

    ``links.src_chunk_id`` / ``links.dst_chunk_id`` are
    ``ON DELETE CASCADE`` — without this step a move would silently
    destroy the agent/user-authored edges anchored at ``paper:slug~N``
    (prod: thousands of them). The matching ref column
    (``src_ref_id`` / ``dst_ref_id``) moves with the chunk. Runs after
    the copies land and before the donor delete, re-linking through the
    same ``(ref_id, ord)`` join as the derived copies; a link anchored
    on both ends is touched twice but counted once. Links are not body
    chunks — the append-only rule doesn't cover them, so a plain UPDATE
    re-point is correct here.

    The UPDATE is a semantic change under the 0214 trust guard: on a
    ``trust_write_version = 1`` link it needs the ``computed`` write
    context :func:`move_body`'s door opens (module docstring), and the
    link is restamped to that producer with a recorded ``revise`` act.
    :func:`_restore_link_stamp` then re-applies the link's own prior
    stamp when the anchor's text is provably unchanged (gr514995).
    """
    moved: set[int] = set()
    for end in ("src", "dst"):
        rows = conn.execute(
            f"UPDATE links l SET {end}_chunk_id = n.chunk_id, "
            f"{end}_ref_id = %(dst)s "
            "FROM chunks o "
            "JOIN chunks n ON n.ref_id = %(dst)s AND n.ord = o.ord "
            " AND n.retired_at IS NULL "
            f"WHERE l.{end}_chunk_id = o.chunk_id "
            " AND o.ref_id = %(src)s AND o.ord >= 0 "
            " AND o.retired_at IS NULL "
            "RETURNING l.link_id",
            {"src": src_ref_id, "dst": dst_ref_id},
        ).fetchall()
        moved.update(int(r[0]) for r in rows)
    for link_id in sorted(moved):
        _restore_link_stamp(conn, link_id)
    return len(moved)


def _restore_link_stamp(conn: Any, link_id: int) -> bool:
    """Re-apply a link's own prior trust stamp after a chunk re-anchor.

    The internal door (``precis.trust_internal`` — the
    ``precis_trust_body_guard`` pattern, the schema's own way past the
    guard's raw-set refusal) under a deliberately narrow contract:

    - It re-applies only the stamp this link itself carried just
      before the re-point — read back from the ``before`` of the
      ``revise`` act the re-point's UPDATE recorded, never from a
      caller-supplied value. It can set no other stamp and none
      higher: the signature takes no stamp parameter, so there is
      nothing to smuggle.
    - It applies only when every anchor the re-point moved lands on a
      copy whose text hashes identically to the donor's
      (``md5(text)``; ``chunks.content_sha`` is a draft/filesystem
      field, NULL on body chunks). A re-point onto changed anchor
      text keeps the ``computed/hunch`` demotion.
    - It applies only inside the re-point's own transaction: the
      prior stamp and the old anchor ids come from ``revisions``
      rows scoped to ``xact = txid_current()`` — a later transaction
      finds nothing to re-apply.
    - Every use writes its own audit act (``revise`` with ``via:
      body-anchor``): the links act trigger stays silent on a
      stamp-only update, so the act is appended the way
      ``precis_trust_body_guard`` records its own re-stamps.

    Module-private by design (gr514995): no MCP door, verb or
    ``args=`` key reaches it. Returns True when a stamp was
    re-applied; False when there was nothing safe to re-apply.
    """
    row = conn.execute(
        "SELECT prev_state, trust_acts FROM revisions "
        "WHERE target_kind = 'link' AND target_id = %s "
        "  AND xact = txid_current()",
        (link_id,),
    ).fetchone()
    if row is None:
        return False
    prev_state = row[0] if isinstance(row[0], dict) else {}
    acts = row[1] if isinstance(row[1], list) else []
    prior = next(
        (
            a["before"]
            for a in acts
            if isinstance(a, dict)
            and a.get("action") == "revise"
            and isinstance(a.get("before"), dict)
        ),
        None,
    )
    if prior is None:
        return False
    cur = conn.execute(
        "SELECT src_chunk_id, dst_chunk_id, trust_origin, "
        "trust_confidence, trust_write_version FROM links "
        "WHERE link_id = %s",
        (link_id,),
    ).fetchone()
    if cur is None or (cur[2], cur[3], cur[4]) == (
        prior["origin"],
        prior["confidence"],
        prior["version"],
    ):
        return False
    # Every anchor the re-point moved must land on byte-identical
    # text; the donor rows are still present at this point (the
    # delete runs after the re-point).
    pairs = (
        (prev_state.get("src_chunk_id"), cur[0]),
        (prev_state.get("dst_chunk_id"), cur[1]),
    )
    n_moved = sum(1 for o, n in pairs if o != n)
    moved_pairs = [
        (int(o), int(n)) for o, n in pairs if o is not None and n is not None and o != n
    ]
    if len(moved_pairs) != n_moved:
        return False  # an anchor moved to or from NULL — unverifiable
    ids = [i for pair in moved_pairs for i in pair]
    hashes = {
        int(r[0]): r[1]
        for r in conn.execute(
            "SELECT chunk_id, md5(text) FROM chunks WHERE chunk_id = ANY(%s)",
            (ids,),
        )
    }
    if any(hashes.get(o) != hashes.get(n) for o, n in moved_pairs):
        return False
    # set_config's third arg keeps the door transaction-local — but
    # move_body runs mid-merge inside dedup's larger transaction, so
    # commit/abort no longer bounds it: left set, the bypass would cover
    # every guarded write the merge makes after this (no acts recorded,
    # stale stamps kept). Reset to '' right after the guarded UPDATE,
    # like the 0214 body guard's own door use — then the act append runs
    # back under the caller's context. No finally-block: on an aborted
    # transaction the door dies with it anyway, and a reset attempt
    # would mask the real error with InFailedSqlTransaction (the caller
    # would lose the CheckViolation's next=).
    conn.execute("SELECT set_config('precis.trust_internal', 'on', true)")
    conn.execute(
        "UPDATE links SET trust_origin = %s, trust_confidence = %s, "
        "trust_write_version = %s WHERE link_id = %s",
        (
            prior["origin"],
            prior["confidence"],
            int(prior["version"]),
            link_id,
        ),
    )
    conn.execute("SELECT set_config('precis.trust_internal', '', true)")
    conn.execute(
        "SELECT precis_trust_append_act('link', %s, "
        "precis_trust_act_json('link', %s, 'revise', NULL, %s::text[], "
        "%s::text[], precis_link_sha_of(l)) "
        '|| \'{"via": "body-anchor"}\'::jsonb, NULL, NULL) '
        "FROM links l WHERE l.link_id = %s",
        (
            link_id,
            link_id,
            [cur[2], cur[3], str(cur[4])],
            [prior["origin"], prior["confidence"], str(prior["version"])],
            link_id,
        ),
    )
    return True


@trust_door("computed")
def move_body(*, src_ref_id: int, dst_ref_id: int, conn: Any) -> BodyReport:
    """Move the donor's body, derived rows, pdf pointers, content ids and
    body-owned meta keys to the target — which the door guarantees holds
    no ``ord >= 0`` chunk. DELETE + INSERT only.

    ``links`` chunk anchors on the donor's live body re-point onto the
    copies (``report.chunk_links`` counts the edges touched); anchors on
    already-retired donor chunks have no copy to land on and cascade
    away with the delete (``report.chunk_links_removed``). Retired
    ``ord >= 0`` rows delete too (``report.retired_chunks``).

    ``pdf_role`` fills only when the target has none (dedup's COALESCE):
    it names the record's SI designation, not a byte pointer.
    ``paper_id``/``pub_id`` move only onto a target that holds none of
    that kind — they are single-valued per ref (``v_refs`` reads them as
    scalars), so a target carrying its own keeps it.
    """
    report = probe_body(conn, src_ref_id)
    _copy_body_chunks(conn, src_ref_id=src_ref_id, dst_ref_id=dst_ref_id)
    _copy_derived_rows(conn, src_ref_id=src_ref_id, dst_ref_id=dst_ref_id)
    repointed = _repoint_chunk_links(conn, src_ref_id=src_ref_id, dst_ref_id=dst_ref_id)
    conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (src_ref_id,))
    report = replace(report, chunk_links=repointed)

    # The pdf pointer moves only onto a target that lacks one (dedup's
    # own rule): the MCP door's target always does, but dedup's survivor
    # may already hold a pdf — then the donor keeps its pointer and the
    # target's pdf-kind rows are live, not stale (gr515106).
    row = conn.execute(
        "SELECT pdf_sha256 FROM refs WHERE ref_id = %s", (dst_ref_id,)
    ).fetchone()
    dst_had_pdf = row is not None and row[0] is not None
    if report.pdf and not dst_had_pdf:
        row = conn.execute(
            "SELECT pdf_sha256, pdf_pages, pdf_role FROM refs WHERE ref_id = %s",
            (src_ref_id,),
        ).fetchone()
        assert row is not None
        pdf_sha, pdf_pages, pdf_role = row
        conn.execute(
            "UPDATE refs SET pdf_sha256 = NULL, pdf_pages = NULL, "
            "updated_at = now() WHERE ref_id = %s",
            (src_ref_id,),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_pages = %s, "
            "pdf_role = COALESCE(%s, pdf_role), updated_at = now() "
            "WHERE ref_id = %s",
            (pdf_sha, pdf_pages, pdf_role, dst_ref_id),
        )

    # Content identifiers follow the body: pdf_sha256/content_hash
    # always (the file moved), paper_id/pub_id only onto a target that
    # lacks the kind (single-valued per ref). The target holds no body
    # (every caller guarantees it — the ``(ref_id, ord)`` uniqueness
    # would refuse a second body anyway), so any pdf-kind rows it
    # carries that describe bytes/text it does not hold are stale and
    # dropped by the re-home (gr515805).
    _rehome_content_identifiers(
        conn,
        src_ref_id=src_ref_id,
        dst_ref_id=dst_ref_id,
        dst_had_pdf=dst_had_pdf,
        dst_had_body=False,
    )

    body_meta = _body_owned_meta(conn, src_ref_id)
    if body_meta:
        _merge_ingest_meta(dst_ref_id, body_meta, conn=conn)
        _strip_body_owned_meta(conn, src_ref_id)
    return report


def ensure_paper_cards(conn: Any, ref_id: int) -> int:
    """Fill the ref's missing ``card_*`` chunks; returns how many wrote.

    A split mint starts chunkless; a bare move target may have a partial
    card set (ingest writes cards at mint for *some* stub paths). Missing
    kinds are appended below the ref's lowest ``ord`` — never re-ording
    an existing card, never colliding on ``(ref_id, ord)``. ``set_by`` is
    left NULL (``chunks.set_by`` FKs to ``actors``; ingest leaves it NULL
    on agent-driven writes too).
    """
    from precis.ingest.pipeline import _build_cards

    existing = {
        str(r[0])
        for r in conn.execute(
            "SELECT chunk_kind FROM chunks WHERE ref_id = %s AND ord < 0",
            (ref_id,),
        )
    }
    row = conn.execute(
        "SELECT title, authors, meta FROM refs WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    if row is None:
        return 0
    meta = row[2] if isinstance(row[2], dict) else {}
    abstract = meta.get("abstract", "")
    keywords = meta.get("keywords", [])
    cards = [
        c
        for c in _build_cards(
            title=row[0] or "",
            authors=list(row[1] or []),
            abstract=abstract if isinstance(abstract, str) else "",
            keywords=list(keywords) if isinstance(keywords, list) else [],
        )
        if c.chunk_kind not in existing
    ]
    if not cards:
        return 0
    row = conn.execute(
        "SELECT LEAST(min(ord), 0) FROM chunks WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    # A chunkless ref (a split mint) gives min(ord) NULL — LEAST skips
    # the NULL and yields 0, so cards start at -1. A ref whose lowest
    # ord is positive (body only, no cards) must not push a card into
    # the ord >= 0 body range — LEAST pins the base at 0.
    min_ord = int(row[0]) if row and row[0] is not None else 0
    written = 0
    for i, card in enumerate(cards):
        cur = conn.execute(
            "INSERT INTO chunks "
            "(ref_id, set_by, ord, chunk_kind, text, section_path, "
            " page_first, page_last, token_count, meta, numerics) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (ref_id, ord) DO NOTHING",
            (
                ref_id,
                None,
                min_ord - 1 - i,
                card.chunk_kind,
                card.text,
                list(card.section_path),
                card.page_first,
                card.page_last,
                card.token_count,
                Jsonb(card.meta or {}),
                list(card.numerics),
            ),
        )
        written += cur.rowcount
    return written


__all__ = [
    "BodyReport",
    "detach_body",
    "ensure_paper_cards",
    "move_body",
    "probe_body",
    "resolve_kept_pdf",
]
