"""The coder intake excludes promoted memory complaints in the DB query."""

from __future__ import annotations

from typing import Any

import pytest

from precis.fixer.intake import gripe_items
from precis.store import ChunkInsert, Store
from precis.store.types import Tag
from tests._refs import insert_gripe


def test_intake_skips_promoted_memory_gripe(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = insert_gripe(store, "Memory routes disagree")
    code = insert_gripe(store, "Code regression")
    for ref in (memory, code):
        store.add_tag(ref.id, Tag.open("auto-fix"), set_by="agent")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text="DIAGNOSIS: fix it",
                    meta={"chunk_kind": "gripe_comment"},
                )
            ],
        )
    store.add_tag(memory.id, Tag.open("project:memory"), set_by="agent")

    class _BorrowedStore:
        chunks = store.chunks

        def list_refs(self, **kwargs: Any) -> Any:
            return store.list_refs(**kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(Store, "connect", lambda *a, **kw: _BorrowedStore())
    items = gripe_items("unused")
    assert [item.ref_id for item in items] == [code.id]
