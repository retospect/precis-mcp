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


@dataclass(frozen=True, slots=True)
class TreeEvidence:
    """One evidence leaf under a tree node: a grounding source ref and
    the edge role (``establishes``/``corroborates``/``contradicts``
    inbound, ``derived-from`` outbound — prod carries both shapes)."""

    ref_id: int
    title: str
    kind: str
    relation: str


@dataclass(frozen=True, slots=True)
class HubTreeNode:
    """One claim hub in the browse tree: its overview row, the claim-link
    relation to its parent (``''`` for a root), nested child hubs
    (inbound ``conjunct-of``/``refines`` sources), and evidence leaves."""

    row: HubOverviewRow
    relation: str
    children: list[HubTreeNode]
    evidence: list[TreeEvidence]


def hub_tree(store: Store) -> list[HubTreeNode]:
    """Every live claim hub as a forest for the ``/nanopub`` browse
    view: a compound nests its conjunct atoms, a refined claim nests
    under what it refines, and each node carries its evidence sources as
    leaves. Roots = hubs that are nobody's atom/refinement; a hub linked
    into several parents appears under each (the links form a DAG); a
    cycle — advisory links, so possible — is cut rather than recursed.
    Root order follows :func:`hub_rows` (disputed first)."""
    rows = hub_rows(store)
    by_id = {r.ref_id: r for r in rows}
    ids = list(by_id)
    if not ids:
        return []
    with store.pool.connection() as conn:
        claim_edges = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id, l.relation
              FROM links l
             WHERE l.relation = ANY(%(rels)s)
               AND l.src_ref_id = ANY(%(ids)s)
               AND l.dst_ref_id = ANY(%(ids)s)
            """,
            {"rels": ["conjunct-of", "refines"], "ids": ids},
        ).fetchall()
        # Support edges come from sources (papers etc.), but a
        # ``contradicts`` edge may come from another claim hub — the same
        # shape the ``disputed`` flag counts — so finding-kind sources are
        # kept for that relation only (the "◆ blocked" badge must never
        # appear with its cause invisible).
        inbound = conn.execute(
            """
            SELECT l.dst_ref_id, l.src_ref_id, l.relation, p.title, p.kind
              FROM links l
              JOIN refs p ON p.ref_id = l.src_ref_id
                         AND p.deleted_at IS NULL
                         AND (p.kind != 'finding' OR l.relation = 'contradicts')
             WHERE l.dst_ref_id = ANY(%(ids)s)
               AND l.relation IN ('establishes', 'corroborates', 'contradicts')
            """,
            {"ids": ids},
        ).fetchall()
        outbound = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id, p.title, p.kind
              FROM links l
              JOIN refs p ON p.ref_id = l.dst_ref_id
                         AND p.deleted_at IS NULL AND p.kind != 'finding'
             WHERE l.src_ref_id = ANY(%(ids)s)
               AND l.relation = 'derived-from'
            """,
            {"ids": ids},
        ).fetchall()

    evidence: dict[int, list[TreeEvidence]] = {}
    seen_ev: set[tuple[int, int, str]] = set()

    def _leaf(hub_id: int, ref_id: int, rel: str, title: str, kind: str) -> None:
        key = (hub_id, ref_id, rel)
        if key in seen_ev:
            return
        seen_ev.add(key)
        evidence.setdefault(hub_id, []).append(
            TreeEvidence(
                ref_id=int(ref_id),
                title=str(title or f"ref {ref_id}"),
                kind=str(kind),
                relation=rel,
            )
        )

    for hub_id, ref_id, rel, title, kind in inbound:
        _leaf(int(hub_id), ref_id, str(rel), title, kind)
    for hub_id, ref_id, title, kind in outbound:
        _leaf(int(hub_id), ref_id, "derived-from", title, kind)

    kids: dict[int, list[tuple[str, int]]] = {}
    child_ids: set[int] = set()
    for src, dst, rel in claim_edges:
        kids.setdefault(int(dst), []).append((str(rel), int(src)))
        child_ids.add(int(src))

    reached: set[int] = set()

    def _build(hub_id: int, relation: str, path: frozenset[int]) -> HubTreeNode:
        reached.add(hub_id)
        children = [
            _build(cid, rel, path | {cid})
            for rel, cid in sorted(kids.get(hub_id, []))
            if cid not in path
        ]
        return HubTreeNode(
            row=by_id[hub_id],
            relation=relation,
            children=children,
            evidence=evidence.get(hub_id, []),
        )

    roots = [
        _build(r.ref_id, "", frozenset({r.ref_id}))
        for r in rows
        if r.ref_id not in child_ids
    ]
    # A pure cycle has no parentless member, so nothing above reaches it —
    # promote one member per unreached cycle to a root rather than
    # dropping the whole loop from the forest.
    for r in rows:
        if r.ref_id not in reached:
            roots.append(_build(r.ref_id, "", frozenset({r.ref_id})))
    return roots


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
