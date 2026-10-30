"""``search(k=...)`` / ``search(limit=...)`` / ``search(offset=...)`` —
legacy spellings mapped onto the bounded top-N model (gr338441 item 3,
gr530420).

``k``/``limit`` read as ``page_size`` on an ``interpreted:`` line.
``offset`` counted items in the paging model main removed: ``offset=0``
is an accepted no-op (announced the same way), ``offset>0`` refuses the
way ``page=2`` does — ``next=`` names ``page_size=`` and a narrower
query. Real-MCP coverage for the same contract lives in
``test_mcp_slim_schema.py``; these tests pin the full function's
forwarding and validation.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult

from precis.tools import core as tools_core


@pytest.fixture
def captured_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
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


def test_page_size_forwards(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="memory", q="x", page_size=7)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 7


def test_no_size_arg_forwards_none(captured_payload: dict[str, Any]) -> None:
    # Unset page_size forwards None so each handler's own default applies —
    # PaperHandler.search uses the gap to pick a mode-aware default: 25
    # papers on the default unique_per='paper' view, 10 chunks on the
    # per-hit drill view.
    out = tools_core.search(kind="memory", q="x")
    assert not _is_error(out)
    assert captured_payload["page_size"] is None


def test_k_maps_to_page_size_with_interpreted_line(
    captured_payload: dict[str, Any],
) -> None:
    out = tools_core.search(kind="memory", q="x", k=7)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 7
    assert _body(out).startswith("interpreted: 'k' read as 'page_size'")


def test_limit_maps_to_page_size_with_interpreted_line(
    captured_payload: dict[str, Any],
) -> None:
    out = tools_core.search(kind="memory", q="x", limit=7)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 7
    assert _body(out).startswith("interpreted: 'limit' read as 'page_size'")


def test_agreeing_page_size_and_alias_is_fine(
    captured_payload: dict[str, Any],
) -> None:
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


def test_offset_zero_is_an_interpreted_noop(
    captured_payload: dict[str, Any],
) -> None:
    out = tools_core.search(kind="memory", q="x", offset=0)
    assert not _is_error(out)
    assert captured_payload["page"] == 1
    assert captured_payload["page_size"] is None
    assert _body(out).startswith("interpreted: 'offset=0' is a no-op")


def test_positive_offset_refuses_naming_page_size() -> None:
    # offset>0 asks for results past the bounded top-N set — the same
    # refusal shape as page=2.
    out = tools_core.search(kind="memory", q="x", offset=5)
    assert _is_error(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "no longer paginates" in body
    assert "page_size" in body


def test_positive_offset_next_echoes_the_callers_kind() -> None:
    # gr536312: the refusal's next= shows the corrected call with the
    # caller's kind — 'paper' is the fallback only when no kind was
    # given.
    out = tools_core.search(kind="memory", q="x", offset=5)
    assert _is_error(out)
    next_line = next(
        (ln for ln in _body(out).splitlines() if ln.strip().startswith("next:")),
        "",
    )
    assert "kind='memory'" in next_line
    assert "kind='paper'" not in next_line


def test_negative_offset_refuses() -> None:
    out = tools_core.search(kind="memory", q="x", offset=-1)
    assert _is_error(out)
    assert "non-negative integer" in _body(out)


def test_limit_and_offset_zero_interpret_together(
    captured_payload: dict[str, Any],
) -> None:
    out = tools_core.search(kind="memory", q="x", limit=4, offset=0)
    assert not _is_error(out)
    assert captured_payload["page_size"] == 4
    assert _body(out).startswith("interpreted: 'limit' read as 'page_size'")
    assert "'offset=0' is a no-op" in _body(out).splitlines()[0]


def test_page_is_not_on_signature() -> None:
    # page= has no reading under the bounded top-N model — it stays
    # undeclared so the slim layer's own refusal is the only answer.
    import inspect

    params = inspect.signature(tools_core.search).parameters
    assert "page" not in params
    for alias in ("k", "limit", "offset"):
        assert alias in params


def test_page_size_must_be_positive() -> None:
    out = tools_core.search(kind="memory", q="x", page_size=0)
    assert _is_error(out)
    assert "page_size must be a positive integer" in _body(out)


def test_page_size_cap() -> None:
    out = tools_core.search(kind="memory", q="x", page_size=101)
    assert _is_error(out)
    assert "exceeds maximum 100" in _body(out)


def test_unique_per_forwards(captured_payload: dict[str, Any]) -> None:
    out = tools_core.search(kind="paper", q="x", unique_per="chunk")
    assert not _is_error(out)
    assert captured_payload["unique_per"] == "chunk"


def test_unique_per_unknown_value_is_bad_input() -> None:
    out = tools_core.search(kind="paper", q="x", unique_per="chapter")
    assert _is_error(out)
    assert "unique_per" in _body(out)
