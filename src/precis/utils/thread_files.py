"""The thread-file slug list: the closed vocabulary of ``thread:<slug>`` tags.

A thread file is ``docs/backlog/threads/<slug>.md`` (``docs/backlog/threads/
README.md``); ``README``, ``INDEX`` and ``PRIORITIES`` sit in that
directory but are not threads. A gripe names its owning thread with the
open tag ``thread:<slug>`` (gripe system batch F slice 5), and
:func:`thread_slugs` is the list that tag is checked against.

The files are read from the checkout this process imports from (the prod
MCP server serves a watched checkout, so the list tracks main);
``PRECIS_THREADS_DIR`` overrides. A process without the directory gets
``None`` — the caller decides whether that is a refusal or an unchecked
pass.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Open-tag prefix that links a ref to its owning thread.
THREAD_TAG_PREFIX = "thread:"

_THREADS_ENV = "PRECIS_THREADS_DIR"
_NOT_THREADS = frozenset({"README", "INDEX", "PRIORITIES"})


def threads_dir() -> Path:
    """``PRECIS_THREADS_DIR``, else this checkout's ``docs/backlog/threads``."""
    env = os.environ.get(_THREADS_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "docs" / "backlog" / "threads"


def thread_slugs(root: Path | None = None) -> frozenset[str] | None:
    """Every thread slug under ``root`` (default :func:`threads_dir`), or
    ``None`` when the directory is absent."""
    path = root if root is not None else threads_dir()
    if not path.is_dir():
        return None
    return frozenset(f.stem for f in path.glob("*.md") if f.stem not in _NOT_THREADS)
