"""Spec-root placement audit (gr631564).

``precis-spec-help``: every spec root — a memory tagged ``spec:<slug>``
and ``section:index`` — is ``part-of`` a programme root under the
Specifications hub ``me568215``; topical hubs relate but never parent.
Nothing enforced it, so seven spec roots landed under topical hubs
(CAD/se ``me477659``, Chemistry ``me477660``) before a manual refactor.

This is the write-path half: after a ``put`` / ``link`` / ``tag`` write
that could change spec-root-ness or membership, a spec root whose live
``part-of`` chain never reaches ``me568215`` — or that has no live
``part-of`` parent at all — is stamped ``AUDIT:spec-root-parent``
(system-set, so ``search(kind='memory',
tags=['AUDIT:spec-root-parent'])`` enumerates them) and the ack carries
an advisory line. A clean re-check clears the flag; an author-set row
is never touched. Advisory only, never a write gate — the same contract
as the shape audit (``precis.handlers._memory_shape``): a store fault
fails open and changes no tag. ``scripts/memory-lint`` check 15 runs
the same reachability rule offline against the export manifest's
``spec`` column.
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

#: The Specifications hub — every spec root's ``part-of`` chain ends
#: here (``precis-spec-help``; Reto's ruling me568168). A programme root
#: is one of its direct ``part-of`` children.
SPECIFICATIONS_HUB_ID = 568215

#: The ``AUDIT`` code stamped on a mis-placed spec root.
AUDIT_SPEC_ROOT_PARENT = "spec-root-parent"

_AUDIT_TAG = Tag.closed("AUDIT", AUDIT_SPEC_ROOT_PARENT)

_HUB_TAG = "section:index"
_SPEC_TAG = "spec:"

#: Spec graphs are shallow (root → programme → hub); the cap only guards
#: a malformed cycle — same bound as ``memory_hubs.hubs_of``.
_MAX_DEPTH = 8


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


def _reaches_hub(store: Store, start: int) -> bool:
    """``start`` — or the live ``part-of`` chain above it — touches the
    Specifications hub. Live parents only (``parents_of``): a chain
    through a retired node does not count."""
    seen = {start}
    frontier = {start}
    for _ in range(_MAX_DEPTH):
        if SPECIFICATIONS_HUB_ID in frontier:
            return True
        frontier = {
            p for ps in parents_of(store, sorted(frontier)).values() for p in ps
        } - seen
        if not frontier:
            return False
        seen |= frontier
    return False


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
        bad: list[int] = []
        if slug is not None:
            live_parents = parents_of(store, [ref_id]).get(ref_id, [])
            bad = [p for p in live_parents if not _reaches_hub(store, p)]
        violated = slug is not None and (not live_parents or bool(bad))
        _apply_audit_tag(store, ref_id, violated)
        if not violated:
            return None
        me = _handle(store, ref_id)
        fix = (
            f"spec roots hang under a programme root of the "
            f"Specifications hub me{SPECIFICATIONS_HUB_ID} "
            f"(precis-spec-help): link(kind='memory', id='{me}', "
            "target='memory:<programme root>', rel='part-of')"
        )
        if not live_parents:
            detail = "has no part-of parent"
        else:
            names = ", ".join(_handle(store, p) for p in bad)
            detail = f"is part-of {names}, whose chain never reaches it"
        return (
            f"AUDIT:{AUDIT_SPEC_ROOT_PARENT}: spec root {me} "
            f"(spec:{slug}) {detail} — {fix}"
        )
    except Exception:
        log.warning("memory spec-root audit failed open for %s", ref_id, exc_info=True)
        return None
