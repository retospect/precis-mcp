"""gr633674: a ``Reto <date>:`` ruling that moves a gripe off ``needs_spec``
sheds ``reto:ask`` in the same write."""

from __future__ import annotations

from typing import Any

import pytest

from precis.store.types import Tag

pytestmark = pytest.mark.db


def _gripe(store: Any, status: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title="x", meta={}, conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
        store.add_tag(ref.id, Tag.open("reto:ask"), set_by="system", conn=conn)
    return int(ref.id)


def _tags(store: Any, gid: int) -> set[str]:
    return {str(t) for t in store.tags_for(gid)}


def test_ruling_off_needs_spec_drops_ask(runtime_with_store: Any) -> None:
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "needs_spec")
    h.put(id=gid, text="Reto 2026-11-11: go with A", tags=["STATUS:triaged"])
    got = _tags(store, gid)
    assert "reto:ask" not in got
    assert "STATUS:triaged" in got


def test_plain_comment_keeps_ask(runtime_with_store: Any) -> None:
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "needs_spec")
    h.put(id=gid, text="Reto asked about it", tags=["STATUS:triaged"])
    assert "reto:ask" in _tags(store, gid)
