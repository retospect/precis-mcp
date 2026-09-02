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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from precis.store._nanopub_ops import TERMINAL_STATES
from precis.taproot.canon import CLAIM_HUB_PREDICATE_PARAMS, claim_hub_predicate_sql

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
    #: A live adjudicated `contradicts` edge touches this hub, either
    #: direction (D1, docs/backlog/disputes-edge-nonblocking-
    #: disagreement.md) → blocked, own bucket. Despite the name, this is
    #: NOT the non-blocking open-question count below — that's
    #: `open_disputes_count`. Kept as `disputed` (not renamed) because a
    #: positional constructor elsewhere in the tree depends on field
    #: order; see that constructor's own note.
    disputed: bool
    #: Oldest live `contradicts` edge touching this hub, either direction
    #: (bucket sort key — a blocking edge must not rot invisibly).
    disputed_since: datetime | None
    #: Inbound evidence edges neither verified nor signed off.
    withheld_count: int
    #: Inbound evidence edges carrying a support verdict or a human
    #: sign-off — the exact complement of ``withheld_count``, so the pair
    #: always sums to the hub's live supporting-edge count. Same
    #: approximation as ``withheld_count``: this counts the *presence* of
    #: a stamp, where the publish preflight additionally invalidates one
    #: whose ``verified_claim_sha`` no longer matches (surfaced as
    #: ``drifted``).
    #:
    #: **Judged, not affirmative.** A ``corroborates``-role edge can carry
    #: ``support: "no"`` — ``taproot/authoring.py`` writes whatever verdict
    #: the supporter attests — and such an edge is verified (someone read
    #: it) while supporting nothing. Anything asking "does the corpus stand
    #: behind this claim?" wants :attr:`supported_count`, never this.
    verified_count: int = 0
    #: Inbound evidence edges whose verdict is **affirmative** —
    #: ``support`` in ``yes``/``partial``, or a human ``publish_signoff``.
    #: The subset of ``verified_count`` that actually backs the claim.
    supported_count: int = 0
    #: 3-6 word human handle (precis.workers.hub_tagline), ``refs.meta
    #: ->>'tagline'`` — presentation only, ``None`` when the ``hub_tagline``
    #: worker pass hasn't reached this hub yet (or a human never set one).
    #: Appended last (rather than kept alongside `title`) so a positional
    #: constructor elsewhere in the tree keeps working unmodified.
    tagline: str | None = None
    #: Live `disputes` edges touching this hub, either direction — the
    #: non-blocking open-question count (D1, migration 0151). Never a
    #: demerit, never a block; distinct from `disputed`/`disputed_since`
    #: above, which count the adjudicated, blocking `contradicts` shape.
    #: Appended last for the same reason as `tagline` — see its note.
    open_disputes_count: int = 0

    @property
    def drifted(self) -> bool:
        """Frozen string no longer matches the live hub title."""
        if self.claim_sha is None:
            return False
        from precis.taproot.canon import claim_sha

        return claim_sha(self.title) != self.claim_sha

    @property
    def frozen(self) -> str:
        """The frozen-ness rung: '', 'string', 'bytes', 'published'.

        The ladder itself lives in :func:`precis.nanopub.state.frozen_rung`
        so this display and :mod:`precis.nanopub.demote`'s policy read one
        definition of where the freeze line falls.
        """
        from precis.nanopub.state import frozen_rung

        return frozen_rung(self.state)


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
        # appear with its cause invisible). ``disputes`` gets the same
        # exemption: a `disputes` edge is at least as likely to come from
        # another claim hub (D1's open-question set) and must show up as
        # an evidence leaf too, so the reviewer sees WHAT it's disputed by,
        # not just that it is.
        inbound = conn.execute(
            """
            SELECT l.dst_ref_id, l.src_ref_id, l.relation, p.title, p.kind
              FROM links l
              JOIN refs p ON p.ref_id = l.src_ref_id
                         AND p.retired_at IS NULL
                         AND (p.kind != 'finding'
                              OR l.relation IN ('contradicts', 'disputes'))
             WHERE l.dst_ref_id = ANY(%(ids)s)
               AND l.relation IN ('establishes', 'corroborates', 'contradicts',
                                   'disputes')
            """,
            {"ids": ids},
        ).fetchall()
        outbound = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id, p.title, p.kind
              FROM links l
              JOIN refs p ON p.ref_id = l.dst_ref_id
                         AND p.retired_at IS NULL AND p.kind != 'finding'
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


def draft_cited_hub_ids(store: Store, draft_ref_id: int) -> set[int]:
    """Claim-hub ref_ids ``draft_ref_id``'s chunks cite outbound — the
    ``cites`` edge complement of :func:`~precis.taproot.seniority.
    hub_citers`'s inbound walk. ``handlers/draft.py::sync_draft_links``
    writes one chunk-grounded ``cites`` edge per citing passage
    (``draft --cites--> finding/paper/patent``) whenever a body chunk
    names a citable source; this reads it straight back, unfiltered by
    kind (the ``/nanopub?draft=`` caller intersects against
    :func:`hub_rows`, which already restricts to live ``TAPROOT:claim``
    hubs, so a cited paper/patent silently drops out rather than needing
    a second kind check here)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT l.dst_ref_id
              FROM links l
              JOIN refs r ON r.ref_id = l.dst_ref_id AND r.retired_at IS NULL
             WHERE l.src_ref_id = %(draft)s AND l.relation = 'cites'
            """,
            {"draft": draft_ref_id},
        ).fetchall()
    return {int(r[0]) for r in rows}


def prune_tree(roots: list[HubTreeNode], cited: set[int]) -> list[HubTreeNode]:
    """Filter a :func:`hub_tree` forest to roots whose subtree touches
    ``cited`` — the ``/nanopub?draft=`` scoping. A kept root retains its
    **full** subtree unmodified: a compound's conjunct atoms stay visible
    even when only the compound itself (or a sibling atom) is directly
    cited, since the point is reviewing everything under what the draft
    invokes, not pruning down to the literal cite targets. Callers that
    tally "what's left to sign" should count :func:`tree_ids` of the
    pruned forest, not ``cited`` — the retained atoms are real sign work
    (atoms publish before their compound)."""
    return [r for r in roots if tree_ids([r]) & cited]


def tree_ids(roots: list[HubTreeNode]) -> set[int]:
    """Every ref_id in a forest — the displayed set a pruned tree's tally
    must be computed over (see :func:`prune_tree`)."""
    out: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        out.add(node.row.ref_id)
        stack.extend(node.children)
    return out


def hub_rows(
    store: Store, *, ref_ids: Sequence[int] | None = None
) -> list[HubOverviewRow]:
    """Every live claim hub — ``TAPROOT:claim`` **and** ``STATUS:canonical``
    (:func:`~precis.taproot.canon.claim_hub_predicate_sql`; a ``finding``
    carrying ``TAPROOT:claim`` alone is a chase-tree finding, not a hub,
    and must not appear here with a publish posture it can never have) —
    with its publish posture, one query. Disputed first (oldest dispute on
    top), then minted hubs in mint order (publish-row ``created_at`` —
    stable across state transitions, so signing a hub never moves its
    row), then unminted.

    ``ref_ids`` narrows to a given set of hubs (an empty sequence returns
    nothing without touching the DB) — the read behind a search result's
    posture columns, where the whole-corpus sweep would be waste. Ids that
    are not live claim hubs simply don't come back, so a caller may pass a
    mixed hit set and read a missing row as "no posture"."""
    if ref_ids is not None and not ref_ids:
        return []
    ref_filter = "TRUE" if ref_ids is None else "r.ref_id = ANY(%(ref_ids)s)"
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, r.title,
                   p.id, p.state, p.approved_title, p.claim_sha,
                   p.trusty_uri, p.batch_id, p.updated_at,
                   d.since AS disputed_since,
                   COALESCE(w.n, 0) AS withheld_count,
                   COALESCE(w.v, 0) AS verified_count,
                   COALESCE(w.s, 0) AS supported_count,
                   r.meta->>'tagline' AS tagline,
                   COALESCE(od.n, 0) AS open_disputes_count
              FROM refs r
              LEFT JOIN nanopub_publish p
                     ON p.claim_ref_id = r.ref_id AND p.state != ALL(%(terminal)s)
              -- Live `contradicts` edges touching this hub, either
              -- direction (D1: the adjudicated pair blocks regardless of
              -- which side is src/dst) — the counterpart ref must be live
              -- of ANY kind, not filtered to `pr.kind` at all.
              LEFT JOIN LATERAL (
                    SELECT MIN(l.created_at) AS since
                      FROM links l
                      JOIN refs pr
                        ON pr.ref_id = CASE WHEN l.dst_ref_id = r.ref_id
                                             THEN l.src_ref_id ELSE l.dst_ref_id END
                       AND pr.retired_at IS NULL
                     WHERE (l.dst_ref_id = r.ref_id OR l.src_ref_id = r.ref_id)
                       AND l.relation = 'contradicts'
                    HAVING COUNT(*) > 0
              ) d ON TRUE
              -- Live `disputes` edges touching this hub, either direction
              -- — the non-blocking open-question count (migration 0151).
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS n
                      FROM links l
                      JOIN refs pr
                        ON pr.ref_id = CASE WHEN l.dst_ref_id = r.ref_id
                                             THEN l.src_ref_id ELSE l.dst_ref_id END
                       AND pr.retired_at IS NULL
                     WHERE (l.dst_ref_id = r.ref_id OR l.src_ref_id = r.ref_id)
                       AND l.relation = 'disputes'
              ) od ON TRUE
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) FILTER (
                             WHERE l.meta->>'support' IS NULL
                               AND l.meta->'publish_signoff' IS NULL
                           ) AS n,
                           COUNT(*) FILTER (
                             WHERE l.meta->>'support' IS NOT NULL
                                OR l.meta->'publish_signoff' IS NOT NULL
                           ) AS v,
                           COUNT(*) FILTER (
                             WHERE l.meta->>'support' IN ('yes', 'partial')
                                OR l.meta->'publish_signoff' IS NOT NULL
                           ) AS s
                      FROM links l
                      JOIN refs pr ON pr.ref_id = l.src_ref_id
                                  AND pr.retired_at IS NULL
                     WHERE l.dst_ref_id = r.ref_id
                       AND l.relation IN ('establishes', 'corroborates')
              ) w ON TRUE
             WHERE r.kind = 'finding' AND r.retired_at IS NULL
               AND {ref_filter}
               AND {claim_hub_predicate_sql()}
             ORDER BY d.since ASC NULLS LAST, p.created_at ASC NULLS LAST,
                      r.ref_id
            """,
            {
                "terminal": list(TERMINAL_STATES),
                "ref_ids": list(ref_ids) if ref_ids is not None else None,
                **CLAIM_HUB_PREDICATE_PARAMS,
            },
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
            verified_count=int(r[11]),
            supported_count=int(r[12]),
            tagline=r[13],
            open_disputes_count=int(r[14]),
        )
        for r in rows
    ]
