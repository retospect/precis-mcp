"""MemoryHandler — through both direct invocation and full dispatch."""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store

# ---------------------------------------------------------------------------
# Handler-level tests (skip MCP, skip dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(store: Store) -> MemoryHandler:
    return MemoryHandler(store=store)


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
    r = handler.put(text="memory with tags")
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.put(id=new_id, tags=["kind:decision", "CONFIDENCE:tentative"])

    got = handler.get(id=new_id)
    assert "kind:decision" in got.body
    assert "CONFIDENCE:tentative" in got.body


def test_update_replaces_closed_prefix(handler: MemoryHandler) -> None:
    """CONFIDENCE:certain replaces previous CONFIDENCE:* per skill semantics."""
    r = handler.put(text="x", tags=["CONFIDENCE:tentative"])
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.put(id=new_id, tags=["CONFIDENCE:certain"])

    got = handler.get(id=new_id)
    assert "CONFIDENCE:certain" in got.body
    assert "CONFIDENCE:tentative" not in got.body


def test_update_no_changes_raises(handler: MemoryHandler) -> None:
    r = handler.put(text="x")
    new_id = int(r.body.rsplit("=", 1)[1])
    with pytest.raises(BadInput, match="at least one"):
        handler.put(id=new_id)


def test_delete(handler: MemoryHandler) -> None:
    r = handler.put(text="goodbye")
    new_id = int(r.body.rsplit("=", 1)[1])

    deleted = handler.put(id=new_id, mode="delete")
    assert "deleted" in deleted.body

    with pytest.raises(NotFound):
        handler.get(id=new_id)


def test_delete_missing_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.put(id=99999, mode="delete")


def test_search_finds_match(handler: MemoryHandler) -> None:
    handler.put(text="nitrate reduction on copper")
    handler.put(text="something completely different")

    r = handler.search(q="nitrate copper")
    assert "nitrate" in r.body
    assert "1 memory" in r.body


def test_search_no_match(handler: MemoryHandler) -> None:
    handler.put(text="hello world")
    r = handler.search(q="frobnicate")
    assert "no memories match" in r.body


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
    handler = runtime_with_store.registry.get("memory")
    assert handler.spec.is_numeric is True
    assert handler.spec.supports_get is True
    assert handler.spec.supports_search is True
    assert handler.spec.supports_put is True
    assert handler.spec.supports_move is False
