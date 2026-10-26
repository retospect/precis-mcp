"""Per-request HintBus collector, safe under concurrent dispatch.

Any layer can `runtime.hints.emit(Hint(...))` deep in the call tree;
the dispatcher invokes `bus.collect()` at end-of-request to drain the
contextvar, deduplicate against recent topics, cap, and return.
Concurrent requests (see `precis.server._offload_sync`, gr330541) each
run in their own copied context, so `emit`/`collect` never cross wires
between requests; a small lock protects the shared dedup ring against
concurrent access from separate worker threads.

Dedup is novelty-decay: a topic emitted within the last `cooldown`
requests is suppressed; after that it can re-fire. So "same old advice"
naturally dampens out, but persistent conditions resurface on schedule.

Hints are non-breaking. Breaking hints live on `PrecisError.next`.
Required supersede receipts are emitted only on link/edit/put/tag, bypass
advice caps and cooldown, and deduplicate within the request. They remain in
the hint footer even on a failed call. Reads retain the ordinary cooled-down
"was merged into" note in the footer instead of the write receipt.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Literal

HintLevel = Literal["tip", "info", "warn"]


@dataclass(frozen=True, slots=True)
class Hint:
    """One ambient tip emitted during a request."""

    text: str
    topic: str  # dedup key; dotted preferred ('cache.stale')
    level: HintLevel = "tip"
    cooldown: int = 10  # suppress if topic shown within N requests
    required: bool = False  # operation receipts bypass advice caps/cooldowns
    read_text: str | None = None  # ordinary advice alternative on read requests


def merged_redirect_hint(old: str, new: str) -> Hint:
    """Non-breaking nudge after a merged/superseded ref transparently
    redirected to its live survivor (handle outliving a dedup)."""
    return Hint(
        text=f"resolved {old} -> {new} (superseded)",
        topic=f"handle.redirect.{old}",
        level="info",
        required=True,
        read_text=f"{old} was merged into {new} — please use that handle going forward.",
    )


def converted_redirect_hint(old: str, new: str) -> Hint:
    """Non-breaking nudge after a kind convert — the ref kept its
    ``ref_id``, so the old-kind handle (``pa<id>`` after paper →
    datasheet) still resolves to the same record under its new kind."""
    return Hint(
        text=(
            f"{old} converted to {new} — it still resolved, but please "
            f"update your reference to {new} going forward. Sorry for the "
            "trouble."
        ),
        topic=f"handle.redirect.{old}",
        level="info",
    )


def bare_numeric_hint(kind: str, bare: str, handle: str) -> Hint:
    """Admonish after the bare-numeric ref_id fallback (A1) fired: the address
    is the handle, and a bare number must never land in cited text."""
    return Hint(
        text=(
            f"resolved id={bare!r} by assuming it was a {kind} ref_id — but the "
            f"address is the handle {handle!r} (keep the 2-char prefix), not a "
            f"bare number. Use {handle!r} next time, and never write a bare "
            "number into cited text: a number is not a citation."
        ),
        topic=f"handle.bare_numeric.{kind}",
        level="warn",
    )


class HintBus:
    """Per-server hint collector. One instance per `PrecisRuntime`.

    Use:
        bus = HintBus()
        with bus.request():
            ...
            bus.emit(Hint("cache is stale", topic="cache.stale"))
            ...
            hints = bus.collect()

    ``_pending`` is a `ContextVar` — correctly request-scoped even when
    concurrent requests run on separate worker threads (each gets its
    own copy of the calling context, e.g. via `anyio.to_thread.run_sync`
    — gr330541). ``_req``/``_recent`` are plain shared instance state
    used only for the best-effort dedup heuristic, so ``_lock`` below
    protects them against a lost increment / torn read under that same
    concurrency, not because a race there could corrupt a response.
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
        self._verb: ContextVar[str | None] = ContextVar(
            "precis_hint_verb", default=None
        )
        self._lock = threading.Lock()

    @contextmanager
    def request(self, *, verb: str | None = None) -> Iterator[int]:
        """Open a request scope. Hints emitted inside are collected here.

        Yields the monotonically increasing request id (useful for tests
        and audit logging)."""
        with self._lock:
            self._req += 1
            req = self._req
        token = self._pending.set([])
        verb_token = self._verb.set(verb)
        try:
            yield req
        finally:
            self._verb.reset(verb_token)
            self._pending.reset(token)

    def emit(self, hint: Hint) -> None:
        """Append a hint to the current request's collector. No-op outside
        a request scope (so module-import-time emissions don't leak)."""
        if hint.required and self._verb.get() not in {"link", "edit", "put", "tag"}:
            if hint.read_text is None or self._verb.get() not in {
                None,
                "get",
                "search",
            }:
                return
            hint = replace(hint, text=hint.read_text, required=False)
        try:
            self._pending.get().append(hint)
        except LookupError:
            return

    def collect(self, *, required_only: bool = False) -> list[Hint]:
        """Drain pending hints; dedup by topic and cap/cool down advice only.

        Failed calls use ``required_only``: discarded advice must not enter
        the recent ring because the caller never saw it.
        Idempotent within a single request scope (calling collect() twice
        returns the same set the first time, then an empty list)."""
        try:
            pending = self._pending.get()
        except LookupError:
            return []
        # gr512233 slice 2: a read-scope connection can't run write
        # recipes — drop hints whose text advertises one (the text-only
        # gripe create survives ``is_write_call``). One verdict per
        # collect. Lazy imports: ``precis.runtime`` pulls this module in
        # during core setup, so a top-level import would cycle.
        from precis.runtime.scope import current_scope
        from precis.utils.skill_scope import is_write_call

        if current_scope() == "read":
            pending[:] = [h for h in pending if not is_write_call(h.text)]
        out: list[Hint] = []
        seen: set[str] = set()
        advice_count = 0
        with self._lock:
            req = self._req
            for h in pending:
                if required_only and not h.required:
                    continue
                dedup_request = h.required or h.read_text is not None
                if dedup_request and h.topic in seen:
                    continue
                if not h.required:
                    if advice_count >= self._max or self._recently_shown_locked(
                        h.topic, h.cooldown, req
                    ):
                        continue
                    advice_count += 1
                out.append(h)
                if dedup_request:
                    seen.add(h.topic)
            for h in out:
                if not h.required:
                    self._recent.append((h.topic, req))
        # Clear pending so a second call returns []
        pending.clear()
        return out

    def _recently_shown_locked(self, topic: str, cooldown: int, req: int) -> bool:
        """``_recently_shown``'s body. Caller must hold ``self._lock``."""
        threshold = req - cooldown
        return any(t == topic and r > threshold for t, r in self._recent)
