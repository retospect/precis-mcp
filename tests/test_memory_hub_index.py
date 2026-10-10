"""Hub ancestry (detail nodes under a summary node) and the SessionStart index
over a ``part-of`` graph."""

from __future__ import annotations

from pathlib import Path

from precis.cli.memory import (
    SPACE_TAG,
    _created_id,
    export_memory_nodes,
    render_memory_index,
)
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.utils import handle_registry
from precis.utils.memory_hubs import hubs_of, link_hints

HUB = "section:index"


def _h(ref_id: int) -> str:
    h = handle_registry.try_format("memory", ref_id)
    assert h is not None
    return h


def _note(
    handler: MemoryHandler, title: str, body: str, *tags: str, hook: str | None = None
) -> int:
    return _created_id(
        handler.put(
            text=body,
            title=title,
            tags=[SPACE_TAG, *tags],
            meta={"hook": hook} if hook else None,
        )
    )


def _part_of(store: Store, member: int, parent: int) -> None:
    store.add_link(src_ref_id=member, dst_ref_id=parent, relation="part-of")


def test_hub_is_the_nearest_section_index_ancestor(hub: Hub, tmp_path: Path) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Pumping hub", "x", HUB)
    sub = _note(handler, "Sub hub", "x", HUB)
    summary = _note(handler, "Summary", "s", "section:threads")
    detail = _note(handler, "Detail", "d", "section:threads")
    deeper = _note(handler, "Deeper", "d", "section:threads")
    loose = _note(handler, "Pumping loose note", "l", "section:threads")
    _part_of(store, sub, root)
    _part_of(store, summary, sub)
    _part_of(store, detail, summary)
    _part_of(store, deeper, detail)

    got = hubs_of(store, [summary, detail, deeper, loose, sub])
    assert got == {summary: [sub], detail: [sub], deeper: [sub], sub: [root]}

    # no hub suggestion for a node whose part-of chain already reaches one
    assert link_hints(handler, detail) == []
    assert link_hints(handler, loose) != []

    export_memory_nodes(store, tmp_path / "n")
    rows = {
        ln.split("\t")[0]: ln.split("\t")
        for ln in (tmp_path / "n" / "_sections.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    # columns 9 (hub = ancestor) and 12 (parent = direct)
    assert rows[_h(detail)][8] == _h(sub) and rows[_h(detail)][11] == _h(summary)
    assert rows[_h(loose)][8] == "" and rows[_h(loose)][11] == ""


def test_index_groups_by_hub_with_type_prefix_nesting_and_recency(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    sub = _note(handler, "Pumps", "x", HUB)
    _part_of(store, sub, root)
    old = _note(handler, "Old gotcha", "o", "section:gotchas", hook="old hook")
    new = _note(handler, "New thread", "n", "section:threads")
    child = _note(handler, "Thread detail", "c", "section:threads")
    top = _note(handler, "Top level", "t", "section:reference")
    orphan = _note(handler, "Orphan", "o")
    for m in (old, new):
        _part_of(store, m, sub)
    _part_of(store, child, new)
    _part_of(store, top, root)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '9 days' WHERE ref_id = %s",
            (old,),
        )

    lines = render_memory_index(store).splitlines()

    assert "## Pumps" in lines
    assert not any(ln.startswith("## Root") for ln in lines)  # root: no header
    pumps = lines.index("## Pumps")
    assert lines[pumps + 2].startswith(f"- [thread] New thread ({_h(new)})")
    assert lines[pumps + 3].startswith(f"  - [thread] Thread detail ({_h(child)})")
    assert lines[pumps + 4].startswith(f"- [gotcha] Old gotcha ({_h(old)}) — old hook")
    # the root's direct member sits before the first header; no hub -> Unfiled
    first_header = next(i for i, ln in enumerate(lines) if ln.startswith("## "))
    assert any(
        f"[reference] Top level ({_h(top)})" in ln for ln in lines[:first_header]
    )
    unfiled = lines.index("## Unfiled")
    assert f"Orphan ({_h(orphan)})" in lines[unfiled + 2]
