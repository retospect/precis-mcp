"""``section:threads`` node conventions + the memory expiry rule.

A *thread node* is a memory tagged ``section:threads`` — one per campaign,
its first body line a ``Left: …`` resume state (``Left: none`` when the
campaign has nothing left). The conventions live here because three
consumers must agree on them: the session-start render
(:mod:`precis.cli.memory`), the write-path hints
(:mod:`precis.handlers.memory` / :mod:`precis.utils.memory_hubs`), and the
retirement sweep (:mod:`precis.workers.memory_thread_sweep`).

The expiry rule lives here for the same reason (gr676320): the garden's
review/compact detectors and the memory handler's read surfaces must
compute the same instant — a writer-set ``meta.expires`` beats the
title/section shape default, and nothing else gets a say.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

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

#: ``YYYY-MM-DD`` anywhere in a body — a thread node's *dated section*. The
#: newest one is the currency signal the landed verdict (scripts/memory-lint)
#: and the sweep's suspect guard share.
_BODY_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

#: A title that names a snapshot even without a handoff/resume section —
#: "state on/as of <date>", "handoff", "resume". Feeds the snapshot shape's
#: 14-day default (gr560763).
SNAPSHOT_TITLE = re.compile(
    r"\b(?:handoff|resume|state\s+(?:on|as\s+of)|as\s+of)\b", re.I
)

#: A title naming a campaign: "waves N-M" or an explicit date range.
#: Feeds the campaign shape's 90-day default (gr560763).
CAMPAIGN_RANGE_TITLE = re.compile(
    r"\bwaves?\s+\d+\s*[-–—]\s*\d+\b|"
    r"\b\d{4}-\d{2}-\d{2}\s*(?:[-–—]|to|through)\s*(?:\d{4}-)?\d{2}-\d{2}\b",
    re.I,
)


def newest_body_date(body: str) -> date | None:
    """The newest ``YYYY-MM-DD`` in the body, or ``None`` when undated.

    "Newest dated section" in the retire rule (gr503035): a date inside
    the stale window means the node's own text claims to be current — a
    currency suspect, never a sweep/lint retire action.
    """
    best: date | None = None
    for m in _BODY_DATE_RE.finditer(body or ""):
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best


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


def memory_expiry_shape(title: str, sections: frozenset[str]) -> tuple[str, int | None]:
    """Classify expiry from section tags and title alone (gr560763).

    An operational title overrides a standing section: a handoff filed as
    reference still expires. Incident/campaign write-ups keep 90 days;
    ordinary reference, gotchas and runbooks have no default expiry.
    DREAM exclusion belongs to the reader, before this helper is called.
    """
    if "threads" in sections:
        return "thread", 14
    if sections & {"handoff", "resume"} or SNAPSHOT_TITLE.search(title):
        return "snapshot", 14
    if sections & {"incident", "incidents"} or re.search(
        r"\b(?:incident|post[- ]?mortem)\b", title, re.I
    ):
        return "incident", 90
    if (
        sections & {"campaign", "campaigns"}
        or re.search(r"\bcampaign\b", title, re.I)
        or CAMPAIGN_RANGE_TITLE.search(title)
    ):
        return "campaign", 90
    return "standing", None


def _parse_instant(raw: Any) -> datetime | None:
    """An ISO date/datetime string or datetime, normalized to UTC.

    ``None`` for anything else — a malformed stored value must read as
    "no instant", never raise on the read path.
    """
    if not isinstance(raw, (str, datetime)):
        return None
    try:
        value = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def effective_expiry(
    title: str,
    sections: frozenset[str],
    meta: dict[str, Any],
    updated_at: datetime,
) -> tuple[datetime | None, str | None]:
    """The expiry instant and the rule that supplied it — the one shared
    computation every read surface agrees on (gr676320).

    ``meta.expires`` (normalized to an absolute instant at write time, or
    an ISO value an older writer stored) always wins; a null or malformed
    value reads as ``(None, "meta.expires")`` — the writer's declaration
    disabled the default and the garden records it unchecked. Otherwise the
    title/section shape default runs from ``updated_at``. Standing shapes
    carry no expiry: ``(None, None)``.
    """
    if "expires" in meta:
        return _parse_instant(meta["expires"]), "meta.expires"
    _shape, days = memory_expiry_shape(title, sections)
    if days is None:
        return None, None
    return updated_at + timedelta(days=days), "shape"


def expiry_line(
    title: str,
    sections: frozenset[str],
    meta: dict[str, Any],
    updated_at: datetime,
) -> str | None:
    """The ``expires: …`` line single-ref reads and create acks end with.

    ``expires: 2026-12-01 (set by writer)`` for ``meta.expires``;
    ``expires: 2026-11-28 (default: thread 14d from last edit)`` for a shape
    default. ``None`` for standing nodes — no line, no claim of permanence
    beyond the shape card's "no age expiry".
    """
    expires, source = effective_expiry(title, sections, meta, updated_at)
    if expires is None:
        return None
    stamp = expires.strftime("%Y-%m-%d")
    if source == "meta.expires":
        return f"expires: {stamp} (set by writer)"
    shape, days = memory_expiry_shape(title, sections)
    return f"expires: {stamp} (default: {shape} {days}d from last edit)"


__all__ = [
    "CAMPAIGN_RANGE_TITLE",
    "INDEX_TAG",
    "SNAPSHOT_TITLE",
    "STALE_DAYS",
    "THREADS_TAG",
    "effective_expiry",
    "expiry_line",
    "first_left",
    "left_is_done",
    "memory_expiry_shape",
    "newest_body_date",
    "title_prefix",
]
