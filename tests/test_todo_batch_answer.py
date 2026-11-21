"""put(kind='todo', id=[...], text=...) answers a list of asks with one
comment (gr735227): the batch form of the comment-then-close put. Each
ask is checked on its own — an ask the caller may not answer (a
lane-breaker / release-review-wait / approval ask from a worker source)
is refused in the batch and named, the rest go through.
"""

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.store.types import Tag


def _one(hub: Hub) -> tuple[TodoHandler, int]:
    h = TodoHandler(hub=hub)
    h.put(text="Synthetic todo")
    tid = next(
        r.id for r in h.store.list_refs(kind="todo") if r.title == "Synthetic todo"
    )
    return h, tid


def _ask(
    hub: Hub,
    title: str,
    tags: list[str] | None = None,
    ask: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> tuple[TodoHandler, int]:
    h = TodoHandler(hub=hub)
    meta: dict[str, Any] = {
        "ask": {"kind": "ruling", "blocks": "nothing", **(ask or {})}
    }
    meta.update(extra_meta or {})
    h.put(text=title, tags=["ask-user", *(tags or [])], meta=meta)
    tid = next(r.id for r in h.store.list_refs(kind="todo") if r.title == title)
    return h, tid


def _tags(h: TodoHandler, ref_id: int) -> set[str]:
    return {str(t) for t in h.store.tags_for(ref_id)}


_BREAKER_META = {
    "scope": "lane",
    "name": "codex",
    "cause": "http-401",
    "jobs": [1, 2, 3, 4, 5],
    "lanes": ["codex"],
    "last_job": 5,
    "k": 5,
}


def test_batch_answer_appends_one_comment_to_every_ask(hub: Hub) -> None:
    h, a1 = _ask(hub, "question one?")
    _h2, a2 = _ask(hub, "question two?")
    resp = h.put(
        id=[a1, a2],
        text="answer: same answer for both",
        tags=["STATUS:done"],
        untags=["ask-user"],
    )
    assert resp.ref_id == a1
    assert f"answered 2 asks (td{a1}, td{a2})" in resp.body
    for tid in (a1, a2):
        assert "answer: same answer for both" in h.get(id=tid, view="comments").body
        assert "STATUS:done" in _tags(h, tid)
        assert "ask-user" not in _tags(h, tid)


def test_batch_answer_refuses_reserved_asks_and_names_them(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker source answers the plain ask; the breaker and
    release-review-wait asks are refused in the batch and named."""
    h, plain = _ask(hub, "plain ruling?")
    _hb, breaker = _ask(
        hub, "lane codex: builds died", extra_meta={"breaker": _BREAKER_META}
    )
    _hw, wait = _ask(hub, "review wait?", tags=["release-review-wait:td901"])
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    resp = h.put(
        id=[plain, breaker, wait],
        text="answer: routed on",
        untags=["ask-user"],
    )
    assert f"answered 1 asks (td{plain})" in resp.body
    assert "answer: routed on" in h.get(id=plain, view="comments").body
    assert "ask-user" not in _tags(h, plain)
    # refused, named, untouched
    assert f"td{breaker}: a lane-breaker ask" in resp.body
    assert f"td{wait}: a release-review-wait ask" in resp.body
    assert "answer: routed on" not in h.get(id=breaker).body
    assert "answer: routed on" not in h.get(id=wait).body
    assert "ask-user" in _tags(h, breaker) and "ask-user" in _tags(h, wait)


def test_batch_answer_owner_source_answers_reserved_asks(hub: Hub) -> None:
    """Owner sources answer anything — the reservation is worker-only."""
    h, breaker = _ask(
        hub,
        "lane codex: builds died",
        extra_meta={"breaker": {"scope": "lane", "name": "codex", "lanes": ["codex"]}},
    )
    resp = h.put(id=[breaker], text="answer: fixed — resume", untags=["ask-user"])
    assert "answered 1 asks" in resp.body
    assert "answer: fixed — resume" in h.get(id=breaker, view="comments").body


def test_batch_answer_all_refused_raises(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, breaker = _ask(
        hub,
        "lane codex: builds died",
        extra_meta={"breaker": {"scope": "lane", "name": "codex"}},
    )
    monkeypatch.setenv("PRECIS_SOURCE", "fleet-coordinator")
    with pytest.raises(BadInput, match="no ask in id=\\[\\.\\.\\.\\] may be answered"):
        h.put(id=[breaker], text="answer: fixed")
    assert "answer: fixed" not in h.get(id=breaker).body


def test_batch_answer_missing_id_named_and_rest_answered(hub: Hub) -> None:
    h, tid = _ask(hub, "missing peer?")
    resp = h.put(id=[tid, 999_999_999], text="answer: same for all")
    assert f"answered 1 asks (td{tid})" in resp.body
    assert "td999999999" in resp.body
    assert "answer: same for all" in h.get(id=tid, view="comments").body


def test_batch_answer_refuses_other_put_kwargs(hub: Hub) -> None:
    h, tid = _one(hub)
    with pytest.raises(BadInput, match="put on existing todo ids is not supported"):
        h.put(id=[tid], text="x", prio=2)
    with pytest.raises(BadInput, match="put on existing todo ids is not supported"):
        h.put(id=[tid], text="x", mode="plan", template="gripe-fix")


def test_batch_answer_requires_text(hub: Hub) -> None:
    h, tid = _one(hub)
    with pytest.raises(BadInput, match="text= \\(the answer\\) is required"):
        h.put(id=[tid], tags=["STATUS:done"])
    # nothing was written
    assert "STATUS:open" in _tags(h, tid)


def test_batch_refuses_nonasks_and_bad_handles_without_aborting(hub: Hub) -> None:
    h, ordinary = _one(hub)
    _h, ask = _ask(hub, "valid peer?")
    resp = h.put(
        id=[ordinary, "broken", "gr123", ask, f"td{ask}"],
        text="answer: proceed",
    )
    assert f"td{ordinary}: not an ask" in resp.body
    assert "broken:" in resp.body
    assert "gr123:" in resp.body
    assert f"answered 1 asks (td{ask})" in resp.body
    assert "answer: proceed" not in h.get(id=ordinary, view="comments").body
    assert h.get(id=ask, view="comments").body.count("answer: proceed") == 1


def test_batch_accepts_legacy_ask_tags(hub: Hub) -> None:
    h, tid = _one(hub)
    h.store.add_tag(tid, Tag.open("ask-user:legacy-question"), set_by="system")
    resp = h.put(id=[tid], text="answer: legacy accepted")
    assert "answered 1 asks" in resp.body
    assert "answer: legacy accepted" in h.get(id=tid, view="comments").body
