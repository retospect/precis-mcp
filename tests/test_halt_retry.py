"""Retry safety requires complete wire evidence, never missing ledger rows."""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.workers.halt_retry import assess_halt_retry


def _stream(*events: dict[str, Any]) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init"},
            *events,
            {"type": "result", "subtype": "success", "is_error": False},
        )
    )


def _call(name: str = "mcp__precis__get", **args: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "call-1", "name": name, "input": args}
            ]
        },
    }


def _result(*, error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "is_error": error,
                }
            ]
        },
    }


def test_complete_no_tool_tick_is_retry_safe() -> None:
    assert assess_halt_retry(_stream())[0]


@pytest.mark.parametrize("error", [False, True])
def test_completed_read_is_retry_safe_even_when_it_failed(error: bool) -> None:
    assert assess_halt_retry(
        _stream(
            _call(kind="memory", id="me1", extent="fisheye+2hop"),
            _result(error=error),
        )
    )[0]


@pytest.mark.parametrize(
    "name,args",
    [
        ("mcp__precis__put", {"kind": "memory", "text": "new claim"}),
        ("mcp__precis__get", {"kind": "part", "id": "C25804"}),
        ("mcp__precis__get", {"kind": "memory", "id": "me1", "op": "process"}),
        (
            "mcp__precis__get",
            {"kind": "memory", "id": "me1", "args": {"process": True}},
        ),
        ("mcp__precis__get", {"kind": "memory", "id": "me1", "mode": "process"}),
        ("mcp__precis__get", {"kind": "memory", "id": "me1", "view": "unknown"}),
        ("mcp__precis__search", {"kind": "*", "q": "query"}),
        ("Bash", {"command": "touch file"}),
        ("Agent", {"prompt": "write something"}),
        ("mcp__precis__precis", {"command": "put memory", "text": "claim"}),
        ("unknown", {}),
    ],
)
def test_potential_writes_require_human_review_even_after_success(
    name: str, args: dict[str, Any]
) -> None:
    assert not assess_halt_retry(_stream(_call(name, **args), _result()))[0]


def test_missing_read_result_is_not_a_complete_audit() -> None:
    assert not assess_halt_retry(_stream(_call(kind="memory", id="me1")))[0]


def test_orphan_tool_result_catches_missing_invocation() -> None:
    assert not assess_halt_retry(_stream(_result()))[0]


def test_sidechain_write_is_not_ignored() -> None:
    call = _call("mcp__precis__put", kind="memory", text="a subagent claim")
    call["parent_tool_use_id"] = "sidechain"
    assert not assess_halt_retry(_stream(call, _result()))[0]


@pytest.mark.parametrize(
    "audit",
    [
        "",
        "plain text halt conclusion",
        "{}",
        "[]",
        '{"type":"system","subtype":"init"}',
        '{"type":"result","is_error":false}',
        _stream() + "\n…(truncated)",
        _stream().replace('"is_error": false', '"is_error": null'),
    ],
)
def test_unavailable_or_incomplete_audit_fails_closed(audit: str) -> None:
    assert not assess_halt_retry(audit)[0]


def test_duplicate_invocation_fails_closed() -> None:
    call = _call(kind="memory", id="me1")
    assert not assess_halt_retry(_stream(call, call, _result()))[0]


def test_query_variant_is_explicitly_read_only() -> None:
    assert assess_halt_retry(
        _stream(
            _call("mcp__precis__search", kind="taxon", q="term", args={"under": "tn1"}),
            _result(),
        )
    )[0]
