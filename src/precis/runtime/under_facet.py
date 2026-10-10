"""``search(kind=<any>, under=<taxon>)`` — instances under a taxon node.

The taxon half of the one ``under=``: ``DispatchMixin._resolve_under``
sends a taxon target here and any other handle to the ``part-of`` walk. It
resolves ``under=`` BEFORE kind dispatch into ``args['include_ref_ids']``
(the same channel ``cited=``/``hubbed=`` use), so every kind's normal search
ranking runs restricted to the id set; with no ``q=`` the set is listed
newest first.

Set = refs with an ``instance-of`` link into the descendant closure of the
node (the node itself included; ``axis=``/``depth=`` shape the closure walk).
A list ``under=`` intersects the per-node sets. ``search(kind='taxon',
under=)`` keeps its own meaning (the descendants, handled by the taxon
handler) and never comes through here.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput, NotFound
from precis.utils import handle_registry

#: The one next-hint every ``under=`` refusal carries (both trees).
UNDER_NEXT = (
    "under= takes a taxon ('tn42', 'taxon:42', 'technique/afm', or a list to "
    "intersect) for its instances, or any other handle ('me5', 'kind:id') for "
    "its part-of descendants"
)


def _taxon_id(hub: Any, token: Any) -> int:
    """Resolve one ``under=`` entry to a live taxon ref id, refusing a
    non-taxon target by name."""
    handler = hub.handler_for("taxon")
    if handler is None:
        raise BadInput("under= needs the taxon kind, which is not loaded here")
    store = hub.store
    s = str(token).strip()
    if ":" in s:
        prefix = s.split(":", 1)[0]
        if prefix != "taxon" and prefix in hub.kinds:
            raise BadInput(
                f"under={token!r} is a {prefix}, not a taxon",
                next=UNDER_NEXT,
            )
    parsed = handle_registry.parse(s) if not s.isdigit() else None
    if parsed is not None and parsed[0] != "taxon" and not parsed[1]:
        raise BadInput(
            f"under={token!r} is a {parsed[0]}, not a taxon",
            next=UNDER_NEXT,
        )
    rid = int(handler.resolve_node(token if not isinstance(token, bool) else s))
    ref = store.get_ref(kind="taxon", id=rid)
    if ref is None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT kind FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        kind = row[0] if row else None
        if kind and kind != "taxon":
            raise BadInput(
                f"under={token!r} is a {kind}, not a taxon",
                next=UNDER_NEXT,
            )
        raise NotFound(
            f"no taxon {token!r}",
            next=f"search(kind='taxon', q='...') to find the node; {UNDER_NEXT}",
        )
    return rid


def resolve_under(
    hub: Any, under: Any, *, axis: Any = None, depth: int | None = None
) -> tuple[str, list[int]]:
    """Resolve a taxon ``under=`` (one node or a list to intersect) to its
    instance set; return ``(note, ids)``. ``depth`` arrives validated by the
    dispatcher."""
    store = hub.store
    if axis is not None and (not isinstance(axis, str) or not axis.strip()):
        raise BadInput(
            f"axis must be a non-empty string, got {axis!r}", next="axis='method'"
        )
    tokens = list(under) if isinstance(under, (list, tuple)) else [under]
    if not tokens:
        raise BadInput(
            "under= is empty", next="under='technique/afm' or a list for intersection"
        )
    result: set[int] | None = None
    names: list[str] = []
    for tok in tokens:
        rid = _taxon_id(hub, tok)
        closure = [rid] + [
            d
            for d, _depth, _ax in store.taxon_descendants(
                rid, axis=axis, max_depth=depth
            )
        ]
        inst = store.taxon_instance_ids(closure)
        result = inst if result is None else result & inst
        ref = store.get_ref(kind="taxon", id=rid)
        names.append(
            handle_registry.format_handle("taxon", rid)
            + " "
            + str(((ref.meta or {}).get("name") if ref else None) or tok)
        )
    ids = sorted(result or set())
    scope = " ∩ ".join(names) + (f" axis={axis}" if axis else "")
    scope += f" depth<={depth}" if depth else ""
    return f"_(under {scope}: {len(ids)} instance{'s' if len(ids) != 1 else ''})_", ids
