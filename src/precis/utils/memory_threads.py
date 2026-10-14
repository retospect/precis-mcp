"""``section:threads`` node conventions shared by the sweep, the handler
hints and the index renderer.

A *thread node* is a memory tagged ``section:threads`` — one per campaign,
its first body line a ``Left: …`` resume state (``Left: none`` when the
campaign has nothing left). The conventions live here because three
consumers must agree on them: the session-start render
(:mod:`precis.cli.memory`), the write-path hints
(:mod:`precis.handlers.memory` / :mod:`precis.utils.memory_hubs`), and the
retirement sweep (:mod:`precis.workers.memory_thread_sweep`).
"""

from __future__ import annotations

import re

#: Open tag marking a memory as a campaign resume node.
THREADS_TAG = "section:threads"

#: Open tag marking a memory as a section hub — never a sweep candidate,
#: even if it also carries ``section:threads``.
INDEX_TAG = "section:index"

#: Days without an edit after which a thread node is stale. The same
#: window ``precis memory index --days`` calls "live" — the sweep retires
#: what the index has already stopped listing.
STALE_DAYS = 14

#: First-line state marker: ``Left: <what remains>`` (``Left: none`` ends
#: the campaign).
_LEFT_RE = re.compile(r"^left:\s*(?P<what>.*?)\s*$", re.IGNORECASE)

#: ``Left:`` values that mean the campaign is over (rule a).
_LEFT_DONE_RE = re.compile(r"^none\.?$", re.IGNORECASE)

#: The date/timestamp that ends a thread title's *prefix* — titles carry
#: the resume date (``Fleet wave — 2026-10-14``), so the campaign identity
#: is the text before the first date or time.
_TITLE_STAMP_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}")

_TRAILING_SEPARATORS = " -–—:;,|("


def first_left(body: str) -> str | None:
    """The ``Left:`` value of a thread body's first non-empty line.

    Returns the text after ``Left:`` (stripped), or ``None`` when the
    first line is not a ``Left:`` line — a node without the convention
    carries no declared resume state at all.
    """
    first = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
    m = _LEFT_RE.match(first)
    return m.group("what") if m else None


def left_is_done(left: str) -> bool:
    """True when a ``Left:`` value closes the campaign (``Left: none``)."""
    return bool(_LEFT_DONE_RE.match(left.strip()))


def title_prefix(title: str) -> str:
    """A thread title's campaign prefix: the text before the first date or
    timestamp, normalized for comparison (lowercase, single spaces, no
    trailing separators). ``''`` when the title *is* just a stamp."""
    prefix = _TITLE_STAMP_RE.split(title or "", maxsplit=1)[0]
    prefix = " ".join(prefix.lower().split()).rstrip(_TRAILING_SEPARATORS)
    return prefix.strip()


__all__ = [
    "INDEX_TAG",
    "STALE_DAYS",
    "THREADS_TAG",
    "first_left",
    "left_is_done",
    "title_prefix",
]
