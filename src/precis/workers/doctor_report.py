"""``doctor_report`` — the report artifact contract for the ``doctor_tick``
job (self-healing spine Layer 3, ``docs/backlog/doctor-tick-report.md``
item 2).

The doctor's report is a ``kind='draft'`` ref, idempotent per UTC day,
tagged ``meta.author='doctor'``. Two halves:

* :func:`find_or_create_report` — mirrors the
  ``find_cast_draft``/``create_cast_draft`` idiom in
  :mod:`precis.reading.cast_common` (a standalone dated draft, **not**
  ``Store.create_draft``, which binds 1:1 to a project and would raise on
  the second day). :mod:`precis.workers.job_types.doctor_tick` calls this
  once per successful tick and appends that tick's reply as a fresh body
  paragraph — the day's report is a running log of the UTC day's ticks,
  not a single frozen snapshot.
* :func:`latest_report` — the "latest report" read side, a **plain SQL
  lookup** per the spec (``kind='draft' AND meta->>'author'='doctor'
  ORDER BY created_at DESC``), no cache key. Kept dependency-light on
  purpose: a later slice wires this into ``health_digest.py``'s push-body
  selection and ``briefing_cast.py``'s health line, and importing FROM
  either of those here would be the wrong direction — this module must
  stay importable by both without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

#: ``meta.author`` stamp on the doctor's report draft — the whole of
#: "latest report"'s selection predicate, per the spec's literal SQL.
AUTHOR: str = "doctor"

#: Spec-decided freshness window (``docs/backlog/doctor-tick-report.md``:
#: "cadence interval 8h; freshness window 12h — at least one report per
#: push window with margin"). The single shared home for this constant:
#: both ``health_digest.py``'s push-body cutover and
#: ``briefing_cast.py``'s health line pass it as ``latest_report``'s
#: ``max_age`` rather than each carrying their own copy of the number.
FRESH_WINDOW: timedelta = timedelta(hours=12)

_SLUG_PREFIX = "doctor"


def utc_date_tag(when: datetime | None = None) -> str:
    """The UTC calendar date a report belongs to, ``YYYY-MM-DD``.

    Not the wall-clock tick time — a doctor tick that happens to run in
    the last minutes before UTC midnight and one that runs just after
    both belong to their own day's report, same as
    ``cast_common.tick_date_tag``'s reasoning for a scheduled cast.
    """
    return (when or datetime.now(UTC)).strftime("%Y-%m-%d")


def report_slug(date_tag: str) -> str:
    """The draft slug (also its ``cite_key`` identifier) for a given day."""
    return f"{_SLUG_PREFIX}-{date_tag}"


def find_report(store: Store, date_tag: str) -> Any | None:
    """The existing doctor report draft for ``date_tag``, or ``None``."""
    return store.get_ref(kind="draft", id=report_slug(date_tag))


def find_or_create_report(
    store: Store,
    date_tag: str,
    *,
    title: str | None = None,
) -> tuple[Any, bool]:
    """Idempotent find-or-create of the per-UTC-day doctor report draft.

    Returns ``(ref, created)`` — a second call for the same ``date_tag``
    returns the existing ref with ``created=False`` and writes nothing,
    so a re-fired cadence window or a manual re-run never mints a second
    ref for the same day. The draft is standalone (no ``draft-of``
    project binding), for the same reason ``create_cast_draft`` is: a
    project owns exactly one draft per relation, which a *daily* artifact
    would trip on day two.
    """
    slug = report_slug(date_tag)
    existing = store.get_ref(kind="draft", id=slug)
    if existing is not None:
        return existing, False
    full_title = title or f"Doctor report — {date_tag}"
    ref = store.insert_ref(
        kind="draft",
        slug=slug,
        title=full_title,
        meta={"author": AUTHOR, "date": date_tag},
    )
    # The ``cite_key`` identifier is inserted ON CONFLICT DO NOTHING (same
    # race cast_common.create_cast_draft documents), so under a race
    # another ref may already own the slug — resolve by slug and adopt
    # the canonical owner rather than leaving ``ref`` an orphan.
    canonical = store.get_ref(kind="draft", id=slug)
    if canonical is not None and int(canonical.id) != int(ref.id):
        return canonical, False
    return ref, True


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """One resolved "latest report" read — everything a consumer
    (health_digest's push-body selector, briefing_cast's health line)
    needs without a second query."""

    ref_id: int
    created_at: datetime
    headline: str
    body: str


def _report_body_text(store: Store, ref_id: int) -> str:
    """Concatenated live body text of a draft, in reading order.

    Best-effort: a draft with no readable body (a partially-written tick,
    a schema surprise) degrades to ``""`` rather than raising — a
    consumer reading an empty body falls back exactly like an absent
    report would.
    """
    try:
        rows = store.drafts.reading_order(ref_id)
    except Exception:
        return ""
    parts = [c.text for c in rows if getattr(c, "text", None)]
    return "\n\n".join(parts)


def _last_tick_evidence(store: Store, ref_id: int, created_at: datetime) -> datetime:
    """The freshness clock for a report: the newest live body chunk's
    ``created_at``, falling back to the ref's own ``created_at`` when the
    draft has no body chunks yet (the narrow window between
    :func:`find_or_create_report` minting the ref and the tick's first
    append).

    ``find_or_create_report`` mints the ref once per UTC day; every
    same-day re-tick appends a paragraph rather than refreshing the ref,
    so the ref's ``created_at`` only ever reflects the *first* tick of the
    day. A day-old ref with an hour-old append must still read as fresh,
    so "was the doctor alive recently" has to be measured off the last
    append, not the mint time.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT max(created_at) FROM chunks "
            "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    latest_chunk_at = row[0] if row else None
    return latest_chunk_at or created_at


def latest_report(
    store: Store,
    max_age: timedelta | None = None,
) -> DoctorReport | None:
    """The most recent doctor-authored report, or ``None`` when absent or
    (with ``max_age`` set) stale.

    Plain SQL lookup per the spec — no cache key: ``kind='draft' AND
    meta->>'author'='doctor' ORDER BY created_at DESC LIMIT 1``.
    ``max_age``, when given, is checked against the newest live body
    chunk's ``created_at`` (:func:`_last_tick_evidence`), not the ref's
    own ``created_at`` — a same-day re-tick appends a paragraph without
    touching the ref, so freshness has to follow the append, not the day
    the report was first minted.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, created_at, title FROM refs "
            "WHERE kind = 'draft' AND deleted_at IS NULL "
            "AND meta->>'author' = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (AUTHOR,),
        ).fetchone()
    if row is None:
        return None
    ref_id, created_at, title = row
    if max_age is not None:
        evidence = _last_tick_evidence(store, int(ref_id), created_at)
        if datetime.now(UTC) - evidence > max_age:
            return None
    body = _report_body_text(store, int(ref_id))
    return DoctorReport(
        ref_id=int(ref_id),
        created_at=created_at,
        headline=str(title or ""),
        body=body,
    )


__all__ = [
    "AUTHOR",
    "FRESH_WINDOW",
    "DoctorReport",
    "find_or_create_report",
    "find_report",
    "latest_report",
    "report_slug",
    "utc_date_tag",
]
