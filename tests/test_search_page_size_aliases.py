"""``search(k=...)`` / ``search(limit=...)`` — aliases for ``page_size=``
(gr338441 item 3).

Declared as real parameters (not swallowed via ``**kw``) so gr334695's
strict unknown-kwarg rejection doesn't reject them, then normalised onto
``page_size`` — the one name every downstream handler actually reads.
These tests stub ``_dispatch`` to capture the resolved payload rather
than standing up a full runtime; the behaviour under test is entirely
in the alias-resolution boundary code, before dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult

from precis.tools import core as tools_core


@pytest.fixture
def captured_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``_dispatch`` to record the payload instead of hitting a
    real runtime; ``search()``'s alias-resolution runs entirely before
    this point."""
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        captured["verb"] = verb
        captured.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)
    return captured


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def _is_error(out: Any) -> bool:
    return isinstance(out, CallToolResult) and bool(out.isError)


def test_k_behaves_as_page_size(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x", k=5)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 5


def test_limit_behaves_as_page_size(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x", limit=7)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 7


def test_agreeing_page_size_and_alias_is_fine(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x", page_size=5, k=5)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 5


def test_conflicting_k_and_limit_is_bad_input() -> None:
    out = tools_core.search(kind="memory", q="x", k=5, limit=10)
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "page_size" in body


def test_conflicting_page_size_and_k_names_page_size_as_canonical() -> None:
    out = tools_core.search(kind="memory", q="x", page_size=20, k=5)
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "page_size" in body


def test_explicit_page_size_at_default_value_still_conflicts() -> None:
    # page_size=10 equals the effective default, but it was explicitly
    # passed — a disagreeing k= must still be a conflict, not a silent
    # override (reviewer finding on gr338443).
    out = tools_core.search(kind="memory", q="x", page_size=10, k=5)
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "page_size=10" in body


def test_no_size_args_resolves_to_default(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x")
    assert not _is_error(out)
    assert captured_payload["page_size"] == 10
