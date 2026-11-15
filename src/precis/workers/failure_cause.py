"""Deterministic parked-child diagnosis from stored evidence, without log IO."""

from collections.abc import Iterable, Mapping
from typing import Any


def classify_failure(
    meta: Mapping[str, Any], tags: Iterable[str], last_event_text: str | None
) -> tuple[str, str]:
    """Prefer explicit failure markers over text; never infer a principal cause.

    Quota and timeout markers precede generic crash evidence, since either can
    also terminate a process. A clean no-commit exit requires affirmative proof.
    """
    failure_class = str(meta.get("failure_class") or "")
    error = " ".join(str(meta.get("error") or "").split())
    text = f"{failure_class} {error} {last_event_text or ''}".lower()
    exit_code = meta.get("exit_code")
    commits = meta.get("branch_commits")
    commit_count = len(commits) if isinstance(commits, list) else "?"
    if meta.get("retry_after") is not None or any(
        marker in text for marker in ("quota", "rate limit", "rate_limit")
    ):
        cause = "quota"
    elif any(marker in text for marker in ("timeout", "timed out")):
        cause = "timeout"
    elif (
        "infra:child-killed" in tags
        or failure_class == "crash"
        or (isinstance(exit_code, int) and exit_code != 0)
    ):
        cause = "crash"
    elif exit_code == 0 and meta.get("no_commit") is True and commits == []:
        cause = "no-commit exit"
    else:
        cause = "unknown"
    evidence = (
        f"exit_code={exit_code}, no_commit={meta.get('no_commit')}, "
        f"branch_commits={commit_count}, failure_class={failure_class or '?'}, "
        f"error={error[:120] or '?'}"
    )
    return cause, evidence
