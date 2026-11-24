"""The daily ``section:threads`` retirement sweep (gr485358 item 1) —
fired from the ``memory_thread_sweep`` scheduler cadence.

Why it exists: thread-node retirement was manual only —
``scripts/memory-lint`` listed candidates and the reconsolidation pass
ran only when a session acted on its nag, so dead resume nodes piled
into ``search(kind='memory')`` recall (2026-10-14: 22 live threads, four
of them one session's resume chain, each saying "Supersedes meN" in
prose with no edge). The rules are the ones the repo-dev memory policy
already wrote down; this cadence is just their executor. Reto's
no-silent-removal ruling (gr746642, 2026-11-21) sets which rules may
retire on their own: only a thread whose work is done. A thread that
still holds open work or open thinking is *asked about*, never retired
unanswered.

* **rule a** — the body's first line is ``Left: none``: the campaign
  declared itself finished. Finished and empty threads retire on the
  next sweep.
* **rule b** — another *live memory* links ``supersedes`` to it: the
  replacement is on record, the old node is clutter. Retires
  automatically like rule a.
* **rule c** — untouched for :data:`~precis.utils.memory_threads.STALE_DAYS`
  days (the same clock ``precis memory index`` calls "live"). This rule
  does **not** retire (gr746642): it raises a decision ask — an
  ``ask-user`` todo with ``meta.ask`` options ``extend`` / ``retire`` /
  ``transfer``, linked back to the thread node and titled with the
  node's title and age. The daily review shows its first-line state
  in ``view='asks'`` and ``view='reto-queue'``; the node stays in recall the whole time. One ask per node:
  while a live ask for the node exists, no new one is minted, and an
  unanswered ask is never a retire reason — the item just stays in the
  review. Answered asks are enacted on a later fire: ``extend`` resets
  the clock (``updated_at`` moves to the enactment instant, so the next
  ask needs another full stale window), ``retire`` soft-retires the
  node, and ``transfer`` soft-retires it too — the answerer promotes
  the still-true parts into a live memory or spec with a
  ``derived-from`` link to the old thread before enactment. An ask the
  sweep can no longer act on is *closed out*, not left to rot in the
  review (td775760 review): an ask whose thread is no longer rule-c
  eligible (edited back into currency, or now a currency suspect)
  closes as ``thread-no-longer-stale``; a closed ask whose ``answer:``
  comment names no valid choice closes as ``answer-unrecognised``; a
  ``transfer`` answer with no live ``derived-from`` successor closes as
  ``transfer-no-successor``. The first case takes no further action —
  the thread's clock simply runs again; the other two leave the thread
  rule-c eligible, so the same fire re-raises the decision, quoting the
  reason in the new ask's body. A closed ask with no ``answer:``
  comment at all is a deliberate human close: it stays in the review as
  a record and is never re-raised.
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
never a hard delete. A scratch tombstone keeps its inbound
``supersedes`` edges: the harvest's clean node links ``supersedes``
each scratch it condenses, and that edge is the provenance the skill
promises survives the dissolve. Every retirement or ask logs one
greppable line with the rule letter::

    memory_thread_sweep: retired me<id> rule=a — first line is 'Left: none'
    memory_thread_sweep: ask raised for me<id> rule=c — untouched 21d (ask=td<n>)
    memory_thread_sweep: enacted extend for me<id> (ask=td<n>)

``dry_run=True`` plans only: each candidate is logged as
``would retire me<id> rule=<r>`` (rules a/b) or
``would ask about me<id> rule=c`` and nothing is written — what
``precis memory sweep`` prints. Per-candidate failures are logged and
counted, never abort the pass (one poisoned row must not strand the
rest), and the run is idempotent: a retired node is gone from the
candidate query and an asked-about node already has its live ask, so
the next fire has nothing to re-act on.
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
    PENDING_THREAD_ASKS,
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
 AND (%s::bigint IS NULL OR r.ref_id = %s)
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
    """One thread node the sweep would act on, with its rule and the
    state a rule-c ask must carry into the daily review."""

    ref_id: int
    title: str
    rule: str  # "a" | "b" | "c" | "s" (currency suspect — listed, never acted on)
    reason: str  # human-readable why, for the log line and dry-run list
    #: Rule c only: the ``Left:`` remainder when the body's first line
    #: carries it — quoted in the ask so the review line shows the open
    #: work the node still holds.
    left: str | None = None
    #: Rule c only: the node's age in whole days at plan time — the ask
    #: title reports it.
    age_days: int | None = None
    #: Rule b only: the live memory that supersedes this node (stamped
    #: onto ``meta.superseded_by``, the same breadcrumb the ``supersede``
    #: verb leaves).
    superseded_by: int | None = None


def plan_thread_sweep(
    store: Store, *, now: datetime | None = None
) -> list[SweepCandidate]:
    """Every live non-scratch ``section:threads`` node due for a sweep
    action (retirement under rules a/b, a decision ask under rule c),
    oldest id first — scratch-owned nodes are the scratch pass's (see
    :data:`_THREAD_CANDIDATES`). Pure read — the dry-run mode is this
    plan printed."""
    with store.pool.connection() as conn:
        return _thread_plan(conn, now or datetime.now(UTC))


def thread_sweep_rule(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        plan = _thread_plan(conn, datetime.now(UTC), ref_id)
    return plan[0].rule if plan else None


def _thread_plan(
    conn: Connection, now: datetime, ref_id: int | None = None
) -> list[SweepCandidate]:
    rows = conn.execute(
        _THREAD_CANDIDATES, (THREADS_TAG, INDEX_TAG, ref_id, ref_id)
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    superseded = (
        {int(d): int(s) for d, s in conn.execute(_SUPERSEDED_BY, (ids,)).fetchall()}
        if ids
        else {}
    )
    cutoff = now - timedelta(days=STALE_DAYS)
    plan: list[SweepCandidate] = []
    for ref_id, title, updated_at, body in rows:
        ref_id = int(ref_id)
        title = str(title or "")
        left = first_left(str(body or ""))
        if not str(body or "").strip() or (left is not None and left_is_done(left)):
            plan.append(
                SweepCandidate(
                    ref_id, title, "a", "empty body or first line is 'Left: none'"
                )
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
        elif updated_at is not None and updated_at <= cutoff:
            # gr503035: a dated section inside the stale window means the
            # node's own text claims to be current — a currency suspect
            # (the same guard memory-lint's landed verdict uses), never a
            # retire action. The sweep has no git oracle, so the
            # "older than the newest cited landing" half of the rule is
            # lint-side; this is the half both agree on.
            fresh = newest_body_date(str(body or ""))
            fresh_from = now.date() - timedelta(days=STALE_DAYS)
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
            age = (now - updated_at).days
            carry = left if left else None
            plan.append(
                SweepCandidate(
                    ref_id,
                    title,
                    "c",
                    f"untouched {age}d",
                    left=carry,
                    age_days=age,
                )
            )
    return plan


_PRIOR_OUTCOME_NOTES = {
    "answer-unrecognised": (
        "the previous ask's `answer:` comment did not name one of extend / "
        "retire / transfer"
    ),
    "transfer-no-successor": (
        "the previous ask was answered transfer, but no live memory yet "
        "links derived-from this thread"
    ),
}


def _mint_thread_ask(
    store: Store, item: SweepCandidate, now: datetime, *, prior: str | None = None
) -> int | None:
    """Raise one decision, serializing scheduler and CLI runs on the thread.

    ``prior`` is the close-out outcome of the thread's previous ask when
    this mint re-raises a decided-but-unusable decision — the body quotes
    the reason so the reviewer sees why a second ask exists."""
    from precis.todo.guards import check_ask_meta
    from precis.utils import handle_registry

    me = handle_registry.try_format("memory", item.ref_id) or f"me{item.ref_id}"
    left = (item.left or "").strip()
    age_days = item.age_days or 0
    prior_note = ""
    if prior in _PRIOR_OUTCOME_NOTES:
        prior_note = (
            "A previous decision ask closed without enacting: "
            + _PRIOR_OUTCOME_NOTES[prior]
            + ". This ask re-raises the decision.\n\n"
        )
    ask = {
        "kind": "design-verdict",
        "options": [
            f"extend — keep the thread; the {STALE_DAYS}-day clock restarts",
            "retire — soft-retire the thread now (leaves recall, recoverable)",
            "transfer — promote the still-true parts, then it retires",
        ],
        "blocks": "nothing",
        "context": ("Left: " + (left or "<no Left: line>"))[:600],
    }
    title = f"{me} untouched {age_days}d — {item.title}"
    check_ask_meta({"ask": ask}, question=title)
    if len(title) > 140:
        title = title[:139].rstrip() + "…"
    with store.tx() as conn:
        _lock_ref(conn, item.ref_id)
        if conn.execute(
            f"SELECT 1 FROM ({PENDING_THREAD_ASKS}) pending WHERE thread_id = %s",
            (item.ref_id,),
        ).fetchone() or _thread_plan(conn, now, item.ref_id) != [item]:
            return None
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta={"memory_thread_sweep_from": item.ref_id, "ask": ask},
            conn=conn,
        )
        store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn)
        store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
        store.add_tag(
            ref.id, Tag.open("thread-sweep-decision"), set_by="system", conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=(
                        f"Thread {me} ({item.title!r}) has been untouched for "
                        f"{age_days} days — at or past the {STALE_DAYS}-day "
                        "thread clock.\n\n"
                        "Left: "
                        + (
                            left
                            if left
                            else "<no Left: line — the node carries no "
                            "declared resume state>"
                        )
                        + "\n\n"
                        + prior_note
                        + "Choose one: comment `answer: extend`, `answer: "
                        "retire` or `answer: transfer`, then tag STATUS:done "
                        "and remove the ask-user tag.\n"
                        f"- extend — the thread stays in recall and the clock "
                        f"restarts; the sweep asks again only after another "
                        f"{STALE_DAYS} untouched days.\n"
                        "- retire — the sweep soft-retires the thread: it "
                        "leaves recall, recoverable at the SQL layer; "
                        "nothing is hard-deleted.\n"
                        "- transfer — first promote the still-true parts "
                        "into a live memory or spec and link it as derived-from "
                        "this thread, then answer "
                        "transfer and the sweep soft-retires this thread.\n\n"
                        "No answer: the item stays in the daily review "
                        "(view='asks') and the thread stays in recall — "
                        "an unanswered ask is never a retire reason "
                        "(gr746642)."
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


_ASK_CHOICES = ("extend", "retire", "transfer")


def _lock_ref(conn: Connection, ref_id: int) -> None:
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref_id,))


def _answer_of(
    store: Store, conn: Connection, todo_id: int
) -> tuple[str | None, str | None]:
    """The last ``answer:`` comment on the ask: ``(choice, raw)``.

    ``choice`` is the normalised answer when it names one of
    :data:`_ASK_CHOICES`, else ``None``; ``raw`` is the normalised answer
    text either way, or ``None`` when there is no ``answer:`` comment at
    all — the caller distinguishes "no answer" from "unusable answer"."""
    chunks = store.chunks.list_chunks_for_ref_by_kind(
        todo_id, "todo_comment", conn=conn
    )
    for c in reversed(chunks):
        text = (c.text or "").strip().lower()
        if text.startswith("answer:"):
            raw = text.removeprefix("answer:").strip().rstrip(".")
            choice = raw if raw in _ASK_CHOICES else None
            return choice, raw
    return None, None


def _pending_decisions(
    conn: Connection, *, thread_id: int | None = None
) -> list[tuple[int, int, bool]]:
    return conn.execute(
        f"""
        SELECT ask_id, thread_id, EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ask_id AND t.namespace = 'STATUS'
               AND t.value = ANY(%s)
               AND (rt.expires_at IS NULL OR rt.expires_at > now())
        )
        FROM ({PENDING_THREAD_ASKS}) pending
        WHERE (%s::bigint IS NULL OR pending.thread_id = %s)
        ORDER BY ask_id
        """,
        (sorted(BLOCKER_TERMINAL_STATUSES), thread_id, thread_id),
    ).fetchall()


def _promotion_of(
    conn: Connection, thread_id: int, *, lock: bool = False
) -> int | None:
    # A derivation records promotion without triggering automatic rule b.
    lock_clause = "FOR SHARE OF m, l" if lock else ""
    row = conn.execute(
        f"""
        SELECT m.ref_id FROM links l JOIN refs m ON m.ref_id = l.src_ref_id
         WHERE l.dst_ref_id = %s AND l.relation = 'derived-from'
           AND m.ref_id <> l.dst_ref_id AND m.kind = 'memory'
           AND m.retired_at IS NULL
         ORDER BY m.ref_id LIMIT 1 {lock_clause}
        """,
        (thread_id,),
    ).fetchone()
    return int(row[0]) if row else None


def _decision_resolution(
    store: Store,
    conn: Connection,
    ask_id: int,
    thread_id: int,
    closed: bool,
    now: datetime,
) -> tuple[str | None, str | None]:
    """Resolve a pending decision ask: ``(enactable choice, close-out
    outcome)`` — at most one is set.

    A *close-out* finishes an ask the sweep can no longer act on, with the
    reason named in ``meta.thread_sweep_enacted`` (review finding on
    td775760): an ask whose thread is no longer rule-c eligible — edited
    back into currency, or now a currency suspect — closes as
    ``thread-no-longer-stale``; a closed ask whose ``answer:`` comment
    names no valid choice closes as ``answer-unrecognised``; a ``transfer``
    answer with no live ``derived-from`` successor closes as
    ``transfer-no-successor``. The first case takes no further action; the
    other two leave the thread rule-c eligible, so the mint pass re-raises
    the decision the same fire, quoting the reason. A closed ask with no
    ``answer:`` comment at all is a deliberate human close — it stays in
    the review as a record and is never re-raised. Rules a/b asks are left
    for the retire pass, which closes them ``thread-already-finished``."""
    plan = _thread_plan(conn, now, thread_id)
    rule = plan[0].rule if plan else None
    if not closed:
        # An open ask whose premise ("untouched 14 days") has become false
        # is obsolete noise in the daily review — close it. Rules a/b are
        # the retire pass's job (the thread retires, not just the ask).
        if rule not in ("a", "b", "c"):
            return None, "thread-no-longer-stale"
        return None, None
    choice, raw = _answer_of(store, conn, ask_id)
    if raw is None:
        return None, None
    if choice is None:
        return None, "answer-unrecognised"
    if rule == "c":
        if choice == "transfer" and _promotion_of(conn, thread_id) is None:
            return None, "transfer-no-successor"
        return choice, None
    if rule in ("a", "b"):
        return None, None
    return None, "thread-no-longer-stale"


def _decision_action(
    store: Store,
    conn: Connection,
    ask_id: int,
    thread_id: int,
    closed: bool,
    now: datetime,
) -> str | None:
    """The enactable choice, or ``None`` — obsolete/unusable asks resolve
    through :func:`_decision_resolution`'s close-out outcome instead."""
    return _decision_resolution(store, conn, ask_id, thread_id, closed, now)[0]


def plan_thread_decisions(
    store: Store, *, now: datetime | None = None
) -> list[tuple[int, int, str | None]]:
    """Pending asks and the choices currently ready to enact."""
    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        return [
            (ask, thread, _decision_action(store, conn, ask, thread, closed, now))
            for ask, thread, closed in _pending_decisions(conn)
        ]


def _finish_decision(store: Store, conn: Connection, ask_id: int, outcome: str) -> None:
    store.stamp_ref_meta(ask_id, {"thread_sweep_enacted": outcome}, conn=conn)
    store.remove_tag(ask_id, Tag.open("ask-user"), conn=conn)
    store.add_tag(
        ask_id,
        Tag.closed("STATUS", "done"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )


def _enact_answered_thread_asks(
    store: Store, now: datetime, *, dry_run: bool
) -> tuple[int, int, int, dict[int, str]]:
    """Recheck provenance, the answer and thread eligibility under lock:
    enact usable answers, close out obsolete or unusable asks.

    Returns ``(claimed, enacted, failed, closeouts)`` — ``closeouts`` maps
    a thread id to the finish outcome of its closed ask, so the mint pass
    can re-raise the decision quoting the reason."""
    claimed = enacted = failed = 0
    closeouts: dict[int, str] = {}
    with store.pool.connection() as conn:
        pending = _pending_decisions(conn)
    for ask_id, thread_id, closed in pending:
        with store.pool.connection() as conn:
            choice, outcome = _decision_resolution(
                store, conn, ask_id, thread_id, closed, now
            )
        if outcome is not None:
            if dry_run:
                log.info(
                    "memory_thread_sweep: would close out ask td%d (me%d): %s",
                    ask_id,
                    thread_id,
                    outcome,
                )
                continue
            try:
                with store.tx() as conn:
                    _lock_ref(conn, thread_id)
                    _lock_ref(conn, ask_id)
                    still = {
                        row[0]: row[2]
                        for row in _pending_decisions(conn, thread_id=thread_id)
                    }
                    if ask_id not in still:
                        continue
                    if _decision_resolution(
                        store, conn, ask_id, thread_id, still[ask_id], now
                    ) != (None, outcome):
                        continue
                    _finish_decision(store, conn, ask_id, outcome)
                closeouts[thread_id] = outcome
                log.info(
                    "memory_thread_sweep: closed out ask td%d (me%d): %s",
                    ask_id,
                    thread_id,
                    outcome,
                )
            except Exception:
                log.exception(
                    "memory_thread_sweep: close-out failed for ask td%d", ask_id
                )
                failed += 1
            continue
        if not closed or choice is None:
            continue
        claimed += 1
        if dry_run:
            log.info("memory_thread_sweep: would enact %s for me%d", choice, thread_id)
            continue
        try:
            with store.tx() as conn:
                _lock_ref(conn, thread_id)
                _lock_ref(conn, ask_id)
                still_rows = _pending_decisions(conn, thread_id=thread_id)
                if (ask_id, thread_id, True) not in still_rows:
                    continue
                enact = _decision_resolution(store, conn, ask_id, thread_id, True, now)
                if enact[0] is None:
                    continue
                if enact[0] == "extend":
                    conn.execute(
                        "UPDATE refs SET updated_at = %s WHERE ref_id = %s",
                        (now, thread_id),
                    )
                else:
                    if enact[0] == "transfer":
                        successor = _promotion_of(conn, thread_id, lock=True)
                        if successor is None:
                            continue
                        store.stamp_ref_meta(
                            thread_id,
                            {"thread_sweep_transferred_to": successor},
                            conn=conn,
                        )
                    store.unlink_ref(thread_id, conn=conn)
                    store.retire_ref(thread_id, conn=conn)
                _finish_decision(store, conn, ask_id, enact[0])
            enacted += 1
            log.info(
                "memory_thread_sweep: enacted %s for me%d (ask=td%d)",
                choice,
                thread_id,
                ask_id,
            )
        except Exception:
            log.exception("memory_thread_sweep: enact failed for ask td%d", ask_id)
            failed += 1
    return claimed, enacted, failed, closeouts


def thread_has_pending_ask(store: Store, ref_id: int) -> bool:
    """True when a live decision ask binds this thread — open in the daily
    review or closed awaiting enactment. The read footer uses it to say
    "ask pending" instead of a stale review date."""
    with store.pool.connection() as conn:
        return bool(_pending_decisions(conn, thread_id=ref_id))


def run_memory_thread_sweep_pass(
    store: Store, *, dry_run: bool = False, now: datetime | None = None
) -> BatchResult:
    """One sweep tick: enact answered rule-c asks, close out obsolete or
    unusable ones, then plan and act.

    Rules a/b retire their candidate in this fire; rule c raises a
    decision ask instead (gr746642 — nothing leaves recall unanswered).
    A closed ask the sweep cannot act on finishes with the reason named
    in ``meta.thread_sweep_enacted`` (``thread-no-longer-stale``,
    ``answer-unrecognised``, ``transfer-no-successor``); the last two
    re-raise the decision the same fire, quoting the reason. ``claimed``
    = thread nodes acted on this fire (due and not already carrying an
    unenacted ask); ``ok`` = actions performed (retirements, asks
    raised or re-raised, answered asks enacted, obsolete asks closed;
    0 in dry-run); ``failed`` = candidates whose action raised (logged,
    the pass continues)."""
    now = now or datetime.now(UTC)
    acted, enacted, failed, closeouts = _enact_answered_thread_asks(
        store, now, dry_run=dry_run
    )
    plan = plan_thread_sweep(store, now=now)
    retired = 0
    asks_raised = 0
    with store.pool.connection() as conn:
        asked = {thread for _, thread, _ in _pending_decisions(conn)}
    for item in plan:
        if item.rule == "s":
            # currency suspects are listed (CLI dry-run shows them) and
            # logged — never acted on (gr503035).
            log.info("memory_thread_sweep: suspect me%d — %s", item.ref_id, item.reason)
            continue
        if item.rule == "c":
            # gr746642: ask, never retire. One unenacted ask per node —
            # while one exists (open in the review, or closed by a human
            # without a usable choice) no second ask is minted.
            if item.ref_id in asked:
                continue
            acted += 1
            if dry_run:
                log.info(
                    "memory_thread_sweep: would ask about me%d rule=%s — %s",
                    item.ref_id,
                    item.rule,
                    item.reason,
                )
                continue
            try:
                ask_id = _mint_thread_ask(
                    store, item, now, prior=closeouts.get(item.ref_id)
                )
            except Exception:
                log.exception(
                    "memory_thread_sweep: ask raise failed for me%d", item.ref_id
                )
                failed += 1
                continue
            if ask_id is None:
                acted -= 1
                continue
            asks_raised += 1
            log.info(
                "memory_thread_sweep: ask raised for me%d rule=%s (ask=td%d) — %s",
                item.ref_id,
                item.rule,
                ask_id,
                item.reason,
            )
            continue
        acted += 1
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
                _lock_ref(conn, item.ref_id)
                if _thread_plan(conn, now, item.ref_id) != [item]:
                    acted -= 1
                    continue
                for ask_id, _, _ in _pending_decisions(conn, thread_id=item.ref_id):
                    _finish_decision(store, conn, ask_id, "thread-already-finished")
                # gr503031: drop the node's edges before retiring so the
                # tombstone leaves no dead ends (same cleanup as the
                # delete verb).
                store.unlink_ref(item.ref_id, conn=conn)
                if item.superseded_by is not None:
                    store.stamp_ref_meta(
                        item.ref_id,
                        {"superseded_by": item.superseded_by},
                        conn=conn,
                    )
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
        claimed=acted,
        ok=retired + asks_raised + enacted + len(closeouts),
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


def _scratch_owner_pairs(values: list[str]) -> list[tuple[str, str, int]]:
    from precis.utils import handle_registry

    pairs: list[tuple[str, str, int]] = []
    seen: set[int] = set()
    for value in values:
        m = _SCRATCH_FOR_RE.match(str(value))
        if not m:
            continue
        handle = m.group(1)
        p = handle_registry.parse(handle)
        if p is None or p[1] or p[2] in seen:
            continue
        seen.add(p[2])
        pairs.append((handle, p[0], p[2]))
    return pairs


def _scratch_owners(
    pairs: list[tuple[str, str, int]],
    closes: dict[int, tuple[str, datetime | None]],
) -> list[_ScratchOwner]:
    return [
        _ScratchOwner(rid, handle, closes[rid][1])
        for handle, kind, rid in pairs
        if rid in closes and closes[rid][0] == kind
    ]


def scratch_retirement_date(
    store: Store,
    meta: dict[str, Any],
    updated_at: datetime,
    tags: list[str],
) -> datetime:
    """Compute the same expiry instant for the footer and the scratch sweep."""
    pairs = _scratch_owner_pairs(tags)
    if not pairs:
        return _scratch_expiry(meta, updated_at)[0]
    with store.pool.connection() as conn:
        closes = _scratch_owner_closes(conn, {rid for _, _, rid in pairs})
    return _scratch_expiry(meta, updated_at, owners=_scratch_owners(pairs, closes))[0]


def plan_scratch_sweep(
    store: Store, *, now: datetime | None = None
) -> list[ScratchCandidate]:
    """Every live ``SPACE:scratch`` node past its expiry, oldest id
    first. Pure read — the dry-run mode is this plan printed."""
    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(_SCRATCH_CANDIDATES, (INDEX_TAG,)).fetchall()
        parsed = [
            (ref_id, title, updated_at, meta, _scratch_owner_pairs(scratch_for or []))
            for ref_id, title, updated_at, meta, scratch_for in rows
        ]
        owner_ids = {rid for _, _, _, _, pairs in parsed for _, _, rid in pairs}
        closes = _scratch_owner_closes(conn, owner_ids) if owner_ids else {}
    plan: list[ScratchCandidate] = []
    for ref_id, title, updated_at, meta, pairs in parsed:
        if updated_at is None:
            continue
        expiry, driver = _scratch_expiry(
            meta, updated_at, owners=_scratch_owners(pairs, closes)
        )
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
    edges drop before the tombstone (gr503031) — except inbound
    ``supersedes`` edges: the scratch harvest (precis-gripe-loop-help
    step 8) links the clean node ``supersedes`` each scratch it
    condenses, and that edge is the promised provenance the survivor
    reads after the dissolve."""
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
                # Inbound ``supersedes`` edges stay (gr503546's tombstone
                # trail, same as the memory delete door): a harvest's
                # clean node keeps its "supersedes me<scratch>" edge
                # readable after the scratch dissolves. Every other edge
                # drops (gr503031).
                store.unlink_ref(
                    item.ref_id, conn=conn, preserve_inbound_supersedes=True
                )
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
