"""Per-tick runtime context for the **in-process** agent loop.

The ``claude -p`` planner tick (``workers/job_types/plan_tick``) hands the
spawned MCP subprocess its runtime context — the parent todo, the workspace,
the model, the agentlog id — through env vars (``PRECIS_CURRENT_TODO`` &c.),
which the handlers read back via ``os.environ``. That works because each tick
is its own OS process, so the env is isolated per tick.

The **OpenAI-backend** tick (``OPENAI_TOOLS`` transport, ADR 0046) has no
subprocess: it drives the precis verbs *in-process* via ``runtime.dispatch``.
So the env back-doors would resolve against the **worker's** ``os.environ``,
not the tick's — and worse, ``claude_inproc`` runs claimed jobs in a
``ThreadPoolExecutor`` when ``PRECIS_INPROC_CONCURRENCY>1``, so two concurrent
ticks mutating ``os.environ`` would clobber each other and cross-attribute one
tick's children to the other's parent.

This module carries that context in a :class:`~contextvars.ContextVar` instead
— **thread-isolated by construction** (each pool thread has its own value) and
set only for the synchronous span of one tick. The env-reader helpers
(``workspace.current_todo_from_env`` &c.) consult :func:`current` first and
fall back to ``os.environ``, so:

* the **spawned claude tick** never sets the ContextVar (different process) →
  reads env exactly as before — byte-identical;
* an **operator CLI session / test** never sets it → reads env — unchanged;
* the **in-process OSS tick** sets it via :func:`tick_context` → the nested
  ``runtime.dispatch`` calls on the *same thread* see the right parent/workspace
  /model/agentlog, with no env mutation and no cross-tick bleed.

Pure stdlib — imports nothing from ``precis`` — so the low-level readers
(``utils.workspace``, ``agentlog``) can import it without a cycle.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TickContext:
    """The runtime context for one in-process agent tick.

    Mirrors the env back-doors the spawned-subprocess tick sets: the parent
    todo children default under, the workspace path file-kinds route by, the
    model the tick runs on, and the agentlog id draft writes attribute to. Any
    field may be ``None`` (no workspace bound, no agentlog opened, …); a reader
    only overrides when its field is present.

    ``disabled_kinds`` is the in-process equivalent of the claude path's
    ``PRECIS_KINDS_DISABLED`` env back-door: a tuple of ``(kind, hint)`` pairs
    the runtime rejects *for the duration of this tick only* (plan_tick gates
    the draft's colliding prose-file kind so the planner writes into the draft,
    not a freestanding file). It can't be an env var here — the in-process Hub
    is built once at worker boot and the kind-gate is construction-time, so a
    per-tick prohibition has to ride the ContextVar and be honored per-call
    (:meth:`precis.runtime.PrecisRuntime._resolve_handler`). Empty ⇒ no
    per-tick prohibition.
    """

    parent_todo: int | None = None
    workspace: str | None = None
    model: str | None = None
    agentlog_id: int | None = None
    disabled_kinds: tuple[tuple[str, str], ...] = ()


_CURRENT: ContextVar[TickContext | None] = ContextVar(
    "precis_inproc_tick", default=None
)


def current() -> TickContext | None:
    """The active in-process tick context for *this thread*, or ``None``.

    ``None`` means no in-process tick is running on this thread — the caller
    (an env-reader helper) then falls back to ``os.environ``.
    """
    return _CURRENT.get()


@contextlib.contextmanager
def tick_context(ctx: TickContext) -> Iterator[None]:
    """Bind ``ctx`` as the active tick context for the duration of the block.

    Thread-isolated: a concurrent tick on another pool thread has its own
    binding. The token is reset on exit (including on exception), so a tick
    never leaks its context onto the next job the thread picks up.
    """
    token = _CURRENT.set(ctx)
    try:
        yield
    finally:
        _CURRENT.reset(token)


__all__ = ["TickContext", "current", "tick_context"]
