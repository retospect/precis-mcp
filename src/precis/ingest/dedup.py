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
    has_pdf: bool = False


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

    def line(self) -> str:
        dups = ", ".join(f"#{d}" for d in self.duplicate_ref_ids)
        return (
            f"MERGE  survivor #{self.survivor_ref_id} <- {dups} "
            f"({self.key_label} {self.key[:24]}…)"
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
        "                 AND ri.id_kind = ANY(%s)) AS has_id, "
        "       r.pdf_sha256 IS NOT NULL AS has_pdf "
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
       AND r.deleted_at IS NULL
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
    holds the bytes wins: restrict to the refs with a PDF when any has one, then
    fall back to the ordinary survivor rule within that set (or the whole group
    if none has a PDF yet — both are stubs, either is fine).
    """
    pdf_bearing = [c for c in cands if c.has_pdf]
    return pick_survivor(pdf_bearing or cands)


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
    ``ref_identifiers_doi_lc`` invariant. Dry-run (default) reports the planned
    merges without writing. Returns one :class:`ReconcileOutcome` per group.
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
# See docs/design/duplicate-paper-handling.md (Phase 3).

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
    SELECT r.ref_id, r.title, r.year
      FROM refs r
     WHERE r.kind = 'paper' AND r.deleted_at IS NULL
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
    reason: str  # 'low-similarity' | 'year-mismatch'

    def line(self) -> str:
        return (
            f"REVIEW stub #{self.stub_ref_id} ~ held #{self.held_ref_id} "
            f"(sim {self.sim:.2f}, {self.reason}): {self.stub_title[:48]}"
        )


def _years_compatible(stub_year: int | None, held_year: int | None) -> bool:
    """A preprint and its published version can differ by a year; allow ±1.
    Unknown on either side isn't evidence of a mismatch, so it passes."""
    if stub_year is None or held_year is None:
        return True
    return abs(int(stub_year) - int(held_year)) <= 1


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
    :data:`_TITLE_AUTO_SIM` with a compatible year auto-merges (the held
    copy is always the survivor). Matches in the review band — lower
    similarity, or high similarity with disagreeing years — are appended
    to ``review_out`` (when provided) and never merged. Dry-run (default)
    plans the merges without writing. Returns one :class:`ReconcileOutcome`
    per auto-merged stub.
    """
    with store.pool.connection() as conn:
        stubs = conn.execute(_TITLE_STUB_SQL, (list(_MIGRATABLE_ID_KINDS),)).fetchall()
    if limit:
        stubs = stubs[:limit]

    outcomes: list[ReconcileOutcome] = []
    for stub_id, stub_title, stub_year in stubs:
        stub_id = int(stub_id)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.ref_id, r.year, similarity(r.title, %s) AS sim "
                "FROM refs r "
                "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
                "  AND r.pdf_sha256 IS NOT NULL AND r.title IS NOT NULL "
                "  AND similarity(r.title, %s) >= %s "
                # Survivor must be a *truly ingested* copy — a pdf_sha256
                # AND body chunks — not a bare held-flag. We never retire a
                # stub in favour of a paper we can't show we actually have.
                "  AND EXISTS (SELECT 1 FROM chunks ck "
                "              WHERE ck.ref_id = r.ref_id AND ck.ord >= 0) "
                "ORDER BY sim DESC, r.ref_id ASC LIMIT 1",
                (stub_title, stub_title, _TITLE_REVIEW_SIM),
            ).fetchone()
        if row is None:
            continue
        held_id, held_year, sim = int(row[0]), row[1], float(row[2])
        if held_id == stub_id:  # defensive; a stub has no PDF so can't self-match
            continue
        years_ok = _years_compatible(stub_year, held_year)
        if sim >= _TITLE_AUTO_SIM and years_ok:
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
                    reason="year-mismatch" if not years_ok else "low-similarity",
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
]
