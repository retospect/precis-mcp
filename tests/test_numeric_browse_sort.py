"""gr468266: advertised browse sorts work through the native tool boundary."""

from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.tools import core


def _text(result: Any) -> str:
    return (
        result
        if isinstance(result, str)
        else "\n".join(c.text for c in result.content if c.type == "text")
    )


@pytest.mark.parametrize("kind", ["gripe", "todo", "quest"])
def test_priority_and_recency_pages(
    runtime_with_store: PrecisRuntime, monkeypatch, kind: str
) -> None:
    rt = runtime_with_store
    monkeypatch.setattr(core, "_get_runtime", lambda: rt)
    handler = rt.hub.handler_for(kind)
    assert handler is not None
    store = rt.hub.live_store
    for label, prio in [
        ("cold", 9),
        ("unset", None),
        ("default", 5),
        ("hot-old", 1),
        ("hot-new", 1),
    ]:
        handler.put(text=f"fixture-sort-{label}")
        rid = next(
            r.id
            for r in store.list_refs(kind=kind)
            if r.title == f"fixture-sort-{label}"
        )
        store.set_prio(rid, prio)
        handler.tag(id=rid, add=["fixture:r14-sort"])
    handler.put(text="fixture-sort-closed")
    closed = next(
        r.id for r in store.list_refs(kind=kind) if r.title == "fixture-sort-closed"
    )
    handler.tag(
        id=closed,
        add=[
            "fixture:r14-sort",
            "STATUS:dormant" if kind == "quest" else "STATUS:done",
        ],
    )
    store.set_prio(closed, 1)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = '2026-01-01' WHERE kind = %s", (kind,)
        )
    args: dict[str, Any] = dict(
        kind=kind,
        tags=["fixture:r14-sort"],
        status="active" if kind == "quest" else "open",
        page_size=2,
    )
    first = _text(core.search(**args, sort="prio"))
    assert "[error:" not in first
    assert first.index("fixture-sort-hot-new") < first.index("fixture-sort-hot-old")
    assert "fixture-sort-cold" not in first
    assert "fixture-sort-closed" not in first
    second = _text(core.search(**args, sort="prio", page=2))
    assert second.index("fixture-sort-default") < second.index("fixture-sort-unset")
    assert "fixture-sort-hot-new" not in second
    last = _text(core.search(**args, sort="prio", page=3))
    assert "fixture-sort-cold" in last
    recent = _text(core.search(**args, sort="recency"))
    assert "[error:" not in recent
    assert "by recency" in recent
    error = _text(core.search(**args, sort="bogus"))
    assert "sort='prio'" in error and "sort='recency'" in error
    assert "requires q=" not in recent


def test_unfiltered_quest_browse(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    handler = runtime_with_store.hub.handler_for("quest")
    assert handler is not None
    handler.put(text="fixture-quest-sort")
    assert "fixture-quest-sort" in _text(core.search(kind="quest", sort="prio"))


def test_priority_not_advertised_for_ranked_source(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    error = _text(core.search(kind="paper", q="fixture", sort="prio"))
    assert "unknown search sort" in error
    assert "sort='prio'" not in error.split("next:")[-1]
