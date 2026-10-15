"""Request isolation on shared (singleton) handler instances.

One handler instance serves concurrent MCP/web calls, so request-only
values threaded from a public verb into a base-class hook must live in
:class:`precis.handlers._request_state.RequestState`, never on ``self``.
Events pause at real base-class handoffs after request state is bound,
before it is consumed. No production writes or outbound network.
"""

import ast
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from precis.handlers._cache_base import FetchResult
from precis.handlers._request_state import RequestState
from precis.handlers.gripe import GripeHandler
from precis.handlers.semanticscholar import SemanticScholarHandler
from precis.handlers.todo import TodoHandler
from precis.store.types import ChunkInsert


def _overlap(
    monkeypatch, handler, boundary, first_key, first, second, *, a_finishes_first=True
):
    """Run ``first`` and ``second`` on one handler with interleaved state.

    A binds its request state and pauses at ``boundary``; B then binds its
    own. With ``a_finishes_first`` A completes (including its cleanup)
    before B resumes; otherwise B completes before A resumes.
    """
    entered = Event()
    overwritten = Event()
    a_done = Event()
    b_done = Event()
    original = getattr(handler, boundary)

    def paused(*args, **kwargs):
        key = kwargs.get("text", kwargs.get("id", args[0] if args else None))
        if key == first_key:
            entered.set()
            assert overwritten.wait(10), "second request did not enter"
            if not a_finishes_first:
                assert b_done.wait(10), "second request did not finish"
        else:
            overwritten.set()
            if a_finishes_first:
                assert a_done.wait(10), "first request did not finish"
        return original(*args, **kwargs)

    def run(call, done):
        try:
            return call()
        finally:
            done.set()

    monkeypatch.setattr(handler, boundary, paused)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(run, first, a_done)
        assert entered.wait(10), "first request did not enter"
        b = executor.submit(run, second, b_done)
        return a.result(timeout=15), b.result(timeout=15)


@pytest.mark.parametrize("a_finishes_first", [True, False])
def test_todo_put_request_isolation(hub, monkeypatch, a_finishes_first):
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
        a_finishes_first=a_finishes_first,
    )
    maybe_refs = [handler.store.get_ref(kind="todo", id=r.ref_id) for r in (a, b)]
    refs = [r for r in maybe_refs if r is not None]
    assert len(refs) == 2
    assert refs[0].parent_id == parent_a
    assert refs[1].parent_id == parent_b
    assert [r.prio for r in refs] == [2, 8]
    assert [r.meta.get("llm_tier") for r in refs] == ["sonnet", "haiku"]
    assert [handler._body_text(r) for r in refs] == ["details-a", "details-b"]
    assert f"td{parent_a}" in a.body and "prio=2" in a.body
    assert f"td{parent_b}" in b.body and "prio=8" in b.body


def test_todo_default_tier_receipt_is_per_request(hub, monkeypatch):
    """A's parented write gets the ``llm_tier='opus'`` default and says so;
    B chose its own tier, so its receipt must not claim the stamp."""
    handler = TodoHandler(hub=hub)
    parent = handler.put(text="parent").ref_id
    a, b = _overlap(
        monkeypatch,
        handler,
        "_create",
        "request-a",
        lambda: handler.put(text="request-a", parent_id=parent),
        lambda: handler.put(
            text="request-b", parent_id=parent, meta={"llm_tier": "haiku"}
        ),
    )
    assert "stamped" in a.body
    assert "stamped" not in b.body
    refs = [handler.store.get_ref(kind="todo", id=r.ref_id) for r in (a, b)]
    assert [r.meta.get("llm_tier") for r in refs if r is not None] == [
        "opus",
        "haiku",
    ]


def test_gripe_put_prio_request_isolation(hub, monkeypatch):
    handler = GripeHandler(hub=hub)
    a, b = _overlap(
        monkeypatch,
        handler,
        "_create",
        "request-a",
        lambda: handler.put(text="request-a", prio=2),
        lambda: handler.put(text="request-b", prio=8),
    )
    # The gripe create ack carries the id in its body ("created gripe id=N").
    matches = [re.search(r"id=(\d+)", r.body) for r in (a, b)]
    ids = [int(m.group(1)) for m in matches if m is not None]
    assert len(ids) == 2
    refs = [handler.store.get_ref(kind="gripe", id=i) for i in ids]
    assert [r.prio for r in refs if r is not None] == [2, 8]


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
    assert "Hit A" not in a.body and "Hit B" in a.body
    assert "Hit B" not in b.body and "Hit A" in b.body


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
    assert "author:111:complete" not in a.body
    assert "author:222:complete" in b.body


def test_s2_author_cache_miss_fetches_each_requests_variant(hub, store, monkeypatch):
    """On a miss, each call's ``complete`` flag picks its own cache key and
    therefore its own fetch policy (capped page vs full pagination)."""
    handler, fetches = _s2_handler(hub, store, monkeypatch)
    _overlap(
        monkeypatch,
        handler,
        "_coerce_query",
        "author:333",
        lambda: handler.get(id="author:333", complete=False),
        lambda: handler.get(id="author:444", complete=True),
    )
    assert sorted(fetches) == ["author:333", "author:444:complete"]


def test_request_state_restores_outer_binding_on_reentry_and_error():
    slot: RequestState[int | None] = RequestState("test_slot", None)
    with slot.bind(1):
        with slot.bind(2):
            assert slot.get() == 2
        assert slot.get() == 1
        with pytest.raises(RuntimeError), slot.bind(3):
            raise RuntimeError("boom")
        assert slot.get() == 1
    assert slot.get() is None


_SRC = Path(__file__).resolve().parents[1] / "src"


def _pending_self_assignments(tree: ast.AST) -> list[tuple[int, str]]:
    """``self._pending*`` assignment targets outside ``__init__``."""
    hits: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if fn.name == "__init__":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                for t in ast.walk(target):
                    if (
                        isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                        and t.attr.startswith("_pending")
                    ):
                        hits.append((node.lineno, t.attr))
    return hits


def test_guard_catches_a_pending_slot_assignment():
    tree = ast.parse(
        "class H:\n"
        "    def put(self, prio):\n"
        "        self._pending_prio = prio\n"
        "    def __init__(self):\n"
        "        self._pending_ok = 0\n"
    )
    assert _pending_self_assignments(tree) == [(3, "_pending_prio")]


def test_no_handler_parks_request_state_on_self():
    """Handlers are process singletons serving concurrent calls: a request
    path that assigns ``self._pending*`` leaks state between requests.
    Thread the value through :class:`RequestState` instead."""
    offenders = [
        f"{path.relative_to(_SRC)}:{line} self.{attr}"
        for path in sorted(_SRC.rglob("*.py"))
        for line, attr in _pending_self_assignments(
            ast.parse(path.read_text(encoding="utf-8"))
        )
    ]
    assert not offenders, "per-request state on a shared instance:\n" + "\n".join(
        offenders
    )
