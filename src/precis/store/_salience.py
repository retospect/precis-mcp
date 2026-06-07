"""Dream-actor suppression for salience bumps.

The salience signal (``last_seen``) must reflect **external** access
only. If the dreaming worker's own searches advanced ``last_seen`` it
would heat the region it is currently wandering into an echo chamber
(docs/design/dreaming.md, §Access accounting: "Dream-actor reads
excluded ... otherwise the dreamer heats its own wandering").

There is no per-request actor identity plumbed through the search path,
and the dream loop runs in its own worker process, so the cleanest
"filter on set_by" is a process/contextvar flag: the dream worker wraps
its run in :func:`as_dream_actor`, and every ``bump_salience`` call made
while that flag is set is a no-op. Default off → ordinary agent/human
reads bump as normal.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_dream_actor: ContextVar[bool] = ContextVar("precis_dream_actor", default=False)


def dream_actor_active() -> bool:
    """True when the current context is a dream run (suppress bumps)."""
    return _dream_actor.get()


@contextmanager
def as_dream_actor() -> Iterator[None]:
    """Mark the enclosed block as dream-actor work.

    Salience bumps inside the block are suppressed so the dreamer's own
    reads don't advance ``last_seen``. Token-scoped so nested/concurrent
    use restores cleanly.
    """
    token = _dream_actor.set(True)
    try:
        yield
    finally:
        _dream_actor.reset(token)
