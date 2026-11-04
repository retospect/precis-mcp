"""Labelled memory search batches preserve ordinary search semantics."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from precis.cli.memory import _created_id
from precis.handlers.memory import MemoryHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
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


def test_tool_boundary_forwards_the_batch_to_ordinary_dispatch(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    _note(rt, "Batchteal dog", "SPACE:personal")
    monkeypatch.setattr(tools_core, "_runtime", rt)
    result = tools_core.search(
        kind="memory",
        mode="lexical",
        args={"batch": [{"key": "dog", "q": "Batchteal"}]},
    )
    assert isinstance(result, str), result
    row = json.loads(result)["results"][0]
    assert row["key"] == "dog" and not row["is_error"]
    assert "Batchteal dog" in row["body"]


@pytest.mark.parametrize(("alias", "value"), [("k", 5), ("limit", 5), ("offset", 0)])
def test_tool_boundary_refuses_prose_alias_acknowledgments_on_batches(
    runtime_stateless: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
    value: int,
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
        args={"batch": [{"key": "dog", "q": "teal"}]},
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
