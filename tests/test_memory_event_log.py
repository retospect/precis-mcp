"""Memory audit reads stay recent, bounded and individually addressable (gr564081)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._event_log_render import _summarise
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.fixture(autouse=True)
def _result_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "0")
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "24576")


def _memory(store: Store) -> int:
    return store.insert_ref(kind="memory", slug=None, title="Event log regression").id


def _events(store: Store, ref_id: int, count: int = 30) -> list[int]:
    # One transaction gives equal timestamps, so this also exercises the
    # event_id tie-break needed for stable offset windows.
    with store.pool.connection() as conn:
        return [
            store.append_event(
                ref_id,
                source="memory",
                event=f"revision_{i:02}",
                payload={"old_text": "o" * 5000, "new_text": "n" * 5000},
                conn=conn,
            )
            for i in range(count)
        ]


def _listed_ids(body: str) -> list[int]:
    return [int(value) for value in re.findall(r"event_id=(\d+)", body)]


def test_memory_log_is_recent_and_bounded(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    ids = _events(store, ref_id)

    response = MemoryHandler(hub=hub).get(id=ref_id, view="log")

    shown = response.result_count
    assert shown is not None and 0 < shown <= 30
    header = response.body.splitlines()[0]
    assert "newest first" in header
    assert f"{shown} of 30" in header
    assert _listed_ids(response.body) == list(reversed(ids))[:shown]
    assert len(response.body.encode("utf-8")) < 12000
    assert "5000" in response.body
    assert "o" * 5000 not in response.body
    assert "n" * 5000 not in response.body
    rows = [line for line in response.body.splitlines() if "event_id=" in line]
    assert len(rows) == shown
    assert max(map(len, rows)) < 800
    # The read surface must not alter the stored audit evidence.
    assert store.events_for(ref_id, limit=1)[0].payload["old_text"] == "o" * 5000


def test_memory_log_window_and_next_call(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    ids = _events(store, ref_id)
    handler = MemoryHandler(hub=hub)

    response = handler.get(id=ref_id, view="log", limit=3, offset=4)

    assert response.result_count == 3
    assert _listed_ids(response.body) == list(reversed(ids))[4:7]
    assert "3 of 30" in response.body.splitlines()[0]
    assert "offset" in response.body.splitlines()[0]
    assert "next:" in response.body
    assert "get(kind='memory'" in response.body
    assert "'limit': 3" in response.body
    assert "'offset': 7" in response.body
    last = handler.get(id=ref_id, view="log", limit=3, offset=29)
    assert _listed_ids(last.body) == [ids[0]]
    assert last.result_count == 1
    assert "next:" not in last.body
    empty = handler.get(id=ref_id, view="log", limit=3, offset=30)
    assert empty.result_count == 0
    assert "30" in empty.body


def test_memory_log_full_event_preserves_old_text(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    old_text = "old-start:" + "x" * 5000 + ":old-end"
    event_id = store.append_event(
        ref_id,
        source="memory",
        event="body_replaced",
        payload={"old_text": old_text, "new_text": "replacement", "reason": "repair"},
    )

    response = MemoryHandler(hub=hub).get(id=ref_id, view="log", event_id=event_id)

    assert old_text in response.body
    assert "replacement" in response.body
    assert "repair" in response.body
    assert response.result_count == 1
    assert "next:" not in response.body


def test_memory_log_full_event_can_continue_unicode_payload(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    event_id = store.append_event(
        ref_id,
        source="memory",
        event="body_replaced",
        payload={"old_text": "\u6f22" * 5000 + "END-OF-OLD-TEXT"},
    )
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    handler = MemoryHandler(hub=hub)
    offset = 0
    pages: list[str] = []
    for _ in range(30):
        response = handler.get(id=ref_id, view="log", event_id=event_id, offset=offset)
        assert len(response.body.encode("utf-8")) <= 4096
        pages.append(response.body)
        if "next:" not in response.body:
            break
        footer = response.body.rsplit("next:", 1)[1]
        assert f"'event_id': {event_id}" in footer
        continuation = re.search(r"'offset': (\d+)", footer)
        assert continuation is not None
        next_offset = int(continuation[1])
        assert next_offset > offset
        offset = next_offset
    else:
        pytest.fail("full event continuation did not finish")
    assert len(pages) > 1
    assert "END-OF-OLD-TEXT" in pages[-1]
    assert sum(page.count("\u6f22") for page in pages) == 5000


@pytest.mark.parametrize(("cap", "text"), [("700", "漢" * 400), ("65536", "x" * 30000)])
def test_memory_log_full_event_pages_reach_the_end_under_any_cap(
    hub: Hub, monkeypatch: pytest.MonkeyPatch, cap: str, text: str
) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    event_id = store.append_event(
        ref_id, source="memory", event="body_replaced", payload={"old_text": text}
    )
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", cap)
    handler = MemoryHandler(hub=hub)
    offset = 0
    seen = 0
    for _ in range(40):
        body = handler.get(id=ref_id, view="log", event_id=event_id, offset=offset).body
        assert len(body.encode("utf-8")) <= min(int(cap), 24576)
        seen += body.count(text[0])
        if "next:" not in body:
            break
        continuation = re.search(r"'offset': (\d+)", body.rsplit("next:", 1)[1])
        assert continuation is not None
        assert int(continuation[1]) > offset
        offset = int(continuation[1])
    else:
        pytest.fail("full event continuation did not finish")
    assert seen >= len(text)


def test_memory_log_missing_event_is_scoped_to_ref(hub: Hub) -> None:
    store = hub.live_store
    ref_id, other_id = _memory(store), _memory(store)
    foreign_event = _events(store, other_id, count=1)[0]
    handler = MemoryHandler(hub=hub)

    for event_id in (foreign_event, foreign_event + 1000000):
        with pytest.raises(NotFound) as caught:
            handler.get(id=ref_id, view="log", event_id=event_id)
        assert caught.value.next
        assert "view='log'" in str(caught.value.next)


@pytest.mark.parametrize("value", ["bad", "1.5", 1.5, 0, -1, True, [], {}])
def test_memory_log_bad_limit_refuses_with_next(hub: Hub, value: Any) -> None:
    ref_id = _memory(hub.live_store)
    with pytest.raises(BadInput) as caught:
        MemoryHandler(hub=hub).get(id=ref_id, view="log", limit=value)
    assert caught.value.next
    assert "limit" in str(caught.value)


@pytest.mark.parametrize("value", ["bad", "1.5", 1.5, -1, True, [], {}])
def test_memory_log_bad_offset_refuses_with_next(hub: Hub, value: Any) -> None:
    ref_id = _memory(hub.live_store)
    with pytest.raises(BadInput) as caught:
        MemoryHandler(hub=hub).get(id=ref_id, view="log", offset=value)
    assert caught.value.next
    assert "offset" in str(caught.value)


def test_memory_log_limit_is_clamped(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    store = hub.live_store
    ref_id = _memory(store)
    _events(store, ref_id, count=1)
    original = store.events_for
    requested_limits: list[int] = []

    def observe(*args: Any, **kwargs: Any) -> Any:
        requested_limits.append(kwargs["limit"])
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "events_for", observe)
    MemoryHandler(hub=hub).get(id=ref_id, view="log", limit=1000000)
    assert requested_limits == [100]


def test_memory_log_args_reach_handler(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.live_store
    ref_id = _memory(store)
    ids = _events(store, ref_id, count=5)
    response = runtime_with_store.dispatch(
        "get",
        {
            "kind": "memory",
            "id": ref_id,
            "view": "log",
            "__extras__": {"limit": 2, "offset": 1},
        },
    )
    assert _listed_ids(response) == [ids[-2], ids[-3]]
    full = runtime_with_store.dispatch(
        "get",
        {
            "kind": "memory",
            "id": ref_id,
            "view": "log",
            "__extras__": {"event_id": ids[0]},
        },
    )
    assert "o" * 5000 in full


def test_event_queries_keep_source_filters_and_stable_windows(store: Store) -> None:
    ref_id = _memory(store)
    ids = _events(store, ref_id, count=3)
    store.append_event(ref_id, source="another-source", event="other")
    assert store.count_events_for(ref_id) == 4
    assert store.count_events_for(ref_id, source="memory") == 3
    assert store.count_events_for(ref_id, source="missing%_'") == 0
    assert [
        event.event_id
        for event in store.events_for(ref_id, source="memory", limit=2, offset=1)
    ] == [ids[1], ids[0]]
    assert store.events_for(ref_id, event_id=ids[1])[0].event_id == ids[1]
    assert store.events_for(ref_id, event_id=ids[1], source="another-source") == []


def test_paper_log_keeps_oldest_first_with_bounded_values(hub: Hub) -> None:
    store = hub.live_store
    ref = store.insert_ref(kind="paper", slug="memlog-paper", title="Paper log")
    _events(store, ref.id, count=3)

    response = PaperHandler(hub=hub).get(id="memlog-paper", view="log")

    positions = [response.body.index(f"revision_{i:02}") for i in range(3)]
    assert positions == sorted(positions)
    assert len(response.body.encode("utf-8")) < 4000
    assert "5000" in response.body
    assert "o" * 5000 not in response.body


@pytest.mark.parametrize(
    ("source", "event", "payload"),
    [
        ("memory", "body_replaced", {"old_text": "a" * 5000}),
        ("other", "unknown", {"message": "b" * 5000}),
        ("chase", "failed", {"error": "c" * 5000}),
        ("fetcher:test", "fetch_failed", {"error": "d" * 5000}),
    ],
)
def test_shared_summary_bounds_long_values(
    source: str, event: str, payload: dict[str, Any]
) -> None:
    summary = _summarise(source, event, payload, None)
    assert "5000" in summary
    assert len(summary) < 500
    assert len(summary.splitlines()) == 1


def test_shared_summary_cannot_inject_new_event_lines() -> None:
    summary = _summarise(
        "memory", "body_replaced", {"old_text": "first\nsecond\rthird"}, None
    )
    assert len(summary.splitlines()) == 1
    assert "18" in summary
