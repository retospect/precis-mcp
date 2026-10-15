"""Per-request state for handlers shared across concurrent calls.

One handler instance serves every request in a process: the MCP server
offloads tools to worker threads (``server._offload_sync``) and the web
app shares its runtime, so two calls can run on the same handler at
once. A subclass that needs to thread a request-only value from its
public verb into a base-class hook (``put`` → ``_create``) must not park
it on ``self`` — the other call reads or clears it.

:class:`RequestState` holds that value in a :class:`~contextvars.ContextVar`
instead: each thread (and each ``anyio.to_thread`` / ``copy_context``
run) sees its own binding. :meth:`RequestState.bind` restores the outer
binding by token on exit, so nested calls and exceptions unwind cleanly.
``tests/test_singleton_request_state_repro.py`` pins the isolation, and
its AST guard refuses a new ``self._pending*`` assignment in a handler.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar


class RequestState[T]:
    """A request-scoped slot with a default for calls that never bind it."""

    def __init__(self, name: str, default: T) -> None:
        self._var: ContextVar[T] = ContextVar(name, default=default)

    def get(self) -> T:
        return self._var.get()

    @contextlib.contextmanager
    def bind(self, value: T) -> Iterator[T]:
        token = self._var.set(value)
        try:
            yield value
        finally:
            self._var.reset(token)
