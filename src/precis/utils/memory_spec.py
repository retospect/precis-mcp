"""Spec-root placement audit (gr631564).

``precis-spec-help``: every spec root — a memory tagged ``spec:<slug>``
and ``section:index`` — is ``part-of`` a programme root under the
Specifications hub ``me568215``; topical hubs relate but never parent.
Nothing enforced it, so seven spec roots landed under topical hubs
(CAD/se ``me477659``, Chemistry ``me477660``) before a manual refactor.

This is the write-path half: after a ``put`` / ``link`` / ``tag`` write
that could change spec-root-ness or membership, a spec root is stamped
``AUDIT:spec-root-parent`` (system-set, so ``search(kind='memory',
tags=['AUDIT:spec-root-parent'])`` enumerates them) unless it has
exactly one live ``part-of`` parent — a programme root, a direct
``part-of`` child of ``me568215`` — and the ack carries an advisory
line. A spec hung directly off the hub, under another spec root, or
with a second topical parent all violate: the chain reaching the hub is
not enough. A clean re-check clears the flag; an author-set row
is never touched. Advisory only, never a write gate — the same contract
as the shape audit (``precis.handlers._memory_shape``): a store fault
fails open and changes no tag. ``scripts/memory-lint`` check 15 runs
the same rule offline against the export manifest's ``spec`` and
``parent`` columns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from precis.store import Tag
from precis.utils.memory_hubs import parents_of

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The Specifications hub — a spec root's single ``part-of`` parent is
#: one of its direct ``part-of`` children, a programme root
#: (``precis-spec-help``; Reto's ruling me568168).
SPECIFICATIONS_HUB_ID = 568215

#: The ``AUDIT`` code stamped on a mis-placed spec root.
AUDIT_SPEC_ROOT_PARENT = "spec-root-parent"

_AUDIT_TAG = Tag.closed("AUDIT", AUDIT_SPEC_ROOT_PARENT)

_HUB_TAG = "section:index"
_SPEC_TAG = "spec:"


def spec_root_slug(values: Iterable[str]) -> str | None:
    """The slug of a spec root — ``section:index`` plus a ``spec:<slug>``
    tag — else ``None``. Every spec *section* tags ``spec:<slug>`` too,
    but carries ``section:reference``: only the index node is a root, so
    a section linked under its spec root never audits."""
    tags = set(values)
    if _HUB_TAG not in tags:
        return None
    return next(
        (
            v[len(_SPEC_TAG) :]
            for v in sorted(tags)
            if v.startswith(_SPEC_TAG) and len(v) > len(_SPEC_TAG)
        ),
        None,
    )


def _handle(store: Store, ref_id: int) -> str:
    from precis.utils import handle_registry

    return handle_registry.try_format("memory", ref_id) or f"me{ref_id}"


def _is_programme_root(store: Store, ref_id: int) -> bool:
    """``ref_id`` is a programme root — a live direct ``part-of`` child of
    the Specifications hub. A chain through a retired node does not count
    (``parents_of`` filters to live parents)."""
    return SPECIFICATIONS_HUB_ID in parents_of(store, [ref_id]).get(ref_id, ())


def _apply_audit_tag(store: Store, ref_id: int, violated: bool) -> None:
    """Set/clear the system-owned flag; an author-set row is never
    touched (same ownership rule as the shape audit's tag maintenance)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT rt.set_by FROM ref_tags rt JOIN tags t "
            "ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s "
            "AND t.namespace = 'AUDIT' AND t.value = %s",
            (ref_id, AUDIT_SPEC_ROOT_PARENT),
        ).fetchone()
        if row is not None and row[0] != "system":
            return
        if violated:
            if row is None:
                store.add_tag(ref_id, _AUDIT_TAG, set_by="system", conn=conn)
        elif row is not None:
            store.remove_tag(ref_id, _AUDIT_TAG, conn=conn)


def spec_root_advisory(store: Store, ref_id: int) -> str | None:
    """Re-check a memory's spec-root placement after a write; maintain
    the ``AUDIT:spec-root-parent`` flag and return the ack advisory on a
    miss.

    ``None`` when the node is clean or not a spec root (a stale system
    flag is still cleared). Never raises — advisory only; a store fault
    logs and changes nothing.
    """
    try:
        tags = {str(t) for t in store.tags_for(ref_id)}
        slug = spec_root_slug(tags)
        if slug is None and str(_AUDIT_TAG) not in tags:
            return None
        live_parents: list[int] = []
        if slug is not None:
            live_parents = sorted(parents_of(store, [ref_id]).get(ref_id, []))
        violated = slug is not None and (
            len(live_parents) != 1 or not _is_programme_root(store, live_parents[0])
        )
        _apply_audit_tag(store, ref_id, violated)
        if not violated:
            return None
        me = _handle(store, ref_id)
        rule = (
            f"spec roots carry exactly one part-of parent, a programme "
            f"root of the Specifications hub me{SPECIFICATIONS_HUB_ID} "
            "(precis-spec-help)"
        )
        if not live_parents:
            detail = "has no part-of parent"
            how = (
                f"link(kind='memory', id='{me}', "
                "target='memory:<programme root>', rel='part-of')"
            )
        elif len(live_parents) > 1:
            names = ", ".join(_handle(store, p) for p in live_parents)
            detail = f"has {len(live_parents)} part-of parents ({names})"
            how = (
                f"unlink every extra one — link(kind='memory', id='{me}', "
                "target='memory:<extra parent>', rel='part-of', mode='remove')"
            )
        else:
            detail = (
                "is part-of the Specifications hub itself, not a programme root"
                if live_parents[0] == SPECIFICATIONS_HUB_ID
                else f"is part-of {_handle(store, live_parents[0])}, "
                "which is not a programme root of it"
            )
            how = (
                f"link(kind='memory', id='{me}', "
                "target='memory:<programme root>', rel='part-of')"
            )
        return (
            f"AUDIT:{AUDIT_SPEC_ROOT_PARENT}: spec root {me} "
            f"(spec:{slug}) {detail} — {rule}: {how}"
        )
    except Exception:
        log.warning("memory spec-root audit failed open for %s", ref_id, exc_info=True)
        return None
