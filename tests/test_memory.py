"""MemoryHandler — through both direct invocation and full dispatch."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime

# ---------------------------------------------------------------------------
# Handler-level tests (skip MCP, skip dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def test_create_returns_id(handler: MemoryHandler) -> None:
    r = handler.put(text="my first memory")
    assert "created memory id=" in r.body


def test_create_requires_text(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.put()


def test_create_then_get(handler: MemoryHandler) -> None:
    r = handler.put(text="findable memory body")
    # parse out the id
    new_id = int(r.body.rsplit("=", 1)[1])

    got = handler.get(id=new_id)
    assert "findable memory body" in got.body
    assert f"memory {new_id}" in got.body


def test_get_unknown_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id=99999)


def test_get_string_id_coerced(handler: MemoryHandler) -> None:
    r = handler.put(text="numeric coercion")
    new_id = int(r.body.rsplit("=", 1)[1])
    got = handler.get(id=str(new_id))
    assert "numeric coercion" in got.body


def test_get_bad_id(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="integer"):
        handler.get(id="not-a-number")


def test_update_text(handler: MemoryHandler) -> None:
    r = handler.put(text="original")
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.put(id=new_id, text="updated")

    got = handler.get(id=new_id)
    assert "updated" in got.body
    assert "original" not in got.body


def test_update_tags_only(handler: MemoryHandler) -> None:
    # The closed-axis allow-list for memory is empty (per-kind axis
    # enforcement) — memory uses only open tags. Demonstrate updating
    # a memory with two open tags from the documented vocabulary.
    r = handler.put(text="memory with tags")
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.tag(id=new_id, add=["kind:decision", "confidence-strong"])

    got = handler.get(id=new_id)
    assert "kind:decision" in got.body
    assert "confidence-strong" in got.body


def test_update_open_tags_accumulate(handler: MemoryHandler) -> None:
    """Open tags accumulate (no axis-replacement contract). Two
    confidence-* tags can coexist — the agent is responsible for
    untagging the old value, exactly the pattern documented in
    ``precis-memory-help`` for the open-tag confidence axis."""
    r = handler.put(text="x", tags=["confidence-tentative"])
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.tag(id=new_id, add=["confidence-strong"])

    got = handler.get(id=new_id)
    # Both stick around — that's the open-tag contract. The
    # remove-on-update workflow uses ``untags=`` (see
    # test_untags_on_put.py).
    assert "confidence-tentative" in got.body
    assert "confidence-strong" in got.body


def test_status_axis_rejected_on_memory(handler: MemoryHandler) -> None:
    """Per-kind axis enforcement: STATUS: belongs on todo/gripe/quest,
    not on memory. The MCP critic flagged ``STATUS:open`` on a memory
    as a smell — the tag is decorative because no STATUS-filtered
    query against ``kind='memory'`` can find it. Reject at the write
    boundary instead."""
    r = handler.put(text="m")
    new_id = int(r.body.rsplit("=", 1)[1])
    with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
        handler.tag(id=new_id, add=["STATUS:open"])


def test_update_no_changes_raises(handler: MemoryHandler) -> None:
    r = handler.put(text="x")
    new_id = int(r.body.rsplit("=", 1)[1])
    with pytest.raises(BadInput, match="at least one"):
        handler.put(id=new_id)


def test_delete(handler: MemoryHandler) -> None:
    r = handler.put(text="goodbye")
    new_id = int(r.body.rsplit("=", 1)[1])

    deleted = handler.delete(id=new_id)
    assert "deleted" in deleted.body

    with pytest.raises(NotFound):
        handler.get(id=new_id)


def test_delete_missing_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.delete(id=99999)


def test_search_finds_match(handler: MemoryHandler) -> None:
    handler.put(text="nitrate reduction on copper")
    handler.put(text="something completely different")

    r = handler.search(q="nitrate copper")
    assert "nitrate" in r.body
    assert "1 memory" in r.body


def test_search_no_match(handler: MemoryHandler) -> None:
    handler.put(text="hello world")
    r = handler.search(q="frobnicate")
    assert "no memory entries match" in r.body


def test_search_requires_q(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()
    with pytest.raises(BadInput):
        handler.search(q="   ")


# ---------------------------------------------------------------------------
# Through the runtime dispatcher
# ---------------------------------------------------------------------------


def test_runtime_create_memory(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("put", {"kind": "memory", "text": "via dispatch"})
    assert "created memory id=" in out


def test_runtime_create_then_get(runtime_with_store: PrecisRuntime) -> None:
    create = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "round trip"}
    )
    new_id = int(create.rsplit("=", 1)[1].split()[0])

    got = runtime_with_store.dispatch("get", {"kind": "memory", "id": new_id})
    assert "round trip" in got


def test_runtime_search_memory(runtime_with_store: PrecisRuntime) -> None:
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kwargs vs modes decision"}
    )
    out = runtime_with_store.dispatch("search", {"kind": "memory", "q": "kwargs"})
    assert "kwargs" in out


def test_runtime_unknown_memory_renders_error(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": 99999})
    assert "[error:NotFound]" in out
    assert "next:" in out


def test_kindspec_supports_get_search_put(runtime_with_store: PrecisRuntime) -> None:
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler.spec.is_numeric is True
    assert handler.spec.supports_get is True
    assert handler.spec.supports_search is True
    assert handler.spec.supports_put is True
    assert handler.spec.supports_move is False
