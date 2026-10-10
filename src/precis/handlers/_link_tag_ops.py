"""Shared link/tag CRUD helpers for handlers that aren't text-mutable.

The MCP critic flagged that read-only kinds — ``paper``, the
Perplexity ``research``/``think``/``websearch`` caches, ``conv``,
``oracle`` — couldn't accept ``link=`` / ``tags=`` ops at all.
Their ``supports_put=False`` (or import-only) made cross-linking
between, say, a memory and the paper that backs it a one-way
street: the memory could `link='paper:slug'` to the paper but
not the other way round.

The fix is to enable link/tag CRUD on read-only kinds **without**
opening up text mutation. This module factors out the validation
and store-call wiring that was inlined in
``_numeric_ref.NumericRefHandler.put`` so paper + cache handlers
can call the same logic without depending on
``NumericRefHandler``'s create/delete/text-update machinery.

The functions are deliberately free-standing rather than methods
on a mixin so the call sites stay obvious — each handler owns
its own ``put`` shape and just delegates the link/tag bits to
these helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.errors import BadInput, NotFound
from precis.handlers._link_target import (
    LinkTarget,
    mint_lazy_link_target,
    parse_link_target,
)
from precis.store import Store, Tag
from precis.store.types import Relation
from precis.utils.relations import (  # noqa: F401  (re-export)
    _DEFAULT_RELATION,
    _VALID_RELATIONS,
    validate_relation,
)


def require_tag_ops(kind: str, add: list[str] | None, remove: list[str] | None) -> None:
    """Reject a ``tag()`` call that supplies neither ``add=`` nor ``remove=``.

    Shared by every handler's ``tag()`` so the guard (and its agent-
    facing wording) lives in one place.
    """
    if not add and not remove:
        raise BadInput(
            f"tag(kind={kind!r}, id=...) requires add= or remove=",
            next=f"tag(kind={kind!r}, id=<id>, add=['topic-...'])",
        )


def require_link_target(kind: str, target: str | None) -> str:
    """Reject a ``link()`` call with no ``target=``; return the target.

    Shared across handlers. Returns the (now non-``None``) target so
    callers re-narrow the type by assigning the result.
    """
    if target is None:
        raise BadInput(
            f"link(kind={kind!r}, id=...) requires target=",
            next=f"link(kind={kind!r}, id=<id>, target='pa5')",
        )
    return target


def validate_link_mode(mode: str) -> str:
    """Validate a ``link()`` ``mode=`` is ``add`` or ``remove``; return it.

    Shared across handlers so the (identical) check and message don't
    drift between kinds.
    """
    if mode not in ("add", "remove"):
        raise BadInput(
            f"link mode must be 'add' or 'remove', got {mode!r}",
            options=["add", "remove"],
        )
    return mode


def _endpoint_kinds(store: Store, a_ref_id: int, b_ref_id: int) -> dict[int, str]:
    """``refs.kind`` for two ref ids, in one round trip."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind FROM refs WHERE ref_id = ANY(%s)",
            ([a_ref_id, b_ref_id],),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def _both_live_claim_hubs(store: Store, a_ref_id: int, b_ref_id: int) -> bool:
    """True iff both refs are live ``TAPROOT:claim`` findings.

    Local import: :mod:`precis.taproot.hub` imports
    :func:`validate_relation` from this module at module scope, so a
    top-level import here would be circular.
    """
    from precis.taproot.hub import _is_claim_hub

    with store.pool.connection() as conn:
        return _is_claim_hub(a_ref_id, conn=conn) and _is_claim_hub(b_ref_id, conn=conn)


_TAXON_HIERARCHY_RELATIONS: frozenset[str] = frozenset({"specialises", "generalises"})


def _taxon_endpoint(
    store: Store, ref_id: int | None, kind: str | None, meta: dict[str, Any] | None
) -> tuple[str, str, str, dict[str, Any]]:
    """``(handle, name, kind, meta)`` of one hierarchy endpoint. A stored
    ref is read from ``refs``; ``ref_id=None`` is the not-yet-inserted node of
    a create-time ``put(link=)``, described by its would-be ``kind``/``meta``."""
    from precis.utils import handle_registry

    if ref_id is None:
        m = dict(meta or {})
        return "(new node)", str(m.get("name") or "?"), str(kind), m
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, title, meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"ref id={ref_id} not found")
    k, title, m = str(row[0]), str(row[1] or ""), dict(row[2] or {})
    handle = handle_registry.try_format(k, ref_id) or f"{k}:{ref_id}"
    return handle, str(m.get("name") or title or "?"), k, m


def guard_taxon_hierarchy(
    store: Store,
    src_ref_id: int | None,
    target: LinkTarget,
    relation: str,
    *,
    src_kind: str | None = None,
    src_meta: dict[str, Any] | None = None,
) -> None:
    """Enforce the taxon hierarchy rules on an add-mode ``specialises`` /
    ``generalises`` link (docs/backlog/term-taxonomy.md AC 3-4). Shared by
    every add-mode link door, beside
    :func:`guard_and_route_contradicts_disputes`.

    Fires only when ``relation`` is ``specialises``/``generalises`` AND at
    least one endpoint is a ``taxon``; two non-taxon refs keep using the pair
    exactly as before. Raises :class:`BadInput` unless:

    * both endpoints are ``taxon`` (the message names the offender's handle
      and kind), and the target is ref-level (no ``dst_pos``);
    * the edge is not a self-link and closes no cycle;
    * every start node reachable from the PARENT (the parent itself when it
      is one) has its ``meta.contract.required_keys`` present and non-null in
      the CHILD's meta.

    ``specialises`` is src=child -> dst=parent; ``generalises`` is the
    inverse, src=parent -> dst=child. ``src_ref_id=None`` is a create-time
    link whose source node does not exist yet: pass its would-be
    ``src_kind``/``src_meta`` and the check runs before any insert, so a
    refused put writes nothing.
    """
    if relation not in _TAXON_HIERARCHY_RELATIONS:
        return
    s_handle, s_name, s_kind, s_meta = _taxon_endpoint(
        store, src_ref_id, src_kind, src_meta
    )
    d_handle, d_name, d_kind, d_meta = _taxon_endpoint(store, target.ref_id, None, None)
    if s_kind != "taxon" and d_kind != "taxon":
        return
    for handle, name, kind in ((s_handle, s_name, s_kind), (d_handle, d_name, d_kind)):
        if kind != "taxon":
            raise BadInput(
                f"{relation!r} on a taxon needs a taxon at both ends: "
                f"{handle} {name!r} is kind={kind!r}",
                next=(
                    "link taxon to taxon; to attach a non-taxon ref to a "
                    "taxon use rel='instance-of'"
                ),
            )
    if target.pos is not None:
        raise BadInput(
            f"a taxon hierarchy edge links whole nodes, not a chunk "
            f"(target {target.raw!r})",
            next="drop the ~position from target=",
        )
    child_id: int | None
    parent_id: int | None
    if relation == "specialises":
        child_id, parent_id = src_ref_id, target.ref_id
        child, parent = (s_handle, s_name, s_meta), (d_handle, d_name, d_meta)
    else:
        child_id, parent_id = target.ref_id, src_ref_id
        child, parent = (d_handle, d_name, d_meta), (s_handle, s_name, s_meta)
    if parent_id is None:
        # create-time ``generalises``: the new node would be the parent of an
        # existing child; it has no ancestors, so no cycle, and the only
        # start node it reaches is itself — its own contract binds the child.
        p_meta = parent[2]
        if p_meta.get("start"):
            for key in (p_meta.get("contract") or {}).get("required_keys") or []:
                if child[2].get(key) is None:
                    raise BadInput(
                        f"{child[0]} {child[1]!r} needs {key!r}: the new start "
                        f"node {parent[1]!r} requires it of everything beneath it",
                        next=f"set meta {key!r} on {child[0]} first, or drop the contract",
                    )
        return
    if child_id is not None and store.taxon_would_cycle(child_id, parent_id):
        raise BadInput(
            f"that link would make a cycle: {child[0]} {child[1]!r} specialises "
            f"{parent[0]} {parent[1]!r}, but {parent[0]} is already "
            f"{child[0]} itself or below it",
            next="a taxon hierarchy is acyclic; pick a parent that is not a descendant",
        )
    child_meta = child[2]
    for start_id in store.taxon_start_nodes_reached(parent_id):
        s_h, s_n, _k, start_meta = _taxon_endpoint(store, start_id, None, None)
        required = (start_meta.get("contract") or {}).get("required_keys") or []
        for key in required:
            if child_meta.get(key) is None:
                raise BadInput(
                    f"{child[0]} {child[1]!r} needs {key!r}: start node "
                    f"{s_h} {s_n!r} requires it of everything beneath it",
                    next=(
                        f"put the node with meta={{{key!r}: ...}} "
                        "(or set it before linking)"
                    ),
                )


def _ref_label(ref_id: int, kind: str) -> str:
    """``kind:id`` handle for an error message (the registry's short handle
    when the kind has one)."""
    from precis.utils import handle_registry

    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def check_relation_constraints(
    store: Store,
    rel: str,
    src_ref_id: int,
    target: LinkTarget,
) -> None:
    """Refuse an add-mode link that breaks the relation's constraint row
    (migration 0180: ``domain_kinds`` / ``range_kinds`` / ``functional`` /
    ``acyclic``). The one validator at both generic link doors
    (:func:`apply_link_ops`, ``NumericRefHandler.link``), mirroring how
    ``Tag.parse_strict`` is the one choke point for tags. A relation with no
    constraint row is a no-op.

    A write that uses the inverse slug of a constrained relation
    (``has-draft`` for ``draft-of``) is checked as the constrained edge
    with its ends swapped, so the inverse is not a bypass.

    * domain / range: ``refs.kind`` of the source / target must be in the
      set; the error names the relation, the offending end and the allowed
      kinds, then the relation's description (the rule's rationale).
    * ``functional``: the target (the owner end, e.g. the project of a
      ``draft-of``) may hold at most one live source; a second raises
      naming the existing one. ``mode='remove'`` the old edge first.
    * ``acyclic``: refused when the target already reaches the source
      along the relation (:meth:`Store.ancestors`, either stored direction,
      depth-capped).

    ``transitive`` is stored for readers; it checks nothing here.
    """
    constraints = store.relation_constraints()
    rc = constraints.get(rel)
    s_id, d_id = src_ref_id, target.ref_id
    if rc is None or not rc.constrained:
        inv = constraints.get(rc.inverse_slug) if rc and rc.inverse_slug else None
        if inv is None or not inv.constrained:
            return
        rc, s_id, d_id = inv, d_id, s_id
    if rc.domain_kinds is not None or rc.range_kinds is not None:
        kinds = _endpoint_kinds(store, s_id, d_id)
        for end, ref_id, allowed, column in (
            ("source", s_id, rc.domain_kinds, "domain_kinds"),
            ("target", d_id, rc.range_kinds, "range_kinds"),
        ):
            kind = kinds.get(ref_id)
            if allowed is None or kind is None or kind in allowed:
                continue
            allowed_s = ", ".join(sorted(allowed))
            why = f" — {rc.description}" if rc.description else ""
            raise BadInput(
                f"{rc.slug!r} {column} = {allowed_s}: {end} "
                f"{_ref_label(ref_id, kind)} is kind {kind!r}{why}",
                next=(
                    f"link a {allowed_s} ref as the {end} of {rc.slug!r}, "
                    f"or pick another rel= (get(kind='skill', "
                    f"id='precis-relations') lists each relation's kinds)"
                ),
            )
    if s_id == d_id:
        return  # add_link refuses the self-loop with its own message
    if rc.functional:
        held = store.functional_conflict(rc.slug, d_id, exclude_src_ref_id=s_id)
        if held is not None:
            kinds = _endpoint_kinds(store, held, d_id)
            raise BadInput(
                f"{rc.slug!r} is functional: {_ref_label(d_id, kinds.get(d_id, '?'))} "
                f"already has {_ref_label(held, kinds.get(held, '?'))}",
                next=(
                    f"unlink the existing one first (link(..., rel={rc.slug!r}, "
                    "mode='remove')), or work on the existing one"
                ),
            )
    if rc.acyclic and d_id in store.ancestors(rc.slug, s_id):
        kinds = _endpoint_kinds(store, s_id, d_id)
        raise BadInput(
            f"{rc.slug!r} would form a cycle: "
            f"{_ref_label(d_id, kinds.get(d_id, '?'))} already reaches "
            f"{_ref_label(s_id, kinds.get(s_id, '?'))} along {rc.slug!r}",
            next=f"{rc.slug!r} is acyclic - pick a target that is not above the source",
        )


def guard_and_route_contradicts_disputes(
    store: Store,
    src_ref_id: int,
    target: LinkTarget,
    relation: Relation,
) -> int | None:
    """Enforce the ``disputes`` write-door policy for an add-mode link:
    delegate a live claim-pair ``disputes`` to
    :func:`precis.taproot.hub.link_claims`.

    Shared by every add-mode link door — the generic ``link()`` handlers
    (:func:`apply_link_ops`, ``NumericRefHandler.link``) and any future
    one — so the policy lives in exactly one place
    (docs/backlog/disputes-edge-nonblocking-disagreement.md D1-D4; a
    second copy of this guard is exactly how the gap this function
    closes was introduced).

    Returns ``None`` when the caller should proceed with its own plain
    ``store.add_link`` write. Returns the number of links added (0 or 1)
    when this helper already performed the write — the claim-pair
    ``disputes`` delegation.

    Only ``disputes`` gets extra routing here. The ``contradicts`` endpoint
    rule (claim-graph ``contradicts`` is adjudication-derived; only
    ``memory``<->``memory`` is fileable, D2) is the ``relations`` row's
    ``domain_kinds``/``range_kinds``, enforced by
    :func:`check_relation_constraints` at the same doors.

    * ``disputes`` — between two live ``TAPROOT:claim`` findings at
      ref-level, this delegates to :func:`precis.taproot.hub.link_claims`
      (the claim-pair door, D4) instead of a plain ``add_link``: it's
      idempotent and enforces both endpoints are live claim hubs. Any
      other endpoint shape (paper->hub, a chunk-position target, etc.)
      returns ``None`` so the caller falls through to its plain write —
      claim-hub links are ref-level only, and a non-claim-hub ``disputes``
      edge (e.g. a review note on a finding) is exactly what
      :func:`precis.taproot.hub.reattach_as_disputes` also writes plainly.
    """
    if (
        relation == "disputes"
        and target.pos is None
        and _both_live_claim_hubs(store, src_ref_id, target.ref_id)
    ):
        from precis.taproot.hub import link_claims

        added = link_claims(
            store,
            from_hub_ref_id=src_ref_id,
            to_hub_ref_id=target.ref_id,
            relation="disputes",
            set_by="agent",
        )
        return 1 if added else 0
    return None


def apply_link_ops(
    store: Store,
    src_ref_id: int,
    *,
    link: str | None,
    unlink: str | None,
    rel: str | None,
    meta: dict[str, Any] | None = None,
    merge_meta: bool = False,
) -> tuple[int, int]:
    """Apply ``link=`` / ``unlink=`` operations against ``src_ref_id``.

    Returns ``(n_added, n_removed)`` so the calling handler can
    render an honest ack. ``parse_link_target`` resolves the
    string spec to a ``(ref_id, pos)`` pair via the store; bad
    targets raise ``BadInput`` before we touch any rows.

    Caller passes either ``link=`` (add) or ``unlink=`` (remove);
    the seven-verb ``link()`` method enforces that they're not both
    set at the call boundary. ``meta=``/``merge_meta=`` (add-only) ride
    straight through to :meth:`Store.add_link` — e.g. a structure
    design's paper-provenance rationale note (gr161577,
    ``links.meta['note']``, ``merge_meta=True`` so re-linking updates
    it). ``merge_meta`` defaults to ``False`` — every other caller of
    this function keeps today's no-op-on-conflict behaviour untouched.

    The relation constraint row (domain/range kinds, functional, acyclic)
    is enforced by :func:`check_relation_constraints`; the ``disputes``
    claim-pair delegation (docs/backlog/disputes-edge-nonblocking-
    disagreement.md D1-D4) lives in
    :func:`guard_and_route_contradicts_disputes`. Both are shared with
    every other add-mode link door.
    """
    relation = validate_relation(rel, store=store)

    n_added = 0
    n_removed = 0

    if link is not None:
        mint_lazy_link_target(link, store=store)
        target = parse_link_target(link, store=store)
        guard_taxon_hierarchy(store, src_ref_id, target, relation)
        check_relation_constraints(store, relation, src_ref_id, target)
        routed = guard_and_route_contradicts_disputes(
            store, src_ref_id, target, relation
        )
        if routed is not None:
            n_added = routed
        else:
            store.add_link(
                src_ref_id=src_ref_id,
                dst_ref_id=target.ref_id,
                dst_pos=target.pos,
                relation=relation,
                meta=meta,
                merge_meta=merge_meta,
            )
            n_added = 1

    if unlink is not None:
        target = parse_link_target(unlink, store=store, include_retired=True)
        # ``rel=`` on unlink is per-relation; absence means "any
        # link to this target at this position". Mirrors
        # ``NumericRefHandler._update``'s behaviour.
        n_removed = store.remove_link(
            src_ref_id=src_ref_id,
            dst_ref_id=target.ref_id,
            dst_pos=target.pos,
            relation=relation if rel is not None else None,
        )

    return n_added, n_removed


def apply_tag_ops(
    store: Store,
    kind: str,
    ref_id: int,
    *,
    tags: list[str] | None,
    untags: list[str] | None,
    ttl_days: int | None = None,
    expires_at: datetime | None = None,
    conn: Any = None,
) -> tuple[int, int]:
    """Apply ``tags=`` / ``untags=`` against ``ref_id``.

    Returns ``(n_added, n_removed)``. Both lists go through
    :meth:`Tag.parse_strict` with the kind passed in so per-kind
    axis enforcement catches closed-axis tags on kinds that
    don't list the axis (e.g. ``STATUS:open`` on a paper).

    Closed-prefix add semantics: a new closed-prefix value
    *replaces* any existing value under the same prefix
    (``STATUS:done`` displaces ``STATUS:open``). This matches
    the workflow expectation that there's only one STATUS at a
    time.

    Validation runs *first* across both lists. The MCP critic
    flagged that the previous loop interleaved parse + write,
    so a bad tag mid-list left the earlier writes committed and
    the later writes skipped — partial state the agent had no way
    to detect. Now any ``BadInput`` is raised before any DB
    write happens; the writes themselves run inside a single
    transaction so a downstream constraint violation rolls back
    every part of the call. (Critic MAJOR #1, read-only-kinds side.)

    ``ttl_days`` / ``expires_at`` (migration 0010) stamps an
    expiry on the *added* tag rows. Passing ``ttl_days=30`` means
    ``expires_at = now() + 30 days`` resolved at call time. The two
    are mutually exclusive; pass at most one. Re-tagging the same
    tag with a fresh ``ttl_days`` refreshes the expiry (the
    underlying ``add_tag`` does ``ON CONFLICT DO UPDATE``). Pass
    neither to keep the prior semantics (no expiry).

    ``conn`` lets a create-path (citation / finding ``put``) apply the
    user's ``tags=`` inside its own transaction so the new ref and its
    tags commit (or roll back) atomically. When ``None`` (the default)
    the writes run in a fresh ``store.tx()`` as before.
    """
    if ttl_days is not None and expires_at is not None:
        raise BadInput(
            "ttl_days= and expires_at= are mutually exclusive",
            next="pass at most one of ttl_days=N or expires_at=<iso8601>",
        )
    resolved_expires: datetime | None = None
    if ttl_days is not None:
        if not isinstance(ttl_days, int) or ttl_days <= 0:
            raise BadInput(
                f"ttl_days must be a positive integer, got {ttl_days!r}",
                next="ttl_days=30",
            )
        resolved_expires = datetime.now(tz=UTC) + timedelta(days=ttl_days)
    elif expires_at is not None:
        resolved_expires = expires_at

    parsed_add: list[Tag] = (
        [Tag.parse_strict(s, kind=kind) for s in tags] if tags else []
    )
    parsed_remove: list[Tag] = (
        [Tag.parse_strict(s, kind=kind) for s in untags] if untags else []
    )

    def _write(c: Any) -> tuple[int, int]:
        n_added = 0
        n_removed = 0
        for tag in parsed_add:
            store.add_tag(
                ref_id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
                expires_at=resolved_expires,
                conn=c,
            )
            n_added += 1
        for tag in parsed_remove:
            # ``remove_tag`` is silent on misses — a value-mismatch
            # ``untags=['STATUS:open']`` against a STATUS:done row
            # is a no-op. Counter ticks optimistically; see the
            # NumericRefHandler tests for the established contract.
            store.remove_tag(ref_id, tag, conn=c)
            n_removed += 1
        return n_added, n_removed

    # Reuse the caller's transaction when given one; otherwise open
    # our own so the add/remove batch stays atomic.
    if conn is not None:
        return _write(conn)
    with store.tx() as own_conn:
        return _write(own_conn)


def format_link_tag_ack(
    *,
    kind: str,
    ref_label: str,
    n_links_added: int,
    n_links_removed: int,
    n_tags_added: int,
    n_tags_removed: int,
) -> str:
    """Render a one-line ack summarising what changed.

    Used by the read-only handlers that gain link/tag CRUD via
    these helpers so their put-response wording is consistent.
    Empty operations are dropped from the line so an
    ``unlink``-only call doesn't lie about adding things.
    """

    def _pluralise(n: int, noun: str) -> str:
        # English plural-s — the previous format emitted ``+2 tag``
        # and ``+2 link`` which read as typos to a 7B caller (MCP
        # critic NIT 2026-05-02).
        return f"{noun}" if n == 1 else f"{noun}s"

    parts: list[str] = []
    if n_links_added:
        parts.append(f"+{n_links_added} {_pluralise(n_links_added, 'link')}")
    if n_links_removed:
        parts.append(f"-{n_links_removed} {_pluralise(n_links_removed, 'link')}")
    if n_tags_added:
        parts.append(f"+{n_tags_added} {_pluralise(n_tags_added, 'tag')}")
    if n_tags_removed:
        parts.append(f"-{n_tags_removed} {_pluralise(n_tags_removed, 'tag')}")
    if not parts:
        # No-op — the handler should have rejected before reaching
        # this point, but render something sensible anyway.
        return f"updated {kind} {ref_label} (no changes)"
    return f"updated {kind} {ref_label}: {', '.join(parts)}"


__all__ = [
    "apply_link_ops",
    "apply_tag_ops",
    "check_relation_constraints",
    "format_link_tag_ack",
    "guard_and_route_contradicts_disputes",
    "guard_taxon_hierarchy",
    "require_link_target",
    "require_tag_ops",
    "validate_link_mode",
    "validate_relation",
]
