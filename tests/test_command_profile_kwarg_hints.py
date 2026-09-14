"""gr338670: the command-profile ``precis(command=...)`` entrypoint's
error rendering for two "error doesn't name the fix" gaps.

1. ``link(..., to=...)`` fails with Python's own ``unexpected keyword
   argument 'to'`` (``TOOL_REGISTRY["link"]["func"]`` has a fixed
   signature, no ``to=``) — the ``next:`` hint must name ``target=``
   explicitly rather than only pointing at the skill.
2. ``ast.parse``'s ``SyntaxError`` for a stray non-ASCII punctuation
   character (em-dash, curly quotes, ...) must, when the command also
   looks like misplaced prose, point at the ``text=`` escape hatch.

:mod:`precis.server`'s ``_kwarg_alias_hint`` is unit-tested directly
(mirrors ``test_server_transport.py``'s precedent for importing
``precis.server`` narrowly); ``precis()`` itself is exercised end to
end with a stateless runtime mounted onto ``tools.core._runtime``,
same shape as ``test_mcp_put_edit_kwarg_doors.py``'s ``mounted_runtime``
fixture — the TypeError/SyntaxError paths never touch the store.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import server
from precis.runtime import PrecisRuntime
from precis.tools import core as tools_core
from precis.tools.command_parser import CommandParseError, parse_command


@pytest.fixture
def mounted_runtime(runtime: PrecisRuntime) -> Iterator[PrecisRuntime]:
    tools_core._runtime = runtime
    try:
        yield runtime
    finally:
        tools_core._runtime = None


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def _is_error(out: Any) -> bool:
    return isinstance(out, CallToolResult) and bool(out.isError)


# ---------------------------------------------------------------------------
# _kwarg_alias_hint — pure function
# ---------------------------------------------------------------------------


def test_kwarg_alias_hint_names_target_for_link_to() -> None:
    exc = TypeError("link() got an unexpected keyword argument 'to'")
    hint = server._kwarg_alias_hint("link", exc)
    assert hint is not None
    assert "target=" in hint


def test_kwarg_alias_hint_covers_search_k_and_limit() -> None:
    for bad in ("k", "limit"):
        exc = TypeError(f"search() got an unexpected keyword argument '{bad}'")
        hint = server._kwarg_alias_hint("search", exc)
        assert hint is not None
        assert "page_size=" in hint


def test_kwarg_alias_hint_none_when_no_alias_known() -> None:
    exc = TypeError("link() got an unexpected keyword argument 'bogus'")
    assert server._kwarg_alias_hint("link", exc) is None


def test_kwarg_alias_hint_none_on_unrelated_typeerror() -> None:
    exc = TypeError("link() missing 1 required keyword-only argument: 'target'")
    assert server._kwarg_alias_hint("link", exc) is None


# ---------------------------------------------------------------------------
# precis(command=...) end to end — link(to=...)
# ---------------------------------------------------------------------------


def test_command_link_to_kwarg_names_target(mounted_runtime: PrecisRuntime) -> None:
    out = server.precis(command="link(kind='gripe', id=1, to='gripe:2')")
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "target=" in body


# ---------------------------------------------------------------------------
# parse_command — invalid-character SyntaxError + misplaced-text hint
# ---------------------------------------------------------------------------


def test_parse_command_invalid_char_hints_text_param_when_long() -> None:
    # Unquoted prose with an em-dash pasted straight into command=,
    # long enough to trip the "looks like misplaced text" heuristic.
    command = (
        "put(kind='memory', text=this has an em-dash — right in the "
        "middle of a long unquoted paragraph that should have used text=)"
    )
    with pytest.raises(CommandParseError, match="text= parameter"):
        parse_command(command)


def test_parse_command_invalid_char_short_command_no_extra_hint() -> None:
    # Same invalid character, but short and with no text-ish param name —
    # a plain typo'd stray character, not the misplaced-prose shape.
    command = "get(kind=—)"
    with pytest.raises(CommandParseError) as exc_info:
        parse_command(command)
    assert "text= parameter" not in str(exc_info.value)
