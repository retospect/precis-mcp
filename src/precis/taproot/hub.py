"""Taproot Phase 2 — the single write door for claim hubs + evidence edges.

Build ticket: ``docs/proposals/taproot-phase2-hub-node.md``; governance:
ADR 0073; design: ``docs/proposals/taproot.md`` §"The core model".

**Single write path (open #16, ADR 0073).** Every hub-finding and every
``establishes``/``corroborates``/``contradicts`` evidence edge is written
through this module. A raw ``INSERT`` / ``store.add_link`` for these
relations elsewhere bypasses the vocabulary + ``TAPROOT:claim`` guards below
and is a defect — the exact silent-junk-edge error taproot exists to prevent.

Three functions:

1. :func:`mint_hub` — create a ``TAPROOT:claim`` ``finding`` hub for a
   paper-grounded claim (open #15: only paper-sourced claims become hubs).
2. :func:`attach_evidence` — write one ``paper --role--> hub`` edge, ``role``
   in :data:`HUB_ROLES`, guarding the target is actually a claim hub.
3. :func:`apply_placement` — bridge a :class:`~precis.taproot.canon.Placement`
   (the canonicalizer's verdict) to the writes above; a ``needs_review``
   placement files a ``kind='todo'`` (via an injected ``todo_fn``) and never
   auto-attaches (open #16).

Phase 2 defines + unit-tests these; the edges are *populated at scale* by the
forward ``chase`` wiring in Phase 3, which supplies the verdict ``meta``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.store.types import BlockInsert, Tag
from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    Placement,
)

log = logging.getLogger(__name__)

#: The three evidence-edge roles a ``paper --> hub`` link may carry
#: (taproot.md Axis A). ``establishes`` = originator (migration 0094);
#: ``corroborates`` (0085) and ``contradicts`` (0001) reuse existing slugs —
#: endpoint kinds disambiguate. Each is still validated through
#: :func:`validate_relation` against the live ``relations`` table before write.
HUB_ROLES: frozenset[str] = frozenset({"establishes", "corroborates", "contradicts"})

#: The default role :func:`apply_placement` attaches with. ``corroborates`` is
#: the *safe* assumption — never falsely claim a paper is the originator.
#: Promotion to ``establishes`` is a derivation over the citation graph
#: (taproot.md §"Seniority is derived", Phase 2c/3), not a write-time guess.
_DEFAULT_ROLE = "corroborates"

_STATUS_NS = "STATUS"
_STATUS_TRACING = "tracing"


def _is_claim_hub(store: Any, ref_id: int, *, conn: Any) -> bool:
    """True iff ``ref_id`` is a live ``finding`` carrying ``TAPROOT:claim``."""
    row = conn.execute(
        """
        SELECT 1
        FROM refs r
        JOIN ref_tags rt ON rt.ref_id = r.ref_id
        JOIN tags t ON t.tag_id = rt.tag_id
                   AND t.namespace = %(ns)s AND t.value = %(val)s
        WHERE r.ref_id = %(rid)s AND r.kind = 'finding' AND r.deleted_at IS NULL
        LIMIT 1
        """,
        {"ns": TAPROOT_NAMESPACE, "val": TAPROOT_CLAIM, "rid": ref_id},
    ).fetchone()
    return row is not None


def mint_hub(
    store: Any,
    claim: CanonicalClaim,
    *,
    set_by: str = "agent",
    conn: Any = None,
) -> int:
    """Create a new ``TAPROOT:claim`` ``finding`` hub. Returns its ref_id.

    Only paper-grounded claims become hubs (open #15); the caller
    (:func:`apply_placement`, driven by the canonicalizer over a paper chunk)
    supplies that provenance — this primitive just writes the hub.

    The hub is a ``finding`` (reuse, not a new kind — ADR 0054 precedent):
    ``claim.sentence`` → ``title`` (list-view scannability) *and* a
    ``finding_body`` chunk at ``ord=0`` (so it embeds + full-text-searches,
    and the card pass emits the ``card_combined`` that :func:`canon.block`
    ANN-retrieves over); ``claim.scope`` → ``meta.scope``; ``STATUS:tracing``;
    ``TAPROOT:claim``. This is taproot's *system-writer* path — the agent-facing
    door is ``FindingHandler.put`` (pub_id dedup + a frontier ``derived-from``);
    taproot dedups upstream via canonicalization, so the hub write is direct.
    """

    def _do(c: Any) -> int:
        ref = store.insert_ref(
            kind="finding",
            slug=None,
            title=claim.sentence.strip()[:200],
            meta={"scope": dict(claim.scope), "source": "taproot"},
            conn=c,
        )
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0,
                    text=claim.sentence.strip(),
                    meta={"chunk_kind": "finding_body"},
                )
            ],
            conn=c,
        )
        # STATUS:tracing — a fresh hub has no resolved originators yet
        # (system-set, mirrors FindingHandler).
        store.add_tag(
            ref.id,
            Tag.closed(_STATUS_NS, _STATUS_TRACING),
            set_by="system",
            replace_prefix=True,
            conn=c,
        )
        store.add_tag(
            ref.id,
            Tag.closed(TAPROOT_NAMESPACE, TAPROOT_CLAIM),
            set_by=set_by,
            replace_prefix=True,
            conn=c,
        )
        return int(ref.id)

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def attach_evidence(
    store: Any,
    *,
    hub_ref_id: int,
    paper_ref_id: int,
    role: str,
    meta: dict[str, Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
) -> None:
    """Write one ``paper --role--> hub`` evidence edge.

    ``role`` must be one of :data:`HUB_ROLES` *and* a registered relation
    (checked via :func:`validate_relation` — the friendly pre-flight for the
    ``links_relation_fkey`` FK). ``hub_ref_id`` must be a ``TAPROOT:claim``
    finding — never attach evidence to a ``TAPROOT:review`` note or a non-finding
    (that is what the classifier + this guard exist to prevent). The edge is
    directed **paper → hub**; the hub reads its evidence via
    ``links_for(direction='in', relation=role)``. ``meta`` carries the chase
    verdict (``support``/``support_reason``/``caveats``/``char_offset``/
    ``source_handle``), populated in Phase 3.
    """
    if role not in HUB_ROLES:
        raise BadInput(
            f"invalid evidence role: {role!r}",
            options=sorted(HUB_ROLES),
            next=f"role must be one of {sorted(HUB_ROLES)}",
        )
    # FK/vocab pre-flight (raises BadInput on an unregistered slug).
    validated = validate_relation(role, store=store)

    def _do(c: Any) -> None:
        if not _is_claim_hub(store, hub_ref_id, conn=c):
            raise BadInput(
                f"hub_ref_id={hub_ref_id} is not a TAPROOT:claim finding",
                next=(
                    "evidence edges attach only to claim hubs — tag the "
                    "finding TAPROOT:claim (axis:taproot) or pick a claim hub"
                ),
            )
        store.add_link(
            src_ref_id=paper_ref_id,
            dst_ref_id=hub_ref_id,
            relation=validated,
            meta=meta,
            set_by=set_by,
            conn=c,
        )

    if conn is not None:
        _do(conn)
    else:
        with store.tx() as c:
            _do(c)


def apply_placement(
    store: Any,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    paper_ref_id: int,
    role: str = _DEFAULT_ROLE,
    meta: dict[str, Any] | None = None,
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None = None,
    set_by: str = "agent",
) -> int | None:
    """Persist a canonicalizer :class:`Placement` through the write door.

    Routing (mirrors :func:`precis.taproot.canon.place`):

    * ``attach`` — attach this paper as evidence on the matched hub.
    * ``new`` — mint a hub, attach the paper.
    * ``new_contradicts`` — mint a hub, attach the paper, and link the new hub
      ``contradicts`` the existing (opposite-*claim*) hub.
    * ``needs_review`` — file a ``kind='todo'`` via ``todo_fn`` and attach
      **nothing** (open #16: a risky merge is never auto-applied).

    Returns the hub ref_id it attached to / minted, or ``None`` for
    ``needs_review``. ``role`` is the evidence role for the paper edge
    (default :data:`_DEFAULT_ROLE`); originator promotion is derived later.
    """
    action = placement.action
    if action == "attach":
        if placement.hub_ref_id is None:
            raise BadInput("attach placement has no hub_ref_id")
        attach_evidence(
            store,
            hub_ref_id=placement.hub_ref_id,
            paper_ref_id=paper_ref_id,
            role=role,
            meta=meta,
            set_by=set_by,
        )
        return placement.hub_ref_id

    if action in ("new", "new_contradicts"):
        with store.tx() as c:
            hub = mint_hub(store, claim, set_by=set_by, conn=c)
            attach_evidence(
                store,
                hub_ref_id=hub,
                paper_ref_id=paper_ref_id,
                role=role,
                meta=meta,
                set_by=set_by,
                conn=c,
            )
            if action == "new_contradicts":
                if placement.contradicts_hub_ref_id is None:
                    raise BadInput(
                        "new_contradicts placement has no contradicts_hub_ref_id"
                    )
                # Hub <-> hub: opposite *claims* (distinct from a paper->hub
                # `contradicts` evidence edge; same slug, different endpoints).
                store.add_link(
                    src_ref_id=hub,
                    dst_ref_id=placement.contradicts_hub_ref_id,
                    relation=validate_relation("contradicts", store=store),
                    set_by=set_by,
                    conn=c,
                )
        return hub

    if action == "needs_review":
        if todo_fn is not None:
            todo_fn(claim, placement)
        else:
            log.warning(
                "taproot: needs_review placement dropped (no todo_fn): %s",
                placement.reason,
            )
        return None

    raise BadInput(f"unknown placement action: {action!r}")


__all__ = [
    "HUB_ROLES",
    "apply_placement",
    "attach_evidence",
    "mint_hub",
]
