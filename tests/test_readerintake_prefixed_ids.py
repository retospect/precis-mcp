"""Memory reader normalization must not reinterpret conversation slugs."""

from __future__ import annotations

from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.mark.parametrize("other_exists", [False, True])
@pytest.mark.parametrize("slug_exists", [False, True])
def test_prefixed_conv_co123_put_uses_literal_slug(
    runtime_with_store: PrecisRuntime,
    store: Store,
    other_exists: bool,
    slug_exists: bool,
) -> None:
    if other_exists:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO refs (ref_id, kind, title) VALUES (123, 'conv', 'B')"
            )
            store.insert_ref_identifiers(
                123, [("cite_key", "conversation-b", "test")], conn=conn
            )
    if slug_exists:
        store.insert_ref(kind="conv", slug="co123", title="A")

    result, failed = runtime_with_store.dispatch_with_status(
        "put", {"id": "conv:co123", "text": "hello"}
    )

    assert not failed, result
    target = store.get_ref(kind="conv", id="co123")
    assert target is not None
    assert [c.text for c in store.chunks.list_chunks_for_ref(target.id)] == ["hello"]
    if other_exists:
        assert target.id != 123
        assert store.chunks.list_chunks_for_ref(123) == []


@pytest.mark.parametrize("verb", ["put", "edit", "delete", "tag", "link"])
def test_prefixed_memory_writes_keep_original_identifier(
    runtime_with_store: PrecisRuntime, verb: str
) -> None:
    args: dict[str, Any] = {"id": "memory:me123"}
    runtime_with_store._maybe_split_prefixed_id(args, verb=verb)
    assert args == {"kind": "memory", "id": "me123"}
