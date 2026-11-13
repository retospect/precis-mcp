"""Memory complaints carry the session's bounded, masked ledger route."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from precis import serve_ledger, tool_ledger
from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.store import Store


class _Session:
    """Weak-referenceable MCP session stand-in."""


@pytest.fixture
def memory_ledger(store: Store) -> Iterator[list[dict[str, Any]]]:
    """Write real dev-DB ledger rows, including rows the route must omit."""
    expected: list[dict[str, Any]] = []
    with serve_ledger.session_scope(_Session()):
        session_id, _ = tool_ledger.current_session_key()
        assert session_id is not None
        for seq in range(1, 15):
            verb = "get" if seq % 2 else "search"
            sample = tool_ledger.arg_sample(
                {"kind": "memory", "q": f"claim {seq}", "token": "private-value"}
            )
            assert sample is not None
            # One search row carries the recall-audit ids (0304); the
            # rest stay NULL like gets and pre-column rows.
            ids = [101, 102] if verb == "search" and seq == 14 else None
            tool_ledger.record_call(
                store,
                tool_ledger.ToolCallRecord(
                    verb=verb,
                    kind="memory",
                    input_keys=["kind", "q", "token"],
                    outcome="ok",
                    session_id=session_id,
                    seq=seq,
                    arg_sample=sample,
                    result_ids=ids,
                ),
            )
            expected.append(
                {
                    "session_id": session_id,
                    "seq": seq,
                    "verb": verb,
                    "kind": "memory",
                    "arg_sample": json.loads(sample),
                    "result_ids": ids,
                }
            )
        for seq, handle in ((15, "me123"), (16, "memory:456"), (17, "memory:me789")):
            sample = tool_ledger.arg_sample({"id": handle})
            assert sample is not None
            tool_ledger.record_call(
                store,
                tool_ledger.ToolCallRecord(
                    verb="get",
                    kind=None,
                    input_keys=["id"],
                    outcome="ok",
                    session_id=session_id,
                    seq=seq,
                    arg_sample=sample,
                ),
            )
            expected.append(
                {
                    "session_id": session_id,
                    "seq": seq,
                    "verb": "get",
                    "kind": None,
                    "arg_sample": json.loads(sample),
                    "result_ids": None,
                }
            )
        for sid, kind, verb in (
            (session_id, "gripe", "get"),
            (session_id, "memory", "edit"),
            ("other-session", "memory", "get"),
        ):
            tool_ledger.record_call(
                store,
                tool_ledger.ToolCallRecord(
                    verb=verb,
                    kind=kind,
                    input_keys=["kind"],
                    outcome="ok",
                    session_id=sid,
                    seq=99,
                ),
            )
        yield expected[-10:]


@pytest.mark.parametrize("via_tag", [False, True])
def test_memory_gripe_keeps_last_ten_session_reads(
    store: Store,
    hub: Hub,
    memory_ledger: list[dict[str, Any]],
    via_tag: bool,
) -> None:
    gripe = GripeHandler(hub=hub)
    gripe.put(
        text="memory: two notes disagree\nGoal: establish the current claim",
        project=None if via_tag else "memory",
        tags=["project:memory"] if via_tag else None,
        new=True,
    )
    refs = store.list_refs(kind="gripe", tags=["project:memory"])
    assert len(refs) == 1
    assert refs[0].meta["route"] == memory_ledger
    assert "private-value" not in json.dumps(refs[0].meta["route"])


def test_non_memory_gripe_has_no_route(
    store: Store, hub: Hub, memory_ledger: list[dict[str, Any]]
) -> None:
    GripeHandler(hub=hub).put(text="Other project complaint", project="precis")
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert "route" not in refs[0].meta


def test_quick_form_memory_gripe_keeps_the_route(
    store: Store, hub: Hub, memory_ledger: list[dict[str, Any]]
) -> None:
    """gr657257 (review td658283): the documented quick form carries no
    tag — ``me<N>: <what is wrong>`` on the first line — so the route
    gate cannot wait on the ``project:memory`` tag triage adds later;
    without this the replay evidence is lost on the sanctioned path."""
    GripeHandler(hub=hub).put(
        text="me101: export notes disagree\nGoal: pick the export format",
        new=True,
    )
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert refs[0].meta["route"] == memory_ledger


def test_concern_linked_memory_gripe_keeps_the_route(
    store: Store, hub: Hub, memory_ledger: list[dict[str, Any]]
) -> None:
    """gr657257: a create-time ``raises-concern-about`` link to a memory
    is the other tagless complaint shape; a ``related-to`` memory link
    stays context and attaches no route."""
    from tests.test_garden_memory import _memory

    mid = _memory(store, "A claim a reader flagged.")
    gripe = GripeHandler(hub=hub)
    gripe.put(
        text="stale claim, flagged by link",
        link=f"me{mid}",
        rel="raises-concern-about",
        new=True,
    )
    flagged = store.list_refs(kind="gripe")
    assert len(flagged) == 1
    assert flagged[0].meta["route"] == memory_ledger
    gripe.put(
        text="ordinary gripe citing a memory as context",
        link=f"me{mid}",
        rel="related-to",
        new=True,
    )
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 2
    plain = next(r for r in refs if r.id != flagged[0].id)
    assert "route" not in plain.meta


def test_me_handle_later_in_the_text_is_not_a_complaint(
    store: Store, hub: Hub, memory_ledger: list[dict[str, Any]]
) -> None:
    """The quick form is the *first line* — a memory handle quoted in the
    body of an ordinary gripe does not attach the route."""
    GripeHandler(hub=hub).put(
        text="router pour fails\nRoute: get me101 returned the stale note",
        new=True,
    )
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert "route" not in refs[0].meta


def test_memory_gripe_without_session_still_files(store: Store, hub: Hub) -> None:
    with serve_ledger.session_scope(None):
        GripeHandler(hub=hub).put(text="memory: unclear claim", project="memory")
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert "route" not in refs[0].meta


def test_ledger_failure_does_not_fail_memory_gripe(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = psycopg.Cursor.execute

    def unavailable(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(query, str) and "FROM tool_calls" in query:
            raise RuntimeError("ledger read unavailable")
        return execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", unavailable)
    with serve_ledger.session_scope(_Session()):
        GripeHandler(hub=hub).put(text="memory: unclear claim", project="memory")
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert "route" not in refs[0].meta


def test_ledger_read_failure_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenStore:
        @property
        def pool(self) -> Any:
            raise RuntimeError("ledger connection unavailable")

    monkeypatch.setattr(tool_ledger, "current_session_key", lambda: ("session", 1))
    broken: Any = BrokenStore()
    assert tool_ledger.memory_route(broken) is None


def test_theme_keeper_gets_latest_memory_route_without_losing_metadata(
    store: Store,
    hub: Hub,
    memory_ledger: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gripe = GripeHandler(hub=hub)
    gripe.put(
        text="Original complaint",
        project="memory",
        tags=["theme:reader-route-test"],
        new=True,
    )
    keeper = store.list_refs(kind="gripe")[0]
    store.update_ref(keeper.id, meta_patch={"route": ["older route"], "other": "keep"})
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _text: ["reader-route-test"]
    )
    response = gripe.put(text="memory: new disagreement", project="memory")
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 1
    assert refs[0].id == keeper.id
    assert refs[0].meta["route"] == memory_ledger
    assert refs[0].meta["other"] == "keep"
    assert "merged into" in response.body
    assert any("new disagreement" in c.text for c in gripe._comments(keeper.id))


@pytest.mark.parametrize("keeper_project", ["memory", "precis"])
@pytest.mark.parametrize("via_tag", [False, True])
def test_theme_merges_keep_memory_and_code_gripes_separate(
    store: Store,
    hub: Hub,
    monkeypatch: pytest.MonkeyPatch,
    keeper_project: str,
    via_tag: bool,
) -> None:
    gripe = GripeHandler(hub=hub)
    gripe.put(
        text="Original complaint",
        project=keeper_project,
        tags=["theme:reader-route-test"],
        new=True,
    )
    keeper = store.list_refs(kind="gripe")[0]
    original_tags = store.tags_for(keeper.id)
    original_comments = gripe._comments(keeper.id)
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _text: ["reader-route-test"]
    )
    project = "precis" if keeper_project == "memory" else "memory"
    args: dict[str, Any] = (
        {"tags": [f"project:{project}"]} if via_tag else {"project": project}
    )
    first = gripe.put(text="A different project's complaint", **args)
    refs = store.list_refs(kind="gripe")
    assert len(refs) == 2
    assert "merged into" not in first.body
    assert store.tags_for(keeper.id) == original_tags
    assert gripe._comments(keeper.id) == original_comments
    created = next(ref for ref in refs if ref.id != keeper.id)
    assert f"project:{project}" in {str(t) for t in store.tags_for(created.id)}

    # Lookup must find the right partition even with an older foreign keeper.
    again = gripe.put(text="Another complaint in the same project", **args)
    assert len(store.list_refs(kind="gripe")) == 2
    assert f"merged into gr{created.id}" in again.body
    assert store.tags_for(keeper.id) == original_tags
    assert gripe._comments(keeper.id) == original_comments
