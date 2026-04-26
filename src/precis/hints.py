"""Single-threaded HintBus collector.

Any layer can `runtime.hints.emit(Hint(...))` deep in the call tree;
the dispatcher invokes `bus.collect()` at end-of-request to drain the
contextvar, deduplicate against recent topics, cap, and return.

Dedup is novelty-decay: a topic emitted within the last `cooldown`
requests is suppressed; after that it can re-fire. So "same old advice"
naturally dampens out, but persistent conditions resurface on schedule.

Hints are non-breaking. Breaking hints live on `PrecisError.next`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

HintLevel = Literal["tip", "info", "warn"]


@dataclass(frozen=True, slots=True)
class Hint:
    """One ambient tip emitted during a request."""

    text: str
    topic: str  # dedup key; dotted preferred ('cache.stale')
    level: HintLevel = "tip"
    cooldown: int = 10  # suppress if topic shown within N requests


class HintBus:
    """Per-server hint collector. One instance per `PrecisRuntime`.

    Use:
        bus = HintBus()
        with bus.request():
            ...
            bus.emit(Hint("cache is stale", topic="cache.stale"))
            ...
            hints = bus.collect()
    """

    def __init__(
        self,
        *,
        ring_size: int = 200,
        max_per_response: int = 3,
    ) -> None:
        self._recent: deque[tuple[str, int]] = deque(maxlen=ring_size)
        self._req: int = 0
        self._max = max_per_response
        self._pending: ContextVar[list[Hint]] = ContextVar("precis_hints")

    @contextmanager
    def request(self) -> Iterator[int]:
        """Open a request scope. Hints emitted inside are collected here.

        Yields the monotonically increasing request id (useful for tests
        and audit logging)."""
        self._req += 1
        token = self._pending.set([])
        try:
            yield self._req
        finally:
            self._pending.reset(token)

    def emit(self, hint: Hint) -> None:
        """Append a hint to the current request's collector. No-op outside
        a request scope (so module-import-time emissions don't leak)."""
        try:
            self._pending.get().append(hint)
        except LookupError:
            return

    def collect(self) -> list[Hint]:
        """Drain pending hints, dedup by topic, cap, record into recent ring.

        Idempotent within a single request scope (calling collect() twice
        returns the same set the first time, then an empty list)."""
        try:
            pending = self._pending.get()
        except LookupError:
            return []
        out: list[Hint] = []
        for h in pending:
            if self._recently_shown(h.topic, h.cooldown):
                continue
            out.append(h)
            if len(out) == self._max:
                break
        for h in out:
            self._recent.append((h.topic, self._req))
        # Clear pending so a second call returns []
        pending.clear()
        return out

    def _recently_shown(self, topic: str, cooldown: int) -> bool:
        threshold = self._req - cooldown
        return any(t == topic and r > threshold for t, r in self._recent)
