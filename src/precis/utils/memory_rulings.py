"""Ruling nodes: the four ``meta`` keys, the lifecycle, the ``rulings`` view (gr548835).

A ruling is a one-line decision of the owner kept as a ``kind='memory'``
node tagged ``ruling``. It is a temporary patch on the documented system:
it is honoured from the day it is made, then integrated into the skills
or spec and archived. Slice 1 of the ruling browser (the web page, the
migrate action, the needs-you expiry rows and brief injection are later
slices) covers four things, all here:

1. **Meta keys** ``ruled_by``, ``ruled_on`` (ISO date), ``status``
   (:data:`STATUSES`) and ``review_by`` (ISO date), writable through
   ``put``/``edit(meta=...)`` only on a node tagged ``ruling``
   (:func:`validate_ruling_meta`). ``review_by`` defaults to
   the later of ``ruled_on`` and today + :data:`REVIEW_DAYS` days (a
   transferred ruling's past ``ruled_on`` must not default it into the
   overdue queue, gr631565); a create defaults ``status`` to
   ``in-force`` and ``ruled_on`` to today (UTC).
2. **The status tag is dropped, not mirrored.** ``meta.status`` is the
   single truth. The backfill's duplicate open tag ``status:<value>`` is
   removed when ``meta.status`` is first written (the ack says so), so the
   two can never disagree; a node still carrying only the legacy tag reads
   as that status (:func:`status_of`), so recall honours it before the
   one-off ``precis memory migrate-rulings`` has run.
3. **Archive, never delete** (owner ruling 2026-11-01, comment 2 of the
   gripe). Integrated or moot rulings go ``absorbed``/``archived`` (or
   ``retired`` for a won't-do): the handle stays resolvable by ``get``,
   but the rulings view hides them unless ``args={'status': '*'}`` and
   recall (:func:`hidden_ids`, applied by ``MemoryHandler.search`` on the
   default and ``view='index'`` renders) leaves them out. Only
   ``in-force`` rulings are live guidance, so ``retired`` hides too.
4. **The view** ``search(kind='memory', tags=['ruling'], view='rulings')``
   (:func:`render_rulings`): rulings grouped by status, in-force first,
   rulings due within :data:`EXPIRING_DAYS` days (or overdue) before the
   rest, then newest; each row lists the nodes it governs one hop out
   (``related-to`` links whose edge meta carries ``rel_intent=decides``,
   either orientation).

Nothing here writes on its own: the one-off migration of the backfilled
nodes' trailing ``Meta:`` body lines (:func:`plan_meta_migration`) runs
only from ``precis memory migrate-rulings`` (dry run by default).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from precis._result_budget import result_context_cap
from precis.errors import BadInput
from precis.store import Store, Tag
from precis.store.types import Ref
from precis.utils import handle_registry

RULING_TAG = "ruling"
STATUSES = ("in-force", "absorbed", "archived", "retired")
LIVE_STATUS = "in-force"
META_KEYS = ("ruled_by", "ruled_on", "status", "review_by")

#: Days from the later of ``ruled_on`` and today to the default
#: ``review_by`` — the floor keeps a backdated ``ruled_on`` (a
#: transferred ruling) from landing already overdue (gr631565).
REVIEW_DAYS = 14
#: A ruling whose ``review_by`` is within this many days (or past) is
#: "expiring" and sorts ahead of the other in-force rulings.
EXPIRING_DAYS = 3

#: A ruling's governed node: a ``related-to`` link whose edge meta says so.
DECIDES_RELATION = "related-to"
DECIDES_INTENT = "decides"

_LEGACY_STATUS_PREFIX = "status:"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXAMPLE = (
    "meta={'ruled_by': 'Reto', 'ruled_on': '2026-11-01', 'status': 'in-force', "
    "'review_by': '2026-11-15'}"
)
_ALL_STATUSES_SPELLINGS = ("*", "any", "all")


def _bad_date(key: str, value: Any) -> BadInput:
    return BadInput(
        f"meta[{key!r}] must be an ISO date YYYY-MM-DD, got {value!r}",
        next=f"meta={{{key!r}: '2026-11-15'}}",
    )


def _iso_date(key: str, value: Any) -> date:
    if isinstance(value, str) and _ISO_DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise _bad_date(key, value)


def _parse_or_none(value: Any) -> date | None:
    try:
        return _iso_date("date", value)
    except BadInput:
        return None


def today_utc() -> date:
    return datetime.now(UTC).date()


def validate_ruling_meta(
    raw: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None,
    today: date | None = None,
) -> dict[str, Any]:
    """The cleaned patch for the ruling keys in ``raw``.

    ``existing=None`` is a create: ``status`` defaults to ``in-force`` and
    ``ruled_on`` to today (UTC). On an edit (``existing`` = the node's
    meta) only the given keys change. Either way ``review_by`` is filled in
    as the later of ``ruled_on`` and today + :data:`REVIEW_DAYS` days when
    neither the patch nor the node has one, and may not precede
    ``ruled_on``.
    """
    out: dict[str, Any] = {}
    if "ruled_by" in raw:
        by = raw["ruled_by"]
        if not isinstance(by, str) or not by.strip() or "\n" in by or "\r" in by:
            raise BadInput(
                "meta['ruled_by'] must be a non-empty single-line str",
                next="meta={'ruled_by': 'Reto'}",
            )
        out["ruled_by"] = by.strip()
    if "status" in raw:
        status = raw["status"]
        status = status.strip() if isinstance(status, str) else status
        if status not in STATUSES:
            raise BadInput(
                f"meta['status'] must be one of {list(STATUSES)}, got {status!r}",
                options=list(STATUSES),
                next="meta={'status': 'absorbed'} — integrated into skills/spec; "
                "'archived' = moot; 'retired' = won't-do",
            )
        out["status"] = status
    for key in ("ruled_on", "review_by"):
        if key in raw:
            out[key] = _iso_date(key, raw[key]).isoformat()
    today = today or today_utc()
    if existing is None:
        out.setdefault("status", LIVE_STATUS)
        out.setdefault("ruled_on", today.isoformat())
    merged = {**(existing or {}), **out}
    ruled_on = _parse_or_none(merged.get("ruled_on"))
    if _parse_or_none(merged.get("review_by")) is None and ruled_on is not None:
        base = max(ruled_on, today)
        out["review_by"] = (base + timedelta(days=REVIEW_DAYS)).isoformat()
        merged["review_by"] = out["review_by"]
    review_by = _parse_or_none(merged.get("review_by"))
    if ruled_on is not None and review_by is not None and review_by < ruled_on:
        raise BadInput(
            f"meta['review_by'] {review_by.isoformat()} is before ruled_on "
            f"{ruled_on.isoformat()}",
            next="pass both: meta={'ruled_on': '2026-11-01', "
            "'review_by': '2026-11-15'}",
        )
    return out


def status_of(meta: Mapping[str, Any] | None, tag_values: Collection[str]) -> str:
    """A ruling's status: ``meta.status``, else the legacy ``status:<v>``
    open tag the backfill carried, else :data:`LIVE_STATUS`."""
    status = (meta or {}).get("status")
    if status in STATUSES:
        return str(status)
    for v in tag_values:
        if v.startswith(_LEGACY_STATUS_PREFIX) and v[7:] in STATUSES:
            return v[7:]
    return LIVE_STATUS


def _open_values(pairs: Iterable[tuple[str, str]]) -> set[str]:
    return {v for _ns, v in pairs}


def is_hidden(meta: Mapping[str, Any] | None, tag_values: Collection[str]) -> bool:
    """True for a ruling node that is no longer in force."""
    return RULING_TAG in tag_values and status_of(meta, tag_values) != LIVE_STATUS


def hidden_ids(store: Store) -> list[int]:
    """Ref ids of live rulings that are absorbed, archived or retired."""
    tags = Tag.normalize_filter([RULING_TAG], kind="memory")
    refs = store.list_refs(kind="memory", tags=tags, limit=5000)
    legacy = [r.id for r in refs if (r.meta or {}).get("status") not in STATUSES]
    tag_map = store.ref_tags_bulk(legacy) if legacy else {}
    return [
        r.id
        for r in refs
        if status_of(r.meta, _open_values(tag_map.get(r.id, ()))) != LIVE_STATUS
    ]


def drop_legacy_status_tags(store: Store, ref_id: int) -> list[str]:
    """Remove the backfill's duplicate ``status:<value>`` open tags from a
    ruling whose ``meta.status`` was just written; returns what was dropped."""
    pairs = store.ref_tags_bulk([ref_id]).get(ref_id, [])
    stale = [
        v for ns, v in pairs if ns == "OPEN" and v.startswith(_LEGACY_STATUS_PREFIX)
    ]
    for value in stale:
        store.remove_tag(ref_id, Tag.open(value))
    return stale


def parse_status_filter(status: str | None) -> frozenset[str]:
    """The statuses a ``view='rulings'`` call shows: ``None`` = in-force only
    (the default), ``'*'``/``any``/``all`` = every status, else the named
    ones (``,``/``|``/``;`` separated)."""
    if status is None:
        return frozenset({LIVE_STATUS})
    spelled = status.strip().lower()
    if spelled in _ALL_STATUSES_SPELLINGS:
        return frozenset(STATUSES)
    parts = [p.strip() for p in re.split(r"[,|;]", spelled) if p.strip()]
    bad = [p for p in parts if p not in STATUSES]
    if bad or not parts:
        raise BadInput(
            f"unknown ruling status {bad[0] if bad else status!r}",
            options=[*STATUSES, "*"],
            next="search(kind='memory', tags=['ruling'], view='rulings', "
            "args={'status': '*'})",
        )
    return frozenset(parts)


@dataclass
class _Row:
    ref: Ref
    status: str
    ruled_on: date | None
    review_by: date | None
    expiring: bool
    governed: list[str] = field(default_factory=list)


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _governed(store: Store, ref_id: int) -> list[str]:
    """``<handle> '<title>'`` per node this ruling decides (one hop)."""
    other_ids: list[int] = []
    for link in store.links_for(ref_id, direction="both"):
        if link.relation != DECIDES_RELATION:
            continue
        if (link.meta or {}).get("rel_intent") != DECIDES_INTENT:
            continue
        other = link.dst_ref_id if link.src_ref_id == ref_id else link.src_ref_id
        if other != ref_id and other not in other_ids:
            other_ids.append(other)
    if not other_ids:
        return []
    refs = store.fetch_refs_by_ids(other_ids, include_deleted=False)
    return [
        f"{_handle(refs[i].kind, i)} {refs[i].title!r}" for i in other_ids if i in refs
    ]


def _review_note(row: _Row, today: date) -> str:
    if row.review_by is None:
        return ""
    days = (row.review_by - today).days
    if row.status != LIVE_STATUS:
        return f"review_by {row.review_by.isoformat()}"
    if days < 0:
        when = f"overdue {-days}d"
    elif days == 0:
        when = "due today"
    else:
        when = f"in {days}d" + (", expiring" if row.expiring else "")
    return f"review_by {row.review_by.isoformat()} ({when})"


def _sort_key(row: _Row) -> tuple[int, int, int, int, int]:
    newest = (row.ruled_on or row.ref.created_at.date()).toordinal()
    due = row.review_by.toordinal() if row.expiring and row.review_by else 0
    return (
        STATUSES.index(row.status),
        0 if row.expiring else 1,
        due,
        -newest,
        -int(row.ref.id),
    )


def _ruling_rows(
    store: Store,
    *,
    status: str | None,
    tags: list[str] | None,
    include_ref_ids: Collection[int] | None,
    exclude_ref_ids: Collection[int] | None,
    today: date,
) -> tuple[list[_Row], dict[str, int]]:
    """The rulings corpus under these predicates: visible rows plus the
    per-status count of those the filter hides. Shared by
    :func:`render_rulings` and :func:`count_rulings` — the domain footer
    must count the same set the view renders."""
    wanted = parse_status_filter(status)
    wanted_tags = [RULING_TAG, *(t for t in tags or [] if t.strip() != RULING_TAG)]
    normalized = Tag.normalize_filter(wanted_tags, kind="memory")
    refs = store.list_refs(kind="memory", tags=normalized, limit=5000)
    if include_ref_ids is not None:
        keep = set(include_ref_ids)
        refs = [r for r in refs if r.id in keep]
    if exclude_ref_ids:
        drop = set(exclude_ref_ids)
        refs = [r for r in refs if r.id not in drop]
    tag_map = store.ref_tags_bulk([r.id for r in refs])
    horizon = today + timedelta(days=EXPIRING_DAYS)
    rows: list[_Row] = []
    hidden: dict[str, int] = {}
    for ref in refs:
        meta = ref.meta or {}
        st = status_of(meta, _open_values(tag_map.get(ref.id, ())))
        if st not in wanted:
            hidden[st] = hidden.get(st, 0) + 1
            continue
        review_by = _parse_or_none(meta.get("review_by"))
        rows.append(
            _Row(
                ref=ref,
                status=st,
                ruled_on=_parse_or_none(meta.get("ruled_on")),
                review_by=review_by,
                expiring=st == LIVE_STATUS
                and review_by is not None
                and review_by <= horizon,
            )
        )
    rows.sort(key=_sort_key)
    return rows, hidden


def count_rulings(
    store: Store,
    *,
    status: str | None = None,
    tags: list[str] | None = None,
    include_ref_ids: Collection[int] | None = None,
    exclude_ref_ids: Collection[int] | None = None,
) -> int:
    """The size of the rulings-view corpus under these predicates — the
    domain-footer's before/after counter."""
    rows, _hidden = _ruling_rows(
        store,
        status=status,
        tags=tags,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
        today=today_utc(),
    )
    return len(rows)


def render_rulings(
    store: Store,
    *,
    status: str | None = None,
    tags: list[str] | None = None,
    include_ref_ids: Collection[int] | None = None,
    exclude_ref_ids: Collection[int] | None = None,
    today: date | None = None,
    budget: int | None = None,
) -> tuple[str, list[int]]:
    """The ``view='rulings'`` body: rulings grouped by status, each with its
    governed nodes. ``status=None`` shows in-force only and counts the rest;
    ``'*'`` shows all. Returns ``(body, rendered ref ids)`` — the ids feed
    the ledger's ``result_ids`` audit column."""
    today = today or today_utc()
    rows, hidden = _ruling_rows(
        store,
        status=status,
        tags=tags,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
        today=today,
    )
    counts = {s: sum(1 for r in rows if r.status == s) for s in STATUSES}
    summary = " · ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
    lines = [f"# rulings: {summary}"]
    if hidden:
        lines.append(
            f"({', '.join(f'{n} {s}' for s, n in sorted(hidden.items()))} hidden — "
            "search(kind='memory', tags=['ruling'], view='rulings', "
            "args={'status': '*'}) shows them)"
        )
    cap = (budget if budget is not None else result_context_cap()) - 400
    used = sum(len(ln) + 1 for ln in lines)
    shown_ids: list[int] = []
    current = ""
    for i, row in enumerate(rows):
        row.governed = _governed(store, row.ref.id)
        meta = row.ref.meta or {}
        block: list[str] = []
        if row.status != current:
            current = row.status
            block.append(f"## {current} ({counts[current]})")
        hook = f" — {meta['hook']}" if meta.get("hook") else ""
        block.append(f"- {row.ref.title} ({_handle('memory', row.ref.id)}){hook}")
        ruled = " ".join(
            p
            for p in (
                f"ruled {row.ruled_on.isoformat()}" if row.ruled_on else "",
                f"by {meta['ruled_by']}" if meta.get("ruled_by") else "",
            )
            if p
        )
        facts = [ruled, _review_note(row, today)]
        facts_line = " · ".join(f for f in facts if f)
        if facts_line:
            block.append(f"    {facts_line}")
        if row.governed:
            block.append(f"    decides: {'; '.join(row.governed)}")
        cost = sum(len(ln) + 1 for ln in block)
        if used + cost > cap:
            lines.append(
                f"… {len(rows) - i} more — narrow with args={{'status': '<one status>'}}"
                " or extra tags=[…]"
            )
            break
        lines.extend(block)
        used += cost
        shown_ids.append(int(row.ref.id))
    if not rows:
        lines.append(
            "no rulings match; file one with put(kind='memory', "
            "tags=['ruling'], text='…', meta={'ruled_by': '…'})"
        )
    return "\n".join(lines), shown_ids


#: ``Meta: ruled_by: Reto; ruled_on: 2026-11-01; status: in-force`` — the
#: backfill's trailing body line; ``=`` or ``:`` separators, ``;``/``,``/``|``
#: between pairs.
_META_LINE_RE = re.compile(r"^\s*Meta:\s*(?P<rest>.+?)\s*$", re.M)
_META_PAIR_RE = re.compile(
    r"\b(?P<key>ruled_by|ruled_on|status|review_by)\s*[:=]\s*(?P<val>[^;,|\n]+)"
)


def parse_meta_line(body: str) -> dict[str, str]:
    """The ruling keys of the body's last ``Meta:`` line (``{}`` when none)."""
    matches = list(_META_LINE_RE.finditer(body))
    if not matches:
        return {}
    return {
        m["key"]: m["val"].strip() for m in _META_PAIR_RE.finditer(matches[-1]["rest"])
    }


@dataclass
class MigrationItem:
    ref_id: int
    title: str
    meta: dict[str, str]
    existing: dict[str, Any]


def plan_meta_migration(
    store: Store, body_of: Callable[[Ref], str]
) -> list[MigrationItem]:
    """Rulings with no ``meta.status`` yet, with the keys their body's
    ``Meta:`` line carries (empty = no such line). ``body_of(ref)`` reads the
    body (the handler's). Nodes already migrated are skipped, so a re-run
    plans nothing."""
    tags = Tag.normalize_filter([RULING_TAG], kind="memory")
    return [
        MigrationItem(
            ref.id, ref.title or "", parse_meta_line(body_of(ref)), dict(ref.meta or {})
        )
        for ref in sorted(
            store.list_refs(kind="memory", tags=tags, limit=5000),
            key=lambda r: r.id,
        )
        if (ref.meta or {}).get("status") not in STATUSES
    ]
