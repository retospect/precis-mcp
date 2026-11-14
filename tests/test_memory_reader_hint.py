"""The memory complaint prompt is scoped to one offer per session."""

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
from precis.runtime.trust_verb import trust_line
from precis.store import ChunkInsert, Store
from precis.tools import core


@pytest.fixture(autouse=True)
def _clean_receipts() -> Iterator[None]:
    from precis.utils.hint_ledger import _clear_for_tests

    reset_read_receipts()
    _clear_for_tests()
    yield
    reset_read_receipts()
    _clear_for_tests()


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
    assert MEMORY_GRIPE_HINT not in another
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
    trust = Mock(wraps=dispatch_module.trust_footer)
    monkeypatch.setattr(dispatch_module, "trust_footer", trust)
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
    # The once-per-session offer lands on the first read that can take
    # it — the fitted read above never offered it (gr499041).
    assert MEMORY_GRIPE_HINT in second


def test_stamped_memory_first_read_fits_without_hint_at_400_bytes(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, failed = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": "A dated claim.\n" * 100}
    )
    assert not failed, created
    ref = store.list_refs(kind="memory")[0]
    line = trust_line(store, "memory", ref.id)
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
    # A members-page read does not offer the hint; the session's one
    # offer lands on the first plain read that follows (gr499041).
    assert MEMORY_GRIPE_HINT in second


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


# ── gr564017: the complaint pointer also rides the first memory search ──
#
# Recall is where a stale or contradicted node is first *used* — the
# exact-get hint above never fires for a session that only searches, which
# is how the soul/bootstrap trial consumed memory.


def _searchable_memory(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="soulbootstrap marker")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="A soulbootstrap marker claim with evidence.",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )


def test_search_hint_first_search_only_per_session(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _searchable_memory(store)
    # Hold the session objects: plain object() has no weakref, so the
    # ledger sid falls back to id() — an inline object() can be recycled
    # across blocks and alias the dead session's receipts.
    sess1 = object()
    sess2 = object()
    with serve_ledger.session_scope(sess1):
        first = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "soulbootstrap"}
        )
        assert MEMORY_GRIPE_HINT in first
        second = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "soulbootstrap"}
        )
        assert MEMORY_GRIPE_HINT not in second
    with serve_ledger.session_scope(sess2):
        another = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "soulbootstrap"}
        )
        assert MEMORY_GRIPE_HINT in another


def test_search_hint_absent_without_session(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _searchable_memory(store)
    result = runtime_with_store.dispatch(
        "search", {"kind": "memory", "q": "soulbootstrap"}
    )
    assert "soulbootstrap marker" in result
    assert MEMORY_GRIPE_HINT not in result


def test_search_hint_absent_for_empty_result(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _searchable_memory(store)
    with serve_ledger.session_scope(object()):
        result = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "no-such-token-xyz"}
        )
    assert MEMORY_GRIPE_HINT not in result


def test_search_hint_absent_for_other_kind_and_views(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _searchable_memory(store)
    paper = store.insert_ref(kind="paper", slug="search-hint-paper", title="Paper")
    with serve_ledger.session_scope(object()):
        other = runtime_with_store.dispatch("search", {"kind": "paper", "q": "Paper"})
        counted = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "soulbootstrap", "view": "count"}
        )
        plain = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "soulbootstrap"}
        )
    assert MEMORY_GRIPE_HINT not in other
    assert MEMORY_GRIPE_HINT not in counted
    # The excluded calls above must not have consumed the session's offer.
    assert MEMORY_GRIPE_HINT in plain


def test_search_hint_does_not_consume_the_node_read_receipt(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The session-once search marker (``ref_id=-1``) and the per-node
    exact-get receipt are different keys: a search first does not suppress
    the pointer on a later exact get of a node, and vice versa."""
    ref_id = _memory(store)
    with serve_ledger.session_scope(object()):
        searched = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "dated"}
        )
        got = runtime_with_store.dispatch("get", {"id": f"me{ref_id}"})
    assert MEMORY_GRIPE_HINT in searched
    assert MEMORY_GRIPE_HINT in got


@pytest.mark.parametrize("slack", [-1, 0])
def test_search_hint_uses_only_spare_bytes(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    slack: int,
) -> None:
    original = "soulbootstrap result line\n"
    monkeypatch.setattr(
        runtime_with_store,
        "_dispatch_with_retry",
        lambda *a, **kw: Response(body=original, result_count=1),
    )
    candidate = f"{original}\n{MEMORY_GRIPE_HINT}"
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", str(len(candidate.encode()) + slack))
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "search", {"kind": "memory", "q": "x"}
        )
        assert not failed, result
        expected = candidate if slack == 0 else original
        assert result.encode("utf-8") == expected.encode("utf-8")
        assert record.call_args.args[2] == result
        # Like the get path, the offer records on return: a cap that
        # refused the append still counts as offered for the session.
        retry = runtime_with_store.dispatch("search", {"kind": "memory", "q": "x"})
    assert retry == original


def test_search_hint_failure_preserves_response(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _searchable_memory(store)
    monkeypatch.setattr(
        runtime_with_store,
        "_dispatch_with_retry",
        lambda *a, **kw: Response(body="result line\n", result_count=1),
    )
    record = Mock()
    monkeypatch.setattr(runtime_with_store, "_record_tool_call", record)
    broken = Mock(side_effect=RuntimeError("hint unavailable"))
    monkeypatch.setattr(handle_deref, "memory_search_gripe_hint", broken)
    with serve_ledger.session_scope(object()):
        result, failed = runtime_with_store.dispatch_with_status(
            "search", {"kind": "memory", "q": "x"}
        )
    broken.assert_called_once()
    assert not failed, result
    assert result == "result line\n"
    assert record.call_args.args[2] == result
