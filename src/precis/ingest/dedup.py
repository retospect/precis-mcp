"""Duplicate-paper reconciliation — merge a duplicate ref into a survivor.

Two entry points share one merge primitive:

* **Phase 1** (in :mod:`precis.ingest.remediate`): when ``fix-metadata``
  re-derives a DOI that a *different* live ref already owns, the suspect
  is a duplicate of that canonical — :func:`merge_duplicate` folds it in
  rather than erroring.
* **Phase 2** (:func:`reconcile_by_pdf_sha256`): a standing sweep that
  collapses live paper refs sharing a ``pdf_sha256`` (the same file
  ingested as two refs) to the best survivor.

The merge is the same shape as ``ingest/add._reconcile_orphan_stub``:
migrate external identifiers + graph edges to the survivor, record a
``supersedes`` edge + ``meta.superseded_by`` on the loser, soft-delete it
(reversible), and write audit ``ref_events`` on both. Never hard-deletes;
``probe_existing`` already filters soft-deleted refs, so a retired
duplicate can't resurrect.

Why a sweep at all: ingest-time dedup (``db_writer.probe_existing`` on
pdf_sha256 / doi / arxiv / s2 / content_hash / paper_id) only misses a
second copy that shares *none* of those keys — a different file of the
same paper that resolved no DOI, or a different OCR of the same text.
Better ingest-time resolution shrinks that set but can't zero it, so the
periodic reconciliation (this module, run by ``paper_reconcile``) is the
safety net. Phase 3 (:func:`reconcile_by_title_similarity`) covers the
id-less title-only-stub class; near-dup detection across two *held*
copies with different OCR (SimHash/MinHash) is not built.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
from precis.store import Store, Tag
from precis.store._body_predicate import has_body_sql
from precis.store.revision_context import trust_door
from precis.utils.title_merge import _first_family, title_merge_refusal

log = logging.getLogger(__name__)

#: External identifier kinds that belong to the *paper*, so they move to
#: the survivor on a merge. Content-derived ids (cite_key, paper_id,
#: pub_id, pdf_sha256, content_hash) stay on the retired ref — they
#: describe *that* artifact — unless the survivor takes the duplicate's
#: body/PDF, in which case they re-home onto it
#: (:func:`_move_body_to_survivor`, :func:`_rehome_content_identifiers`).
_MIGRATABLE_ID_KINDS = ("doi", "arxiv", "s2", "pubmed", "openalex", "mag", "dblp")

#: Content-derived identifier kinds that follow the PDF / body to the survivor.
_PDF_ID_KINDS = ("pdf_sha256", "content_hash")
#: Single-valued per ref (the ``v_refs`` view reads them as scalar
#: subqueries), so they move only onto a survivor that holds none.
_SINGLE_ID_KINDS = ("paper_id", "pub_id")


def _rehome_content_identifiers(
    conn: Any,
    *,
    src_ref_id: int,
    dst_ref_id: int,
    dst_had_pdf: bool,
    dst_had_body: bool,
) -> None:
    """Re-home the donor's content identifiers onto the target.

    Nothing constrains ``(ref_id, id_kind)`` — the pk is
    ``(id_kind, id_value)`` — so a target's own same-kind rows would
    double up with the re-homed ones (gr515805). Rows that name bytes
    or a body the target does not hold are stale: ``pdf_sha256`` on a
    pdf-less target, ``content_hash`` on a bodiless one (the hash
    covers the canonical body text, not file bytes). They are dropped
    first; the flags carry *pre-move* state because a pdf move
    rewrites ``refs.pdf_sha256`` ahead of this step.
    ``paper_id``/``pub_id`` are single-valued per ref (``v_refs`` reads
    them as scalars): each moves only onto a target that holds none of
    that kind — a target that has one keeps it and the donor's row
    stays behind.
    """
    stale = [
        kind
        for kind, dst_had in (
            ("pdf_sha256", dst_had_pdf),
            ("content_hash", dst_had_body),
        )
        if not dst_had
    ]
    if stale:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = ANY(%s)",
            (dst_ref_id, stale),
        )
    held = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT id_kind FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = ANY(%s)",
            (dst_ref_id, list(_SINGLE_ID_KINDS)),
        )
    }
    kinds = list(_PDF_ID_KINDS) + [k for k in _SINGLE_ID_KINDS if k not in held]
    conn.execute(
        "UPDATE ref_identifiers SET ref_id = %s "
        "WHERE ref_id = %s AND id_kind = ANY(%s)",
        (dst_ref_id, src_ref_id, kinds),
    )


def _move_body_to_survivor(
    store: Store,
    *,
    survivor_ref_id: int,
    duplicate_ref_id: int,
    source: str,
    conn: Any,
) -> list[str]:
    """Move the duplicate's body and/or PDF onto a survivor that lacks them.

    Returns what moved (``"body"``, ``"pdf"``); empty when the survivor
    already holds whatever the duplicate has (the ordinary merge).

    * **body**: survivor has no ``ord >= 0`` chunk and the duplicate does.
      Body chunks are append-only, so each is INSERTed on the survivor and
      the duplicate's DELETEd (never UPDATEd). Embeddings, summaries and
      keywords are left to the workers (ingest never fills them); the
      duplicate's cards (``ord < 0``) stay behind, the survivor keeps its own.
    * **pdf**: survivor has no ``pdf_sha256`` and the duplicate does:
      ``refs.pdf_sha256/pdf_pages/pdf_role`` move (the ``pdfs`` row and its
      stored file are keyed by sha, so the file follows the sha).
    * Either move carries the content identifiers (``pdf_sha256``,
      ``content_hash``, and ``paper_id``/``pub_id`` when the survivor has
      none) so a re-ingest of the file probes onto the survivor; the
      survivor's own stale rows of those kinds are dropped first
      (:func:`_rehome_content_identifiers`, gr515805).
    """
    rows = conn.execute(
        "SELECT ref_id, pdf_sha256, pdf_pages, pdf_role, meta, "
        f"       {has_body_sql('r')} "
        "  FROM refs r WHERE ref_id = ANY(%s) FOR NO KEY UPDATE",
        ([survivor_ref_id, duplicate_ref_id],),
    ).fetchall()
    by_id = {int(r[0]): r for r in rows}
    sv, du = by_id.get(survivor_ref_id), by_id.get(duplicate_ref_id)
    if sv is None or du is None:
        return []
    move_body = bool(du[5]) and not sv[5]
    move_pdf = du[1] is not None and sv[1] is None
    moved: list[str] = []
    if not (move_body or move_pdf):
        return moved

    if move_pdf:
        conn.execute(
            "UPDATE refs SET pdf_sha256 = NULL, updated_at = now() WHERE ref_id = %s",
            (duplicate_ref_id,),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_pages = %s, "
            "pdf_role = COALESCE(%s, pdf_role), updated_at = now() "
            "WHERE ref_id = %s",
            (du[1], du[2], du[3], survivor_ref_id),
        )
        moved.append("pdf")

    if move_body:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "  section_path, page_first, page_last, token_count, meta, numerics) "
            "SELECT %s, set_by, ord, chunk_kind, text, section_path, "
            "       page_first, page_last, token_count, meta, numerics "
            "  FROM chunks WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL "
            " ORDER BY ord",
            (survivor_ref_id, duplicate_ref_id),
        )
        conn.execute(
            "DELETE FROM chunks WHERE ref_id = %s AND ord >= 0",
            (duplicate_ref_id,),
        )
        # The extraction-describing meta keys belong to the body they describe.
        from precis.ingest.db_writer import _BODY_OWNED_META_KEYS, _merge_ingest_meta

        dmeta = du[4] if isinstance(du[4], dict) else {}
        _merge_ingest_meta(
            survivor_ref_id,
            {k: v for k, v in dmeta.items() if k in _BODY_OWNED_META_KEYS},
            conn=conn,
        )
        moved.append("body")

    _rehome_content_identifiers(
        conn,
        src_ref_id=duplicate_ref_id,
        dst_ref_id=survivor_ref_id,
        dst_had_pdf=sv[1] is not None,
        dst_had_body=bool(sv[5]),
    )
    store.append_event(
        survivor_ref_id,
        source=source,
        event="body_moved_from_duplicate",
        payload={"duplicate_ref_id": duplicate_ref_id, "moved": moved},
        conn=conn,
    )
    return moved


@trust_door("computed")
def inherit_missing_identity(
    store: Store,
    *,
    survivor_ref_id: int,
    loser_ref_id: int,
    source: str,
    conn: Any,
) -> list[str]:
    """Copy the loser's title and authors onto the survivor where it has none.

    Fill-only, never overwrite: a blank survivor title takes the loser's
    non-blank title; a survivor with no ``paper_authors`` rows and an empty
    ``refs.authors`` takes the loser's byline. DOIs and other external ids
    already move with the identifier migration at both call sites. Call
    before the loser is retired (gr353777: a fetched PDF superseding a stub
    left the surviving ref titleless). Returns the field names filled.
    """
    s = conn.execute(
        "SELECT title, authors FROM refs WHERE ref_id = %s AND retired_at IS NULL",
        (survivor_ref_id,),
    ).fetchone()
    lo = conn.execute(
        "SELECT title, authors FROM refs WHERE ref_id = %s", (loser_ref_id,)
    ).fetchone()
    if s is None or lo is None:
        return []
    filled: list[str] = []
    lo_title = (lo[0] or "").strip()
    if not (s[0] or "").strip() and lo_title and not _title_is_junk(lo_title):
        # A stub titled by its own cite_key (e.g. "krishnan97") is a
        # placeholder, not a title.
        keys = {
            str(r[0]).strip().lower()
            for r in conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'cite_key'",
                (loser_ref_id,),
            )
        }
        if lo_title.lower() not in keys:
            store.chunks.set_ref_title(survivor_ref_id, lo[0], source=source, conn=conn)
            filled.append("title")
    if lo[1] and not s[1]:
        has_rows = conn.execute(
            "SELECT 1 FROM paper_authors WHERE ref_id = %s LIMIT 1",
            (survivor_ref_id,),
        ).fetchone()
        if has_rows is None:
            # Verbatim row copy (per-row source/orcid/verified_at); the
            # jsonb projection is the loser's, which was derived from them.
            copied = conn.execute(
                "INSERT INTO paper_authors (ref_id, position, given, middle, "
                "family, name_raw, orcid, openalex_author_id, person_ref_id, "
                "source, verified_at) "
                "SELECT %s, position, given, middle, family, name_raw, orcid, "
                "openalex_author_id, person_ref_id, source, verified_at "
                "FROM paper_authors WHERE ref_id = %s",
                (survivor_ref_id, loser_ref_id),
            ).rowcount
            if copied:
                conn.execute(
                    "UPDATE refs SET authors = (SELECT authors FROM refs "
                    "WHERE ref_id = %s), updated_at = now() WHERE ref_id = %s",
                    (loser_ref_id, survivor_ref_id),
                )
                filled.append("authors")
    return filled


def _cleared_mark(conn: Any, ref_id: int) -> dict[str, Any]:
    """The ref's ``meta.cleared_identifiers`` dict, ``{}`` when absent or
    malformed (gr515246)."""
    row = conn.execute(
        "SELECT meta->'cleared_identifiers' FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    return dict(row[0]) if row and isinstance(row[0], dict) else {}


def _mark_values(entry: Any) -> set[str] | None:
    """The recorded ``id_value`` set of one marker entry, or ``None`` for
    a legacy scheme-only mark (value unknowable — pre-dates the
    ``values`` field, gr515246)."""
    if not isinstance(entry, dict):
        return None
    vs = entry.get("values")
    if isinstance(vs, list):
        return {str(v) for v in vs}
    if entry.get("value"):
        return {str(entry["value"])}
    return None


def _drop_cleared_loser_rows(
    conn: Any, *, survivor_ref_id: int, loser_ref_id: int
) -> None:
    """Delete the loser's migratable-kind identifier rows whose value the
    survivor's operator already cleared — run BEFORE the identifier
    migration, so they can't ride across (gr515246). Per *value*: a
    loser's row the human never cleared isn't touched. Legacy
    scheme-only marks (value unknowable) still drop the whole scheme —
    resurrection is the worse error. DOIs compare case-insensitively:
    marker ``values`` are recorded post-normalisation (lowercase) but a
    legacy loser row can still carry the original mixed case."""
    cleared = _cleared_mark(conn, survivor_ref_id)
    for scheme, entry in cleared.items():
        if scheme not in _MIGRATABLE_ID_KINDS:
            continue
        values = _mark_values(entry)
        if values:
            conn.execute(
                "DELETE FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = %s "
                "  AND CASE WHEN id_kind = 'doi' "
                "           THEN lower(id_value) = ANY(%s) "
                "           ELSE id_value = ANY(%s) END",
                (loser_ref_id, scheme, list(values), list(values)),
            )
        elif values is None:
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = %s",
                (loser_ref_id, scheme),
            )


def _union_cleared_entry(own: Any, loser: Any) -> Any:
    """Merge the loser's marker entry into the survivor's own entry for
    the same scheme: keep the survivor's record (``by``/``at``), union
    the recorded ``values`` so neither cleared ``id_value`` is forgotten
    (gr515246)."""
    if not isinstance(own, dict) or not isinstance(loser, dict):
        return loser
    merged = dict(own)
    merged.pop("value", None)
    union = (_mark_values(own) or set()) | (_mark_values(loser) or set())
    if union:
        merged["values"] = sorted(union)
    return merged


def _merge_cleared_markers(
    conn: Any, *, survivor_ref_id: int, loser_ref_id: int
) -> None:
    """Carry the loser's ``meta.cleared_identifiers`` onto the survivor,
    per cleared *value* (gr515246).

    For each marked scheme:

    * the survivor holds no identifier row of that scheme → the mark
      unions across (the clear blocks re-derivation on the merged ref);
    * the survivor holds the very ``id_value`` the human cleared → that
      row is deleted (it may have just migrated off the loser) and the
      mark unions across;
    * the survivor holds a *different* value → it is the survivor's own
      identifier, not the cleared one: no union, no delete. Legacy
      scheme-only marks (no recorded ``values``) take this lane too —
      unverifiable, so nothing is dropped.

    Restricted to :data:`_MIGRATABLE_ID_KINDS`: content-derived schemes
      (``cite_key``/``paper_id``/…) never merge and their marks stay on
    the loser. Dropped rows also lose the ``meta.<scheme>`` display
    ghost (``doi``/``arxiv``, mirroring ``_stamp_cleared``).
    """
    loser_mark = _cleared_mark(conn, loser_ref_id)
    if not loser_mark:
        return
    held: dict[str, set[str]] = {}
    for kind, value in conn.execute(
        "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
        (survivor_ref_id,),
    ).fetchall():
        # doi compares lowercase: marker values are post-normalisation,
        # a legacy row can still be mixed-case (the DELETE below
        # matches with lower() the same way).
        v = str(value)
        held.setdefault(str(kind), set()).add(v.lower() if str(kind) == "doi" else v)
    surv_mark = _cleared_mark(conn, survivor_ref_id)
    changed = False
    drop: dict[str, set[str]] = {}
    for scheme, entry in loser_mark.items():
        if scheme not in _MIGRATABLE_ID_KINDS:
            continue
        own = held.get(scheme) or set()
        values = _mark_values(entry)
        if values is not None and scheme == "doi":
            values = {v.lower() for v in values}
        if not own:
            surv_mark[scheme] = _union_cleared_entry(surv_mark.get(scheme), entry)
            changed = True
        elif values is not None and values & own:
            # Only the matching rows go — other values of the same scheme
            # are the survivor's own.
            drop[scheme] = values & own
            surv_mark[scheme] = _union_cleared_entry(surv_mark.get(scheme), entry)
            changed = True
    for scheme, vals in drop.items():
        conn.execute(
            "DELETE FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = %s "
            "  AND CASE WHEN id_kind = 'doi' "
            "           THEN lower(id_value) = ANY(%s) "
            "           ELSE id_value = ANY(%s) END",
            (survivor_ref_id, scheme, list(vals), list(vals)),
        )
        if scheme in ("doi", "arxiv"):
            conn.execute(
                "UPDATE refs SET meta = meta - %s::text "
                "WHERE ref_id = %s AND kind = 'paper' "
                "AND meta ? %s::text",
                (scheme, survivor_ref_id, scheme),
            )
    if changed:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set("
            "  coalesce(meta, '{}'::jsonb), '{cleared_identifiers}', %s::jsonb) "
            "WHERE ref_id = %s",
            (Jsonb(surv_mark), survivor_ref_id),
        )


@trust_door("computed")
def merge_duplicate(
    store: Store,
    *,
    survivor_ref_id: int,
    duplicate_ref_id: int,
    source: str,
    reason: str,
    conn: Any,
) -> None:
    """Fold ``duplicate_ref_id`` into ``survivor_ref_id`` (must run in a tx).

    Migrates external identifiers + links to the survivor, records the
    ``supersedes`` edge + ``meta.superseded_by`` + audit events, and
    soft-deletes the duplicate. When the survivor lacks a body and/or PDF
    that the duplicate holds, those move too
    (:func:`_move_body_to_survivor`, gr479362); a survivor that already has
    them keeps them and the duplicate's stay behind. Mirrors
    :func:`precis.ingest.add._reconcile_orphan_stub`.
    """
    if survivor_ref_id == duplicate_ref_id:
        raise ValueError("merge_duplicate: survivor and duplicate are the same ref")

    # Lock both refs rows first, in id order: set_ref_identifier and
    # clear_ref_identifier take refs before ref_identifiers, and this
    # fold writes ref_identifiers before the marker union writes refs —
    # without a common first lock the two orders deadlock (gr515246).
    conn.execute(
        "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
        "ORDER BY ref_id FOR NO KEY UPDATE",
        ([survivor_ref_id, duplicate_ref_id],),
    )

    # (a) External identifiers → survivor. First drop any identifier the
    # survivor *already* holds, so the move below can't hit the
    # (id_kind, id_value) PK. DOIs are compared case-insensitively: the 0049
    # BEFORE-UPDATE trigger lowercases id_value on write, so moving a stub's
    # mixed-case `.../D19-1371` onto a survivor that already owns the lowercase
    # `.../d19-1371` would collide — that is exactly the reconcile-doi-case
    # duplicate pair. Drop the redundant row, then migrate the rest.
    conn.execute(
        "DELETE FROM ref_identifiers d USING ref_identifiers s "
        " WHERE d.ref_id = %s AND s.ref_id = %s "
        "   AND d.id_kind = s.id_kind AND d.id_kind = ANY(%s) "
        "   AND CASE WHEN d.id_kind = 'doi' "
        "            THEN lower(d.id_value) = lower(s.id_value) "
        "            ELSE d.id_value = s.id_value END",
        (duplicate_ref_id, survivor_ref_id, list(_MIGRATABLE_ID_KINDS)),
    )
    # A value the survivor's operator deliberately cleared must not ride
    # across on the duplicate's rows — the move below would resurrect the
    # very identifier the human removed (gr515246).
    _drop_cleared_loser_rows(
        conn, survivor_ref_id=survivor_ref_id, loser_ref_id=duplicate_ref_id
    )
    conn.execute(
        "UPDATE ref_identifiers SET ref_id = %s "
        "WHERE ref_id = %s AND id_kind = ANY(%s)",
        (survivor_ref_id, duplicate_ref_id, list(_MIGRATABLE_ID_KINDS)),
    )
    _merge_cleared_markers(
        conn, survivor_ref_id=survivor_ref_id, loser_ref_id=duplicate_ref_id
    )
    # (a2) Body / PDF → a survivor that lacks them (gr479362).
    moved = _move_body_to_survivor(
        store,
        survivor_ref_id=survivor_ref_id,
        duplicate_ref_id=duplicate_ref_id,
        source=source,
        conn=conn,
    )
    # (b) Graph edges → survivor (self-loops + dup rows dropped internally).
    store.migrate_links(duplicate_ref_id, survivor_ref_id, conn=conn)
    # (b2) Repoint bib entries that already resolved to the loser — else
    # they keep citing a soft-deleted ref forever (nothing else re-checks
    # a bib entry once match_conf is set; see resolve.py's retired_at
    # guard for the belt-and-suspenders side of this).
    conn.execute(
        "UPDATE paper_bib_entries SET held_ref_id = %s WHERE held_ref_id = %s",
        (survivor_ref_id, duplicate_ref_id),
    )
    # (b3) Fill what the survivor lacks (title/authors) from the loser.
    inherit_missing_identity(
        store,
        survivor_ref_id=survivor_ref_id,
        loser_ref_id=duplicate_ref_id,
        source=source,
        conn=conn,
    )
    # (c) supersedes edge + provenance on the loser.
    store.add_link(
        src_ref_id=survivor_ref_id,
        dst_ref_id=duplicate_ref_id,
        relation="supersedes",
        set_by="system",
        conn=conn,
    )
    store.stamp_ref_meta(
        duplicate_ref_id,
        {"superseded_by": survivor_ref_id, "dedup": reason},
        conn=conn,
    )
    # (d) soft-delete + audit both sides.
    store.retire_ref(duplicate_ref_id, conn=conn)
    store.append_event(
        survivor_ref_id,
        source=source,
        event="duplicate_merged",
        payload={
            "duplicate_ref_id": duplicate_ref_id,
            "reason": reason,
            **({"moved": moved} if moved else {}),
        },
        conn=conn,
    )
    store.append_event(
        duplicate_ref_id,
        source=source,
        event="soft_deleted_duplicate",
        payload={"duplicate_of": survivor_ref_id, "reason": reason},
        conn=conn,
    )
    log.info(
        "dedup: merged duplicate ref_id=%s into survivor ref_id=%s (%s)",
        duplicate_ref_id,
        survivor_ref_id,
        reason,
    )


# ---------------------------------------------------------------------------
# Survivor selection
# ---------------------------------------------------------------------------


@dataclass
class _Cand:
    ref_id: int
    title: str
    n_authors: int
    has_ext_id: bool
    has_pdf: bool = False
    #: Has extracted text (a body chunk at ``ord >= 0``) — distinct from
    #: ``has_pdf``: a promoted ref can hold bytes and no text (gr453860).
    has_body: bool = False


def _title_is_junk(title: str | None) -> bool:
    t = (title or "").strip()
    return not t or is_pii(t) or is_garbage_title(t)


def pick_survivor(cands: list[_Cand]) -> int:
    """Pick the survivor of a duplicate group.

    Priority (best first): has an external id (DOI/arXiv/…) → non-junk
    title → most authors → lowest ref_id. Never "lowest id alone" — that
    was the deleted ``dedupe-papers`` bug that would keep junk over the
    canonical.
    """
    return min(
        cands,
        key=lambda c: (
            not c.has_ext_id,  # False (has id) sorts first
            _title_is_junk(c.title),  # False (good title) sorts first
            -c.n_authors,  # more authors first
            c.ref_id,  # stable tiebreak
        ),
    ).ref_id


# ---------------------------------------------------------------------------
# Phase 2 — standing reconciliation sweep
# ---------------------------------------------------------------------------


@dataclass
class ReconcileOutcome:
    survivor_ref_id: int
    duplicate_ref_ids: list[int]
    key: str  # the shared value (pdf_sha256 / lowercase DOI) the group collapsed on
    key_label: str = "pdf_sha256"  # what `key` names, for display
    #: Set when the group was left live for human review instead of
    #: merged — the corroboration gate refused it (gr501446).
    skipped: str | None = None

    def line(self) -> str:
        dups = ", ".join(f"#{d}" for d in self.duplicate_ref_ids)
        if self.skipped is not None:
            return (
                f"SKIP   #{self.survivor_ref_id} vs {dups} "
                f"({self.key_label} {self.key[:24]}…; {self.skipped})"
            )
        return (
            f"MERGE  survivor #{self.survivor_ref_id} <- {dups} "
            f"({self.key_label} {self.key[:24]}…)"
        )


_GROUPS_SQL = """
    SELECT pdf_sha256, array_agg(ref_id ORDER BY ref_id) AS ids
      FROM refs
     WHERE kind = 'paper' AND retired_at IS NULL AND pdf_sha256 IS NOT NULL
     GROUP BY pdf_sha256
    HAVING count(*) > 1
     ORDER BY pdf_sha256
"""


def _candidates(store: Store, conn: Any, ref_ids: list[int]) -> list[_Cand]:
    """Build survivor-rule candidates for a group of ref_ids."""
    rows = conn.execute(
        "SELECT r.ref_id, r.title, "
        "       jsonb_array_length(COALESCE(r.authors, '[]'::jsonb)) AS n_auth, "
        "       EXISTS (SELECT 1 FROM ref_identifiers ri "
        "               WHERE ri.ref_id = r.ref_id "
        "                 AND ri.id_kind = ANY(%s)) AS has_id, "
        "       r.pdf_sha256 IS NOT NULL AS has_pdf, "
        f"       {has_body_sql('r')} AS has_body "
        "FROM refs r WHERE r.ref_id = ANY(%s)",
        (list(_MIGRATABLE_ID_KINDS), ref_ids),
    ).fetchall()
    return [
        _Cand(
            ref_id=int(r[0]),
            title=r[1] or "",
            n_authors=int(r[2]),
            has_ext_id=r[3],
            has_pdf=r[4],
            has_body=r[5],
        )
        for r in rows
    ]


def reconcile_by_pdf_sha256(
    store: Store,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    source: str = "reconcile",
) -> list[ReconcileOutcome]:
    """Collapse live paper refs that share a ``pdf_sha256`` (same file).

    Picks the survivor per :func:`pick_survivor` and merges the rest in.
    Dry-run (default) reports the planned merges without writing. Returns
    one :class:`ReconcileOutcome` per duplicate group acted on.
    """
    with store.pool.connection() as conn:
        groups = conn.execute(_GROUPS_SQL).fetchall()
    if limit:
        groups = groups[:limit]

    outcomes: list[ReconcileOutcome] = []
    for sha, ids in groups:
        ref_ids = [int(i) for i in ids]
        with store.pool.connection() as conn:
            cands = _candidates(store, conn, ref_ids)
        if len(cands) < 2:
            continue
        survivor = pick_survivor(cands)
        dups = [c.ref_id for c in cands if c.ref_id != survivor]
        if dry_run:
            outcomes.append(ReconcileOutcome(survivor, dups, sha))
            continue
        try:
            with store.tx() as conn:
                for dup in dups:
                    merge_duplicate(
                        store,
                        survivor_ref_id=survivor,
                        duplicate_ref_id=dup,
                        source=source,
                        reason="reconcile-pdf-sha256",
                        conn=conn,
                    )
            outcomes.append(ReconcileOutcome(survivor, dups, sha))
        except Exception:
            log.exception("reconcile: group %s failed", sha[:12])
    return outcomes


# ---------------------------------------------------------------------------
# DOI-case reconciliation — collapse refs that share a DOI modulo case
# ---------------------------------------------------------------------------

#: Live paper refs whose DOI is the *same* modulo case as another live paper's.
#: These are the stub↔ingested-paper duplicates that the exact-match stub
#: upgrade missed because one side stored a publisher-cased DOI. Grouped on the
#: lowercase form so `.../d19-1371` and `.../D19-1371` land together.
_DOI_CASE_GROUPS_SQL = """
    SELECT lower(ri.id_value) AS ldoi,
           array_agg(DISTINCT ri.ref_id ORDER BY ri.ref_id) AS ids
      FROM ref_identifiers ri
      JOIN refs r ON r.ref_id = ri.ref_id
     WHERE ri.id_kind = 'doi'
       AND r.kind = 'paper'
       AND r.retired_at IS NULL
     GROUP BY lower(ri.id_value)
    HAVING count(DISTINCT ri.ref_id) > 1
     ORDER BY lower(ri.id_value)
"""


def pick_survivor_keep_chunks(cands: list[_Cand]) -> int:
    """Survivor selection that keeps the ingested (chunked) copy.

    A DOI-case duplicate group is almost always one PDF-less stub plus one
    fully-ingested paper. The stub carries the DOI too, so :func:`pick_survivor`
    (which weighs external-id presence first) can't tell them apart — and its
    author/id tiebreaks could keep the empty stub. Here the copy that actually
    holds the *text* wins: restrict to the refs with body chunks when any has
    them (a markup-ingested ref with no PDF outranks a bodiless PDF-carrying
    one — gr453860), else to the refs with a PDF (the bytes are still worth
    keeping), then fall back to the ordinary survivor rule within that set
    (or the whole group if none has either — both are stubs, either is fine).
    """
    body_bearing = [c for c in cands if c.has_body]
    pdf_bearing = [c for c in cands if c.has_pdf]
    return pick_survivor(body_bearing or pdf_bearing or cands)


def _normalize_doi_rows(conn: Any) -> int:
    """Lowercase every stored DOI in ``ref_identifiers``; return rows changed.

    Two steps, both collision-safe:

    1. Within a single ref, drop a mixed-case DOI row when its lowercase twin
       already sits on the same ref — this is the leftover after a merge moved
       the stub's lowercase DOI onto a survivor that kept its own upper-case
       one.
    2. Lowercase the remaining lone mixed-case rows, but skip any whose
       lowercase target is already owned by a *different* live ref (an
       un-merged group, e.g. under a ``--limit`` run) — those wait for a full
       reconcile pass rather than raising a PK violation.
    """
    conn.execute(
        "DELETE FROM ref_identifiers a "
        " USING ref_identifiers b "
        " WHERE a.id_kind = 'doi' AND b.id_kind = 'doi' "
        "   AND a.ref_id = b.ref_id "
        "   AND a.id_value <> lower(a.id_value) "
        "   AND b.id_value = lower(a.id_value)"
    )
    cur = conn.execute(
        "UPDATE ref_identifiers r "
        "   SET id_value = lower(r.id_value) "
        " WHERE r.id_kind = 'doi' AND r.id_value <> lower(r.id_value) "
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM ref_identifiers t "
        "        WHERE t.id_kind = 'doi' "
        "          AND t.id_value = lower(r.id_value) "
        "          AND t.ref_id <> r.ref_id)"
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _maybe_validate_doi_constraint(conn: Any) -> bool:
    """Promote the ``ref_identifiers_doi_lc`` CHECK from NOT VALID once clean.

    Migration 0049 added the constraint ``NOT VALID`` because the legacy
    mixed-case DOI rows would fail an immediate validation. Once a reconcile
    pass has lowercased every stored DOI, the constraint can be validated so it
    is enforced for existing rows too — turning the manual post-deploy step into
    something the nightly sweep finishes itself. Idempotent: skips if the
    constraint is already validated (or absent) or if any mixed-case DOI still
    remains (e.g. an un-merged group left by a ``--limit`` run). Returns True
    only when it actually validated.
    """
    row = conn.execute(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ref_identifiers_doi_lc'"
    ).fetchone()
    if row is None or row[0]:
        return False
    remaining = conn.execute(
        "SELECT 1 FROM ref_identifiers "
        "WHERE id_kind = 'doi' AND id_value <> lower(id_value) LIMIT 1"
    ).fetchone()
    if remaining is not None:
        return False
    conn.execute(
        "ALTER TABLE ref_identifiers VALIDATE CONSTRAINT ref_identifiers_doi_lc"
    )
    log.info("reconcile: validated ref_identifiers_doi_lc constraint")
    return True


def _doi_case_corroborated(conn: Any, a_id: int, b_id: int) -> bool:
    """True when a DOI-case pair still looks like one work, not just one DOI.

    The group key is only a DOI match modulo case — a typo'd or
    publisher-reused DOI groups unrelated papers (gr501446: pa42498 was
    merged into pa44470 — same year, entirely different titles). Require
    the same corroboration the other merge paths demand before folding:
    trigram title similarity at :data:`_TITLE_AUTO_SIM`, or an agreeing
    first-author family name with years within ±1. Missing evidence (a
    blank/junk title, a NULL year, no authors) fails closed — the group is
    left live and flagged for review rather than merged on a maybe.
    """
    rows = conn.execute(
        "SELECT ref_id, title, year, authors FROM refs WHERE ref_id = ANY(%s)",
        ([a_id, b_id],),
    ).fetchall()
    by_id = {int(r[0]): r for r in rows}
    a, b = by_id.get(a_id), by_id.get(b_id)
    if a is None or b is None:
        return False
    ta, tb = (a[1] or "").strip(), (b[1] or "").strip()
    if ta and tb and not (_title_is_junk(ta) or _title_is_junk(tb)):
        row = conn.execute("SELECT similarity(%s, %s)", (ta, tb)).fetchone()
        if row and row[0] is not None and float(row[0]) >= _TITLE_AUTO_SIM:
            return True
    fa, fb = _first_family(a[3]), _first_family(b[3])
    if fa and fa == fb and a[2] is not None and b[2] is not None:
        return abs(int(a[2]) - int(b[2])) <= 1
    return False


_DOI_CASE_REVIEW_SOURCE = "reconcile:doi-case-review"
_DOI_CASE_REVIEW_TAG = "paper-doi-case-review"


def _raise_doi_case_review(
    store: Store, *, ref_ids: list[int], ldoi: str
) -> int | None:
    """File a ``STATUS:open`` review todo for a skipped DOI-case group.

    Deduped on ``(source, doi)`` like ``raise_junk_title_papers``: an open
    (or human-closed non-``done``) todo already covering the group is not
    re-filed, so the nightly sweep doesn't re-raise it every pass. Returns
    the todo ref_id, or ``None`` when one already stands.
    """
    from precis.config import load_config
    from precis.utils.handle_registry import format_handle

    with store.tx() as conn:
        standing = conn.execute(
            "SELECT 1 FROM refs t "
            "WHERE t.kind = 'todo' AND t.retired_at IS NULL "
            "  AND t.meta->>'source' = %s AND t.meta->>'doi' = %s "
            "  AND NOT EXISTS (SELECT 1 FROM ref_tags rt "
            "    JOIN tags tg USING (tag_id) "
            "    WHERE rt.ref_id = t.ref_id "
            "      AND tg.namespace = 'STATUS' AND tg.value = 'done')",
            (_DOI_CASE_REVIEW_SOURCE, ldoi),
        ).fetchone()
        if standing is not None:
            return None
        handles = ", ".join(format_handle("paper", i) for i in ref_ids)
        todo_ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=(
                f"Review DOI-case duplicate group on {ldoi}: {handles} — "
                "the refs share a DOI modulo case but title/authors do not "
                "corroborate them as one work (a DOI typo or publisher "
                "reuse). Merge by hand only if they really are the same "
                "paper (gr501446)."
            ),
            meta={
                "source": _DOI_CASE_REVIEW_SOURCE,
                "doi": ldoi,
                "ref_ids": sorted(ref_ids),
            },
            conn=conn,
        )
        store.add_tag(
            todo_ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for tag in (_DOI_CASE_REVIEW_TAG, f"waiting-for:{load_config().owner}"):
            store.add_tag(
                todo_ref.id, Tag.parse_strict(tag), set_by="system", conn=conn
            )
    return todo_ref.id


def reconcile_by_doi_case(
    store: Store,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    source: str = "reconcile",
) -> list[ReconcileOutcome]:
    """Collapse live paper refs that share a DOI modulo case.

    Keeps the ingested (chunked) copy per :func:`pick_survivor_keep_chunks` and
    merges the PDF-less stub(s) into it, then canonicalises every stored DOI to
    lowercase (:func:`_normalize_doi_rows`) so the surviving rows satisfy the
    ``ref_identifiers_doi_lc`` invariant. A group whose members fail the
    corroboration gate (:func:`_doi_case_corroborated`, gr501446) is skipped —
    recorded as a skipped outcome and, outside dry-run, filed as a review
    todo — never merged on the DOI alone. Dry-run (default) reports the
    planned merges without writing. Returns one :class:`ReconcileOutcome`
    per group.
    """
    with store.pool.connection() as conn:
        groups = conn.execute(_DOI_CASE_GROUPS_SQL).fetchall()
    if limit:
        groups = groups[:limit]

    outcomes: list[ReconcileOutcome] = []
    for ldoi, ids in groups:
        ref_ids = [int(i) for i in ids]
        with store.pool.connection() as conn:
            cands = _candidates(store, conn, ref_ids)
        if len(cands) < 2:
            continue
        survivor = pick_survivor_keep_chunks(cands)
        dups = [c.ref_id for c in cands if c.ref_id != survivor]
        with store.pool.connection() as conn:
            uncorroborated = [
                d for d in dups if not _doi_case_corroborated(conn, survivor, d)
            ]
        if uncorroborated:
            outcomes.append(
                ReconcileOutcome(
                    survivor,
                    dups,
                    ldoi,
                    key_label="doi",
                    skipped=f"uncorroborated: {uncorroborated}",
                )
            )
            if not dry_run:
                _raise_doi_case_review(
                    store, ref_ids=[survivor, *uncorroborated], ldoi=ldoi
                )
            continue
        if dry_run:
            outcomes.append(ReconcileOutcome(survivor, dups, ldoi, key_label="doi"))
            continue
        try:
            with store.tx() as conn:
                for dup in dups:
                    merge_duplicate(
                        store,
                        survivor_ref_id=survivor,
                        duplicate_ref_id=dup,
                        source=source,
                        reason="reconcile-doi-case",
                        conn=conn,
                    )
                _normalize_doi_rows(conn)
            outcomes.append(ReconcileOutcome(survivor, dups, ldoi, key_label="doi"))
        except Exception:
            log.exception("reconcile: doi group %s failed", ldoi[:24])

    # Lone mixed-case DOIs (no duplicate twin) still need lowercasing so the
    # constraint can be validated. Safe and idempotent; runs once per pass.
    if not dry_run:
        try:
            with store.tx() as conn:
                _normalize_doi_rows(conn)
        except Exception:
            log.exception("reconcile: doi-case normalize sweep failed")
        # Once every DOI is lowercase, promote the NOT-VALID guard so it is
        # enforced retroactively — the nightly sweep finishes the job itself.
        try:
            with store.tx() as conn:
                _maybe_validate_doi_constraint(conn)
        except Exception:
            log.exception("reconcile: doi-case constraint validate failed")
    return outcomes


# ---------------------------------------------------------------------------
# Title-similarity reconciliation — the Phase 3 near-duplicate case
# ---------------------------------------------------------------------------
#
# A title-only stub (no DOI/arXiv/S2 to collapse on) minted for a paper we
# already hold slips past every identifier-based path above — its
# title-derived cite_key can't collide with the held paper's author key.
# Only a fuzzy title match catches it.
#
# The narrow-gate principle (it has to be TRUE, not just tidy): we only
# retire a stub when the survivor is a paper we *demonstrably have* — a
# pdf_sha256 AND real body chunks (a bare held-flag on a never-ingested ref
# is not enough) — and the title/year match is high-confidence. This pass
# only touches **id-less** stubs: a stub carrying its own DOI/arXiv/S2
# asserts it is a distinct work, so a mere title match is not proof of
# sameness (preprint vs published, erratum, version, namesake). Those are
# left to the identifier-proven paths (reconcile_by_doi_case / pdf) or
# surfaced for human review — never auto-merged on title alone.

#: Auto-merge floor: trigram title similarity at/above this (with a
#: compatible year) folds the stub into the held paper unattended.
_TITLE_AUTO_SIM = 0.85
#: Review floor: matches in ``[_TITLE_REVIEW_SIM, _TITLE_AUTO_SIM)`` — or
#: high-similarity matches whose years disagree — are surfaced, never
#: auto-merged.
_TITLE_REVIEW_SIM = 0.6

#: Live, id-less, PDF-less paper stubs — the only refs this pass may retire.
#: (A stub carrying any external id is reconcile-able by the DOI/pdf paths
#: and chaseable by fetch_oa, so it's out of scope here.)
_TITLE_STUB_SQL = """
    SELECT r.ref_id, r.title, r.year, r.authors
      FROM refs r
     WHERE r.kind = 'paper' AND r.retired_at IS NULL
       AND r.pdf_sha256 IS NULL AND r.title IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM ref_identifiers ri
              WHERE ri.ref_id = r.ref_id AND ri.id_kind = ANY(%s))
     ORDER BY r.ref_id
"""


@dataclass
class TitleMatchReview:
    """A stub↔held fuzzy-title pair in the review band (not auto-merged)."""

    stub_ref_id: int
    held_ref_id: int
    sim: float
    stub_title: str
    # 'low-similarity' | title_merge_refusal reason ('short-title',
    # 'year-unknown', 'year-mismatch', 'author-mismatch')
    reason: str

    def line(self) -> str:
        return (
            f"REVIEW stub #{self.stub_ref_id} ~ held #{self.held_ref_id} "
            f"(sim {self.sim:.2f}, {self.reason}): {self.stub_title[:48]}"
        )


def reconcile_by_title_similarity(
    store: Store,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    source: str = "reconcile",
    review_out: list[TitleMatchReview] | None = None,
) -> list[ReconcileOutcome]:
    """Fold id-less title-only stubs into the held paper they duplicate.

    For each live, PDF-less, identifier-less paper stub, find the best
    trigram-title match among *held* papers. A match at/above
    :data:`_TITLE_AUTO_SIM` that also passes :func:`title_merge_refusal`
    (>=5 title tokens, matching known years, agreeing first author)
    auto-merges (the held copy is always the survivor). Matches in the
    review band — lower similarity, or high similarity the corroboration
    gate refuses — are appended
    to ``review_out`` (when provided) and never merged. Dry-run (default)
    plans the merges without writing. Returns one :class:`ReconcileOutcome`
    per auto-merged stub.
    """
    with store.pool.connection() as conn:
        stubs = conn.execute(_TITLE_STUB_SQL, (list(_MIGRATABLE_ID_KINDS),)).fetchall()
    if limit:
        stubs = stubs[:limit]

    outcomes: list[ReconcileOutcome] = []
    for stub_id, stub_title, stub_year, stub_authors in stubs:
        stub_id = int(stub_id)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.ref_id, r.year, similarity(r.title, %s) AS sim, "
                "       r.title, r.authors "
                "FROM refs r "
                "WHERE r.kind = 'paper' AND r.retired_at IS NULL "
                "  AND r.pdf_sha256 IS NOT NULL AND r.title IS NOT NULL "
                "  AND similarity(r.title, %s) >= %s "
                # Survivor must be a *truly ingested* copy — a pdf_sha256
                # AND body chunks — not a bare held-flag. We never retire a
                # stub in favour of a paper we can't show we actually have.
                f"  AND {has_body_sql('r')} "
                "ORDER BY sim DESC, r.ref_id ASC LIMIT 1",
                (stub_title, stub_title, _TITLE_REVIEW_SIM),
            ).fetchone()
        if row is None:
            continue
        held_id, held_year, sim = int(row[0]), row[1], float(row[2])
        held_title, held_authors = row[3], row[4]
        if held_id == stub_id:  # defensive; a stub has no PDF so can't self-match
            continue
        refusal = title_merge_refusal(
            stub_title, stub_year, stub_authors, held_title, held_year, held_authors
        )
        if sim >= _TITLE_AUTO_SIM and refusal is None:
            if dry_run:
                outcomes.append(
                    ReconcileOutcome(held_id, [stub_id], stub_title[:24], "title")
                )
                continue
            try:
                with store.tx() as conn:
                    merge_duplicate(
                        store,
                        survivor_ref_id=held_id,
                        duplicate_ref_id=stub_id,
                        source=source,
                        reason="reconcile-title",
                        conn=conn,
                    )
                outcomes.append(
                    ReconcileOutcome(held_id, [stub_id], stub_title[:24], "title")
                )
            except Exception:
                log.exception("reconcile: title stub #%s failed", stub_id)
        elif review_out is not None:
            review_out.append(
                TitleMatchReview(
                    stub_ref_id=stub_id,
                    held_ref_id=held_id,
                    sim=sim,
                    stub_title=stub_title or "",
                    reason=refusal if refusal is not None else "low-similarity",
                )
            )
    return outcomes


__all__ = [
    "ReconcileOutcome",
    "TitleMatchReview",
    "merge_duplicate",
    "pick_survivor",
    "pick_survivor_keep_chunks",
    "reconcile_by_doi_case",
    "reconcile_by_pdf_sha256",
    "reconcile_by_title_similarity",
    "title_merge_refusal",
]
