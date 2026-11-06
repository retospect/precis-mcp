"""The ``get(kind='agentlog', id='/scope?…')`` readout (td575705 G1).

Seeds ``tool_calls`` rows the way production gets them — a bound slice
principal writes through ``runtime.dispatch_with_status`` under
``mcp.slice_scope=observe`` (ok rows marked
``error_type='SliceScopeWouldRefuse'``) and ``enforce`` (Denied rows
whose ``error_head`` carries the same k=v line) — then reads the view
back: grouped counts, per-row would-refuse/refused markers, filters,
the unknown-filter BadInput and the empty-window state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from precis import principal, settings
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.inproc_context import TickContext, tick_context
from tests.conftest import id_of


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    with tick_context(TickContext()):
        yield


@pytest.fixture
def enforce(store: Store) -> Iterator[None]:
    settings.set_setting("mcp.slice_scope", "enforce", store=store)
    yield
    settings.clear_setting("mcp.slice_scope", store=store)


def _mk_todo(runtime: PrecisRuntime, text: str = "scope view") -> int:
    body, error = runtime.dispatch_with_status("put", {"kind": "todo", "text": text})
    assert not error, body
    return id_of(body)


def _builder(todo_id: int) -> Any:
    return principal.bind(principal.for_slice(f"td{todo_id}j9", "builder"))


def _scope(runtime: PrecisRuntime, qs: str) -> tuple[str, bool]:
    return runtime.dispatch_with_status(
        "get", {"kind": "agentlog", "id": f"/scope?{qs}"}
    )


def test_scope_groups_observe_and_enforce_rows(
    runtime_with_store: PrecisRuntime, store: Store, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    # enforce: two Denied rows in different groups
    with _builder(own):
        runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": "enforced refusal"}
        )
        runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": other, "text": "foreign comment"}
        )
    settings.clear_setting("mcp.slice_scope", store=store)
    # observe: the same out-of-scope write lands ok but marks its row
    with _builder(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": "observed write"}
        )
    assert not error, body
    body, error = _scope(runtime_with_store, "since=24")
    assert not error, body
    # groups fold observe markers and Denied rows by the same k=v keys
    assert "builder kind put memory: 2" in body
    assert "builder comment-shape put todo: 1" in body
    # newest rows say which shape each was
    assert "— refused rule=kind" in body
    assert "— would-refuse rule=kind" in body
    assert "— refused rule=comment-shape" in body


def test_scope_filters_role_rule_verb_kind(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    with _builder(own):
        runtime_with_store.dispatch_with_status("put", {"kind": "memory", "text": "x"})
        runtime_with_store.dispatch_with_status(
            "tag", {"kind": "todo", "id": own, "add": ["x"]}
        )
    with principal.bind(principal.for_slice(f"td{own}j9", "generator")):
        runtime_with_store.dispatch_with_status("put", {"kind": "memory", "text": "y"})
    body, error = _scope(runtime_with_store, "since=24&role=generator")
    assert not error, body
    assert "generator kind put memory: 1" in body
    assert "builder" not in body
    body, error = _scope(runtime_with_store, "since=24&rule=tag-allowlist")
    assert not error, body
    assert "builder tag-allowlist tag todo: 1" in body
    body, error = _scope(runtime_with_store, "since=24&verb=tag")
    assert not error, body
    assert "tag-allowlist" in body and "kind put" not in body
    body, error = _scope(runtime_with_store, "since=24&kind=todo")
    assert not error, body
    assert "tag todo" in body and "put memory" not in body


def test_scope_unknown_filter_refused(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, error = _scope(runtime_with_store, "since=24&bogus=1")
    assert error
    assert "bogus" in body and "[error:BadInput]" in body


def test_scope_empty_window_names_the_mode(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, error = _scope(runtime_with_store, "since=24")
    assert not error, body
    assert "no scope-marked rows" in body
    assert "mcp.slice_scope=observe" in body


def test_scope_limit_folds_from_top_level(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    with _builder(own):
        for _ in range(3):
            runtime_with_store.dispatch_with_status(
                "put", {"kind": "memory", "text": "x"}
            )
    body, error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "agentlog", "id": "/scope?since=24", "limit": 2}
    )
    assert not error, body
    assert "2 row(s) shown" in body
