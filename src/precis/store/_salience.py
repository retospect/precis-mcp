"""Background-actor suppression for salience bumps.

The salience signal (``last_seen``) must reflect **external** access
only. If a background loop's own searches advanced ``last_seen`` it
would heat the region it is currently wandering into an echo chamber
(docs/design/dreaming.md, §Access accounting: "Dream-actor reads
excluded ... otherwise the dreamer heats its own wandering").

The dreamer was the first such loop; the **watcher**
(docs/design/watching.md) is the second and has the identical failure
mode — it reads the corpus to pick salient papers and to embed citing
papers, and must not heat what it watches. So the suppression is a
generic *background-actor* flag rather than a dream-specific one: both
loops wrap their run in :func:`as_background_actor` and every
``bump_salience`` made inside is a no-op. This shared suppression is a
correctness invariant — a second loop that re-implemented it and forgot
the flag would silently re-introduce the echo chamber.

There is no per-request actor identity plumbed through the search path,
and each background loop runs in its own worker process, so the cleanest
"filter on set_by" is a process/contextvar flag. Default unset →
ordinary agent/human reads bump as normal.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar

#: Name of the background actor running in the current context, or
#: ``None`` for ordinary (external) agent/human work. Holds the name
#: rather than a bool so telemetry / future per-actor policy can tell
#: ``dream`` from ``watch`` without a second flag.
_background_actor: ContextVar[str | None] = ContextVar(
    "precis_background_actor", default=None
)


def background_actor_active() -> bool:
    """True when a background loop (dream/watch/…) owns the context.

    The single predicate ``bump_salience`` checks to decide whether to
    suppress a self-heat. Any background actor suppresses; only external
    reads bump.
    """
    return _background_actor.get() is not None


def current_background_actor() -> str | None:
    """The active background actor's name, or ``None`` for external work."""
    return _background_actor.get()


@contextmanager
def as_background_actor(name: str) -> Iterator[None]:
    """Mark the enclosed block as background-actor work.

    Salience bumps inside the block are suppressed so the loop's own
    reads don't advance ``last_seen``. Token-scoped so nested/concurrent
    use restores cleanly. ``name`` (e.g. ``"dream"`` / ``"watch"``) is
    recorded for telemetry; suppression itself is name-agnostic.
    """
    token = _background_actor.set(name)
    try:
        yield
    finally:
        _background_actor.reset(token)


# ── back-compat: the dreamer was the first background actor ──────────


def as_dream_actor() -> AbstractContextManager[None]:
    """Deprecated alias of ``as_background_actor("dream")``.

    Preserves ``with as_dream_actor(): ...`` for the dream dispatch path.
    """
    return as_background_actor("dream")
