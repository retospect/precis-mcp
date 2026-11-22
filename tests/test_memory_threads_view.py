"""gr681310: ``get(kind='memory', id='/threads')`` — the live-threads roster.

The session-start roster (top-level ``section:threads`` nodes edited in the
last 14 days, newest first) had only a CLI door, ``precis memory index``;
``search(kind='memory', tags=['section:threads'])`` has no recency filter.
The list view reuses the CLI's ``_live_thread_lines`` and takes
``args={'days': N}``.
"""

from __future__ import annotations

from importlib import resources

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry
from tests.conftest import id_of


def _put_thread(hub: Hub, store: Store, title: str, body: str, *, age_days: int) -> str:
    ref_id = id_of(
        MemoryHandler(hub=hub)
        .put(text=body, title=title, tags=["SPACE:repo-dev", "section:threads"])
        .body
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - make_interval(days => %s) "
            "WHERE ref_id = %s",
            (age_days, ref_id),
        )
    return handle_registry.format_handle("memory", ref_id)


def test_threads_view_filters_by_edit_time(store: Store, hub: Hub) -> None:
    fresh = _put_thread(hub, store, "Fresh thread", "Left: ship it\nmore", age_days=1)
    stale = _put_thread(hub, store, "Stale thread", "Left: nothing\n", age_days=20)
    h = MemoryHandler(hub=hub)

    out = h.get(id="/threads", days=14).body
    assert f"- {fresh} Fresh thread — Left: ship it" in out
    assert stale not in out
    assert "+1 older thread node" in out
    assert "id='/threads', args={'days': 28}" in out

    assert h.get(id="/threads").body == out  # 14 days is the default

    wide = h.get(id="/threads", days=30).body
    assert f"- {fresh} Fresh thread — Left: ship it" in wide
    assert f"- {stale} Stale thread — Left: nothing" in wide
    assert wide.index(fresh) < wide.index(stale)  # newest first
    assert "older thread node" not in wide


def test_threads_view_with_no_live_thread_names_the_older_ones(
    store: Store, hub: Hub
) -> None:
    _put_thread(hub, store, "Stale thread", "Left: nothing\n", age_days=20)
    out = MemoryHandler(hub=hub).get(id="/threads").body
    assert "no threads edited in the last 14 days" in out
    assert "+1 older thread node" in out


def test_threads_view_days_through_the_args_gate(
    runtime_with_store: PrecisRuntime, store: Store, hub: Hub
) -> None:
    """``args={'days': N}`` rides the extras channel: the argument gate
    refuses an undeclared key as a typo."""
    stale = _put_thread(hub, store, "Stale thread", "Left: nothing\n", age_days=20)
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": "/threads", "__extras__": {"days": 30}}
    )
    assert f"- {stale} Stale thread — Left: nothing" in out


def test_threads_view_is_a_named_list_view(hub: Hub) -> None:
    assert "threads" in MemoryHandler(hub=hub)._supported_list_views()


def test_days_is_refused_off_the_threads_view(store: Store, hub: Hub) -> None:
    h = MemoryHandler(hub=hub)
    with pytest.raises(BadInput, match="days= only applies"):
        h.get(id="/recent", days=3)
    with pytest.raises(BadInput, match="days= must be a positive integer"):
        h.get(id="/threads", days=0)
    with pytest.raises(BadInput, match="days= must be a positive integer"):
        h.get(id="/threads", days="soon")
    with pytest.raises(BadInput, match="takes only args=\\{'days'"):
        h.get(id="/threads", days=7, page=2)


def test_memory_skill_names_the_threads_view() -> None:
    text = (
        resources.files("precis.data")
        .joinpath("skills/precis-memory-help.md")
        .read_text(encoding="utf-8")
    )
    assert "id='/threads'" in text
