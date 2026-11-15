"""Stored failure evidence gives a bounded, deterministic diagnosis."""

from typing import Any

import pytest

from precis.workers.failure_cause import classify_failure


@pytest.mark.parametrize(
    ("meta", "tags", "event", "expected"),
    [
        (
            {"exit_code": 0, "no_commit": True, "branch_commits": []},
            [],
            "",
            "no-commit exit",
        ),
        ({}, ["infra:child-killed"], "", "crash"),
        ({"exit_code": 1}, [], "", "crash"),
        ({"failure_class": "timeout", "exit_code": -9}, [], "", "timeout"),
        ({}, [], "Process timed out", "timeout"),
        ({"retry_after": "2026-11-16T00:00Z"}, [], "", "quota"),
        ({}, [], "Quota exceeded", "quota"),
        ({}, [], "", "unknown"),
        ({"exit_code": 0, "no_commit": True}, [], "", "unknown"),
    ],
)
def test_classify_failure(
    meta: dict[str, Any], tags: list[str], event: str, expected: str
) -> None:
    cause, evidence = classify_failure(meta, tags, event)
    assert cause == expected
    assert "exit_code=" in evidence
    assert "no_commit=" in evidence
    assert "branch_commits=" in evidence
    assert "failure_class=" in evidence


def test_classify_evidence() -> None:
    _, evidence = classify_failure(
        {"exit_code": 0, "no_commit": True, "branch_commits": [], "error": "x" * 150},
        [],
        "",
    )
    assert evidence == (
        "exit_code=0, no_commit=True, branch_commits=0, failure_class=?, error="
        + "x" * 120
    )
