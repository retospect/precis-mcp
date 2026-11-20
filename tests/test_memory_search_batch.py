"""Labelled memory search batches preserve ordinary search semantics."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from psycopg.errors import DeadlockDetected, LockNotAvailable

from precis.cli.memory import _created_id
from precis.handlers.memory import MemoryHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.runtime import dispatch as runtime_dispatch
from precis.store import Store
from precis.tools import core as tools_core


def _batch(
    runtime: PrecisRuntime, items: list[dict[str, Any]], **shared: Any
) -> list[dict[str, Any]]:
    body, is_error = runtime.dispatch_with_status(
        "search", {"kind": "memory", "__extras__": {"batch": items}, **shared}
    )
    assert not is_error, body
    return json.loads(body)["results"]


def _note(runtime: PrecisRuntime, title: str, *tags: str) -> int:
    return _created_id(
        MemoryHandler(hub=runtime.hub).put(text=title, title=title, tags=list(tags))
    )


def test_ordered_results_equal_individual_searches(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    _note(rt, "Batchamber Jane dog is red", "SPACE:personal")
    _note(rt, "Batchamber Bill cat is black", "SPACE:personal")
    shared = {"mode": "lexical", "page_size": 5, "tags": ["SPACE:personal"]}
    items = [
        {"key": "bill-cat", "q": "Batchamber Bill cat"},
        {"key": "jane-dog", "q": "Batchamber Jane dog"},
    ]
    original = copy.deepcopy(items)
    results = _batch(rt, items, **shared)
    assert [row["key"] for row in results] == ["bill-cat", "jane-dog"]
    for item, row in zip(items, results, strict=True):
        single = rt.dispatch("search", {"kind": "memory", "q": item["q"], **shared})
        assert row == {
            "key": item["key"],
            "q": item["q"],
            "is_error": False,
            "body": single,
        }
    assert items == original


def test_child_tags_cannot_drop_shared_space(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    _note(rt, "Batchsilver inside cat", "SPACE:personal", "topic:pets")
    _note(rt, "Batchsilver outside cat", "SPACE:research", "topic:pets")
    _note(rt, "Batchsilver inside boat", "SPACE:personal", "topic:boats")
    rows = _batch(
        rt,
        [
            {"key": "pets", "q": "Batchsilver", "tags": ["topic:pets"]},
            {"key": "shared", "q": "Batchsilver", "tags": []},
        ],
        tags=["SPACE:personal"],
        mode="lexical",
    )
    assert "inside cat" in rows[0]["body"]
    assert "outside cat" not in rows[0]["body"]
    assert "inside boat" not in rows[0]["body"]
    assert "inside boat" in rows[1]["body"]
    assert "outside cat" not in rows[1]["body"]


def test_default_limit_and_child_override_match_single_searches(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    for number in range(7):
        _note(rt, f"Batchgold animal {number}", "SPACE:personal")
    rows = _batch(
        rt,
        [
            {"key": "default", "q": "Batchgold"},
            {"key": "small", "q": "Batchgold", "page_size": 2},
        ],
        mode="lexical",
    )
    for row, size in zip(rows, (5, 2), strict=True):
        single = rt.dispatch(
            "search",
            {"kind": "memory", "q": "Batchgold", "mode": "lexical", "page_size": size},
        )
        assert row["body"] == single
    assert rows[0]["body"] != rows[1]["body"]


def test_each_child_resolves_its_own_subtree(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rt = runtime_with_store
    jane = _note(rt, "Batchviolet Jane", "section:index")
    bill = _note(rt, "Batchviolet Bill", "section:index")
    dog = _note(rt, "Batchviolet red dog", "topic:pets")
    cat = _note(rt, "Batchviolet black cat", "topic:pets")
    store.add_link(src_ref_id=dog, dst_ref_id=jane, relation="part-of")
    store.add_link(src_ref_id=cat, dst_ref_id=bill, relation="part-of")
    rows = _batch(
        rt,
        [
            {"key": "jane", "q": "Batchviolet"},
            {"key": "bill", "q": "Batchviolet", "under": f"me{bill}"},
        ],
        under=f"me{jane}",
        depth=1,
        mode="lexical",
    )
    assert "red dog" in rows[0]["body"] and "black cat" not in rows[0]["body"]
    assert "black cat" in rows[1]["body"] and "red dog" not in rows[1]["body"]


def test_child_search_failure_is_isolated(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    _note(rt, "Batchcopper dog", "SPACE:personal")
    rows = _batch(
        rt,
        [
            {"key": "before", "q": "Batchcopper"},
            {"key": "bad-owner", "q": "Batchcopper", "under": "me999999999"},
            {"key": "after", "q": "Batchcopper"},
        ],
        mode="lexical",
    )
    assert [row["is_error"] for row in rows] == [False, True, False]
    assert "resolves to no live ref" in rows[1]["body"]
    assert rows[0]["body"] == rows[2]["body"]


@pytest.mark.parametrize(
    ("fault", "failures", "attempts", "is_error"),
    [
        (LockNotAvailable, 3, 4, False),
        (LockNotAvailable, 4, 4, True),
        (DeadlockDetected, 3, 3, True),
    ],
)
def test_batch_child_lock_retry_is_bounded_and_preserves_siblings(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    fault: type[Exception],
    failures: int,
    attempts: int,
    is_error: bool,
) -> None:
    calls: list[str] = []
    waits: list[int] = []

    def search(self: PrecisRuntime, verb: str, args: dict[str, Any]) -> Response:
        query = args["q"]
        calls.append(query)
        assert args["tags"] == ["SPACE:research"]
        if query == "ECSE" and calls.count(query) <= failures:
            raise fault("database contention")
        return Response(body=query)

    monkeypatch.setattr(type(runtime_stateless), "_dispatch_inner_core", search)
    monkeypatch.setattr(runtime_dispatch, "_retry_sleep", waits.append)
    queries = ["liquid water", "ECSE", "HEA25"]
    rows = _batch(
        runtime_stateless,
        [{"key": query, "q": query} for query in queries],
        tags=["SPACE:research"],
    )
    assert [row["key"] for row in rows] == queries
    assert [row["is_error"] for row in rows] == [False, is_error, False]
    assert calls == ["liquid water", *["ECSE"] * attempts, "HEA25"]
    assert waits == list(range(1, attempts))
    if is_error:
        assert f"retried {attempts - 1} times" in rows[1]["body"]
    else:
        assert rows[1]["body"] == "ECSE"


def test_unknown_shared_extra_is_refused(runtime_stateless: PrecisRuntime) -> None:
    body, error = runtime_stateless.dispatch_with_status(
        "search",
        {
            "kind": "memory",
            "__extras__": {"batch": [{"key": "a", "q": "cat"}], "bogus": 1},
        },
    )
    assert error, body
    assert "bogus" in body and "unsupported shared batch args" in body


@pytest.mark.parametrize(
    "batch_kwarg",
    [
        {"args": {"batch": [{"key": "dog", "q": "Batchteal"}]}},
        # gr568808: the advertised top-level spelling forwards the same way.
        {"batch": [{"key": "dog", "q": "Batchteal"}]},
    ],
)
def test_tool_boundary_forwards_the_batch_to_ordinary_dispatch(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    batch_kwarg: dict[str, Any],
) -> None:
    rt = runtime_with_store
    _note(rt, "Batchteal dog", "SPACE:personal")
    monkeypatch.setattr(tools_core, "_runtime", rt)
    result = tools_core.search(
        kind="memory",
        mode="lexical",
        **batch_kwarg,
    )
    assert isinstance(result, str), result
    row = json.loads(result)["results"][0]
    assert row["key"] == "dog" and not row["is_error"]
    assert "Batchteal dog" in row["body"]


@pytest.mark.parametrize(
    "batch_kwarg",
    [
        {"args": {"batch": [{"key": "dog", "q": "teal"}]}},
        # gr568808: the advertised top-level spelling gets the same gate.
        {"batch": [{"key": "dog", "q": "teal"}]},
    ],
)
@pytest.mark.parametrize(("alias", "value"), [("k", 5), ("limit", 5), ("offset", 0)])
def test_tool_boundary_refuses_prose_alias_acknowledgments_on_batches(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
    value: int,
    batch_kwarg: dict[str, Any],
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_stateless)
    dispatched = []

    def should_not_dispatch(*args: Any, **kwargs: Any) -> None:
        dispatched.append(args)
        raise AssertionError("batch alias reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", should_not_dispatch)
    options: dict[str, Any] = {alias: value}
    result = tools_core.search(
        kind="memory",
        **batch_kwarg,
        **options,
    )
    assert isinstance(result, CallToolResult) and result.isError
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert "[error:BadInput]" in content.text
    assert f"{alias}=" in content.text
    assert "use page_size=" in content.text
    assert dispatched == []


@pytest.mark.parametrize(
    ("cap_name", "fold"),
    [("PRECIS_MAX_BODY_BYTES", False), ("PRECIS_TOOL_RESULT_CONTEXT_BYTES", True)],
)
def test_aggregate_envelope_is_whole_or_a_budget_error(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    cap_name: str,
    fold: bool,
) -> None:
    def long_result(self: PrecisRuntime, verb: str, args: dict[str, Any]) -> Response:
        return Response(body=f"{args['q']}: " + "x" * 1000)

    monkeypatch.setattr(type(runtime_stateless), "_dispatch_inner_core", long_result)
    args = {
        "kind": "memory",
        "__extras__": {
            "batch": [{"key": "one", "q": "dog"}, {"key": "two", "q": "cat"}]
        },
    }
    monkeypatch.setenv(cap_name, "1600")
    body, error = runtime_stateless.dispatch_with_status("search", args, fold=fold)
    assert error and "[error:ResultBudgetExceeded]" in body, body
    assert "fewer batch items or page_size=1" in body
    assert "truncated: showing" not in body

    monkeypatch.setenv(cap_name, "4096")
    body, error = runtime_stateless.dispatch_with_status("search", args, fold=fold)
    assert not error, body
    assert [row["key"] for row in json.loads(body)["results"]] == ["one", "two"]


@pytest.mark.parametrize(
    ("items", "extra", "message"),
    [
        ([], {}, "1..8"),
        ([{"key": str(i), "q": "x"} for i in range(9)], {}, "1..8"),
        (
            [{"key": "a", "q": "cat"}, {"key": "a", "q": "dog"}],
            {},
            "duplicate batch key",
        ),
        ([{"key": "a", "q": "cat"}], {"q": "outer"}, "outer q"),
        ([{"key": "a", "q": "cat", "batch": []}], {}, "nested batches"),
        ([{"key": "a", "q": "cat", "args": {"under": "me3"}}], {}, "nested batches"),
        ([{"key": "a", "q": "cat", "bogus": 1}], {}, "unsupported fields"),
        ([{"key": "", "q": "cat"}], {}, "key must be a nonempty string"),
        ([{"key": "a", "q": " "}], {}, "q must be a nonempty string"),
        ([{"key": "a", "q": "cat", "page_size": True}], {}, "positive integer"),
        ([{"key": "a", "q": "cat", "page_size": 11}], {}, "maximum 10"),
        ([{"key": "a", "q": "cat"}], {"page_size": 11}, "maximum 10"),
        ([{"key": "a", "q": "cat", "tags": "SPACE:private"}], {}, "list of strings"),
        ([{"key": "a", "q": "cat", "under": {"id": "me3"}}], {}, "handle/string"),
        ([{"key": "a", "q": "cat"}], {"kind": "paper"}, "kind='memory'"),
        ([{"key": "a", "q": "cat"}], {"angle": 0.5}, "unsupported shared batch fields"),
        ([{"key": "a", "q": "cat"}], {"view": "dreamable"}, "view must be"),
        ([{"key": "a", "q": "cat", "view": "stubs"}], {}, "view must be"),
        ([{"key": "a", "q": "cat", "view": "rulings"}], {}, "view must be"),
    ],
)
def test_invalid_envelope_refuses_before_searching(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    items: Any,
    extra: dict[str, Any],
    message: str,
) -> None:
    searched = []

    def should_not_search(self: PrecisRuntime, verb: str, args: dict[str, Any]) -> None:
        searched.append(args)
        raise AssertionError("malformed batch ran a child search")

    monkeypatch.setattr(
        type(runtime_stateless), "_dispatch_inner_core", should_not_search
    )
    body, error = runtime_stateless.dispatch_with_status(
        "search", {"kind": "memory", "__extras__": {"batch": items}, **extra}
    )
    assert error and "[error:BadInput]" in body, body
    assert message in body
    assert "next:" in body.lower()
    assert searched == []


@pytest.mark.parametrize("numeric", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_scope_handle_aliases_under(
    runtime_with_store: PrecisRuntime, store: Store, numeric: bool, nested: bool
) -> None:
    rt = runtime_with_store
    hub = _note(rt, "Scopepump hub", "section:index")
    child = _note(rt, "Scopepump child")
    grandchild = _note(rt, "Scopepump grandchild")
    _note(rt, "Scopepump outside")
    store.add_link(src_ref_id=child, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=grandchild, dst_ref_id=child, relation="part-of")
    shared = {"kind": "memory", "q": "Scopepump", "mode": "lexical"}
    expected = rt.dispatch("search", {**shared, "under": f"me{hub}"})
    scope = str(hub) if numeric else f"me{hub}"
    for under in (None, str(hub), f"me{hub}"):
        filters: dict[str, Any] = {"scope": scope}
        if under is not None:
            filters["under"] = under
        args = {**shared, "__extras__": filters} if nested else {**shared, **filters}
        body, error = rt.dispatch_with_status("search", args)
        assert not error, body
        assert body == expected
        assert "Scopepump child" in body and "Scopepump grandchild" in body
        assert "Scopepump outside" not in body
    body, error = rt.dispatch_with_status(
        "search", {**shared, "scope": scope, "__extras__": {"under": f"me{child}"}}
    )
    assert error and "different subtree" in body


@pytest.mark.parametrize("scope", ["bogus", "pa3", "mb3"])
def test_scope_bad_value_lists_title_body_handle(
    runtime_with_store: PrecisRuntime, scope: str
) -> None:
    body, error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "q": "pump", "scope": scope}
    )
    assert error and "unknown scope" in body
    assert "title" in body and "body" in body
    assert "a memory handle (alias of under=)" in body


def test_search_id_range_bounds_hits(runtime_with_store: PrecisRuntime) -> None:
    """gr725290: ``id_min``/``id_max`` bound a memory search by ref id."""
    rt = runtime_with_store
    lo = _note(rt, "Idrangeword alpha", "SPACE:personal")
    hi = _note(rt, "Idrangeword beta", "SPACE:personal")
    base = {"kind": "memory", "q": "Idrangeword", "tags": ["SPACE:personal"]}
    body, err = rt.dispatch_with_status(
        "search", {**base, "__extras__": {"id_min": hi, "id_max": hi}}
    )
    assert not err, body
    assert f"me{hi}" in body or f"memory {hi}" in body
    assert f"me{lo}" not in body and f"memory {lo}" not in body
    body, err = rt.dispatch_with_status(
        "search", {**base, "__extras__": {"id_max": lo - 1}}
    )
    assert not err, body
    assert f"me{lo}" not in body and f"memory {hi}" not in body

    body, err = rt.dispatch_with_status(
        "search", {**base, "view": "count", "__extras__": {"id_min": str(hi)}}
    )
    assert not err and "# 1 memory entry" in body


@pytest.mark.parametrize(
    ("bounds", "message"),
    [
        ({"id_min": "oops"}, "id_min= must be an integer"),
        ({"id_max": 1.5}, "id_max= must be an integer"),
        ({"id_min": 5, "id_max": 4}, "is greater than"),
    ],
)
def test_search_id_range_rejects_invalid_bounds(
    runtime_with_store: PrecisRuntime, bounds: dict[str, object], message: str
) -> None:
    body, error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "index", "__extras__": bounds}
    )
    assert error and "BadInput" in body and message in body


def test_broad_ranked_memory_range_points_to_index(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.handlers._numeric_ref._ID_RANGE_HITS_CAP", 1)
    rt = runtime_with_store
    lo = _note(rt, "Range cap alpha", "SPACE:personal")
    hi = _note(rt, "Range cap beta", "SPACE:personal")
    base = {"kind": "memory", "__extras__": {"id_min": lo, "id_max": hi}}
    for view in (None, "index"):
        body, error = rt.dispatch_with_status(
            "search", {**base, "q": "range", "view": view}
        )
        assert error and "too many" in body and "view='index'" in body
    body, error = rt.dispatch_with_status("search", {**base, "view": "index"})
    assert not error and "Range cap alpha" in body and "Range cap beta" in body
