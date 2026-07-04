"""Deterministic paper-hygiene heals — the low-crud, self-healing sweeps.

These are the belt to the dedup braces: pure DB repairs with no judgment
and no network, safe to run unattended on a cadence (the ``paper_reconcile``
worker pass drives them). Each fixes a class of *legacy residue* left by
ingestion/edit bugs that the current code no longer produces:

* :func:`heal_drifted_cards` — a paper whose title was repaired but whose
  embedded ``card_combined`` search chunk was never rebuilt, so search
  still matches the old junk text. (All *current* write paths call
  ``rewrite_cards``; this heals history and makes the drift transient even
  if some future path forgets.)
* :func:`collapse_superseded_chains` — a retired ref whose
  ``meta.superseded_by`` points at *another* retired ref instead of the
  final live survivor (the "stub points at a stub" dereference chain).
* :func:`migrate_dangling_paper_links` — a non-``supersedes`` graph edge
  still pointing at a soft-deleted paper; repoint it to the survivor.

All are dry-run by default and idempotent: a clean corpus yields empty
results and the next pass is a cheap no-op.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.ingest.cards import rewrite_cards
from precis.store import Store

log = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(s: str | None) -> str:
    """Lowercase + strip non-alphanumerics — punctuation/markup-insensitive."""
    return _NON_ALNUM.sub("", (s or "").lower())


# ---------------------------------------------------------------------------
# Card drift
# ---------------------------------------------------------------------------


def heal_drifted_cards(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Rebuild ``card_combined`` chunks whose text lost the current title.

    A cheap SQL prefilter finds papers whose ``card_combined`` doesn't
    contain the title's first 25 chars; each candidate is then verified in
    Python against a punctuation-insensitive match (so an en-dash / markup
    difference is *not* treated as drift) before ``rewrite_cards`` rebuilds
    it from the live metadata. Returns the healed ``ref_id``s.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id "
            "FROM refs r "
            "JOIN chunks c ON c.ref_id = r.ref_id "
            "               AND c.chunk_kind = 'card_combined' "
            "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
            "  AND r.title IS NOT NULL AND btrim(r.title) <> '' "
            "  AND position(lower(left(r.title, 25)) in lower(c.text)) = 0 "
            "ORDER BY r.ref_id"
        ).fetchall()
    candidates = [int(r[0]) for r in rows]
    if limit:
        candidates = candidates[:limit]

    healed: list[int] = []
    for rid in candidates:
        ref = store.fetch_refs_by_ids([rid]).get(rid)
        if ref is None or not ref.title:
            continue
        title = ref.title
        author_names = [a.get("name", "") for a in (ref.authors or []) if a.get("name")]
        meta = ref.meta or {}
        abstract = meta.get("abstract", "")
        abstract = abstract if isinstance(abstract, str) else ""
        kw_raw = meta.get("keywords", [])
        keywords = list(kw_raw) if isinstance(kw_raw, list) else []

        with store.pool.connection() as conn:
            got = conn.execute(
                "SELECT text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'card_combined' LIMIT 1",
                (rid,),
            ).fetchone()
        if got is None:
            continue
        tnorm = _norm(title)[:60]
        # Genuine drift only: the current title isn't in the card even after
        # normalising punctuation/markup away — so the card carries a
        # different (stale) title, not just a formatting variant.
        if tnorm and tnorm in _norm(got[0]):
            continue
        if dry_run:
            healed.append(rid)
            continue
        with store.tx() as conn:
            rewrite_cards(
                conn,
                rid,
                title=title,
                author_names=author_names,
                abstract=abstract,
                keywords=keywords,
            )
        healed.append(rid)
    if healed and not dry_run:
        log.info("paper_hygiene: rebuilt %d drifted card(s)", len(healed))
    return healed


# ---------------------------------------------------------------------------
# Superseded-chain collapse
# ---------------------------------------------------------------------------


def _terminal_survivor(conn: Any, start_ref_id: int) -> int | None:
    """Follow ``meta.superseded_by`` to the ref that has none (the final
    survivor). Returns None on a cycle or a broken pointer."""
    seen: set[int] = set()
    cur = start_ref_id
    while True:
        if cur in seen:
            return None  # cycle guard
        seen.add(cur)
        row = conn.execute(
            "SELECT meta->>'superseded_by' FROM refs WHERE ref_id = %s", (cur,)
        ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return cur
        cur = int(row[0])


def collapse_superseded_chains(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[tuple[int, int]]:
    """Repoint ``meta.superseded_by`` chains at the final live survivor.

    Finds retired refs whose ``superseded_by`` target is *itself* retired
    (superseded) and rewrites the pointer to the terminal survivor, so a
    dereference is always one hop. Returns ``(ref_id, terminal)`` pairs.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, (r.meta->>'superseded_by')::bigint "
            "FROM refs r "
            "JOIN refs s ON s.ref_id = (r.meta->>'superseded_by')::bigint "
            "WHERE r.kind = 'paper' AND r.meta ? 'superseded_by' "
            "  AND s.meta ? 'superseded_by' "
            "ORDER BY r.ref_id"
        ).fetchall()
    pairs = [(int(a), int(b)) for a, b in rows]
    if limit:
        pairs = pairs[:limit]

    fixed: list[tuple[int, int]] = []
    for rid, mid in pairs:
        with store.pool.connection() as conn:
            terminal = _terminal_survivor(conn, mid)
        if terminal is None or terminal in (mid, rid):
            continue
        if not dry_run:
            with store.tx() as conn:
                store.stamp_ref_meta(rid, {"superseded_by": terminal}, conn=conn)
        fixed.append((rid, terminal))
    if fixed and not dry_run:
        log.info("paper_hygiene: collapsed %d superseded chain(s)", len(fixed))
    return fixed


# ---------------------------------------------------------------------------
# Dangling links to soft-deleted papers
# ---------------------------------------------------------------------------


def migrate_dangling_paper_links(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Repoint non-``supersedes`` edges off soft-deleted papers.

    A ``supersedes`` edge legitimately points at the retired ref (it's the
    audit record); every *other* relation pointing at a soft-deleted paper
    is a dangling dereference and is moved to the survivor
    (``meta.superseded_by``), dropping the row instead when the move would
    self-loop or collide with an existing survivor edge. Returns the
    ``link_id``s acted on.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.link_id, l.src_ref_id, l.relation, "
            "       (tgt.meta->>'superseded_by')::bigint AS surv "
            "FROM links l "
            "JOIN refs tgt ON tgt.ref_id = l.dst_ref_id "
            "WHERE tgt.kind = 'paper' AND tgt.deleted_at IS NOT NULL "
            "  AND l.relation <> 'supersedes' AND tgt.meta ? 'superseded_by' "
            "ORDER BY l.link_id"
        ).fetchall()
    dangling = [(int(r[0]), r[1], r[2], int(r[3])) for r in rows]
    if limit:
        dangling = dangling[:limit]

    acted: list[int] = []
    for link_id, src, relation, surv in dangling:
        if not dry_run:
            with store.tx() as conn:
                # A self-loop (src already the survivor) or an existing
                # equivalent survivor edge → drop; otherwise repoint.
                collides = (
                    src == surv
                    or conn.execute(
                        "SELECT 1 FROM links "
                        "WHERE src_ref_id IS NOT DISTINCT FROM %s AND dst_ref_id = %s "
                        "  AND relation = %s AND link_id <> %s LIMIT 1",
                        (src, surv, relation, link_id),
                    ).fetchone()
                    is not None
                )
                if collides:
                    conn.execute("DELETE FROM links WHERE link_id = %s", (link_id,))
                else:
                    conn.execute(
                        "UPDATE links SET dst_ref_id = %s WHERE link_id = %s",
                        (surv, link_id),
                    )
        acted.append(link_id)
    if acted and not dry_run:
        log.info("paper_hygiene: migrated %d dangling link(s)", len(acted))
    return acted


__all__ = [
    "collapse_superseded_chains",
    "heal_drifted_cards",
    "migrate_dangling_paper_links",
]
