"""Identity of a per-worktree session MCP server (dev-loop-scratch D1).

``scripts/wt-mcp up`` starts a second ``precis serve`` for one git worktree
through the same ensure script as the shared session server, with
``PRECIS_MCP_WORKTREE=<tree>`` in the container's environment. That one
variable is this module's whole input. Everything here degrades to "not a
worktree server" when it is unset, so the shared server and every cluster
daemon are untouched.

What the tree name reaches:

* the ``serverInfo.instructions`` preamble (:func:`banner`) — a session
  connected to a worktree server reads ``worktree server <tree> @ <sha>``
  first, before the verb blurb;
* every ref a write creates (:func:`stamp_meta`) — ``meta.worktree`` names
  the tree whose code made it, so a bad write traces to its source;
* the ``tool_calls`` ledger's ``session_id`` (:func:`session_prefix`) —
  rows from a worktree server sort under ``wt-<tree>-``.

The sha comes from the checkout watchdog's reader
(:func:`precis.install_watchdog.checkout_fingerprint` over the ``/src``
mount), the same source ``precis-status`` reports.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Set by the ensure script in worktree mode; the tree name as
#: ``scripts/wt-mcp`` derived it (basename of the worktree, sanitised).
ENV = "PRECIS_MCP_WORKTREE"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def worktree_name() -> str | None:
    """The tree this server serves, or ``None`` on a non-worktree server.

    A malformed value is treated as unset rather than raising: this is
    read on the boot path and on every write, where a typo in an env var
    must not take the server down.
    """
    raw = (os.environ.get(ENV) or "").strip()
    if not raw or not _NAME_RE.match(raw):
        return None
    return raw


def banner(sha: str | None) -> str:
    """The instructions preamble for a worktree server, else ``""``.

    One line, ending in a newline so it stacks above the sandbox preamble
    and the verb blurb. Says what the spec requires a session to know:
    which tree and sha, that writes go to prod, and that no worker runs
    here (a job it mints runs on prod workers and prod code).
    """
    tree = worktree_name()
    if tree is None:
        return ""
    short = (sha or "")[:12] or "unknown"
    return (
        f"worktree server {tree} @ {short}: read-write on prod; runs no "
        "workers — a job minted here runs on prod workers and prod code.\n"
    )


def stamp_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """``meta`` with ``worktree: <tree>`` added on a worktree server.

    Returns the input unchanged (same object) when this is not a worktree
    server, so the shared server's writes carry no new key. Never
    overwrites a ``worktree`` key the caller set.
    """
    tree = worktree_name()
    if tree is None:
        return meta
    out = dict(meta or {})
    out.setdefault("worktree", tree)
    return out


def session_prefix() -> str:
    """``"wt-<tree>-"`` on a worktree server, else ``""``."""
    tree = worktree_name()
    return "" if tree is None else f"wt-{tree}-"
