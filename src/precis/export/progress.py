"""Export progress hook: one ambient callback the exporters report into.

The web layer (``precis_web.export_jobs``) installs a callback with
:func:`bind` around a background export; the exporters call :func:`report`
at coarse stages and per chunk. With no callback bound (CLI, tests, the
worker job) :func:`report` is a no-op, so the export signatures stay
unchanged. A ``ContextVar`` carries the callback across
``asyncio.to_thread`` (which copies the context).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: ``(stage, done, total)`` — ``done``/``total`` are ``None`` for a stage
#: without a countable unit.
ProgressFn = Callable[[str, int | None, int | None], None]

_CURRENT: ContextVar[ProgressFn | None] = ContextVar("export_progress", default=None)


@contextmanager
def bind(fn: ProgressFn) -> Iterator[None]:
    """Route :func:`report` calls in this context to ``fn``."""
    token = _CURRENT.set(fn)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def report(stage: str, done: int | None = None, total: int | None = None) -> None:
    """Report progress; never raises (a broken callback must not fail an export)."""
    fn = _CURRENT.get()
    if fn is None:
        return
    try:
        fn(stage, done, total)
    except Exception:  # pragma: no cover — defensive
        pass
