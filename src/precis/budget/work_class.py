"""Work class — who is waiting on an LLM call (gr347551, Reto 2026-10-12).

The quota taper used to see only the tier, so the 07:00 morning briefing
(FRONTIER) paused at the seven-day warning exactly like a background quest
tick and nothing reached #news. The taper now reads a second axis:

* ``interactive`` — a person is waiting right now: asa conversational
  turns, web UI actions, session MCP tool calls. Never tapered; only the
  hard park (window exhausted / rejected) stops it.
* ``deliverable`` — a scheduled output with a reader waiting (morning
  briefing, briefing_audio, other recurring posts). Tapered later than
  background, and the caller degrades to a smaller tier and posts a notice
  instead of pausing silently.
* ``background`` — every other worker pass (quest ticks, dream, taproot,
  deep_review, structural, llm_summarize, classify, ...). Paused first.

Resolution, most specific first: the request's own ``work_class`` →
the ambient :data:`_current` context (set by an entry point with
:func:`work_class_scope`) → ``background``. Defaulting to background keeps
every unmarked worker pass on the old rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum


class WorkClass(StrEnum):
    INTERACTIVE = "interactive"
    DELIVERABLE = "deliverable"
    BACKGROUND = "background"


_current: ContextVar[WorkClass | None] = ContextVar("precis_work_class", default=None)


def resolve(explicit: str | WorkClass | None = None) -> WorkClass:
    """The effective class: ``explicit`` → ambient scope → background. An
    unknown string falls back to background (never fail a call on a label)."""
    for value in (explicit, _current.get()):
        if value is None:
            continue
        try:
            return WorkClass(value)
        except ValueError:
            continue
    return WorkClass.BACKGROUND


@contextmanager
def work_class_scope(cls: str | WorkClass) -> Iterator[WorkClass]:
    """Mark every LLM call made inside the block (same thread / task) as
    ``cls`` unless the request names its own class."""
    resolved = WorkClass(cls)
    token = _current.set(resolved)
    try:
        yield resolved
    finally:
        _current.reset(token)


__all__ = ["WorkClass", "resolve", "work_class_scope"]
