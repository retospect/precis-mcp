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
  not a single frozen snapshot. The reply goes through
  :func:`strip_preamble` first, which owns the "what counts as a report
  body" rule this module defines.
* :func:`latest_report` — the "latest report" read side, a **plain SQL
  lookup** per the spec (``kind='draft' AND meta->>'author'='doctor'
  ORDER BY created_at DESC``), no cache key. Kept dependency-light on
  purpose: a later slice wires this into ``health_digest.py``'s push-body
  selection and ``briefing_cast.py``'s health line, and importing FROM
  either of those here would be the wrong direction — this module must
  stay importable by both without a cycle.
* :func:`convert_needs_a_human` — piece B of ``docs/backlog/doctor-
  report-and-alert-channel-quality.md``: turns each bullet of the body's
  ``## Needs a human`` section into (or bumps) a ``waiting-for:reto``
  todo, and rewrites the section with ``- td<id>: ...`` lines so the
  filed report links into Reto's queue. Runs between
  :func:`strip_preamble` and the body append in
  :func:`precis.workers.job_types.doctor_tick.run`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.handlers.todo import _BODY_KIND
from precis.store.types import ChunkInsert, Tag

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Drive folder title the dated reports are filed under, so a daily
#: artifact does not accumulate loose at the Drive root (same reason
#: ``cast_common.CastProfile.folder`` exists for the casts).
FOLDER: str = "Doctor report"

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

#: First of the four Markdown section headings a report body must carry
#: (``data/prompts/doctor-prompt.md`` §END OF TICK). The body is the
#: agent's final reply *verbatim*, so anything said before this heading is
#: chit-chat addressed to nobody — :func:`strip_preamble` drops it at
#: filing time.
REPORT_FIRST_HEADING: str = "## Classification"

#: Tolerant of the heading level and the case the model actually emits,
#: but line-anchored: the words "## Classification" inside a preamble
#: sentence are not a heading.
_FIRST_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{2,4}[ \t]+classification\b",
    re.IGNORECASE | re.MULTILINE,
)


def strip_preamble(text: str) -> str | None:
    """A tick's reply cut back to the report body, or ``None`` when the
    reply contains no report at all.

    The prompt already says "do not add a preamble"; the model ignored it
    on both of the first two scheduled ticks after the 2026-09-10
    recovery, opening the day's report with "All writes are done. Filing
    the report now." instead of :data:`REPORT_FIRST_HEADING`. Strengthening
    the wording is not a fix for a model that didn't follow it — strip
    deterministically here instead: drop everything before the first
    ``## Classification`` heading, and return ``None`` when that heading
    is absent so the caller fails the tick rather than filing unstructured
    prose in front of whoever is on call (the reply survives on the job
    ref's ``meta.transcript`` either way).
    """
    if not text:
        return None
    match = _FIRST_HEADING_RE.search(text)
    if match is None:
        return None
    return text[match.start() :].strip()


#: Meta marker on the schedule-less container every minted "needs a
#: human" ask parents under — the same ``meta.builtin`` idiom
#: ``schedule.seed.ensure_watches_root`` uses for the Watches umbrella
#: (a folder, not a schedule row), so the nursery's orphan detector
#: (``_detect_orphans`` in ``workers/nursery.py``) treats the subtree as
#: recurring and skips it. Deliberately NOT ``meta.rotation_root`` — the
#: 2026-09-18 ruling on the pre-existing ``waiting-for:reto`` rows says
#: these asks are not rotation units (``docs/backlog/doctor-report-and-
#: alert-channel-quality.md``).
_ASKS_ROOT_BUILTIN = "doctor-asks-root"
_ASKS_ROOT_TITLE = "Doctor — needs a human"

#: The report-section heading :func:`convert_needs_a_human` looks for.
#: Tolerant of heading level and case, same posture as
#: :data:`REPORT_FIRST_HEADING`'s regex.
NEEDS_A_HUMAN_HEADING: str = "## Needs a human"

_NEEDS_A_HUMAN_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+needs a human\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
#: Any Markdown heading line — used to find where the section ends.
_ANY_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
#: A top-level bullet start: ``- ``, ``* ``, ``1.`` or ``1)`` at column 0
#: only — no leading whitespace tolerated, so an indented sub-bullet
#: (``  - saw it fail twice more``) does NOT start a new item; it joins
#: the preceding one like any other continuation line.
_BULLET_START_RE = re.compile(r"^(?:[-*][ \t]+|\d+[.)][ \t]+)")

#: gripe/alert/todo/draft/job id tokens stripped before hashing, so a
#: bullet that only differs in which id it names (or a restated "still N
#: hours") still dedups to the same key.
_ASK_ID_PREFIX_RE = re.compile(r"\b(?:gr|al|td|dr|jo)\d+\b", re.IGNORECASE)
_ASK_DIGIT_RE = re.compile(r"\d+")
_ASK_WS_RE = re.compile(r"\s+")

#: Title clip, mirrors ``backlog_groom``'s 160-char guard on a minted
#: todo's title.
_ASK_TITLE_MAX = 160


def _normalize_ask_text(text: str) -> str:
    """Lowercase, id/number-stripped form of an ask's text — the input to
    :func:`_doctor_ask_key`."""
    out = text.lower()
    out = _ASK_ID_PREFIX_RE.sub(" ", out)
    out = _ASK_DIGIT_RE.sub(" ", out)
    return _ASK_WS_RE.sub(" ", out).strip()


def _doctor_ask_key(text: str) -> str:
    """``meta.doctor_ask_key`` for a bullet's full text (title +
    continuation lines) — the dedup key a same-day re-tick or a
    restated-with-a-fresher-number ask still resolves to."""
    return hashlib.sha1(
        _normalize_ask_text(text).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def _parse_needs_a_human_bullets(body: str) -> tuple[list[str], int, int] | None:
    """The section's bullet texts plus its ``(start, end)`` offsets in
    ``body``, or ``None`` when :data:`NEEDS_A_HUMAN_HEADING` is absent.

    Each column-0 bullet is one item; any other line — a plain
    continuation, or an indented sub-bullet — joins the preceding item
    (blank lines preserved inside an item, trimmed at its ends). Text
    before the first bullet inside the section (malformed prose instead
    of a list) is silently dropped — the caller treats an empty item
    list as "nothing to convert" rather than raising.
    """
    heading = _NEEDS_A_HUMAN_RE.search(body)
    if heading is None:
        return None
    section_start = heading.end()
    next_heading = _ANY_HEADING_RE.search(body, section_start)
    section_end = next_heading.start() if next_heading else len(body)
    section_text = body[section_start:section_end]

    items: list[str] = []
    current: list[str] = []

    def _flush() -> None:
        joined = "\n".join(current).strip()
        if joined:
            items.append(joined)
        current.clear()

    for line in section_text.splitlines():
        if _BULLET_START_RE.match(line):
            _flush()
            current.append(_BULLET_START_RE.sub("", line, count=1))
        elif current:
            current.append(line.strip())
        # else: stray text before any bullet — ignored.
    _flush()
    return items, section_start, section_end


def _ensure_asks_root(store: Store) -> int | None:
    """Find (or create) the schedule-less container every minted ask
    parents under. Mirrors :func:`precis.workers.schedule.seed.ensure_watches_root`'s
    find-then-create-under-lock shape. Best-effort — ``None`` on any
    failure so a DB hiccup degrades to a parentless mint with a logged
    warning rather than losing the ask.
    """
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE kind='todo' AND retired_at IS NULL "
                "AND meta->>'builtin' = %s LIMIT 1",
                (_ASKS_ROOT_BUILTIN,),
            ).fetchone()
        if row is not None:
            return int(row[0])
        with store.tx() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE kind='todo' AND retired_at IS NULL "
                "AND meta->>'builtin' = %s LIMIT 1 FOR UPDATE",
                (_ASKS_ROOT_BUILTIN,),
            ).fetchone()
            if row is not None:
                return int(row[0])
            ref = store.insert_ref(
                kind="todo",
                slug=None,
                title=_ASKS_ROOT_TITLE,
                meta={"builtin": _ASKS_ROOT_BUILTIN},
                conn=conn,
            )
            return int(ref.id)
    except Exception:  # pragma: no cover - defensive, see docstring
        log.warning(
            "doctor_report: could not resolve/mint the asks root", exc_info=True
        )
        return None


def _find_open_ask(store: Store, key: str) -> tuple[int, int] | None:
    """``(ref_id, seen_count)`` of a live, not-done ``waiting-for:reto``
    ask carrying ``meta.doctor_ask_key == key``, or ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, COALESCE((r.meta->>'seen_count')::int, 1)
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND r.meta ->> 'doctor_ask_key' = %s
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                       WHERE rtg.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', %s, %s)
             ORDER BY r.ref_id DESC
             LIMIT 1
            """,
            (key, "won't-do", "auto-timeout"),
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1])


def _mint_ask_todo(
    store: Store,
    *,
    parent_id: int | None,
    key: str,
    title: str,
    detail: str | None,
    report_ref_id: int | None,
) -> int:
    """Mint one ``waiting-for:reto`` todo for a fresh ask."""
    meta: dict[str, Any] = {"doctor_ask_key": key, "seen_count": 1}
    if report_ref_id is not None:
        meta["doctor_report_id"] = report_ref_id
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=parent_id,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open("waiting-for:reto"), set_by="system", conn=conn)
        if detail:
            store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=0, text=detail, meta={"chunk_kind": _BODY_KIND})],
                conn=conn,
            )
    return int(ref.id)


def convert_needs_a_human(
    store: Store,
    body: str,
    *,
    report_ref_id: int | None = None,
) -> str:
    """Turn each bullet of the report's :data:`NEEDS_A_HUMAN_HEADING`
    section into (or bump) a ``waiting-for:reto`` todo, rewriting the
    section in the returned body as ``- td<id>: <first line>`` lines so
    the filed report links into Reto's queue (``search(kind='todo',
    tags=['waiting-for:reto'])``) instead of leaving the ask stranded in
    prose (``docs/backlog/doctor-report-and-alert-channel-quality.md``
    piece B).

    Called from :func:`precis.workers.job_types.doctor_tick.run` after
    :func:`strip_preamble`, before the body is appended to the day's
    report draft. No section is a no-op; a parse problem or a DB
    surprise logs at WARNING and returns ``body`` unchanged — converting
    the section is a nicety, never a reason to fail a tick that would
    otherwise have filed a clean report.
    """
    try:
        parsed = _parse_needs_a_human_bullets(body)
        if parsed is None:
            return body
        items, section_start, section_end = parsed
        if not items:
            return body

        parent_id = _ensure_asks_root(store)
        if parent_id is None:
            log.warning(
                "doctor_report: no asks root resolved; minting needs-a-human "
                "todo(s) parentless"
            )

        rendered: list[str] = []
        for item in items:
            lines = item.splitlines()
            first_line = lines[0].strip()
            detail = "\n".join(lines[1:]).strip() or None
            title = first_line
            if len(title) > _ASK_TITLE_MAX:
                title = title[: _ASK_TITLE_MAX - 1].rstrip() + "…"
            key = _doctor_ask_key(item)

            existing = _find_open_ask(store, key)
            if existing is not None:
                ref_id, seen_count = existing
                seen_count += 1
                store.stamp_ref_meta(ref_id, {"seen_count": seen_count})
                rendered.append(f"- td{ref_id}: {first_line} (seen {seen_count}×)")
            else:
                ref_id = _mint_ask_todo(
                    store,
                    parent_id=parent_id,
                    key=key,
                    title=title,
                    detail=detail,
                    report_ref_id=report_ref_id,
                )
                rendered.append(f"- td{ref_id}: {first_line}")

        new_section = "\n" + "\n".join(rendered) + "\n"
        return body[:section_start] + new_section + body[section_end:]
    except Exception:  # pragma: no cover - defensive, see docstring
        log.warning("doctor_report: needs-a-human conversion failed", exc_info=True)
        return body


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
    _file_under_folder(store, int(ref.id))
    return ref, True


def ensure_report_folder(store: Store) -> int | None:
    """Find (or create) the Drive folder the dated reports are filed under.

    Idempotent on the folder title. Best-effort — a failure logs and
    returns ``None`` so placement never blocks a report the doctor is
    about to write into.
    """
    try:
        existing = store.folder_ref_ids_by_title(FOLDER)
        if existing:
            return int(existing[0])
        return int(store.insert_ref(kind="folder", slug=None, title=FOLDER).id)
    except Exception:  # pragma: no cover - placement is a nicety, never fatal
        log.warning("doctor_report: could not ensure folder %r", FOLDER, exc_info=True)
        return None


def _file_under_folder(store: Store, ref_id: int) -> None:
    """Place a fresh report draft under :data:`FOLDER`. Never raises."""
    folder_id = ensure_report_folder(store)
    if folder_id is None:
        return
    try:
        store.set_parent(ref_id, folder_id)
    except Exception:  # pragma: no cover - placement is a nicety, never fatal
        log.warning(
            "doctor_report: could not file draft %s under folder %s",
            ref_id,
            folder_id,
            exc_info=True,
        )


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
            "WHERE kind = 'draft' AND retired_at IS NULL "
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
    "FOLDER",
    "FRESH_WINDOW",
    "NEEDS_A_HUMAN_HEADING",
    "REPORT_FIRST_HEADING",
    "DoctorReport",
    "convert_needs_a_human",
    "ensure_report_folder",
    "find_or_create_report",
    "find_report",
    "latest_report",
    "report_slug",
    "strip_preamble",
    "utc_date_tag",
]
