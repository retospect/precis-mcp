"""The "see all the things" read — every claim hub by publish state, in
one bounded query family (spec, Web view: "a table, not a graph";
derived entirely from publish rows + the drift recompute, no new state).

Also the home of the **frozen-ness ladder** the review surface renders
(what may still be edited, per state):

* *(no row)* / ``candidate`` — nothing frozen; edit the hub freely.
* ``reviewed`` — the claim **string** is frozen (``approved_title`` +
  ``claim_sha``); an edit is a reopen.
* ``signed`` / ``anchored`` — the artifact **bytes** are frozen in the
  append-only proof store (DB triggers refuse UPDATE/DELETE; the audit
  recomputes ``byte_sha256`` and reparses). Pre-anchor a reopen discards
  the *pointer* and re-mints; the old artifact row remains forever.
* ``published`` — public and immutable; change = supersede/retract,
  never an edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from precis.store._nanopub_ops import TERMINAL_STATES

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class HubOverviewRow:
    """One claim hub's publish/review posture for the queue table."""

    ref_id: int
    title: str
    #: Live publish-row state, or ``None`` for an unminted hub.
    state: str | None
    publish_row_id: int | None
    approved_title: str | None
    claim_sha: str | None
    trusty_uri: str | None
    batch_id: int | None
    updated_at: datetime | None
    #: Live inbound `contradicts` edge exists → blocked, own bucket.
    disputed: bool
    #: Oldest live contradicts edge (bucket sort key — disputes must
    #: not rot invisibly).
    disputed_since: datetime | None
    #: Inbound evidence edges neither verified nor signed off.
    withheld_count: int

    @property
    def drifted(self) -> bool:
        """Frozen string no longer matches the live hub title."""
        if self.claim_sha is None:
            return False
        from precis.taproot.canon import claim_sha

        return claim_sha(self.title) != self.claim_sha

    @property
    def frozen(self) -> str:
        """The frozen-ness rung: '', 'string', 'bytes', 'published'."""
        if self.state in ("signed", "anchored"):
            return "bytes"
        if self.state == "reviewed":
            return "string"
        if self.state == "published":
            return "published"
        return ""


def hub_rows(store: Store) -> list[HubOverviewRow]:
    """Every live ``TAPROOT:claim`` hub with its publish posture, one
    query. Disputed first (oldest dispute on top), then by state age."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   p.id, p.state, p.approved_title, p.claim_sha,
                   p.trusty_uri, p.batch_id, p.updated_at,
                   d.since AS disputed_since,
                   COALESCE(w.n, 0) AS withheld_count
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
                         AND t.namespace = 'TAPROOT' AND t.value = 'claim'
              LEFT JOIN nanopub_publish p
                     ON p.claim_ref_id = r.ref_id AND p.state != ALL(%(terminal)s)
              LEFT JOIN LATERAL (
                    SELECT MIN(l.created_at) AS since
                      FROM links l
                      JOIN refs pr ON pr.ref_id = l.src_ref_id
                                  AND pr.deleted_at IS NULL
                     WHERE l.dst_ref_id = r.ref_id
                       AND l.relation = 'contradicts'
                    HAVING COUNT(*) > 0
              ) d ON TRUE
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS n
                      FROM links l
                      JOIN refs pr ON pr.ref_id = l.src_ref_id
                                  AND pr.deleted_at IS NULL
                     WHERE l.dst_ref_id = r.ref_id
                       AND l.relation IN ('establishes', 'corroborates')
                       AND l.meta->>'support' IS NULL
                       AND l.meta->'publish_signoff' IS NULL
              ) w ON TRUE
             WHERE r.kind = 'finding' AND r.deleted_at IS NULL
             ORDER BY d.since ASC NULLS LAST, p.updated_at ASC NULLS LAST,
                      r.ref_id
            """,
            {"terminal": list(TERMINAL_STATES)},
        ).fetchall()
    return [
        HubOverviewRow(
            ref_id=int(r[0]),
            title=str(r[1] or ""),
            publish_row_id=int(r[2]) if r[2] is not None else None,
            state=r[3],
            approved_title=r[4],
            claim_sha=r[5],
            trusty_uri=r[6],
            batch_id=int(r[7]) if r[7] is not None else None,
            updated_at=r[8],
            disputed=r[9] is not None,
            disputed_since=r[9],
            withheld_count=int(r[10]),
        )
        for r in rows
    ]
