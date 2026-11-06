"""Publish an energy/current budget from a workspace script (pcb-design-flow).

The budget MODEL stays plain Python in the caller's script; this module only
does the two writes that follow it: budget tables into a design draft (so an
embedded figure re-renders from them) and each rail's ``est_current_a`` onto
the pcb's nets (the hand-off to post-route widening).
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput


def publish(
    hub: Any,
    *,
    draft: str,
    pcb: str,
    tables: dict[str, dict],
    nets: dict[str, float],
) -> dict:
    """Publish budget ``tables`` into ``draft`` and ``nets`` onto ``pcb``.

    ``tables`` is an ORDERED ``{caption: {"header": [...], "rows": [...]}}``.
    A draft table chunk whose caption matches is replaced IN PLACE (the
    chunk id survives, so a figure's ``plots`` link holds and the figure
    re-renders from the new content); otherwise the table is appended.
    ``nets`` is ``{net_name: est_current_a}``; every name must already exist
    on the board.

    Gating: the caller's ``assert``-ed budget margins gate the publish -- a
    failing assert raises in the model before ``publish`` is ever called, so
    nothing is written. ``publish`` itself validates every input (draft, pcb,
    net names, table shape) before any write, so an invalid call also writes
    nothing.

    Returns ``{"draft", "tables": {"created", "updated"}, "nets"}``.
    """
    from precis.handlers.draft import DraftHandler

    store = hub.live_store
    dref = store.get_ref(kind="draft", id=draft)
    if dref is None:
        raise BadInput(
            f"draft {draft!r} not found",
            next="create it first: put(kind='draft', id='<slug>', title='…')",
        )

    pref = None
    if nets:
        pref = store.get_ref(kind="pcb", id=pcb)
        if pref is None:
            raise BadInput(
                f"pcb {pcb!r} not found",
                next="check the slug: search(kind='pcb', q='<name>')",
            )
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM pcb_nets WHERE ref_id = %s AND retired_at IS NULL",
                (pref.id,),
            ).fetchall()
        known = {str(r[0]) for r in rows}
        unknown = sorted(n for n in nets if n not in known)
        if unknown:
            raise BadInput(
                f"net(s) not on pcb {pcb!r}: {', '.join(unknown)}",
                next="add the net to the board first (put(kind='pcb', ...)), "
                "or fix the name",
            )

    for caption, table in tables.items():
        if not isinstance(table, dict) or "header" not in table or "rows" not in table:
            raise BadInput(
                f"table {caption!r} needs 'header' and 'rows'",
                next='pass tables={caption: {"header": [...], "rows": [[...]]}}',
            )

    # -- all inputs valid: write ------------------------------------------
    d = DraftHandler(hub=hub)
    existing: dict[str, Any] = {}
    for c in store.drafts.reading_order(dref.id):
        if c.chunk_kind == "table":
            cap = (c.meta or {}).get("caption")
            if cap is not None:
                existing.setdefault(cap, c)
    created: list[str] = []
    updated: list[str] = []
    for caption, table in tables.items():
        chunk = existing.get(caption)
        if chunk is not None:
            d.edit(id="dc" + str(chunk.chunk_id), table=table, caption=caption)
            updated.append(caption)
        else:
            d.put(
                id=draft,
                chunk_kind="table",
                table=table,
                caption=caption,
                at={"last": True},
            )
            created.append(caption)

    if nets and pref is not None:
        # pcb_apply is the public store path: a re-put of an existing net
        # PATCHES est_current_a (see its docstring), no private helper needed.
        store.pcb_apply(
            slug=pcb,
            title=pref.title,
            components=[],
            connections=[],
            nets=[{"name": n, "est_current_a": a} for n, a in nets.items()],
        )

    return {
        "draft": draft,
        "tables": {"created": created, "updated": updated},
        "nets": dict(nets),
    }
