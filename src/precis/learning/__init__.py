"""Private, append-only self-reported learning engagement.

``LearningStore`` shares the existing Store pool, but never creates refs,
chunks, embeddings, reviews or mastery claims. A studied check-in records
engagement even when its duration is short or unknown; suggested minutes
are removable guidance, never a completion threshold.

Every operation revalidates the trusted request principal before touching
private rows. Explicit owner predicates and transaction-local forced RLS
provide independent boundaries. Operators remain trusted; the custom setting
is set only by the server and does not grant an untrusted DB client identity.
Check-ins lock the owner's settings row to serialize idempotency and date
corrections without editing history. Each correction supersedes the current
event for that date; retries return their original event even after correction.
An omitted retry date keeps the original resolved date across midnight, and
an accepted explicit date remains retryable after timezone or clock changes.

Calendar dates are reported local dates, retained across timezone changes.
The activity grid reduces the latest unsuperseded check-in for each date;
``last_studied`` considers all current past dates, beyond the displayed grid.
No aggregate from another source is treated as dated learning evidence.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from precis.errors import BadInput, Conflict, NotFound, PrecisError, Upstream
from precis.user_context import require_user

if TYPE_CHECKING:
    from precis.store import Store

__all__ = ["LearningEvent", "LearningSettings", "LearningStore"]

_EVENT_COLS = "event_id, study_date, status, minutes, note, source, created_at"


@dataclass(frozen=True, slots=True)
class LearningSettings:
    """The current owner's local day boundary and optional session suggestion."""

    timezone: str = "UTC"
    typical_session_minutes: int | None = 30


@dataclass(frozen=True, slots=True)
class LearningEvent:
    """One immutable self-report; an older correction remains readable."""

    id: int
    study_date: date
    status: str
    minutes: int | None
    note: str
    source: str
    created_at: datetime


def _event(row: Sequence[Any]) -> LearningEvent:
    return LearningEvent(
        id=int(row[0]),
        study_date=row[1],
        status=row[2],
        minutes=row[3],
        note=row[4],
        source=row[5],
        created_at=row[6],
    )


def _zone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BadInput(
            "timezone must be an IANA timezone name",
            next="set timezone='UTC' or a name such as 'Europe/Dublin'",
        )
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BadInput(
            "unknown timezone",
            next="set timezone='UTC' or a valid IANA name such as 'Europe/Dublin'",
        ) from exc


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BadInput(
            "now must be a timezone-aware datetime",
            next="supply an aware UTC datetime or omit now",
        )
    return value


def _minutes(value: int | None, *, suggestion: bool = False) -> None:
    minimum = 1 if suggestion else 0
    if value is not None and (type(value) is not int or not minimum <= value <= 1440):
        name = "typical_session_minutes" if suggestion else "minutes"
        raise BadInput(
            f"{name} must be an integer from {minimum} to 1440, or null",
            next=f"set {name}=null to leave the duration unspecified",
        )


def _text(value: str, *, name: str, maximum: int, minimum: int = 0) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or "\x00" in value
        or (minimum > 0 and not value.strip())
    ):
        raise BadInput(
            f"{name} must contain {minimum} to {maximum} characters without NUL",
            next=f"supply a {'nonblank ' if minimum else ''}{name} within that limit",
        )


def _study_date(
    value: str | date | None, *, today: date, allow_future: bool = False
) -> date:
    if value is None or value == "":
        return today
    parsed: date
    if type(value) is date:
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise BadInput(
                "study_date must be a date in YYYY-MM-DD form",
                next="supply study_date='YYYY-MM-DD' or omit it for your local today",
            ) from exc
        if parsed.isoformat() != value:
            raise BadInput(
                "study_date must be a date in YYYY-MM-DD form",
                next="supply study_date='YYYY-MM-DD' or omit it for your local today",
            )
    else:
        raise BadInput(
            "study_date must be a date in YYYY-MM-DD form",
            next="supply study_date='YYYY-MM-DD' or omit it for your local today",
        )
    if not allow_future and parsed > today:
        raise BadInput(
            "study_date cannot be in the future in your configured timezone",
            next="use your local today or an earlier date; check /settings timezone",
        )
    return parsed


class LearningStore:
    """Owner-only learning SQL over a shared existing Store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @contextmanager
    def _tx(self, owner: int) -> Iterator[psycopg.Connection]:
        try:
            with self._store.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "SELECT set_config('precis.learning_user_id', %s, true)",
                    (str(owner),),
                )
                yield conn
        except PrecisError:
            raise
        except psycopg.Error as exc:
            # SQL diagnostics can contain the private note. Surface only a
            # bounded service error; generic tool logging must not copy it.
            raise Upstream(
                "learning storage is unavailable",
                next="retry the learning operation when the database is available",
            ) from exc

    @staticmethod
    def _settings(conn: psycopg.Connection, owner: int) -> LearningSettings:
        row = conn.execute(
            "SELECT timezone, typical_session_minutes FROM learning_settings "
            "WHERE user_id = %s",
            (owner,),
        ).fetchone()
        return LearningSettings(*row) if row else LearningSettings()

    @staticmethod
    def _lock_settings(conn: psycopg.Connection, owner: int) -> LearningSettings:
        conn.execute(
            "INSERT INTO learning_settings (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (owner,),
        )
        row = conn.execute(
            "SELECT timezone, typical_session_minutes FROM learning_settings "
            "WHERE user_id = %s FOR UPDATE",
            (owner,),
        ).fetchone()
        assert (
            row is not None
        )  # The insert and owner-scoped lock share one transaction.
        return LearningSettings(*row)

    def settings(self) -> LearningSettings:
        """Read defaults or saved settings after revalidating the owner."""
        owner = require_user(self._store)
        with self._tx(owner) as conn:
            return self._settings(conn, owner)

    def set_settings(
        self, *, timezone: str, typical_session_minutes: int | None
    ) -> LearningSettings:
        """Replace only this owner's preferences; null removes the suggestion."""
        owner = require_user(self._store)
        _zone(timezone)
        _minutes(typical_session_minutes, suggestion=True)
        with self._tx(owner) as conn:
            self._lock_settings(conn, owner)
            conn.execute(
                "UPDATE learning_settings SET timezone = %s, "
                "typical_session_minutes = %s, updated_at = now() WHERE user_id = %s",
                (timezone, typical_session_minutes, owner),
            )
        return LearningSettings(timezone, typical_session_minutes)

    def checkin(
        self,
        *,
        study_date: str | date | None = None,
        status: str,
        minutes: int | None = None,
        note: str = "",
        idempotency_key: str,
        now: datetime | None = None,
    ) -> LearningEvent:
        """Append a correction or return the original exactly matching retry."""
        owner = require_user(self._store)
        if not isinstance(status, str) or status not in ("studied", "rest"):
            raise BadInput(
                "status must be studied or rest",
                options=["studied", "rest"],
                next="set status='studied' for engagement or status='rest' for rest",
            )
        _minutes(minutes)
        if status == "rest" and minutes is not None:
            raise BadInput(
                "rest check-ins cannot contain minutes",
                next="set minutes=null for rest, or choose status='studied'",
            )
        _text(note, name="note", maximum=500)
        _text(idempotency_key, name="idempotency_key", minimum=1, maximum=128)
        instant = _now(now)
        with self._tx(owner) as conn:
            settings = self._lock_settings(conn, owner)
            original = conn.execute(
                f"SELECT {_EVENT_COLS} FROM learning_events "
                "WHERE user_id = %s AND idempotency_key = %s",
                (owner, idempotency_key),
            ).fetchone()
            if original:
                event = _event(original)
                # Retrying an accepted payload must not depend on a new local
                # day boundary. Only a new event receives future-date checks.
                reported = _study_date(
                    study_date, today=event.study_date, allow_future=True
                )
                if (event.study_date, event.status, event.minutes, event.note) != (
                    reported,
                    status,
                    minutes,
                    note,
                ):
                    raise Conflict(
                        "idempotency_key already belongs to a different check-in payload",
                        next="retry the original payload or use a new idempotency_key",
                    )
                return event
            reported = _study_date(
                study_date, today=instant.astimezone(_zone(settings.timezone)).date()
            )
            previous = conn.execute(
                "SELECT e.event_id FROM learning_events e "
                "WHERE e.user_id = %s AND e.study_date = %s "
                "AND NOT EXISTS (SELECT 1 FROM learning_events successor "
                "WHERE successor.user_id = %s AND successor.supersedes_id = e.event_id) "
                "ORDER BY e.event_id DESC LIMIT 1",
                (owner, reported, owner),
            ).fetchone()
            row = conn.execute(
                "INSERT INTO learning_events "
                "(user_id, study_date, status, minutes, note, idempotency_key, supersedes_id) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_EVENT_COLS}",
                (
                    owner,
                    reported,
                    status,
                    minutes,
                    note,
                    idempotency_key,
                    previous[0] if previous else None,
                ),
            ).fetchone()
            assert row is not None
            return _event(row)

    def get_event(self, event_id: int) -> LearningEvent:
        """Read an own historical event; missing and foreign IDs are identical."""
        owner = require_user(self._store)
        if type(event_id) is not int or event_id <= 0:
            raise BadInput(
                "event_id must be a positive integer",
                next="use the event ID returned by your check-in",
            )
        with self._tx(owner) as conn:
            row = conn.execute(
                f"SELECT {_EVENT_COLS} FROM learning_events "
                "WHERE user_id = %s AND event_id = %s",
                (owner, event_id),
            ).fetchone()
        if row is None:
            raise NotFound(
                "learning event not found",
                next="read /activity to find one of your recorded check-ins",
            )
        return _event(row)

    def activity(self, days: int = 84, now: datetime | None = None) -> dict[str, Any]:
        """Return a bounded local-date grid, including days without a report."""
        owner = require_user(self._store)
        if type(days) is not int or not 1 <= days <= 90:
            raise BadInput(
                "days must be an integer from 1 to 90",
                next="request days=84 for the standard activity grid",
            )
        instant = _now(now)
        with self._tx(owner) as conn:
            settings = self._settings(conn, owner)
            today = instant.astimezone(_zone(settings.timezone)).date()
            first = today - timedelta(days=days - 1)
            rows = conn.execute(
                "SELECT e.study_date, e.status, e.minutes, e.source, e.event_id "
                "FROM learning_events e WHERE e.user_id = %s "
                "AND e.study_date BETWEEN %s AND %s "
                "AND NOT EXISTS (SELECT 1 FROM learning_events successor "
                "WHERE successor.user_id = %s AND successor.supersedes_id = e.event_id) "
                "ORDER BY e.study_date, e.event_id",
                (owner, first, today, owner),
            ).fetchall()
            last = conn.execute(
                "SELECT max(e.study_date) FROM learning_events e "
                "WHERE e.user_id = %s AND e.study_date <= %s AND e.status = 'studied' "
                "AND NOT EXISTS (SELECT 1 FROM learning_events successor "
                "WHERE successor.user_id = %s AND successor.supersedes_id = e.event_id)",
                (owner, today, owner),
            ).fetchone()
        by_date = {row[0]: row for row in rows}
        grid = []
        for offset in range(days):
            day = first + timedelta(days=offset)
            event = by_date.get(day)
            grid.append(
                {
                    "date": day.isoformat(),
                    "status": event[1] if event else "unrecorded",
                    "minutes": event[2] if event else None,
                    "source": event[3] if event else None,
                    "event_id": event[4] if event else None,
                }
            )
        return {
            "timezone": settings.timezone,
            "today": today.isoformat(),
            "last_studied": last[0].isoformat() if last and last[0] else None,
            "days": grid,
        }
