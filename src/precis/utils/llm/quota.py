"""Shared Anthropic account-quota-exhaustion wording (gr344988, gr345336).

Anthropic's own CLI/API surfaces an exhausted usage quota as ordinary
text — never a distinguished exit code, event type, or stderr marker
(:mod:`precis.utils.claude_agent`'s stream-json ``result`` event carries
none that callers can rely on: a quota-exhausted run looks like a clean
answer — a trailing ``result`` event with no ``error_*`` subtype and
``terminal_reason='completed'``; newer CLIs add ``is_error=true`` and
``api_error_status=429``, older ones did not). Every consumer therefore has to recognize the *wording*
instead, and there have been three of them observed in prod so far, all
sharing one lead-in family:

* ``"Claude AI usage limit reached|<epoch>"`` — the CLI's own legacy
  shape, a bare unix-timestamp reset with no locale clock.
* ``"...hit your (weekly|session) limit · resets <clock> (<tz>)"``
  (gr344988).
* ``"You're out of extra usage · resets <clock> (<tz>)"`` — the
  pay-as-you-go wording (gr345336).

One module owns the wording so a fourth variant is one edit here, not a
hunt across every consumer. Reset parsing lives here as well so live
router results and executor parking agree on the next UTC instant.
Missing or malformed clock clauses keep the existing six-hour backoff;
legacy usage-limit notices keep the executor's two-hour backoff. Account
API notices use their absolute reset date, clamped to now when expired. Clock
notices take precedence over a bare 429: parking for fifteen minutes
would exhaust bounded unpark retries before the quota actually resets.
Imported by :mod:`precis.utils.llm.router`
(pauses a live dispatch whose CLEAN final text turns out to just BE a
quota notice) and :mod:`precis.workers.executors._common` (classifies a
captured failure *reason* string for retry backoff, gr344988).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Lead-ins that share a "resets <clock> (<tz>)" clause — the shape
#: :data:`QUOTA_RESET_PATTERN` extracts a wall-clock reset instant from.
#: The bare "usage limit reached|<epoch>" shape is deliberately excluded
#: here: it names an epoch, not a locale clock, and
#: :mod:`precis.workers.executors._common`'s generic transient-failure
#: bucket already classifies it at a known-good fixed backoff (gr344988)
#: — folding it into the reset-clause parser would only replace that
#: fixed answer with an attempt to parse a clause that isn't there.
_QUOTA_CLOCK_LEAD_IN = (
    r"hit your (?:weekly|session) limit" r"|(?:you'?re )?out of extra usage"
)

#: Every known lead-in, loosely — the live full-text detector
#: (:data:`QUOTA_MESSAGE_PATTERN`) keys on these alone, tolerating the
#: wording drift around them (e.g. a reworded lead sentence); a fourth
#: wording only needs a new alternative here. Anchored on multi-word
#: phrases specific to a quota notice, never on the bare word "limit" —
#: ordinary prose that happens to mention a limit must not match.
QUOTA_LEAD_IN = rf"usage limit|{_QUOTA_CLOCK_LEAD_IN}"

#: Reset-clause-parseable subset (weekly/session + pay-as-you-go), with
#: the ``hour``/``minute``/``meridiem``/``tz`` capture groups
#: :func:`parse_quota_reset_at` reads.
#: The optional trailing group means a match with the lead-in but no
#: (or an unparseable) reset clause still matches — the caller falls
#: back to a fixed conservative backoff rather than treating it as a
#: non-match.
QUOTA_RESET_PATTERN = re.compile(
    rf"(?:{_QUOTA_CLOCK_LEAD_IN})"
    r"(?:.*?resets\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<meridiem>am|pm)(?:\s*\(\s*(?P<tz>[^)]+?)\s*\))?)?",
    re.IGNORECASE | re.DOTALL,
)

#: Live-dispatch detector (router.py): does ``text`` contain ANY of the
#: known quota-exhaustion lead-ins, in any of the three known wordings or
#: a close variant. Callers additionally require the match to consume
#: (close to) the WHOLE text — see :func:`is_quota_exhaustion_text` — a
#: long legitimate answer merely quoting one of these phrases is not
#: itself a quota exhaustion.
QUOTA_MESSAGE_PATTERN = re.compile(QUOTA_LEAD_IN, re.IGNORECASE)

#: Above this length a match is nowhere near "the entire reply is the
#: quota notice" — generously sized for the three known wordings plus
#: reasonable drift (a longer reset clause, a reworded lead sentence),
#: while still excluding a real multi-paragraph answer that happens to
#: quote one of these phrases in passing.
_MAX_QUOTA_TEXT_CHARS = 160


def parse_quota_reset_at(match: re.Match[str], now: datetime) -> datetime | None:
    """Absolute UTC instant named by a matched :data:`QUOTA_RESET_PATTERN`
    "resets <clock> (<tz>)" clause, or ``None`` when that clause is absent
    or unparseable.

    The message gives a bare wall-clock time with no date — it always
    means "the next occurrence of this clock time" in the named zone (UTC
    when the ``(<tz>)`` suffix is omitted), which for a reset notice is
    always within the next 24h of ``now``.
    """
    hour_s, meridiem = match.group("hour"), match.group("meridiem")
    if hour_s is None or meridiem is None:
        return None
    hour, minute = int(hour_s), int(match.group("minute") or 0)
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    hour24 = hour % 12
    if meridiem.lower() == "pm":
        hour24 += 12
    tz_name = (match.group("tz") or "UTC").strip()
    try:
        tz = UTC if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour24, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


#: Absolute account-level reset dates can be days away (gr456240).
#: Falling back to two hours would burn unpark retries before access returns.
_API_USAGE_LIMIT_PATTERN = re.compile(
    r"regain access on\s+"
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\s+"
    r"at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?:UTC)?",
    re.IGNORECASE,
)


def parse_api_usage_reset_at(reason: str) -> datetime | None:
    """UTC account reset date, or None for a missing/malformed clause."""
    match = _API_USAGE_LIMIT_PATTERN.search(reason)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def quota_retry_at(reason: str, *, now: datetime | None = None) -> datetime | None:
    """Router-owned retry horizon; unknown wording has no quota horizon."""
    now = now if now is not None else datetime.now(UTC)
    match = QUOTA_RESET_PATTERN.search(reason)
    if match is not None:
        return parse_quota_reset_at(match, now) or now + timedelta(hours=6)
    reset_at = parse_api_usage_reset_at(reason)
    if reset_at is not None:
        return max(reset_at, now)
    if QUOTA_MESSAGE_PATTERN.search(reason) is not None:
        return now + timedelta(hours=2)
    return None


def is_quota_exhaustion_text(text: str) -> bool:
    """True when ``text`` (an agent's clean final answer) reads as
    ENTIRELY an account-quota-exhaustion notice, not a legitimate answer
    that merely mentions or quotes one.

    Requires both: a lead-in match (:data:`QUOTA_MESSAGE_PATTERN` — any of
    the three known wordings, or a close variant) AND the whole
    (whitespace-normalized) text staying under
    :data:`_MAX_QUOTA_TEXT_CHARS` chars. A long answer that happens to
    quote "hit your weekly limit" in the middle of a real response must
    not be swallowed as a pause.
    """
    if not text:
        return False
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > _MAX_QUOTA_TEXT_CHARS:
        return False
    return QUOTA_MESSAGE_PATTERN.search(normalized) is not None


__all__ = [
    "QUOTA_LEAD_IN",
    "QUOTA_MESSAGE_PATTERN",
    "QUOTA_RESET_PATTERN",
    "is_quota_exhaustion_text",
    "parse_api_usage_reset_at",
    "quota_retry_at",
]
