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
    if kind == "gripe":
        handler.put(id=closed, text="resolved: abc (r1) — fixture")
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
    # PRIO ties: todo/quest put the newest first; gripe puts the oldest
    # first (a triage queue — the longest-waiting gripe leads, 2026-10-18).
    oldest_first = kind == "gripe"
    first = _text(core.search(**args, sort="prio"))
    assert "[error:" not in first
    new_at, old_at = (
        first.index("fixture-sort-hot-new"),
        first.index("fixture-sort-hot-old"),
    )
    assert (old_at < new_at) if oldest_first else (new_at < old_at)
    assert "fixture-sort-cold" not in first
    assert "fixture-sort-closed" not in first
    larger = _text(core.search(**{**args, "page_size": 5}, sort="prio"))
    default_at, unset_at = (
        larger.index("fixture-sort-default"),
        larger.index("fixture-sort-unset"),
    )
    assert (unset_at < default_at) if oldest_first else (default_at < unset_at)
    assert "fixture-sort-cold" in larger
    recent = _text(core.search(**args, sort="recency"))
    assert "[error:" not in recent
    assert "by recency" in recent
    error = _text(core.search(**args, sort="bogus"))
    assert "'prio'" in error and "'recency'" in error
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


@pytest.mark.parametrize("kind", ["gripe", "todo", "quest"])
def test_empty_prio_tag_filter_hint(
    runtime_with_store: PrecisRuntime, monkeypatch, kind: str
) -> None:
    """gr676981: a ``PRIO:*`` pin inside ``tags=`` can never match on the
    workflow kinds — the write spelling translates to ``refs.prio`` and
    is stripped from the tag set. The empty listing's Next names the
    real door, ``args={'sort': 'prio'}``, and only there."""
    rt = runtime_with_store
    monkeypatch.setattr(core, "_get_runtime", lambda: rt)
    handler = rt.hub.handler_for(kind)
    assert handler is not None
    handler.put(text="fixture-prio-hint")
    out = _text(core.search(kind=kind, tags=["PRIO:high"]))
    assert "[error:" not in out
    assert "args={'sort': 'prio'}" in out
    plain = _text(core.search(kind=kind, tags=["fixture:no-such-tag"]))
    assert "args={'sort': 'prio'}" not in plain


def test_empty_prio_tag_filter_hint_keeps_other_tags(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sort hint keeps ``STATUS:triaged`` as scope and no suggested
    call repeats the dead ``PRIO:`` pin."""
    rt = runtime_with_store
    monkeypatch.setattr(core, "_get_runtime", lambda: rt)
    out = _text(
        core.search(kind="gripe", tags=["STATUS:triaged", "PRIO:high", "!PRIO:low"])
    )
    nxt = out.split("Next:", 1)[1]
    assert "q='topic', tags=['STATUS:triaged']" in nxt
    assert "tags=['STATUS:triaged'], args={'sort': 'prio'}" in nxt
    assert "PRIO:high" not in nxt
    assert "!PRIO:low" not in nxt


def test_empty_prio_tag_filter_hint_labels_scope_reset(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    out = _text(core.search(kind="gripe", status="*", tags=["PRIO:high"]))
    assert "[error:" not in out
    nxt = out.split("Next:", 1)[1]
    assert "args={'sort': 'prio'}" in nxt
    assert "Start a fresh browse with default scope" in nxt


def test_prio_tag_filter_refused_off_workflow_kinds(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    """The other half of gr676981: ``PRIO:`` isn't a memory/job axis at
    all — the filter refuses at the boundary rather than silently
    matching nothing."""
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    for kind in ("memory", "job"):
        out = _text(core.search(kind=kind, tags=["PRIO:high"]))
        assert "[error:" in out and "axis not allowed" in out, out
