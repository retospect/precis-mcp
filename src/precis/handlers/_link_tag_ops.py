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
from typing import Any, cast, get_args

from precis.errors import BadInput
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.store import Store, Tag
from precis.store.types import Relation

# Mirror of the ``Relation`` literal's allowed values. Surfacing
# the tuple form here lets ``validate_relation`` enumerate options
# for the agent's BadInput hint without re-importing typing
# internals at every call.
_VALID_RELATIONS: tuple[str, ...] = get_args(Relation)
_DEFAULT_RELATION: Relation = "related-to"


def validate_relation(rel: str | None, *, store: Store | None = None) -> Relation:
    """Validate ``rel=`` against the registered relations vocabulary.

    Returns the canonical default ``related-to`` when ``rel`` is
    ``None`` so callers can pass the result straight to
    :meth:`Store.add_link`. Raises ``BadInput`` with the full
    options list when an unknown relation is given — that's
    cheaper feedback than the FK violation the DB would otherwise
    return at insert time.

    Built-ins in the ``Relation`` literal are always accepted (fast,
    store-free path — keeps the typo-safety guarantee). When a
    ``store`` is supplied, an *unknown* slug is checked against the
    live ``relations`` table so a **plugin**-registered relation
    (seeded by the plugin's migration, e.g. autocatpath's ``pathway-node``
    or a chem route's ``consumes``/``produces``) is accepted without a
    core edit to the literal. The DB FK is the durable guard; this
    pre-flight just opens the friendly handler-layer check to plugins.
    """
    if rel is None:
        return _DEFAULT_RELATION
    if rel in _VALID_RELATIONS:
        # Narrow ``str`` → ``Relation`` (Literal) for downstream type-
        # checkers; the membership check above is the runtime guarantee.
        return cast(Relation, rel)
    if store is not None:
        extra = store.valid_relations()
        if rel not in extra:
            # Miss against the cache — re-read once in case a relation
            # was registered after the store's vocabulary was first read
            # (plugin migrated mid-process, or a test seeded one).
            extra = store.valid_relations(refresh=True)
        if rel in extra:
            return cast(Relation, rel)
        options = sorted({*_VALID_RELATIONS, *extra})
    else:
        options = list(_VALID_RELATIONS)
    raise BadInput(
        f"unknown relation: {rel!r}",
        options=options,
        next=(
            "pick from the registered relations or omit rel= "
            f"for the default {_DEFAULT_RELATION!r}"
        ),
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


def guard_and_route_contradicts_disputes(
    store: Store,
    src_ref_id: int,
    target: LinkTarget,
    relation: Relation,
) -> int | None:
    """Enforce the ``contradicts``/``disputes`` write-door policy for an
    add-mode link, and delegate a live claim-pair ``disputes`` to
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

    Two relations get extra routing on the add path:

    * ``contradicts`` — claim-graph ``contradicts`` is adjudication-derived
      only (Part 2); no agent-facing door can file it manually. The one
      exception is ``memory``<->``memory`` (a different subsystem, D2),
      which keeps working via the caller's plain write (this function
      returns ``None`` for that pair). Any other endpoint pair raises
      ``BadInput`` pointing at ``disputes``.
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
    if relation == "contradicts":
        kinds = _endpoint_kinds(store, src_ref_id, target.ref_id)
        if kinds.get(src_ref_id) != "memory" or kinds.get(target.ref_id) != "memory":
            raise BadInput(
                "claim-graph 'contradicts' is adjudication-derived and "
                "cannot be filed manually",
                next=(
                    "file rel='disputes' instead — free to file, "
                    "non-blocking (memory<->memory contradicts is "
                    "unaffected)"
                ),
            )
        return None
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

    The ``contradicts``/``disputes`` add-path guard and claim-pair
    delegation (docs/backlog/disputes-edge-nonblocking-disagreement.md
    D1-D4) live in :func:`guard_and_route_contradicts_disputes`, shared
    with every other add-mode link door.
    """
    relation = validate_relation(rel, store=store)

    n_added = 0
    n_removed = 0

    if link is not None:
        target = parse_link_target(link, store=store)
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
        target = parse_link_target(unlink, store=store)
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
    "format_link_tag_ack",
    "guard_and_route_contradicts_disputes",
    "require_link_target",
    "require_tag_ops",
    "validate_link_mode",
    "validate_relation",
]
