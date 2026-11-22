"""gr681310: ``get(kind='memory', id='/review')`` — the lint/health view.

Ask (b) of gr681310: an agent had no MCP door for memories under review
(an open ``memory-review`` todo or gripe) or whose processing stalled.
``id='/review'`` renders both blocks; ``args={'hours': N}`` (default 6)
is the stall floor for the processing scan, which reads only the 50 most
recently edited memories.
"""

from __future__ import annotations

from importlib import resources

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils import handle_registry
from precis.utils.memory_hubs import REVIEW_TAG
from tests.conftest import id_of
from tests.test_memory_processing import BODY, _finish


def _put(hub: Hub, title: str) -> int:
    return id_of(MemoryHandler(hub=hub).put(text=BODY, title=title).body)


def _file_review_todo(store: Store, memory_id: int) -> str:
    """The ``memory-review`` todo the garden files, pointed at the memory."""
    todo = store.insert_ref(
        kind="todo", slug=None, title=f"review me{memory_id}: needs a look"
    )
    for tag in (
        Tag.parse_strict("STATUS:open", kind="todo"),
        Tag.open(REVIEW_TAG),
    ):
        store.add_tag(todo.id, tag, set_by="system")
    store.add_link(
        src_ref_id=todo.id,
        dst_ref_id=memory_id,
        relation="raises-concern-about",
        set_by="system",
    )
    return handle_registry.format_handle("todo", todo.id)


def test_review_view_lists_open_review_todo_and_pending_processing(
    store: Store, hub: Hub
) -> None:
    reviewed = _put(hub, "A node under review")
    td = _file_review_todo(store, reviewed)
    stalled = _put(hub, "A stalled node")
    processed = _put(hub, "A processed node")
    _finish(store, processed)
    h = MemoryHandler(hub=hub)

    out = h.get(id="/review", hours=0).body
    under, _, proc = out.partition("## Processing outstanding")
    assert "A node under review" in under
    assert f"me{reviewed} " in under
    assert td in under
    assert "A stalled node" not in under

    assert "A stalled node" in proc
    assert f"get(kind='memory', id='me{stalled}', view='processing')" in proc
    assert "A processed node" not in out

    # hours=6 is the default floor: a fresh write is expected to be
    # pending, so the stalled node drops out of the processing block.
    default = h.get(id="/review").body
    assert "A node under review" in default
    assert "A stalled node" not in default.split("Processing outstanding")[1]


def test_review_view_lists_open_gripe(store: Store, hub: Hub) -> None:
    from precis.handlers.gripe import GripeHandler

    reviewed = _put(hub, "A griped node")
    gid = id_of(
        GripeHandler(hub=hub)
        .put(
            text="this memory misled me",
            link=f"me{reviewed}",
            rel="raises-concern-about",
        )
        .body
    )
    out = MemoryHandler(hub=hub).get(id="/review").body
    under = out.split("## Processing outstanding")[0]
    assert "A griped node" in under
    assert f"gr{gid}" in under


def test_review_view_hours_through_the_args_gate(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """``args={'hours': N}`` rides the extras channel: the argument gate
    refuses an undeclared key as a typo."""
    _put(hub, "Stalled via extras")
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": "/review", "__extras__": {"hours": 0}}
    )
    assert "Stalled via extras" in out


def test_review_view_is_a_named_list_view(hub: Hub) -> None:
    assert "review" in MemoryHandler(hub=hub)._supported_list_views()


def test_hours_is_refused_off_the_review_view(store: Store, hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="hours= only applies"):
        h.get(id="/recent", hours=3)
    with pytest.raises(BadInput, match="hours= only applies"):
        h.get(id="/threads", hours=3)
    with pytest.raises(BadInput, match="hours= must be a non-negative integer"):
        h.get(id="/review", hours=-1)
    with pytest.raises(BadInput, match="hours= must be a non-negative integer"):
        h.get(id="/review", hours="later")
    with pytest.raises(BadInput, match="takes only args=\\{'hours'"):
        h.get(id="/review", hours=6, page=2)


def test_memory_skill_names_the_review_view() -> None:
    text = (
        resources.files("precis.data")
        .joinpath("skills/precis-memory-help.md")
        .read_text(encoding="utf-8")
    )
    assert "id='/review'" in text
