#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Bounded top-hits lookup shared by the pykind nudges (gr520146).

Reto's comment-4 ruling: when a reminder hook fires on a bare-symbol or
concept grep, it runs the python-kind lexical search ITSELF and appends
the top 3 hits, so the agent sees the better answer, not just a pointer
to it. A PreToolUse hook can't call the session MCP, so this shells out
to ``scripts/coderef find`` — the same AST-symbol backend shape the
python kind uses, bounded to files containing the needle by ``git
grep`` — under a hard timeout. The child runs under THIS interpreter
(``sys.executable``; coderef is stdlib-only), never ``uv run``: the
script's project-mode shebang could sync the venv, and a sync inside a
per-grep hook may reach the network.

Every failure path returns ``[]`` (timeout, non-zero rc, missing
coderef, weird cwd): the caller then emits today's plain nudge text —
the lookup must never block or fail the tool call.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

#: Hard budget for the whole lookup — spawn + git grep + partial AST
#: index. Lexical only (the ruling: semantic may still be warming).
TIMEOUT_S = 1.0
MAX_HITS = 3

#: One hit line as ``cmd_find`` prints it: ``module::qualname  path:line``.
_HIT_RE = re.compile(r"^\S+::\S+\s+\S+:\d+$")


def _tree_root(cwd: str) -> Path | None:
    """The git worktree root containing ``cwd``, else None.

    Walks up to the first ``.git`` — a directory in a normal checkout, a
    FILE in a linked worktree — so no subprocess burns the hook budget.
    """
    if not cwd:
        return None
    try:
        d = Path(cwd).resolve()
    except OSError:
        return None
    for base in (d, *d.parents):
        if (base / ".git").exists():
            return base
    return None


def src_scoped(targets: list[str], cwd: str) -> bool:
    """True when the grep's scope covers the session tree's ``src/``.

    The lookup trigger (gr520146): an unscoped grep (no targets), a
    target inside the tree's ``src``, or a target that CONTAINS src
    (``.``, the tree root — the grep still covers src). A real target
    outside src (``tests/``, ``docs/``, a venv, another checkout) is
    grep's domain — no lookup. A MISSING target can't be classified: not
    src-scoped, no lookup (a guess would index the wrong tree).
    """
    if not cwd:
        return False
    tree = _tree_root(cwd)
    base = tree if tree is not None else Path(cwd).resolve(strict=False)
    src = base / "src"
    for arg in targets:
        p = Path(arg)
        cand = p if p.is_absolute() else Path(cwd) / p
        try:
            r = cand.resolve(strict=False)
        except OSError:
            return False
        if not r.exists():
            return False  # unclassifiable — don't guess at a tree
        if r == src or src in r.parents or r in src.parents:
            continue  # inside src/, or an ancestor that covers it
        return False
    return True


def _coderef(cwd: str) -> Path | None:
    """``scripts/coderef`` for the tree the grep ran in.

    The payload's cwd resolves to its enclosing git worktree — that
    tree's own ``scripts/coderef`` indexes the right checkout.
    ``__file__`` (the hook's own copy — hooks may run from a synced copy
    outside the repo) is the fallback ONLY when cwd isn't inside a git
    tree at all: a git tree without ``scripts/coderef`` gets no lookup —
    indexing this hook's tree would name hits from the wrong checkout.
    """
    tree = _tree_root(cwd)
    if tree is not None:
        p = tree / "scripts" / "coderef"
        return p if p.is_file() else None
    own = Path(__file__).resolve().parent.parent / "coderef"
    return own if own.is_file() else None


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the coderef child AND its process group — it spawns ``git
    grep``, which a plain ``proc.kill()`` would orphan. POSIX only; the
    Windows path keeps the direct-child kill it always had."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass  # already dead — the timeout path only needs best effort


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort group kill + reap after a failed lookup."""
    _kill_group(proc)
    try:
        proc.wait(timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass


def pykind_hits(token: str, cwd: str, *, limit: int = MAX_HITS) -> list[str]:
    """Top-``limit`` ``module::qualname  path:line`` hit lines, or ``[]``.

    ``[]`` on timeout, non-zero exit, missing coderef or unparsable
    output — the nudge's whole job is to be cheap and never wrong.
    """
    script = _coderef(cwd)
    if script is None or not sys.executable:
        return []
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script), "find", token, "--limit", str(limit)],
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=os.name == "posix",
        )
    except OSError:
        return []
    try:
        out, _ = proc.communicate(timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        _terminate(proc)
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in (ln.strip() for ln in out.splitlines()) if _HIT_RE.match(ln)][
        :limit
    ]
