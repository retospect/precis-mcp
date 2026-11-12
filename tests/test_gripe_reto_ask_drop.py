"""gr633674 / gr633601: an answered ask sheds ``reto:ask`` in the same
write — a ``Reto <date>:`` ruling comment (any status), or a gripe
leaving ``needs_spec`` (put's tag leg and the bare ``tag`` verb alike).
"""

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


def test_ruling_comment_alone_drops_ask(runtime_with_store: Any) -> None:
    """gr633601: the ruling answers the ask wherever the gripe sits —
    no STATUS move needed (gr562902's triaged + reto:ask)."""
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "triaged")
    h.put(id=gid, text="Reto 2026-11-12: ship the small fix")
    got = _tags(store, gid)
    assert "reto:ask" not in got
    assert "STATUS:triaged" in got


def test_leaving_needs_spec_drops_ask_without_ruling(
    runtime_with_store: Any,
) -> None:
    """gr633601: the spec-wait is over — a move off needs_spec sheds
    the pending ask even when the comment isn't a ruling."""
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "needs_spec")
    h.put(
        id=gid,
        text="spec landed in docs/backlog/x.md",
        tags=["STATUS:triaged"],
    )
    got = _tags(store, gid)
    assert "reto:ask" not in got
    assert "STATUS:triaged" in got


def test_tag_move_off_needs_spec_drops_ask(runtime_with_store: Any) -> None:
    """The bare ``tag`` verb drops too — the web detail control moves
    ``needs_spec`` → ``triaged`` through it, not through ``put``."""
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "needs_spec")
    h.tag(id=gid, add=["STATUS:triaged"])
    got = _tags(store, gid)
    assert "reto:ask" not in got
    assert "STATUS:triaged" in got


def test_status_move_off_triaged_keeps_ask(runtime_with_store: Any) -> None:
    """Only the needs_spec leave sheds the tag: a triaged gripe moving
    on with its ask still pending keeps it for the ruling."""
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "triaged")
    h.tag(id=gid, add=["STATUS:fixing"])
    assert "reto:ask" in _tags(store, gid)


def test_plain_comment_keeps_ask(runtime_with_store: Any) -> None:
    """Neither a ruling nor a status move → the pending ask stays."""
    store = runtime_with_store.hub.live_store
    h = runtime_with_store.hub.handlers["gripe"]
    gid = _gripe(store, "needs_spec")
    h.put(id=gid, text="Reto asked about it")
    assert "reto:ask" in _tags(store, gid)
    assert "STATUS:needs_spec" in _tags(store, gid)
