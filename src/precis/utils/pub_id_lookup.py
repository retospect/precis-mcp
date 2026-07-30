"""Shared ``[pub_id]`` → finding resolution.

Two surfaces mine the same ``[ab12c3]`` placeholder grammar and need to
agree on what it resolves to:

1. ``precis resolve`` (:mod:`precis.cli.resolve`) — substitutes an
   established finding's cite_key, or a Taproot claim hub's derived
   evidence, at document-finalisation time.
2. The reference ring's Claims group (:mod:`precis.utils.refeye`, Taproot
   slice R1) — explodes a cited claim hub into its evidence at read time.

Factored here so both agree on the regex + the ``ref_identifiers`` /
``TAPROOT:claim`` lookup rather than drifting apart.
"""

from __future__ import annotations

import re
from typing import Any

from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE

#: Placeholder grammar: ``[<6 base32 lowercase chars>]``. The same
#: alphabet :func:`precis.identity.make_pub_id` produces, so any pub id
#: ever minted matches and bracketed strings of other shapes (cite keys,
#: S2 ids, prose ALL-CAPS) don't.
PLACEHOLDER_RE = re.compile(r"\[([a-z2-7]{6})\]")


def lookup_pub_id_finding(store: Any, pub_id: str) -> dict[str, Any] | None:
    """Resolve a pub_id to its finding ref, or ``None`` when there's no
    matching finding (different kind, no such row, soft-deleted).

    Returns ``{ref_id, status, primary_cite_key, dead_reason,
    human_verified, is_hub}``. ``is_hub`` is True iff the finding carries
    ``TAPROOT:claim`` — a living-citation claim hub (Taproot slice A1),
    resolved by callers via :func:`precis.taproot.seniority.derive_evidence`
    instead of the plain status/primary_cite_key path.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.deleted_at, r.meta,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'STATUS'
                     LIMIT 1) AS status,
                   r.human_verified_at,
                   EXISTS (
                     SELECT 1 FROM ref_tags rt2 JOIN tags t2 USING (tag_id)
                      WHERE rt2.ref_id = r.ref_id
                        AND t2.namespace = %(taproot_ns)s
                        AND t2.value = %(taproot_claim)s
                   ) AS is_hub
              FROM ref_identifiers ri
              JOIN refs r ON r.ref_id = ri.ref_id
             WHERE ri.id_kind = 'pub_id' AND ri.id_value = %(pub_id)s
            """,
            {
                "pub_id": pub_id,
                "taproot_ns": TAPROOT_NAMESPACE,
                "taproot_claim": TAPROOT_CLAIM,
            },
        ).fetchone()
    if row is None:
        return None
    ref_id, kind, deleted_at, meta, status, human_verified_at, is_hub = row
    if kind != "finding":
        return None
    if deleted_at is not None:
        return None
    meta = dict(meta or {})
    return {
        "ref_id": int(ref_id),
        "status": status,
        "primary_cite_key": meta.get("primary_cite_key"),
        "dead_reason": meta.get("dead_reason"),
        "human_verified": human_verified_at is not None,
        "is_hub": bool(is_hub),
    }


__all__ = ["PLACEHOLDER_RE", "lookup_pub_id_finding"]
