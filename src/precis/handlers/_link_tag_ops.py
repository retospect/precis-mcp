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

from typing import cast, get_args

from precis.errors import BadInput
from precis.handlers._link_target import parse_link_target
from precis.store import Store, Tag
from precis.store.types import Relation

# Mirror of the ``Relation`` literal's allowed values. Surfacing
# the tuple form here lets ``validate_relation`` enumerate options
# for the agent's BadInput hint without re-importing typing
# internals at every call.
_VALID_RELATIONS: tuple[str, ...] = get_args(Relation)
_DEFAULT_RELATION: Relation = "related-to"


def validate_relation(rel: str | None) -> Relation:
    """Validate ``rel=`` against the registered relations vocabulary.

    Returns the canonical default ``related-to`` when ``rel`` is
    ``None`` so callers can pass the result straight to
    :meth:`Store.add_link`. Raises ``BadInput`` with the full
    options list when an unknown relation is given — that's
    cheaper feedback than the FK violation the DB would otherwise
    return at insert time.
    """
    if rel is None:
        return _DEFAULT_RELATION
    if rel not in _VALID_RELATIONS:
        raise BadInput(
            f"unknown relation: {rel!r}",
            options=list(_VALID_RELATIONS),
            next=(
                "pick from the registered relations or omit rel= "
                f"for the default {_DEFAULT_RELATION!r}"
            ),
        )
    # Narrow ``str`` → ``Relation`` (Literal) for downstream type-
    # checkers; the membership check above is the runtime guarantee.
    return cast(Relation, rel)


def apply_link_ops(
    store: Store,
    src_ref_id: int,
    *,
    link: str | None,
    unlink: str | None,
    rel: str | None,
) -> tuple[int, int]:
    """Apply ``link=`` / ``unlink=`` operations against ``src_ref_id``.

    Returns ``(n_added, n_removed)`` so the calling handler can
    render an honest ack. ``parse_link_target`` resolves the
    string spec to a ``(ref_id, pos)`` pair via the store; bad
    targets raise ``BadInput`` before we touch any rows.

    Caller passes either ``link=`` (add) or ``unlink=`` (remove);
    the seven-verb ``link()`` method enforces that they're not both
    set at the call boundary.
    """
    relation = validate_relation(rel)

    n_added = 0
    n_removed = 0

    if link is not None:
        target = parse_link_target(link, store=store)
        store.add_link(
            src_ref_id=src_ref_id,
            dst_ref_id=target.ref_id,
            dst_pos=target.pos,
            relation=relation,
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
    """
    parsed_add: list[Tag] = (
        [Tag.parse_strict(s, kind=kind) for s in tags] if tags else []
    )
    parsed_remove: list[Tag] = (
        [Tag.parse_strict(s, kind=kind) for s in untags] if untags else []
    )

    n_added = 0
    n_removed = 0
    with store.tx() as conn:
        for tag in parsed_add:
            store.add_tag(
                ref_id,
                tag,
                set_by="agent",
                replace_prefix=(tag.namespace == "closed"),
                conn=conn,
            )
            n_added += 1
        for tag in parsed_remove:
            # ``remove_tag`` is silent on misses — a value-mismatch
            # ``untags=['STATUS:open']`` against a STATUS:done row
            # is a no-op. Counter ticks optimistically; see the
            # NumericRefHandler tests for the established contract.
            store.remove_tag(ref_id, tag, conn=conn)
            n_removed += 1

    return n_added, n_removed


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
    parts: list[str] = []
    if n_links_added:
        parts.append(f"+{n_links_added} link")
    if n_links_removed:
        parts.append(f"-{n_links_removed} link")
    if n_tags_added:
        parts.append(f"+{n_tags_added} tag")
    if n_tags_removed:
        parts.append(f"-{n_tags_removed} tag")
    if not parts:
        # No-op — the handler should have rejected before reaching
        # this point, but render something sensible anyway.
        return f"updated {kind} {ref_label} (no changes)"
    return f"updated {kind} {ref_label}: {', '.join(parts)}"


__all__ = [
    "apply_link_ops",
    "apply_tag_ops",
    "format_link_tag_ack",
    "validate_relation",
]
