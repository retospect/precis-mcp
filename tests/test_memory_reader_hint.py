"""The memory complaint prompt is scoped to one node in one session."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from precis import serve_ledger
from precis.response import Response
from precis.runtime import PrecisRuntime, handle_deref
from precis.runtime import dispatch as dispatch_module
from precis.runtime.handle_deref import MEMORY_GRIPE_HINT, reset_read_receipts
from precis.store import ChunkInsert, Store
from precis.tools import core


@pytest.fixture(autouse=True)
def _clean_receipts() -> Iterator[None]:
    reset_read_receipts()
    yield
    reset_read_receipts()


def _memory(store: Store) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title="Reader hint fixture")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="A dated claim.", meta={"chunk_kind": "memory_body"})],
    )
    return ref.id


@pytest.mark.parametrize("view", [None, "full", "body", "meta"])
@pytest.mark.parametrize("address", ["numeric", "handle", "kind-id", "kind-handle"])
def test_hint_first_read_only_and_per_session(
    runtime_with_store: PrecisRuntime,
    store: Store,
    view: str | None,
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    ref_id = _memory(store)
    identifiers: dict[str, str | int] = {
        "numeric": ref_id,
        "handle": f"me{ref_id}",
        "kind-id": f"memory:{ref_id}",
        "kind-handle": f"memory:me{ref_id}",
    }
    args: dict[str, Any] = {
        "kind": "memory",
        "id": identifiers[address],
    }
    if view is not None:
        args["view"] = view
    first_ctx = cast(Any, SimpleNamespace(session=object()))
    second_ctx = cast(Any, SimpleNamespace(session=object()))
    first = core.get(**args, ctx=first_ctx)
    assert isinstance(first, str), first
    second = core.get(id=f"me{ref_id}", ctx=first_ctx)
    another = core.get(id=f"me{_memory(store)}", ctx=first_ctx)
    assert isinstance(second, str), second
    assert isinstance(another, str), another
    assert MEMORY_GRIPE_HINT in first
    if view is None:
        assert first.index("trust:") < first.index(MEMORY_GRIPE_HINT)
    assert MEMORY_GRIPE_HINT not in second
    assert MEMORY_GRIPE_HINT in another
    other_session = core.get(**args, ctx=second_ctx)
    assert isinstance(other_session, str), other_session
    assert MEMORY_GRIPE_HINT in other_session


def test_hint_absent_without_session(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    result = runtime_with_store.dispatch("get", {"id": f"me{_memory(store)}"})
    assert MEMORY_GRIPE_HINT not in result


def test_hint_absent_for_listing_and_other_kind(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust = Mock(wraps=dispatch_module.trust_line)
    monkeypatch.setattr(dispatch_module, "trust_line", trust)
    _memory(store)
    paper = store.insert_ref(kind="paper", slug="reader-hint-paper", title="Paper")
    with serve_ledger.session_scope(object()):
        listing = runtime_with_store.dispatch("get", {"kind": "memory"})
        other = runtime_with_store.dispatch("get", {"id": f"pa{paper.id}"})
    assert MEMORY_GRIPE_HINT not in listing
    assert MEMORY_GRIPE_HINT not in other
    assert "Paper" in other
    assert all(call.args[1] != "memory" for call in trust.call_args_list)


def test_hint_failure_preserves_response(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref_id = _memory(store)
    original = "A dated claim.\n\ntrust: fixture\n\n"
    monkeypatch.setattr(
        runtime_with_store,
        "_dispatch_with_retry",
        lambda *a, **kw: Response(body=original),
    )
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    broken = Mock(side_effect=RuntimeError("hint unavailable"))
    monkeypatch.setattr(handle_deref, "memory_gripe_hint", broken)
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "get", {"kind": "memory", "id": ref_id}
        )
    broken.assert_called_once()
    assert not failed, result
    assert result.encode("utf-8") == original.encode("utf-8")
    assert record.call_args.args[2] == result
    assert record.call_args.kwargs["budget_fitted"] is False


def test_hint_is_one_line_and_does_not_propose_an_edit() -> None:
    assert "\n" not in MEMORY_GRIPE_HINT
    assert "edit(" not in MEMORY_GRIPE_HINT
    assert len(MEMORY_GRIPE_HINT.encode("utf-8")) < 400


@pytest.mark.parametrize("cap", [400, 2000])
@pytest.mark.parametrize("fold", [False, True])
def test_fitted_read_is_identical_without_hint(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    fold: bool,
) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="Long reader fixture")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="A claim with its dated evidence.\n" * 400,
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    monkeypatch.setenv(
        "PRECIS_TOOL_RESULT_CONTEXT_BYTES" if fold else "PRECIS_MAX_BODY_BYTES",
        str(cap),
    )
    expected, failed = runtime_with_store.dispatch_with_status(
        "get", {"id": f"me{ref.id}"}, fold=fold
    )
    assert not failed, expected
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    with serve_ledger.session_scope(object()):
        first, failed = runtime_with_store.dispatch_with_status(
            "get", {"id": f"me{ref.id}"}, fold=fold
        )
        assert not failed, first
        assert record.call_args.args[2] == first
        assert record.call_args.kwargs["budget_fitted"] is True
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "24576")
        monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "0")
        second = runtime_with_store.dispatch("get", {"id": f"me{ref.id}"}, fold=fold)
    assert "truncated" in first
    assert first.encode("utf-8") == expected.encode("utf-8")
    assert MEMORY_GRIPE_HINT not in first
    assert len(first.encode("utf-8")) <= cap
    assert "truncated" not in second
    assert MEMORY_GRIPE_HINT not in second


def test_stamped_memory_first_read_fits_without_hint_at_400_bytes(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, failed = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": "A dated claim.\n" * 100}
    )
    assert not failed, created
    ref = store.list_refs(kind="memory")[0]
    line = dispatch_module.trust_line(store, "memory", ref.id)
    assert line is not None and "unstamped" not in line
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "400")
    expected, failed = runtime_with_store.dispatch_with_status(
        "get", {"id": f"me{ref.id}"}, fold=True
    )
    assert not failed, expected
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "get", {"id": f"me{ref.id}"}, fold=True
        )
    assert not failed, result
    assert len(result.encode("utf-8")) <= 400
    assert "truncated" in result
    assert result.encode("utf-8") == expected.encode("utf-8")
    assert MEMORY_GRIPE_HINT not in result
    assert record.call_args.args[2] == result
    assert record.call_args.kwargs["budget_fitted"] is True


@pytest.mark.parametrize("fold", [False, True])
@pytest.mark.parametrize(
    "cap_name", ["PRECIS_MAX_BODY_BYTES", "PRECIS_TOOL_RESULT_CONTEXT_BYTES"]
)
@pytest.mark.parametrize("slack", [-1, 0])
def test_hint_uses_only_spare_bytes(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    fold: bool,
    cap_name: str,
    slack: int,
) -> None:
    ref_id = _memory(store)
    original = "A dated claim with café evidence.\n\ntrust: fixture\n"
    candidate = f"{original}\n{MEMORY_GRIPE_HINT}"
    monkeypatch.setattr(
        runtime_with_store,
        "_dispatch_with_retry",
        lambda *a, **kw: Response(body=original),
    )
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "24576")
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "24576")
    monkeypatch.setenv(cap_name, str(len(candidate.encode("utf-8")) + slack))
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "get", {"kind": "memory", "id": ref_id}, fold=fold
        )
        assert not failed, result
        expected = (
            candidate
            if slack == 0
            or (not fold and cap_name == "PRECIS_TOOL_RESULT_CONTEXT_BYTES")
            else original
        )
        assert result.encode("utf-8") == expected.encode("utf-8")
        assert record.call_args.args[2] == result
        assert record.call_args.kwargs["budget_fitted"] is False
        monkeypatch.setenv(cap_name, "24576")
        second = runtime_with_store.dispatch(
            "get", {"kind": "memory", "id": ref_id}, fold=fold
        )
        assert second == original


def test_members_page_is_identical_without_hint(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _memory(store)
    for _ in range(30):
        store.add_link(src_ref_id=_memory(store), dst_ref_id=root, relation="part-of")
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "1200")
    args: dict[str, Any] = {"kind": "memory", "id": root, "view": "members"}
    expected, failed = runtime_with_store.dispatch_with_status("get", args, fold=True)
    assert not failed, expected
    assert "'offset':" in expected
    assert "truncated" not in expected
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status("get", args, fold=True)
        assert not failed, result
        second = runtime_with_store.dispatch("get", {"id": f"me{root}"})
    assert result.encode("utf-8") == expected.encode("utf-8")
    assert MEMORY_GRIPE_HINT not in result
    assert MEMORY_GRIPE_HINT not in second


@pytest.mark.parametrize(
    "extra", [{"view": "fisheye+1hop"}, {"extent": "fisheye+1hop"}, {"view": "raw"}]
)
def test_hint_absent_for_other_views(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, str],
) -> None:
    original = "An unchanged view.\n"
    monkeypatch.setattr(
        runtime_with_store,
        "_dispatch_with_retry",
        lambda *a, **kw: Response(body=original),
    )
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "get", {"kind": "memory", "id": _memory(store), **extra}
        )
    assert not failed, result
    assert result == original
