"""The daily ``section:threads`` retirement sweep (gr485358 item 1) —
fired from the ``memory_thread_sweep`` scheduler cadence.

Why it exists: thread-node retirement was manual only —
``scripts/memory-lint`` listed candidates and the reconsolidation pass
ran only when a session acted on its nag, so dead resume nodes piled
into ``search(kind='memory')`` recall (2026-10-14: 22 live threads, four
of them one session's resume chain, each saying "Supersedes meN" in
prose with no edge). The rules are the ones the repo-dev memory policy
already wrote down; this cadence is just their executor:

* **rule a** — the body's first line is ``Left: none``: the campaign
  declared itself finished.
* **rule b** — another *live memory* links ``supersedes`` to it: the
  replacement is on record, the old node is clutter.
* **rule c** — untouched for :data:`~precis.utils.memory_threads.STALE_DAYS`
  days (the same clock ``precis memory index`` calls "live"). When the
  node's ``Left:`` line names open work, a todo carrying that line —
  linked back to the retired node — is minted in the same transaction
  first, so the sweep forgets the *state* without dropping the *work*.
* **rule s** — ``SPACE:scratch`` (gr568398): the opt-in throwaway space.
  A scratch node untouched past its expiry self-retires — default
  :data:`SCRATCH_TTL_DAYS` days from ``updated_at``, ``meta.expires`` a
  per-node override (absolute instant, or a ``<n>[mhdw]`` duration from
  ``updated_at``, shorter or longer). A ``scratch-for:td<N>`` /
  ``scratch-for:gr<N>`` open tag keys the clock to the owning task
  instead (gr605668): while an owner is still open the
  :data:`SCRATCH_FLOOR_DAYS`-day floor from ``updated_at`` is the only
  limit; once every owner is closed — terminal ``STATUS`` stamp or
  retirement — expiry is the last close + :data:`SCRATCH_OWNER_GRACE_DAYS`
  days grace, still capped by the floor so a task that never closes
  loses its scratch anyway. ``meta.expires`` overrides both clocks.
  Probes, session scratch and drafts clean themselves up without anyone
  remembering a delete; the untagged default stays ``SPACE:research``
  so nothing expires by accident.

Scope is deliberately narrow: ``kind='memory'`` rows tagged
``section:threads`` only — never other kinds, never ``section:index``
hubs (excluded even if someone tags one ``threads`` too), never
runbook/gotcha/workflow/reference sections, and never a scratch-owned
node. A node whose only ``SPACE:`` value is ``scratch`` belongs to the
scratch pass end to end — including one also tagged
``section:threads``: rules a–c never see it, so a ``meta.expires``
override on a scratch thread node is honoured instead of being cut
short by rule c's stale clock (a ``Left: none`` scratch thread just
waits out its TTL — scratch means self-cleaning, not curated). A
second ``SPACE:`` value returns the node to thread rules: the scratch
pass refuses ambiguous space tags, and nothing may fall between the
two passes. The scratch pass is just as narrow: ``kind='memory'``
rows carrying ``SPACE:scratch`` and no other ``SPACE:`` value, hubs
excluded the same way. Retirement is the normal
soft-delete (:meth:`Store.retire_ref`), recoverable at the SQL layer —
never a hard delete. Every retirement logs one greppable line with the
rule letter::

    memory_thread_sweep: retired me<id> rule=c — untouched 21d (todo=td<n>)

``dry_run=True`` plans only: each candidate is logged as
``would retire me<id> rule=<r>`` and nothing is written — what
``precis memory sweep`` prints. Per-candidate failures are logged and
counted, never abort the pass (one poisoned row must not strand the
rest), and the run is idempotent: a retired node is gone from the
candidate query, so the next fire has nothing to re-act on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES
from precis.utils.memory_threads import (
    INDEX_TAG,
    STALE_DAYS,
    THREADS_TAG,
    first_left,
    left_is_done,
    newest_body_date,
)
from precis.utils.timeutil import as_utc
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: Live memories tagged ``section:threads`` (never ``section:index``
#: hubs), with their body chunk's first lines evaluated in Python — the
#: ``Left:`` grammar lives in :mod:`precis.utils.memory_threads`, not in
#: SQL. Scratch-owned nodes (``SPACE:scratch`` and no other ``SPACE:``
#: value, the mirror of :data:`_SCRATCH_CANDIDATES`) are excluded: the
#: scratch pass owns their TTL, ``meta.expires`` included — a scratch
#: thread node must never hit rule c's :data:`STALE_DAYS` clock. A
#: second ``SPACE:`` value keeps it here on purpose: the scratch pass
#: refuses ambiguous space tags, so excluding mixed nodes too would
#: strand them unowned.
_THREAD_CANDIDATES = """
SELECT r.ref_id, r.title, r.updated_at,
       (SELECT c.text FROM chunks c
         WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'memory_body'
         ORDER BY c.ord LIMIT 1) AS body
  FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN' AND t.value = %s)
   AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN' AND t.value = %s)
   AND (NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'SPACE' AND t.value = 'scratch')
        OR EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'SPACE' AND t.value <> 'scratch'))
 ORDER BY r.ref_id
"""

#: ``SPACE:scratch`` default TTL (gr568398) — days from ``updated_at``
#: after which an *ownerless* scratch node self-retires. Deliberately
#: longer than the one hour the gripe ruled out: an hour races
#: enrichment (gr563702) and is too short to be useful.
SCRATCH_TTL_DAYS = 7

#: Grace after the owning task closes before its ``scratch-for:`` nodes
#: dissolve (gr605668) — expiry = the owner's close stamp + this many
#: days.
SCRATCH_OWNER_GRACE_DAYS = 3

#: Hard floor for owner-tagged scratch (gr605668) — days from
#: ``updated_at`` regardless of owner state, so scratch of a task that
#: never closes still dissolves. Also caps the close+grace expiry.
SCRATCH_FLOOR_DAYS = 30

#: Live memories whose *only* ``SPACE:`` tag is ``scratch`` — a second
#: space would make the expiry intent ambiguous, and ``section:index``
#: hubs are structural even when scratch-tagged. ``meta`` rides along
#: for the ``meta.expires`` override (parsed in Python, like ``Left:``),
#: and ``scratch_for`` carries the node's ``scratch-for:<handle>`` open
#: tag values for the owner-keyed clock (gr605668).
_SCRATCH_CANDIDATES = """
SELECT r.ref_id, r.title, r.updated_at, r.meta,
       (SELECT array_agg(t.value ORDER BY t.value)
          FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
         WHERE rt.ref_id = r.ref_id
           AND t.namespace = 'OPEN' AND t.value LIKE 'scratch-for:%%')
       AS scratch_for
  FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'SPACE' AND t.value = 'scratch')
   AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'SPACE' AND t.value <> 'scratch')
   AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN' AND t.value = %s)
 ORDER BY r.ref_id
"""

#: ``meta.expires`` duration grammar — mirrors the alias handler's
#: ``expires=`` (``'7d'``/``'12h'``/``'30m'``/``'2w'``). Validated writes
#: store an absolute instant; a raw duration can still arrive via an
#: unvalidated writer, so the sweep honours it relative to ``updated_at``.
_SCRATCH_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")
_SCRATCH_DURATION_UNITS = {
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}

#: The ``scratch-for:<handle>`` owner tag (gr605668): an open tag naming
#: the todo or gripe the scratch belongs to — the same tag the
#: thread-scoped recall work reads. Any other value under the prefix is
#: ignored (a ``scratch-for:me5`` keeps the default TTL), as is one whose
#: ``td``/``gr`` prefix does not match the stored row's kind.
_SCRATCH_FOR_RE = re.compile(r"^scratch-for:((?:td|gr)\d+)$")

#: Status rows of every ``scratch-for`` owner in one fetch. The inner
#: join keeps ``ref_tags`` rows pointing at ``STATUS`` tags only, so a
#: ref with no STATUS at all surfaces as a single all-NULL row (still
#: open). Expired tag rows are ignored the way search ignores them.
#: ``r.kind`` rides along so the caller can drop a handle whose prefix
#: lies — ``td<N>`` naming a memory row is not an owner (td606757).
_SCRATCH_OWNER_STATUS = """
SELECT r.ref_id, r.kind, r.retired_at, t.value, rt.created_at
  FROM refs r
  LEFT JOIN (ref_tags rt JOIN tags t
               ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
              AND (rt.expires_at IS NULL OR rt.expires_at > now()))
         ON rt.ref_id = r.ref_id
 WHERE r.ref_id = ANY(%s)
"""

#: ``{superseded ref_id: superseding live memory id}`` — the *edge*, never
#: the prose: a body that merely says "supersedes meN" retires nothing
#: (the lint's job to flag), only a real ``supersedes`` link counts.
_SUPERSEDED_BY = """
SELECT l.dst_ref_id, min(l.src_ref_id)
  FROM links l
  JOIN refs s ON s.ref_id = l.src_ref_id
             AND s.retired_at IS NULL AND s.kind = 'memory'
 WHERE l.relation = 'supersedes' AND l.dst_ref_id = ANY(%s)
   AND l.src_ref_id <> l.dst_ref_id
 GROUP BY l.dst_ref_id
"""


@dataclass(frozen=True)
class SweepCandidate:
    """One thread node the sweep would retire, with its rule and the
    carry-over state a rule-c retirement must not lose."""

    ref_id: int
    title: str
    rule: str  # "a" | "b" | "c" | "s" (currency suspect — listed, never retired)
    reason: str  # human-readable why, for the log line and dry-run list
    #: Rule c only: the ``Left:`` remainder when it names open work —
    #: minted into a carry-over todo before the node retires.
    left: str | None = None
    #: Rule b only: the live memory that supersedes this node (stamped
    #: onto ``meta.superseded_by``, the same breadcrumb the ``supersede``
    #: verb leaves).
    superseded_by: int | None = None


def plan_thread_sweep(
    store: Store, *, now: datetime | None = None
) -> list[SweepCandidate]:
    """Every live non-scratch ``section:threads`` node due for
    retirement, oldest id first — scratch-owned nodes are the scratch
    pass's (see :data:`_THREAD_CANDIDATES`). Pure read — the dry-run
    mode is this plan printed."""
    with store.pool.connection() as conn:
        rows = conn.execute(_THREAD_CANDIDATES, (THREADS_TAG, INDEX_TAG)).fetchall()
        ids = [int(r[0]) for r in rows]
        superseded: dict[int, int] = {}
        if ids:
            superseded = {
                int(d): int(s)
                for d, s in conn.execute(_SUPERSEDED_BY, (ids,)).fetchall()
            }
    cutoff = (now or datetime.now(UTC)) - timedelta(days=STALE_DAYS)
    plan: list[SweepCandidate] = []
    for ref_id, title, updated_at, body in rows:
        ref_id = int(ref_id)
        title = str(title or "")
        left = first_left(str(body or ""))
        if left is not None and left_is_done(left):
            plan.append(
                SweepCandidate(ref_id, title, "a", "first line is 'Left: none'")
            )
        elif ref_id in superseded:
            plan.append(
                SweepCandidate(
                    ref_id,
                    title,
                    "b",
                    f"superseded by me{superseded[ref_id]}",
                    superseded_by=superseded[ref_id],
                )
            )
        elif updated_at is not None and updated_at < cutoff:
            # gr503035: a dated section inside the stale window means the
            # node's own text claims to be current — a currency suspect
            # (the same guard memory-lint's landed verdict uses), never a
            # retire action. The sweep has no git oracle, so the
            # "older than the newest cited landing" half of the rule is
            # lint-side; this is the half both agree on.
            fresh = newest_body_date(str(body or ""))
            fresh_from = ((now or datetime.now(UTC)).date()) - timedelta(
                days=STALE_DAYS
            )
            if fresh is not None and fresh >= fresh_from:
                plan.append(
                    SweepCandidate(
                        ref_id,
                        title,
                        "s",
                        f"currency suspect — dated section {fresh} within {STALE_DAYS}d",
                    )
                )
                continue
            age = ((now or datetime.now(UTC)) - updated_at).days
            carry = left if left else None
            plan.append(
                SweepCandidate(
                    ref_id,
                    title,
                    "c",
                    f"untouched {age}d",
                    left=carry,
                )
            )
    return plan


def _mint_leftover_todo(store: Store, item: SweepCandidate, conn: Connection) -> int:
    """The rule-c carry-over: a todo holding the retired node's ``Left:``
    line, linked back to it — open work survives the sweep. Returns the
    todo's ref id."""
    from precis.utils import handle_registry

    me = handle_registry.try_format("memory", item.ref_id) or f"me{item.ref_id}"
    title = item.left or ""
    if len(title) > 140:
        title = title[:139].rstrip() + "…"
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title or f"leftover work from {me}",
        meta={"memory_thread_sweep_from": item.ref_id},
        conn=conn,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn)
    store.add_tag(ref.id, Tag.open("memory-sweep"), set_by="system", conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=(
                    f"Left: {item.left}\n\n"
                    f"Carried over when the thread sweep retired {me} "
                    f"({item.title!r}, untouched ≥{STALE_DAYS}d). The retired "
                    "node still answers get() — restore or work it, then "
                    "close this."
                ),
                meta={"chunk_kind": TODO_BODY_KIND},
            )
        ],
        conn=conn,
    )
    store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=item.ref_id,
        relation="related-to",
        set_by="system",
        conn=conn,
    )
    return int(ref.id)


def run_memory_thread_sweep_pass(
    store: Store, *, dry_run: bool = False, now: datetime | None = None
) -> BatchResult:
    """One sweep tick: plan, then retire each candidate.

    ``claimed`` = thread nodes due this fire; ``ok`` = actually retired
    (0 in dry-run); ``failed`` = candidates whose retire raised (logged,
    the pass continues)."""
    plan = plan_thread_sweep(store, now=now)
    retired = 0
    failed = 0
    suspects = 0
    for item in plan:
        if item.rule == "s":
            # currency suspects are listed (CLI dry-run shows them) and
            # logged — never retired (gr503035).
            suspects += 1
            log.info("memory_thread_sweep: suspect me%d — %s", item.ref_id, item.reason)
            continue
        if dry_run:
            log.info(
                "memory_thread_sweep: would retire me%d rule=%s — %s",
                item.ref_id,
                item.rule,
                item.reason,
            )
            continue
        try:
            todo_id: int | None = None
            with store.tx() as conn:
                # gr503031: drop the node's edges before retiring so the
                # tombstone leaves no dead ends (same cleanup as the
                # delete verb). Runs before the leftover todo is minted —
                # its pointer edge is the intentional exception.
                store.unlink_ref(item.ref_id, conn=conn)
                if item.rule == "c" and item.left:
                    todo_id = _mint_leftover_todo(store, item, conn)
                if item.superseded_by is not None:
                    store.stamp_ref_meta(
                        item.ref_id,
                        {"superseded_by": item.superseded_by},
                        conn=conn,
                    )
                store.retire_ref(item.ref_id, conn=conn)
            retired += 1
            log.info(
                "memory_thread_sweep: retired me%d rule=%s%s — %s",
                item.ref_id,
                item.rule,
                f" todo=td{todo_id}" if todo_id else "",
                item.reason,
            )
        except Exception:
            log.exception("memory_thread_sweep: retire failed for me%d", item.ref_id)
            failed += 1
    return BatchResult(
        handler="memory_thread_sweep",
        claimed=len(plan) - suspects,
        ok=retired,
        failed=failed,
    )


@dataclass(frozen=True)
class ScratchCandidate:
    """One ``SPACE:scratch`` node past its expiry — always ``rule='s'``,
    matching the letter the gripe names; the currency-suspect ``s`` lives
    in the thread plan only, the two never mix."""

    ref_id: int
    title: str
    rule: str  # "s" — scratch expiry
    reason: str  # human-readable why, for the log line and dry-run list


@dataclass(frozen=True)
class _ScratchOwner:
    """A resolved ``scratch-for:<handle>`` tag — the task the scratch
    belongs to and its close stamp (``None`` while the task is still
    open)."""

    ref_id: int
    handle: str  # e.g. 'td606756' — for the plan's reason line
    closed_at: datetime | None


def _scratch_owner_closes(
    conn: Connection, owner_ids: set[int]
) -> dict[int, tuple[str, datetime | None]]:
    """``ref_id → (kind, close stamp)`` for every owner ref that exists
    — ``None`` while the owner is still open.

    The close stamp is the terminal ``STATUS`` tag's
    ``ref_tags.created_at`` (the row is rewritten on every status move,
    so it is the transition time), or the owner's own ``retired_at``
    when the task was tombstoned — the earlier of the two when both
    exist. A handle naming a nonexistent ref is absent from the map, and
    the returned kind lets the caller drop one whose ``td``/``gr``
    prefix does not match the stored row — both keep the node on the
    default TTL."""
    rows: dict[int, tuple[str, datetime | None]] = {}
    status: dict[int, tuple[datetime, str]] = {}
    retired: dict[int, datetime] = {}
    for ref_id, kind, retired_at, value, stamped in conn.execute(
        _SCRATCH_OWNER_STATUS, (sorted(owner_ids),)
    ).fetchall():
        rid = int(ref_id)
        rows[rid] = (str(kind), None)
        if retired_at is not None:
            retired[rid] = retired_at
        if value is not None:
            prev = status.get(rid)
            if prev is None or stamped > prev[0]:
                status[rid] = (stamped, str(value))
    for rid, (kind, _) in rows.items():
        stamps = []
        current = status.get(rid)
        if current is not None and current[1] in BLOCKER_TERMINAL_STATUSES:
            stamps.append(current[0])
        if rid in retired:
            stamps.append(retired[rid])
        rows[rid] = (kind, min(stamps) if stamps else None)
    return rows


def _scratch_expiry(
    meta: dict[str, Any] | None,
    updated_at: datetime,
    *,
    owners: list[_ScratchOwner] | None = None,
) -> tuple[datetime, str]:
    """When a scratch node expires, and what drove it — ``(expiry,
    driver)`` where driver is the short phrase the plan's reason line
    reports.

    ``meta.expires`` wins: an absolute instant or ``<n>[mhdw]`` duration
    from ``updated_at`` when set and parseable; an unparseable override
    falls through — a typo must not make a throwaway node immortal.
    Next the ``scratch-for:`` owner (gr605668): while any owner is still
    open the only clock is the :data:`SCRATCH_FLOOR_DAYS`-day floor from
    ``updated_at``; once every owner is closed the expiry is the last
    close stamp + :data:`SCRATCH_OWNER_GRACE_DAYS` — the floor still
    caps it, so a task that never closes loses its scratch anyway.
    Ownerless nodes keep the :data:`SCRATCH_TTL_DAYS`-day default."""
    raw = (meta or {}).get("expires")
    expires = as_utc(raw)
    if expires is None and isinstance(raw, str):
        m = _SCRATCH_DURATION_RE.match(raw.strip().lower())
        if m:
            expires = updated_at + timedelta(
                **{_SCRATCH_DURATION_UNITS[m.group(2)]: int(m.group(1))}
            )
    if expires is not None:
        return expires, "meta.expires"
    if owners:
        floor = updated_at + timedelta(days=SCRATCH_FLOOR_DAYS)
        open_handles = [o.handle for o in owners if o.closed_at is None]
        if open_handles:
            return floor, (
                f"owner {', '.join(open_handles)} open; {SCRATCH_FLOOR_DAYS}d floor"
            )
        last_close = max(c for c in (o.closed_at for o in owners) if c is not None)
        named = next(o.handle for o in owners if o.closed_at == last_close)
        close_expiry = last_close + timedelta(days=SCRATCH_OWNER_GRACE_DAYS)
        if close_expiry <= floor:
            return close_expiry, (
                f"owner {named} closed {last_close.date().isoformat()} "
                f"+{SCRATCH_OWNER_GRACE_DAYS}d grace"
            )
        return floor, (
            f"{SCRATCH_FLOOR_DAYS}d floor bound before owner {named}'s close+grace"
        )
    return updated_at + timedelta(days=SCRATCH_TTL_DAYS), (
        f"default {SCRATCH_TTL_DAYS}d TTL"
    )


def plan_scratch_sweep(
    store: Store, *, now: datetime | None = None
) -> list[ScratchCandidate]:
    """Every live ``SPACE:scratch`` node past its expiry, oldest id
    first. Pure read — the dry-run mode is this plan printed."""
    from precis.utils import handle_registry

    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(_SCRATCH_CANDIDATES, (INDEX_TAG,)).fetchall()
        parsed: list[
            tuple[int, str, datetime | None, Any, list[tuple[str, str, int]]]
        ] = []
        owner_ids: set[int] = set()
        for ref_id, title, updated_at, meta, scratch_for in rows:
            pairs: list[tuple[str, str, int]] = []
            seen: set[int] = set()
            for value in scratch_for or []:
                m = _SCRATCH_FOR_RE.match(str(value))
                if not m:
                    continue
                handle = m.group(1)
                p = handle_registry.parse(handle)
                if p is None or p[1] or p[2] in seen:
                    continue
                seen.add(p[2])
                pairs.append((handle, p[0], p[2]))
            owner_ids.update(rid for _, _, rid in pairs)
            parsed.append((int(ref_id), str(title or ""), updated_at, meta, pairs))
        closes = _scratch_owner_closes(conn, owner_ids) if owner_ids else {}
    plan: list[ScratchCandidate] = []
    for ref_id, title, updated_at, meta, pairs in parsed:
        if updated_at is None:
            continue
        owners = [
            _ScratchOwner(rid, handle, closes[rid][1])
            for handle, kind, rid in pairs
            if rid in closes and closes[rid][0] == kind
        ]
        expiry, driver = _scratch_expiry(meta, updated_at, owners=owners)
        if expiry <= now:
            age = (now - updated_at).days
            plan.append(
                ScratchCandidate(
                    ref_id,
                    title,
                    "s",
                    f"scratch node expired {expiry.date().isoformat()} "
                    f"({driver}; untouched {age}d)",
                )
            )
    return plan


def run_scratch_sweep_pass(
    store: Store, *, dry_run: bool = False, now: datetime | None = None
) -> BatchResult:
    """The rule-s tick: retire each expired ``SPACE:scratch`` node.

    ``claimed`` = scratch nodes due this fire; ``ok`` = actually retired
    (0 in dry-run); ``failed`` = candidates whose retire raised (logged,
    the pass continues). Same transaction shape as the thread pass —
    edges drop before the tombstone (gr503031)."""
    plan = plan_scratch_sweep(store, now=now)
    retired = 0
    failed = 0
    for item in plan:
        if dry_run:
            log.info(
                "memory_thread_sweep: would retire me%d rule=%s — %s",
                item.ref_id,
                item.rule,
                item.reason,
            )
            continue
        try:
            with store.tx() as conn:
                store.unlink_ref(item.ref_id, conn=conn)
                store.retire_ref(item.ref_id, conn=conn)
            retired += 1
            log.info(
                "memory_thread_sweep: retired me%d rule=%s — %s",
                item.ref_id,
                item.rule,
                item.reason,
            )
        except Exception:
            log.exception("memory_thread_sweep: retire failed for me%d", item.ref_id)
            failed += 1
    return BatchResult(
        handler="memory_thread_sweep",
        claimed=len(plan),
        ok=retired,
        failed=failed,
    )


__all__ = [
    "SCRATCH_FLOOR_DAYS",
    "SCRATCH_OWNER_GRACE_DAYS",
    "SCRATCH_TTL_DAYS",
    "ScratchCandidate",
    "SweepCandidate",
    "plan_scratch_sweep",
    "plan_thread_sweep",
    "run_memory_thread_sweep_pass",
    "run_scratch_sweep_pass",
]
