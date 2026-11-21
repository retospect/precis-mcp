"""Builders for the per-task session scripts a scenario writes.

Each function returns one step dict of the vocabulary ``shim.py`` runs;
:func:`attempt` wraps a step list (and optionally the final message) as one
session's script; :func:`builder` is the whole well-behaved builder. A
scenario then reads as the story it tells::

    d.write_script(plan.build, [
        steps.attempt(*steps.builder("docs/a.md", "text")),
        steps.attempt(steps.write("docs/a.md", "more"), steps.commit(), steps.push(), steps.verify()),
    ])
"""

from __future__ import annotations

from typing import Any

Step = dict[str, Any]


def write(path: str, text: str, *, append: bool = False) -> Step:
    return {"do": "write", "path": path, "text": text, "append": append}


def delete(path: str) -> Step:
    return {"do": "delete", "path": path}


def commit(message: str | None = None, *, allow_empty: bool = False) -> Step:
    step: Step = {"do": "commit", "allow_empty": allow_empty}
    if message:
        step["message"] = message
    return step


def amend() -> Step:
    return {"do": "amend"}


def rebase(*, conflict: str | dict[str, str] = "fail", base: str | None = None) -> Step:
    """``conflict``: ``"fail"`` (abort and end the session), ``"leave"`` (leave
    the rebase in progress), or ``{path: text}`` (resolve and continue)."""
    step: Step = {"do": "rebase", "conflict": conflict}
    if base:
        step["base"] = base
    return step


def check(cmd: str = "true", *, allow_fail: bool = False) -> Step:
    return {"do": "check", "cmd": cmd, "allow_fail": allow_fail}


def push(
    *,
    on_refused: list[Step] | None = None,
    allow_refused: bool = False,
    retry: bool = True,
) -> Step:
    """Push the attempt ref. ``on_refused`` runs when the hub refuses (then
    the push is retried); ``allow_refused`` lets the session go on after a
    refusal it does not fix."""
    step: Step = {"do": "push", "allow_refused": allow_refused, "retry": retry}
    if on_refused is not None:
        step["on_refused"] = on_refused
    return step


def verify() -> Step:
    return {"do": "verify"}


def wip(why: str) -> Step:
    """Commit what exists as ``wip: snapshot - <why>``, push and verify."""
    return {"do": "wip", "why": why}


def hang_in_commit(marker: str, message: str | None = None) -> Step:
    """Block inside a commit that holds ``index.lock`` until the process
    group is killed; ``marker`` is written when the lock is held."""
    step: Step = {"do": "hang_in_commit", "marker": marker}
    if message:
        step["message"] = message
    return step


def wait_for(path: str, *, timeout: float = 600.0) -> Step:
    return {"do": "wait_for", "path": path, "timeout": timeout}


def touch(path: str, text: str = "") -> Step:
    return {"do": "touch", "path": path, "text": text}


def sleep(seconds: float) -> Step:
    return {"do": "sleep", "seconds": seconds}


def exit_(code: int = 0, say: str | None = None) -> Step:
    """End the session now, nothing more pushed."""
    step: Step = {"do": "exit", "code": code}
    if say is not None:
        step["say"] = say
    return step


def verdict(kind: str = "LAND", text: str = "", *, anchors: str | None = None) -> Step:
    """A reviewer's verdict (``LAND`` or ``FIX``) naming the head it judged."""
    step: Step = {"do": "verdict", "verdict": kind, "text": text}
    if anchors:
        step["anchors"] = anchors
    return step


def attempt(*steps: Step, say: str | None = None) -> dict[str, Any]:
    """One session's script. ``say`` is the final message unless a verdict
    step already made one."""
    out: dict[str, Any] = {"steps": list(steps)}
    if say is not None:
        out["say"] = say
    return out


def builder(path: str, text: str, *, message: str | None = None) -> list[Step]:
    """The well-behaved builder: write, commit, rebase onto the base, run a
    trivial check, push to the attempt ref and read it back."""
    return [
        write(path, text),
        commit(message),
        rebase(),
        check(),
        push(),
        verify(),
    ]
