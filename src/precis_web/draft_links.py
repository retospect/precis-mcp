"""In/out link-edges + anchored flags for one draft chunk.

The **shared data path** BOTH draft readers (classic ``/drafts`` and the
experimental ``/smartdraft``) consume — so a chunk's full connectivity
(inbound + outbound graph edges, plus any anchored "flag it in the draft"
change-request) is legible in both, not just one (gripe 178766). Before this,
``Store.chunk_connections`` already returned both directions per chunk, but
the classic reader's ``_connection_chips`` merged + discarded ``direction``,
and the smartdraft reader read neither connections nor anchored todos at all.

The readers must differ **only** in how they render this data (chip markup,
panel layout) — never in how they assemble it. Both call :func:`chunk_links`;
neither re-derives the in/out split or re-queries anchored todos itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store


def chunk_links(
    store: Store,
    ref_id: int,
    handle: str,
    *,
    conns: dict[str, list[dict[str, Any]]] | None = None,
    flags: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """``{links_out, links_in, flags}`` for chunk ``handle`` on ``ref_id``.

    ``links_out``/``links_in`` split ``store.chunk_connections``'s rows by
    ``direction`` — that store method already joins the ``links`` table both
    ways per chunk, so this does no new graph query, just the split
    ``_connection_chips`` used to discard. ``flags`` is
    ``store.anchored_todos`` — the change-request todos anchored directly at
    this chunk via ``meta.anchor``, which are **not** ``links`` rows at all
    (a standalone one has no project link and no job), so they're unioned in
    separately rather than folded into the edge lists.

    ``conns``/``flags`` accept a caller's own already-batched lookup (keyed
    by handle) so a per-row hydrate over a whole window of chunks (the
    classic reader) doesn't pay an N+1 query — pass the same dicts
    ``store.chunk_connections``/``store.anchored_todos`` already returned
    for the batch. Omit them for a single-chunk call (smartdraft's one-focus
    lookup) and this fetches for just ``handle``.
    """
    if conns is None:
        conns = store.drafts.chunk_connections(ref_id, [handle])
    if flags is None:
        flags = store.drafts.anchored_todos([handle])
    rows = conns.get(handle, [])
    return {
        "links_out": [c for c in rows if c.get("direction") == "out"],
        "links_in": [c for c in rows if c.get("direction") == "in"],
        "flags": flags.get(handle, []),
    }
