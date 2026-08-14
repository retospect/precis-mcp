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

**Authorial cite pinning (Taproot slice A2).** A hub cite is a *living
default* — it resolves to whatever ``seniority.derive_evidence`` currently
derives. An author can pin it inside the token itself (syntactic, no
storage, no draft-side edge — we own the rendering so the grammar is
ours):

- ``[<pub_id>>pa5,pc293]`` — **replace**: cite exactly these handles,
  overriding the derived originators.
- ``[<pub_id>+pa5]`` — **supplement**: the derived originators plus these
  (deduped).

Handles are universal handles (:mod:`precis.utils.handle_registry`):
``pa5`` names a paper, ``pc293`` a paper *chunk*/passage — a passage
handle resolves to its parent paper's cite_key (the ``.bib`` is
paper-level; the passage granularity is the author saying "grounded at
this figure"). A plain ``[pub_id]`` (no pin) parses exactly as before.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE

if TYPE_CHECKING:
    from precis.store.store import Store

#: Placeholder grammar: ``[<6 base32 lowercase chars>]`` — the same
#: alphabet :func:`precis.identity.make_pub_id` produces, so any pub id
#: ever minted matches and bracketed strings of other shapes (cite keys,
#: S2 ids, prose ALL-CAPS) don't — plus an optional **pin** (Taproot slice
#: A2): an op char (``>`` replace / ``+`` supplement) followed by a
#: comma-separated list of universal handles (``pa5``, ``pc293``, …).
#: Group 1 = pub_id, group 2 = op or ``None``, group 3 = the raw
#: comma-separated handle list or ``None``. A bare ``[pub_id]`` (no pin)
#: leaves groups 2/3 ``None``, matching the pre-A2 grammar exactly.
PLACEHOLDER_RE = re.compile(
    r"\[([a-z2-7]{6})(?:([>+])([a-z]{2}[0-9]+(?:,[a-z]{2}[0-9]+)*))?\]"
)


def parse_pin(
    token: str,
) -> tuple[str, str | None, list[str]] | None:
    """Decode a full placeholder token (e.g. ``[ab12c3>pa5,pc293]``) into
    ``(pub_id, op, handles)``.

    ``op`` is ``'>'`` (replace) / ``'+'`` (supplement) / ``None`` (no
    pin — a plain ``[pub_id]``). ``handles`` is the comma-split handle
    list, empty when there's no pin. Returns ``None`` when ``token``
    isn't a well-formed placeholder at all (callers that already matched
    via :data:`PLACEHOLDER_RE` won't hit this).
    """
    m = PLACEHOLDER_RE.fullmatch(token)
    if m is None:
        return None
    pub_id, op, handles_raw = m.group(1), m.group(2), m.group(3)
    handles = handles_raw.split(",") if handles_raw else []
    return pub_id, op, handles


def lookup_pub_id_finding(store: Store, pub_id: str) -> dict[str, Any] | None:
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


__all__ = ["PLACEHOLDER_RE", "lookup_pub_id_finding", "parse_pin"]
