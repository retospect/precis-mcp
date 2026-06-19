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

See ``docs/design/duplicate-paper-handling.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
from precis.store import Store

log = logging.getLogger(__name__)

#: External identifier kinds that belong to the *paper*, so they move to
#: the survivor on a merge. Content-derived ids (cite_key, paper_id,
#: pub_id, pdf_sha256, content_hash) stay on the retired ref — they
#: describe *that* artifact, and leaving them avoids a unique-constraint
#: fight with the survivor's own content ids.
_MIGRATABLE_ID_KINDS = ("doi", "arxiv", "s2", "pubmed", "openalex", "mag", "dblp")


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
    soft-deletes the duplicate. Mirrors
    :func:`precis.ingest.add._reconcile_orphan_stub`.
    """
    if survivor_ref_id == duplicate_ref_id:
        raise ValueError("merge_duplicate: survivor and duplicate are the same ref")

    # (a) External identifiers → survivor. Each value is globally unique
    # per id_kind (PK on (id_kind, id_value)), so this never collides with
    # the survivor's own rows.
    conn.execute(
        "UPDATE ref_identifiers SET ref_id = %s "
        "WHERE ref_id = %s AND id_kind = ANY(%s)",
        (survivor_ref_id, duplicate_ref_id, list(_MIGRATABLE_ID_KINDS)),
    )
    # (b) Graph edges → survivor (self-loops + dup rows dropped internally).
    store.migrate_links(duplicate_ref_id, survivor_ref_id, conn=conn)
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
    store.soft_delete_ref(duplicate_ref_id, conn=conn)
    store.append_event(
        survivor_ref_id,
        source=source,
        event="duplicate_merged",
        payload={"duplicate_ref_id": duplicate_ref_id, "reason": reason},
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
    key: str  # the shared pdf_sha256 (truncated) the group collapsed on

    def line(self) -> str:
        dups = ", ".join(f"#{d}" for d in self.duplicate_ref_ids)
        return (
            f"MERGE  survivor #{self.survivor_ref_id} <- {dups} "
            f"(pdf_sha256 {self.key[:12]}…)"
        )


_GROUPS_SQL = """
    SELECT pdf_sha256, array_agg(ref_id ORDER BY ref_id) AS ids
      FROM refs
     WHERE kind = 'paper' AND deleted_at IS NULL AND pdf_sha256 IS NOT NULL
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
        "                 AND ri.id_kind = ANY(%s)) AS has_id "
        "FROM refs r WHERE r.ref_id = ANY(%s)",
        (list(_MIGRATABLE_ID_KINDS), ref_ids),
    ).fetchall()
    return [
        _Cand(ref_id=int(r[0]), title=r[1] or "", n_authors=int(r[2]), has_ext_id=r[3])
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


__all__ = [
    "ReconcileOutcome",
    "merge_duplicate",
    "pick_survivor",
    "reconcile_by_pdf_sha256",
]
