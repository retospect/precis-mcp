"""Shared folder-placement helpers (ADR 0045).

Placement is the *extrinsic* organization axis: an artifact ref sits
in at most one ``kind='folder'`` container via ``refs.parent_id`` —
the same column the todo tree uses, addressed through the same
reserved virtual ``parent`` relation (ADR 0027, generalized here).
Each placeable handler's ``link()`` intercepts ``rel='parent'`` and
routes to :func:`place_ref`; every other relation falls through to
the stored-link machinery unchanged.

The todo handler does NOT route here — its ``_reparent`` carries
todo-specific guards (owner-only, level gradient, subtree depth) and
only shares the underlying ``Store.set_parent`` column write.

Guards are deliberately thin: the target must be a live folder, and
no cycle may form (only reachable when the child is itself a folder,
but the ancestry walk is cheap and generic). Folder-depth discipline
(shallow, 1-2 levels) is policy taught by ``precis-folder-help``,
not a hard guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound
from precis.handlers._link_tag_ops import validate_link_mode
from precis.handlers._link_target import parse_link_target
from precis.response import Response
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store

#: The reserved virtual relation (ADR 0027). Never in the ``Relation``
#: vocabulary or the ``relations`` table — a façade over
#: ``refs.parent_id``, intercepted per-kind before vocabulary
#: validation.
RESERVED_PARENT_REL = "parent"


def check_parent_is_folder(store: Store, parent_id: int) -> None:
    """Raise unless ``parent_id`` is a live ``kind='folder'`` ref.

    The placement counterpart of ``_todo_guards.check_parent_exists``
    (which enforces todo-or-folder parents for the todo tree): for
    every *other* placeable kind, the only legal parent is a folder.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, deleted_at FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()
    if row is None or row[1] is not None:
        raise NotFound(
            f"folder id={parent_id} not found",
            next="get(kind='folder') to list folders",
        )
    if row[0] != "folder":
        raise BadInput(
            f"placement target id={parent_id} is a {row[0]!r} ref, not a folder",
            next=(
                "rel='parent' places into a folder - pass target='folder:N' "
                "(put(kind='folder', text='<name>') creates one)"
            ),
        )


def check_no_placement_cycle(store: Store, *, child_id: int, parent_id: int) -> None:
    """Reject a placement that would make a folder contain an ancestor.

    Same recursive walk as the todo tree's cycle guard, with
    kind-neutral wording. Only a folder child can actually form a
    cycle (non-folder artifacts have no children in the placement
    sense), but the check is cheap and generic so callers never have
    to reason about it.
    """
    if child_id == parent_id:
        raise BadInput(
            f"ref id={child_id} cannot be placed inside itself",
            next="pick a different folder",
        )
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT ref_id, parent_id FROM refs WHERE ref_id = %s
                UNION ALL
                SELECT r.ref_id, r.parent_id
                  FROM refs r
                  JOIN ancestors a ON r.ref_id = a.parent_id
            )
            SELECT 1 FROM ancestors WHERE ref_id = %s LIMIT 1
            """,
            (parent_id, child_id),
        ).fetchone()
    if row is not None:
        raise BadInput(
            f"cycle: folder id={parent_id} is already inside ref id={child_id}",
            next="pick a folder that is not under this one",
        )


def _handle_for(kind: str, ref: Ref) -> str:
    """Best available display handle for a ref (uhandle > kind:ident)."""
    ident = ref.slug if ref.slug is not None else str(ref.id)
    return handle_registry.try_format(kind, ref.id) or f"{kind}:{ident}"


def place_ref(
    store: Store,
    *,
    kind: str,
    ref: Ref,
    target: str | None,
    mode: str = "add",
) -> Response:
    """Apply a ``rel='parent'`` placement move / unfile for ``ref``.

    * ``mode='add'``    — place ``ref`` into ``target`` (a ``folder:N``).
    * ``mode='remove'`` — unfile to top level (``parent_id=NULL``);
      an optional ``target=`` must name the *current* folder so a
      stale request can't silently unfile from a different one.
    """
    validate_link_mode(mode)
    child_handle = _handle_for(kind, ref)

    if mode == "remove":
        if target is not None:
            claimed = parse_link_target(target, store=store)
            if ref.parent_id != claimed.ref_id:
                raise BadInput(
                    f"{child_handle} is not in {target!r} (current folder: "
                    f"{'#' + str(ref.parent_id) if ref.parent_id else 'none'})",
                    next="omit target= to unfile, or pass the actual current folder",
                )
        store.set_parent(ref.id, None)
        return Response(body=f"unfiled {child_handle} (no folder)")

    if target is None:
        raise BadInput(
            f"link(kind={kind!r}, id=..., rel='parent') requires target=",
            next=(
                f"link(kind={kind!r}, id=..., target='folder:N', rel='parent') "
                "- get(kind='folder') lists folders"
            ),
        )
    parent = parse_link_target(target, store=store)
    check_parent_is_folder(store, parent.ref_id)
    check_no_placement_cycle(store, child_id=ref.id, parent_id=parent.ref_id)
    store.set_parent(ref.id, parent.ref_id)
    folder_handle = handle_registry.try_format("folder", parent.ref_id) or (
        f"folder:{parent.ref_id}"
    )
    return Response(body=f"placed {child_handle} in {folder_handle}")
