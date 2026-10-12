"""Deterministic triage reproducers; xfails pin desired request isolation.

No production writes or outbound network. Events pause at real base-class
handoffs after instance state is set, before it is consumed.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from precis.handlers._cache_base import FetchResult
from precis.handlers.semanticscholar import SemanticScholarHandler
from precis.handlers.todo import TodoHandler
from precis.store.types import ChunkInsert


def _overlap(monkeypatch, handler, boundary, first_key, first, second):
    entered = Event()
    overwritten = Event()
    finished = Event()
    original = getattr(handler, boundary)

    def paused(*args, **kwargs):
        key = kwargs.get("text", kwargs.get("id", args[0] if args else None))
        if key == first_key:
            entered.set()
            assert overwritten.wait(10), "second request did not enter"
        else:
            overwritten.set()
            assert finished.wait(10), "first request did not finish"
        return original(*args, **kwargs)

    def run_first():
        try:
            return first()
        finally:
            finished.set()

    monkeypatch.setattr(handler, boundary, paused)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(run_first)
        assert entered.wait(10), "first request did not enter"
        b = executor.submit(second)
        return a.result(timeout=15), b.result(timeout=15)


@pytest.mark.xfail(
    strict=True, reason="todo put consumes another call's instance state"
)
def test_todo_put_request_isolation(hub, monkeypatch):
    handler = TodoHandler(hub=hub)
    parent_a = handler.put(text="parent-a").ref_id
    parent_b = handler.put(text="parent-b").ref_id
    a, b = _overlap(
        monkeypatch,
        handler,
        "_create",
        "request-a",
        lambda: handler.put(
            text="request-a",
            parent_id=parent_a,
            prio=2,
            meta={"llm_tier": "sonnet"},
            body="details-a",
        ),
        lambda: handler.put(
            text="request-b",
            parent_id=parent_b,
            prio=8,
            meta={"llm_tier": "haiku"},
            body="details-b",
        ),
    )
    maybe_refs = [handler.store.get_ref(kind="todo", id=r.ref_id) for r in (a, b)]
    refs = [r for r in maybe_refs if r is not None]
    assert len(refs) == 2
    observed = [
        (r.title, r.parent_id, r.prio, r.meta, handler._body_text(r)) for r in refs
    ]
    print("todo observed:", observed, "acks:", a.body, b.body)
    assert refs[0].parent_id == parent_a
    assert refs[1].parent_id == parent_b
    assert [r.prio for r in refs] == [2, 8]
    assert [handler._body_text(r) for r in refs] == ["details-a", "details-b"]


def _s2_handler(hub, store, monkeypatch, *, papers=None):
    store.upsert_kinds([SemanticScholarHandler.spec])
    handler = SemanticScholarHandler(hub=hub)
    fetches = []

    def fetch(key):
        fetches.append(key)
        return FetchResult(
            title=key,
            body_blocks=[ChunkInsert(ord=0, text=key)],
            meta={"papers": papers} if papers else {},
        )

    monkeypatch.setattr(handler, "_fetch", fetch)
    return handler, fetches


@pytest.mark.xfail(strict=True, reason="S2 get consumes another call's exclude list")
def test_s2_cached_exclude_request_isolation(hub, store, monkeypatch):
    papers = [{"title": "Hit A"}, {"title": "Hit B"}]
    pa = store.insert_ref(kind="paper", slug="request-a", title="Hit A", meta={})
    pb = store.insert_ref(kind="paper", slug="request-b", title="Hit B", meta={})
    handler, fetches = _s2_handler(hub, store, monkeypatch, papers=papers)
    monkeypatch.setattr(
        handler,
        "_corpus_flags_bulk",
        lambda papers: [(f"stub:pa{pa.id}", pa.id), (f"stub:pa{pb.id}", pb.id)],
    )
    handler.get(id="request-a")
    handler.get(id="request-b")
    a, b = _overlap(
        monkeypatch,
        handler,
        "_coerce_query",
        "request-a",
        lambda: handler.get(id="request-a", exclude=["request-a"]),
        lambda: handler.get(id="request-b", exclude=["request-b"]),
    )
    assert len(fetches) == 2, "overlap must exercise cache hits"
    print("S2 exclude observed:", a.body, b.body)
    assert "Hit A" not in a.body and "Hit B" in a.body
    assert "Hit B" not in b.body and "Hit A" in b.body


@pytest.mark.xfail(
    strict=True, reason="S2 complete flag changes another call's cache key"
)
def test_s2_cached_author_complete_request_isolation(hub, store, monkeypatch):
    handler, fetches = _s2_handler(hub, store, monkeypatch)
    handler.get(id="author:111", complete=True)
    handler.get(id="author:111", complete=False)
    handler.get(id="author:222", complete=True)
    handler.get(id="author:222", complete=False)
    a, b = _overlap(
        monkeypatch,
        handler,
        "_coerce_query",
        "author:111",
        lambda: handler.get(id="author:111", complete=False),
        lambda: handler.get(id="author:222", complete=True),
    )
    assert len(fetches) == 4, "overlap must exercise cache hits"
    print("S2 author observed:", a.body, b.body)
    assert "author:111:complete" not in a.body
    assert "author:222:complete" in b.body
