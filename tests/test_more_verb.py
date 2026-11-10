"""The ``more`` continuation verb (gr592662).

Agent definitions list ``mcp__precis__more`` for paging truncated reads,
but the tool was dropped with the old cursor-cache pagination — a
subagent told to "page with more" failed with "no such tool". The verb
is back in the modern idiom: ``more(cursor=...)`` re-issues the
ready-made read a truncation footer names after ``next:`` (memory's
paged ``view='members'`` listing is the canonical producer), read verbs
only, judged at every gate exactly as the hand-issued call.

DB-free: the runtime is stubbed and only the tool layer runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.tools import core
from precis.tools.command_parser import CommandParseError, parse_more_cursor
from precis.user_context import user_scope


class _Rt:
    """Stub runtime: records dispatches, renders the canonical envelope."""

    store = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch_with_status(self, verb, payload, *, fold=False):
        self.calls.append((verb, payload))
        return "page-2", False

    def render_error(self, err: Exception) -> str:
        return f"[error:{type(err).__name__}] {err}"


@pytest.fixture
def rt(monkeypatch: pytest.MonkeyPatch) -> _Rt:
    runtime = _Rt()
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime)
    return runtime


def _text(result: Any) -> str:
    from mcp.types import CallToolResult

    if isinstance(result, CallToolResult):
        assert result.isError
        return "".join(getattr(b, "text", "") for b in result.content)
    raise AssertionError(f"expected an error envelope, got {result!r}")


# ---------------------------------------------------------------------------
# parse_more_cursor — the shared unwrap every gate uses
# ---------------------------------------------------------------------------


def test_parse_more_cursor_accepts_footer_calls() -> None:
    verb, kwargs = parse_more_cursor(
        "get(kind='memory', id=474312, view='members', args={'offset': 20})"
    )
    assert verb == "get"
    assert kwargs == {
        "kind": "memory",
        "id": 474312,
        "view": "members",
        "args": {"offset": 20},
    }


@pytest.mark.parametrize("label", ["next:", "rest:", "NEXT:", " next : "])
def test_parse_more_cursor_tolerates_a_footer_label(label: str) -> None:
    verb, kwargs = parse_more_cursor(f"{label} search(kind='paper', q='x')")
    assert verb == "search"
    assert kwargs == {"kind": "paper", "q": "x"}


@pytest.mark.parametrize(
    "cursor",
    [
        "put(kind='memory', text='x')",
        "edit(kind='memory', id=1, text='x')",
        "delete(kind='todo', id=1)",
        "tag(kind='todo', id=1, add=['x'])",
        "link(kind='todo', id=1, target='gr2')",
        # Nested continuation is nonsense: more continues reads, not more.
        "more(cursor=\"get(kind='memory', id=1)\")",
    ],
)
def test_parse_more_cursor_refuses_writes_and_nesting(cursor: str) -> None:
    with pytest.raises(CommandParseError, match="read listings only"):
        parse_more_cursor(cursor)


def test_parse_more_cursor_refuses_garbage() -> None:
    with pytest.raises(CommandParseError):
        parse_more_cursor("not a call")


# ---------------------------------------------------------------------------
# The verb — delegation, refusals, envelopes
# ---------------------------------------------------------------------------


def test_more_continues_a_get_footer_call(rt: _Rt) -> None:
    out = core.more(
        cursor=(
            "get(kind='memory', id=474312, view='members', "
            "args={'offset': 20, 'depth': 1})"
        )
    )
    assert out == "page-2"
    assert rt.calls == [
        (
            "get",
            {
                "kind": "memory",
                "id": 474312,
                "view": "members",
                "q": None,
                "__extras__": {"offset": 20, "depth": 1},
            },
        )
    ]


def test_more_continues_a_search_footer_call(rt: _Rt) -> None:
    out = core.more(cursor="search(kind='paper', q='two-photon', page_size=5)")
    assert out == "page-2"
    verb, payload = rt.calls[0]
    assert verb == "search"
    assert payload["kind"] == "paper"
    assert payload["q"] == "two-photon"
    assert payload["page_size"] == 5


def test_more_tolerates_the_pasted_label(rt: _Rt) -> None:
    out = core.more(cursor="next: get(kind='skill', id='toc')")
    assert out == "page-2"
    assert rt.calls[0][0] == "get"


def test_more_without_a_cursor_names_the_shape(rt: _Rt) -> None:
    body = _text(core.more())
    assert "[error:BadInput]" in body
    assert "next:" in body
    assert "view='members'" in body
    assert rt.calls == []


@pytest.mark.parametrize(
    "cursor",
    [
        "put(kind='memory', text='x')",
        "more(cursor=\"get(kind='memory', id=1)\")",
        "not a call",
    ],
)
def test_more_refuses_non_continuations(rt: _Rt, cursor: str) -> None:
    body = _text(core.more(cursor=cursor))
    assert "[error:BadInput]" in body
    assert rt.calls == []


def test_more_bad_inner_kwarg_is_a_validation_error(rt: _Rt) -> None:
    body = _text(core.more(cursor="get(bogus=1)"))
    assert "[error:BadInput]" in body
    assert "bogus" in body
    assert rt.calls == []


def test_more_inner_error_envelope_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal from the continued read comes back verbatim (isError
    flag included) — ``more`` adds no wrapper of its own."""
    from mcp.types import CallToolResult

    class _ErrRt(_Rt):
        def dispatch_with_status(self, verb, payload, *, fold=False):
            self.calls.append((verb, payload))
            return "[error:NotFound] no such ref", True

    monkeypatch.setattr(core, "_get_runtime", lambda: _ErrRt())
    result = core.more(cursor="get(kind='memory', id=999999999)")
    assert isinstance(result, CallToolResult)
    assert result.isError
    assert "no such ref" in result.content[0].text


# ---------------------------------------------------------------------------
# The gates — ``more`` is judged as the call it continues
# ---------------------------------------------------------------------------


def test_personal_scope_judges_the_continued_call() -> None:
    """A personal token's ``more`` inherits the boundary of the read it
    re-issues: the exact learning-help read passes, everything else
    refuses — and the cursor itself is never echoed into the verdict."""
    from precis.tools.core import _personal_call_allowed

    with user_scope(1, 1):
        assert _personal_call_allowed(
            "more", {"cursor": "get(kind='skill', id='precis-learning-help')"}
        )
        assert _personal_call_allowed("more", {"cursor": "get(kind='learning')"})
        assert not _personal_call_allowed(
            "more", {"cursor": "get(kind='memory', id=474312)"}
        )
        assert not _personal_call_allowed("more", {"cursor": "garbage"})
        assert not _personal_call_allowed("more", {})


def test_slice_scope_judges_the_continued_call() -> None:
    from precis.errors import Denied
    from precis.slice_reader_scope import refusal

    # A reader grant pages a free-kind read through ``more`` exactly as
    # it could issue the get by hand.
    assert (
        refusal("reader", "more", {"cursor": "get(kind='python', id='x')"}, ()) is None
    )
    # Bound kinds stay bound to the grant's refs through the cursor.
    assert (
        refusal("reader", "more", {"cursor": "get(kind='memory', id=1)"}, ())
        is not None
    )
    # A write cursor fails closed, as does an unparseable one.
    for cursor in ("put(kind='todo', id=1, text='x')", "garbage"):
        denied = refusal("reader", "more", {"cursor": cursor}, ())
        assert isinstance(denied, Denied)
        assert "get/search continuation" in denied.cause
    # Non-dict payloads fail closed too.
    assert isinstance(refusal("reader", "more", {"cursor": 42}, ()), Denied)
