"""Read-only lookups over the claim-hub evidence graph, for callers that
just need "what does this paper ground" rather than a full seniority
derivation (:mod:`precis.taproot.seniority`) or a write (:mod:`~precis.
taproot.hub` / :mod:`~precis.taproot.authoring`).

Kept separate from :mod:`precis.taproot.authoring` (a write helper) —
this module never opens a transaction, mirroring :mod:`~precis.taproot.
seniority`'s "pure read/derive" stance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE
from precis.taproot.hub import HUB_ROLES

if TYPE_CHECKING:
    from precis.store.protocols import PoolStore

__all__ = ["hubs_grounded_by_paper"]


def hubs_grounded_by_paper(
    store: PoolStore, paper_ref_id: int, *, require_pub_id: bool = True
) -> list[dict[str, Any]]:
    """Every live ``TAPROOT:claim`` hub ``paper_ref_id`` has an inbound
    evidence edge to — the many-to-many read a cite-time nudge needs
    ("this paper already grounds N claims").

    One bounded query: ``links`` (src = the paper, relation one of
    :data:`~precis.taproot.hub.HUB_ROLES`) -> ``refs``/``ref_tags`` (dst is
    a live ``finding`` carrying ``TAPROOT:claim``) -> ``ref_identifiers``
    (the hub's citable ``pub_id``). With the default ``require_pub_id=True``
    the last join is an INNER JOIN, so a hub with no ``pub_id`` yet (not
    citable) drops out entirely — empty list when the paper grounds no
    hub, or grounds only such hubs. This is right for a cite-time nudge
    (never steer toward something uncitable) but wrong for a reader
    wanting "what does this paper ground, including the mint-frontier" —
    pass ``require_pub_id=False`` there to LEFT JOIN instead, keeping
    those rows with ``pub_id: None``.

    Returns one dict per hub: ``{'hub_ref_id', 'pub_id', 'claim',
    'role'}`` — ``claim`` is the hub's title (``mint_hub`` stamps
    ``claim.sentence`` there); ``role`` is whichever evidence relation
    this paper's edge carries (``establishes``/``corroborates``/
    ``contradicts``).
    """
    join_kind = "JOIN" if require_pub_id else "LEFT JOIN"
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, ri.id_value, r.title, l.relation
            FROM links l
            JOIN refs r ON r.ref_id = l.dst_ref_id
            JOIN ref_tags rt ON rt.ref_id = r.ref_id
            JOIN tags t ON t.tag_id = rt.tag_id
                       AND t.namespace = %(ns)s AND t.value = %(claim)s
            {join_kind} ref_identifiers ri ON ri.ref_id = r.ref_id
                       AND ri.id_kind = 'pub_id'
            WHERE l.src_ref_id = %(paper)s
              AND l.relation = ANY(%(roles)s)
              AND r.kind = 'finding'
              AND r.deleted_at IS NULL
            ORDER BY r.ref_id
            """,
            {
                "ns": TAPROOT_NAMESPACE,
                "claim": TAPROOT_CLAIM,
                "paper": paper_ref_id,
                "roles": list(HUB_ROLES),
            },
        ).fetchall()

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for hub_ref_id, pub_id, title, relation in rows:
        if hub_ref_id in seen:
            continue
        seen.add(hub_ref_id)
        out.append(
            {
                "hub_ref_id": int(hub_ref_id),
                "pub_id": pub_id,
                "claim": title,
                "role": relation,
            }
        )
    return out
