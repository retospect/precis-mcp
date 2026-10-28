"""Complete-result budgeting: targeted refusal, never a partial page."""

from __future__ import annotations

import pytest

from precis._result_budget import check_result_budget
from precis.errors import BadInput
from precis.runtime import PrecisRuntime


def test_small_result_passes() -> None:
    check_result_budget(
        "small",
        verb="get",
        kind="skill",
        context_limited=True,
        targeted_hint=None,
    )


def test_context_oversize_refuses_with_targeted_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    with pytest.raises(BadInput) as caught:
        check_result_budget(
            "x" * 5000,
            verb="get",
            kind="skill",
            context_limited=True,
            targeted_hint="get(kind='skill', id='precis-search-help/toc')",
        )
    assert "no partial result was returned" in caught.value.cause
    assert caught.value.next == "get(kind='skill', id='precis-search-help/toc')"


def test_context_budget_zero_still_enforces_frame_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "0")
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "100")
    with pytest.raises(BadInput) as caught:
        check_result_budget(
            "x" * 101,
            verb="get",
            kind="skill",
            context_limited=True,
            targeted_hint=None,
        )
    assert "PRECIS_MAX_BODY_BYTES" in caught.value.cause


def test_oversize_skill_read_returns_no_partial_body(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "100")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "skill", "id": "precis-search-help"}, fold=True
    )
    assert is_error
    assert "no partial result was returned" in body
    assert "get(kind='skill', id='precis-search-help/toc')" in body
    assert "# precis-search-help" not in body
    assert "more(cursor=" not in body


def test_more_and_cursor_state_are_gone(runtime_with_store: PrecisRuntime) -> None:
    from precis import server
    from precis.tools import TOOL_REGISTRY
    from precis.utils.llm.precis_tools import precis_tool_specs

    assert "more" not in TOOL_REGISTRY
    assert "more" not in server.READ_ONLY_VERBS
    assert "more" not in {spec.name for spec in precis_tool_specs()}
    assert not hasattr(runtime_with_store, "fetch_more")
    assert not hasattr(runtime_with_store, "pagination")
