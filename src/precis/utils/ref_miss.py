"""Typed miss for a kind-scoped numeric-id lookup (gr593339).

``refs.ref_id`` is one global sequence across kinds, so a miss on
``get(kind='todo', id=N)`` is often not a missing row — N names a
live (or tombstoned) ref of *another* kind, which is the caller
pasting a foreign id (a gripe id into a todo read). A bare "not
found" sends the session repeating the identical call or searching
for an id it already holds; naming the real kind is the recovery.
The numeric-ref handler's ``_resolve_live_ref`` and todo's
``view='tree'`` root check raise through here so the two miss
shapes cannot drift. ``delete`` keeps its own wrapper on top —
its ack must also state that nothing was deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from precis.errors import Gone, NotFound
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store


def raise_ref_miss(
    store: Store,
    *,
    kind: str,
    ref_id: int,
    sense: str | None = None,
    list_view: str | None = None,
) -> NoReturn:
    """Raise for ``<kind> id=<ref_id>`` once the kind-scoped live and
    tombstone probes have both missed. Never returns.

    ``sense`` is the message noun the caller's kind renders ("todo",
    "memory", …); defaults to ``kind``. ``list_view`` is an
    ``id='/<view>'`` listing the caller vouches for (the handler's
    ``_supported_list_views()``); when given, the truly-absent
    ``next=`` offers it alongside search as the zero-input recovery.

    Outcomes, by what the global id space holds for ``ref_id``:

    - a live ref of another kind → ``NotFound`` naming that kind
      (and its handle), with the working ``get`` for it;
    - a tombstoned ref (of any kind) → ``Gone`` — it existed and was
      deleted — with the ``view='retired'`` tombstone read;
    - no row at all → plain ``NotFound`` with the list/search doors.
    """
    noun = sense or kind
    foreign = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if foreign is not None:
        if foreign.retired_at is not None:
            raise Gone(
                f"{noun} id={ref_id} not found - that id was a "
                f"{foreign.kind!r} ref, soft-deleted",
                next=f"get(kind={foreign.kind!r}, id={ref_id}, view='retired')",
            )
        handle = handle_registry.try_format(foreign.kind, foreign.id)
        suffix = f" ({handle})" if handle else ""
        raise NotFound(
            f"{noun} id={ref_id} not found - that id is a live "
            f"{foreign.kind!r} ref{suffix}",
            next=(
                f"get(kind={foreign.kind!r}, id={ref_id}) to inspect it, "
                f"or search(kind={kind!r}, q='...') for the right id"
            ),
        )
    doors = [f"search(kind={kind!r}, q='...') to find existing"]
    if list_view is not None:
        doors.insert(0, f"get(kind={kind!r}, id='/{list_view}') to list existing ids")
    raise NotFound(f"{noun} id={ref_id} not found", next=doors)
