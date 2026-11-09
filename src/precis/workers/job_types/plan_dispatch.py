"""``plan_dispatch`` job_type — the dark-factory tick that drives plan subtrees.

Spec: ``docs/backlog/dark-factory.md`` § "Plans" → "The dispatcher tick" and
"Job lifecycle per task"; the kill switch is step 7's. A plan is a todo
subtree whose nodes are shaped by :mod:`precis.plans.nodes`. One pass:

1. **gates** — every live, unblocked gate todo is evaluated
   (:func:`precis.plans.gates.evaluate`); the verdict lands on
   ``meta.gate_result = {open, why, at}`` (written when it changes) and an
   open gate goes ``STATUS:done``, which frees its ``blocked-by`` dependants.
   A release-pass approval ask (``meta.ask.kind == 'approval'``) is first
   offered to :func:`precis.plans.release.auto_answer_approval` — the
   ``release.auto_deploy`` standing approval answers ``deploy r<N>``
   itself once CI is recorded green on the cut's exact sha and
   ``release.round`` names an open round on it (td573809, me573231).
2. **loops** — once every current child of a loop is closed: ``until`` open
   or ``meta.loop.count`` (default 0) at ``max`` closes the loop; otherwise
   the body is re-minted under it (:func:`precis.plans.templates.mint_body`)
   and the count bumped. A body with no minter closes the loop with a
   comment naming the gap. A ``release-fix`` loop's automatic cap is
   :data:`_RELEASE_FIX_AUTO_CAP` regardless of its stamped ``max``: the
   second red iteration files a ruling ask (naming the round, the head
   sha and the failing CI) and closes the loop — a third fix is a human
   call (me573231).
2a. **reviews** — a reviewer generator (``meta.generate.role ==
   'reviewer'``, ``from: 'td<id>'``) whose newest job succeeded is judged by
   its newest comment starting ``review:`` posted since that job was minted
   — a ``review:``-starting comment always wins — else its newest
   ``handoff:`` carrying a ``review: LAND|FIX`` token at a line start
   (markdown ``- ``/``* ``/``**``/``#``/``> `` markers may sit between
   the line start and the token) or right after sentence-final
   punctuation, outside backticks/parens — the verdict lines too, so a
   quoted grammar echo never wins the strictest pick (td591267) — no
   ``<…>`` rest (a lane gluing
   the verdict to its narration, gr573564; on disagreement a reasoned
   FIX beats a LAND; REVERT never comes from a token). A LAND rest
   opening ``(clean`` or ``|`` is the grammar echo, and a placeholder
   ``review: FIX|REVERT — <…>`` template line anywhere in the comment
   suppresses every LAND in it. Grammar, verdict word
   case-insensitive, any of ``—`` / ``–`` / ``-`` as the dash::

       review: LAND (read-only, tests not run)
       review: FIX — <the list of findings> (read-only, tests not run)
       review: REVERT — <why> (read-only, tests not run)

   The ``(read-only, tests not run)`` marker is stripped where it ends a
   line before matching — accepted, never required, so a forgotten
   marker never strands a review. A LAND is a diff read, never a test
   result. ``LAND`` closes the node. ``FIX`` mints one ``run_slice`` task
   ``Fix review findings for td<id>`` (body = the list) under the plan root
   and closes the node. ``REVERT`` mints one ``run_slice`` task ``Revert
   <sha> on main`` (the ``landed: <sha>`` comment of each reviewed node --
   ``td<id>`` itself, or its child todos when it has them, as a slicer
   does) under the root, reopens each reviewed node ``blocked-by`` that
   revert with a ``handoff: review: REVERT — <why>`` comment (the rebuild's
   brief), and leaves the reviewer open to review the rebuild; both get a
   fresh retry budget (``meta.rearmed_after_job``). Each ``sha-in-prod``
   gate of the plan on a reverted sha goes back to ``PENDING_SHA`` (reopened
   if it had opened) with a ``sha reset after review REVERT`` comment. A missing or malformed
   verdict reopens the node — the retry stays on the picked lane (a
   format miss never climbs the ladder, gr573564) — and counts against
   the retry cap; at cap step 5 sends the reviewer to rescue as cls
   ``review``. ``meta.review = {verdict, job, at,
   tests_run}`` records the job judged, so a pass never acts twice on one
   job; ``tests_run`` is always ``False`` — the reviewer is read-only —
   and only this step and its retry path read the record, so nothing
   reads a verdict as test evidence.
3. **phases** — first reopens a generator that is ``STATUS:done`` while a
   child is still open (a generator's session mints children, it does not
   close the node; counted as ``reopened``). Then closes every node whose work
   is finished: a phase
   (``meta.auto_close``) or a generator with children, once every child is
   closed; an ``analyst`` generator once its newest job succeeded and an
   ``analysis:`` comment was posted since (td543749); a mechanical task (``meta.op`` other than ``run_slice``) whose
   newest job succeeded; a ``run_slice`` task whose newest job is a
   succeeded ``land_slice`` (a succeeded build alone never closes it, and
   a build plus its red land count as one attempt). Deepest first, so
   nested phases cascade in one pass.
4. **dispatch** — each doable task (``node_kind`` task, a generator with no
   children yet, or a plain leaf under a plan root; ``STATUS:open``, no live
   ``blocked-by`` target on it or an ancestor, no doable-exclusion tag other
   than ``child-failed:``, no paused ancestor, no live child job, no
   ``claimed-by:`` tag, under its retry cap) gets one job parented to it:
   ``meta.op`` resolved to ``{type, params}`` (a bare name is
   ``{type: name, params: {todo_id}}``), default ``run_slice {todo_id}``.
   The tick owns plan retries, so a retry drops the ``child-failed:`` tags
   the failure bubble left. **Re-land** (gr570187): a task whose newest
   job is a failed ``land_slice`` carrying ``attempt_free == "infra-land"``
   re-mints ``land_slice`` with the failed job's own params (idem
   ``land_slice:td<id>``) — no builder, no ladder rung, no review lane —
   up to :data:`LAND_AGAIN_CAP` consecutive infra lands, then the hygiene
   ask offers ``land again (no builder)``. **Order:** candidates from
   every plan root are
   taken together, sorted by (prio ascending, todo id ascending); a node
   with no ``prio`` takes its nearest ancestor's, else 5. So a prio-1
   release fixer is minted (and reaches the lanes) before a prio-5 gripe
   build, whichever root is older. A reviewer's ``run_slice`` is pinned to
   ``pick_lane('review', exclude_vendor=<the builder lane's vendor>)``;
   while no other vendor's lane is enabled that falls back to the
   builder's lane and the action note says so.
5. **hygiene** — a ``claimed-by:`` tag older than 4 h on a todo with no live
   child job is released (``STATUS:open``); the same for a ``STATUS:fixing``
   gripe (lease past ``expires_at``, no live job under or ``fixes``-linked to
   it; ``release_claims``, back to ``STATUS:triaged`` with a comment — unless
   its newest comment is ``resolved:``, then only the lease goes; gr542086);
   a task whose newest job ended
   (failed, or succeeded without closing it) with attempts at its retry
   cap is parked. The cap is an explicit ``meta.retries`` pin, else the
   node's lane-ladder length for a ``run_slice`` op — one rung per
   attempt, so the ladder is spent before the cap fires (td584381 S1a);
   a reviewer counts only the picked lane's rungs, a model-pinned op
   keeps :data:`DEFAULT_RETRIES`, and so does a ladder-less one. What
   parks it follows the failure: a failed job stamped ``no_commit``
   (a clean exit that wrote nothing — no rung teaches that) or an
   exhausted ``run_slice`` node enters rescue —
   :func:`precis.workers.factory_rescue.enter_rescue` tags it
   ``rescue:<cls>`` (cls ``review`` for a reviewer, else ``builder``),
   files or joins the signature gripe, and the rescue pass owns what
   follows — one diagnose-and-re-arm, then its own ask on a second
   exhaustion (``meta.rescue.n >= 1``), the end of the autonomous road;
   answering that ask re-arms the node and clears the rescue tag in the
   same step (``_step_rearm``). A failed job stamped
   ``human_only_hit=[paths]`` (``run_slice`` writes
   it when the branch touches ``lanes.HUMAN_ONLY_PATHS`` — no lane may
   write those paths) and a ``run_slice`` with no enabled fit lane still
   get the old ask, at once and naming the paths or the gap; a
   mechanical op (``meta.op`` other than ``run_slice``) asks too. The
   ask is one ask-user todo under the plan root (``meta.ask`` kind
   ``ruling``, tags ``ask-user`` + ``showstopper``) the task is
   ``blocked-by``. A land that died past its own gate-kill retries
   carries ``meta.land_again_cap`` and goes straight to that ask — the
   capped run is done, no auto re-land (gr570187). An answered re-land
   ask re-lands only when the answer names ``land again (no builder)``
   and the task's brief is untouched; a rewritten brief — or any other
   answer — mints a builder on the task's new brief.
   Attempts count the ended jobs since the newest ask on
   the task (or the ``attempt:<n>`` tag ``run_slice`` writes, before any ask),
   so answering the ask re-arms the task — except a job carrying
   ``meta.host_refusal``: the lane's host refused the launch, so nothing
   was tried (gr571395); a job carrying ``meta.attempt_free`` failed for
   a reason outside the slice's diff (``"lane-wait"``, gr572118;
   ``"infra-land"``, gr570187) and likewise burned no attempt; an
   operator-cancelled job is likewise free — its node re-queues on the
   next dispatch, but only when the job's type is on
   :data:`_REQUEUE_ON_CANCEL` (the default op plus confirmed
   read-only/idempotent ops). Any other cancelled type may carry side
   effects a kill did not roll back — a land killed mid-ship may
   already have merged, a ``pr_action`` may already have posted — so it
   keeps the "not redispatched" skip for the rescue pass (gr587772).
   A job still queued is never an
   attempt; one queued over 10 min whose ``ServiceSpec.placement``
   capabilities no live host advertises gets the note ``td<id>: job jo<N>
   queued <m> min — no host advertises {tokens}`` (td543917).

Plan roots: the ids named by ``plan:<id>`` tags on live todos, plus every
live todo whose meta makes it a gate / loop / generator / phase; a root
inside another root's subtree is folded into it. Closed roots are skipped.

**Kill switch.** ``PRECIS_FACTORY=off`` in the worker's env, a
``service_config`` row for ``plan_dispatch`` with ``prio = 0`` (any host), or
``params.dry_run`` makes the pass read-only: every step runs on the
in-memory model and counts what it would have done, nothing is written. The
job summary's first line is
``plan_dispatch: roots=N gates=o/c loops=minted/closed phases=closed
dispatched=N released=N asks=N dry_run=bool reopened=N reviews=N``; the
actions follow it.

**Arming** (an operator write, not done by any deploy) — the recurring todo
the minter turns into a job every 5 minutes, like ``graph_garden``::

    put(kind='todo', text='plan_dispatch tick (dark factory)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'plan_dispatch'})

``dedupe_pending`` coalesces a tick whose previous run is still pending. The
pass and the job may run side by side: :func:`run_pass` takes a global
advisory lock for the whole tick and a second tick skips instead of racing it.
Workers never import handlers: every write here is a store call.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.alerts import raise_alert
from precis.errors import BadInput
from precis.plans import gates as _gates
from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.plans import templates as _templates
from precis.plans.logbook import log_plan_event
from precis.plans.nodes import MOOT_TAG, node_kind
from precis.store._todo_sql import _DOABLE_EXCLUSION_TAGS
from precis.store.advisory import try_xact_advisory_lock
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta, release_claims
from precis.todo.views import _fetch_subtree
from precis.workers.job_types import JobTypeSpec, get_job_type

log = logging.getLogger(__name__)

NAME = "plan_dispatch"

#: Advisory-lock key serialising whole ticks (ascii "PLANTICK"; fits bigint).
#: Held as a transaction-scoped lock on a dedicated connection for the full
#: pass, so the job path and the worker-lane pass never mint or dispatch the
#: same thing twice.
TICK_LOCK_KEY: int = 0x504C414E5449434B
#: ``idle_in_transaction_session_timeout`` for the lock connection: longer than
#: any tick, so a tick that overruns loses the lock (the intended failure)
#: rather than pinning it forever.
TICK_LOCK_IDLE_TIMEOUT = "900s"
#: ``application_name`` of the lock connection in ``pg_stat_activity``.
TICK_LOCK_APP_NAME = "precis plan_tick lock"

#: ``service_config.service`` of the kill switch (a ``prio = 0`` row).
SERVICE = "plan_dispatch"

#: A ``claimed-by:`` tag older than this, with no live job, is released.
CLAIM_STALE = timedelta(hours=4)

#: A plan job queued longer than this with no host advertising its placement
#: capabilities gets a pass note (td543917).
QUEUED_NOTE_AFTER = timedelta(minutes=10)

#: Default ``meta.retries`` of a task.
DEFAULT_RETRIES = 2

#: The job type a task without ``meta.op`` (and every generator) runs.
DEFAULT_OP = "run_slice"

#: The todo's comment chunk kind (``handlers.todo._COMMENT_KIND``; restated —
#: workers never import handlers).
_COMMENT_KIND = "todo_comment"
#: The gripe comment chunk kind (``handlers.gripe._COMMENT_KIND``; restated).
_GRIPE_COMMENT_KIND = "gripe_comment"

_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_JOB_ENDED = frozenset({"succeeded", "failed"})
_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

#: The ONLY cancelled-job types the tick re-queues blind
#: (:func:`_step_dispatch`): the default op plus ops confirmed
#: read-only/idempotent — ``ci_verdict_poll`` is one GitHub GET plus a
#: verdict row keyed per sha. Every other type may carry side effects a
#: kill cannot be assumed to have rolled back (a land may already have
#: merged, a ``pr_action`` may already have posted), so it keeps the
#: "not redispatched" skip for the rescue tick; an unregistered type
#: defaults to the same safe side (gr587772). ``handlers.job``'s
#: ``mode='retry'`` ack reads this set.
_REQUEUE_ON_CANCEL = frozenset({DEFAULT_OP, "ci_verdict_poll"})

#: ``run_slice.ATTEMPT_FREE_KEY``, restated — the dependency stays
#: one-way (a test pins the two spellings equal). A job carrying the key
#: failed for a reason outside the slice's diff and burned no attempt:
#: ``"lane-wait"`` (the lane never freed a slot, gr572118), ``"infra-land"``
#: (the gate's own machinery killed the land, gr570187).
ATTEMPT_FREE_KEY = "attempt_free"

#: ``run_slice.HUMAN_ONLY_KEY``, restated — the dependency stays
#: one-way (a test pins the two spellings equal). The failed job lists
#: the ``lanes.HUMAN_ONLY_PATHS`` globs its branch touched; a stamped
#: failure is a human call — no lane may write those paths — so the
#: tick asks at once instead of climbing the ladder (td584381 S1a).
HUMAN_ONLY_KEY = "human_only_hit"

#: ``run_slice.NO_COMMIT_KEY``, restated — the same one-way rule. A
#: clean-exit failure that left no commit on the slice branch is the
#: LAST attempt: no rung can teach a session to write what it chose
#: not to, so the tick caps at once (td584381 S1a F4).
NO_COMMIT_KEY = "no_commit"

#: ``run_slice.MOOT_VERIFIED_KEY``, restated — the same one-way rule. A
#: code task's VERIFIED ``moot:`` close carries ``{"sha", "line"}`` on
#: the succeeded job; :func:`_step_moot` resolves the plan's gripe on
#: that sha exactly as a land does (td584381 S1d).
MOOT_VERIFIED_KEY = "moot_verified"

#: Consecutive ``_notify_gripe`` raises on a verified task moot —
#: stamped on the moot node's meta by :func:`_step_moot` (td584381 S1d
#: review round 2). At :data:`_MOOT_STAMP_ASK_AFTER` the tick files one
#: ask on the plan root and parks the stamp: a silent ``res.notes``
#: loop would page nobody. The park lifts when the ask is answered or a
#: human clears the key.
MOOT_STAMP_KEY = "moot_stamp_failures"

#: Failed gripe-stamp ticks before the moot close parks behind an ask.
_MOOT_STAMP_ASK_AFTER = 3

#: Options of the ask a parked moot stamp files (review round 2).
_MOOT_STAMP_ASK_OPTIONS: list[str] = [
    "retry the gripe stamp",
    "won't do",
]

#: ``run_slice.VALIDATED_NOREPRO_KEY`` / ``run_slice.NOREPRO_WAIT_TAG``,
#: restated — the same one-way rule. The stamp a fix node's succeeded
#: ``validated:``-no-commit job carries, and the open tag that parks it
#: for :func:`_step_ci_rerun` (gr588028): a ``waiting-for:`` prefix is a
#: doable exclusion, so dispatch leaves the node to this step.
VALIDATED_NOREPRO_KEY = "validated_norepro"
NOREPRO_WAIT_TAG = "waiting-for:ci-rerun"

#: How long a parked fix node may wait on the rerun and its verdict
#: before the tick files the stall ask (gr588028 item 4): still parked,
#: never a second mint.
RERUN_WAIT_CAP = timedelta(hours=2)

#: Options of the ask a stalled rerun path files under the plan root —
#: it only RECORDS the stall (the loop already continued or still waits
#: without it), so the one option is the acknowledgement.
_RERUN_ASK_OPTIONS: list[str] = [
    "acknowledged",
]

#: Consecutive infra-red lands (:func:`_infra_red_lands`) before the tick
#: parks the node behind a "land again" ask (gr570187): the re-land is
#: bounded so a broken gate environment cannot loop the lander forever.
LAND_AGAIN_CAP = 2

#: Dispatch prio of a node with no prio on it or any ancestor.
DEFAULT_PRIO = 5

#: A reviewer's verdict comment (module docstring, step 2a).
_VERDICT_RE = re.compile(
    r"review:\s*(LAND|FIX|REVERT)\b[\s:]*(?:[—–-]+\s*)?(.*)",
    re.IGNORECASE | re.DOTALL,
)
#: A verdict token for the hand-off fallback (gr573564, coordinator
#: ruling): only LAND or FIX — a REVERT is destructive and is never taken
#: from a token glued into narration; it needs the line-start comment.
_VERDICT_TOKEN_RE = re.compile(r"review:\s*(LAND|FIX)\b", re.IGNORECASE)
#: A placeholder template line (``review: FIX — <list>`` / ``REVERT —
#: <why>``) marks a grammar echo — it suppresses every LAND in the same
#: hand-off.
_VERDICT_TEMPLATE_RE = re.compile(r"review:\s*(?:FIX|REVERT)\b", re.IGNORECASE)
#: The ``[\s:]*`` + optional dashes lead ``_VERDICT_RE`` allows between
#: the verdict word and its rest — applied after a glued token.
_VERDICT_LEAD_RE = re.compile(r"[\s:]*(?:[—–-]+\s*)?")
#: The read-only reviewer's verdict-line marker (gr573555): stripped
#: where it ends a line (optional trailing ``.``, CRLF tolerated) before
#: matching — accepted, never required, and never the findings.
_READONLY_MARKER_RE = re.compile(
    re.escape(_templates.REVIEW_READONLY_MARKER) + r"\.?[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
#: A ``review:`` verdict token anywhere; :func:`_verdict_lines` keeps the
#: line-start ones — including REVERT, which is only ever a verdict line
#: (never a fallback token).
_VERDICT_LINE_RE = re.compile(r"review:\s*(LAND|FIX|REVERT)\b", re.IGNORECASE)
#: Sentence-final punctuation a glued token may directly follow.
_SENTENCE_END = frozenset(".!?")
#: Markdown line-start markers a fallback token may directly follow
#: (``- ``/``* `` bullets, ``**`` bold, ``#`` heading, ``> `` quote).
_MD_MARKERS = " \t-*#>"
_LANDED_RE = re.compile(r"landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)
_TD_FROM_RE = re.compile(r"\s*td(\d+)\s*")
_SHA_FROM_RE = re.compile(r"\s*sha:([0-9a-f]{7,40})\s*", re.IGNORECASE)

#: Doable exclusions the tick honours: the shared registry minus
#: ``child-failed:`` — plan retries are the tick's, not the bubble's —
#: plus ``rescue:``: a node in rescue belongs to the rescue pass, never
#: to a build (td584381 S1a).
_EXCLUSIONS: tuple[str, ...] = tuple(
    t for t in _DOABLE_EXCLUSION_TAGS if t != "child-failed:"
) + ("rescue:",)

#: Executor preference when a job type is compatible with several.
_EXECUTOR_ORDER: tuple[str, ...] = (
    "job_inproc",
    "coordinator",
    "claude_inproc",
    "ssh_node",
)

#: ``meta.ask.context`` cap (``todo.guards._ASK_CONTEXT_CAP``).
_ASK_CONTEXT_CAP = 600
_ASK_OPTIONS: list[str] = [
    "retry with a different lane/model",
    "rewrite the brief",
    "won't do",
]
#: Automatic ``release-fix`` iterations the tick runs unattended
#: (me573231): two red fixes, then the exhausted-loop ask — a third fix
#: never starts by itself. The stamped ``meta.loop.max`` may allow more;
#: the cap here governs what the tick does on its own.
_RELEASE_FIX_AUTO_CAP = 2
_LOOP_ASK_OPTIONS: list[str] = [
    "close this pass and let release_poll start a fresh one",
    "hand release with /go",
]
#: Action lines kept in the job summary.
_NOTES_CAP = 60

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Run the pass read-only: count and report, write nothing.
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark-factory plan tick: evaluate gates, advance loops, act on reviewer "
    "verdicts (LAND/FIX/REVERT), close finished phases, mint one job per "
    "doable plan task in (prio, id) order (run_slice by default), "
    "release stale claims, enter rescue once a lane-run task's retry "
    "ladder is spent, and file an ask-user for a human-only hit or a "
    "missing lane. Read-only while PRECIS_FACTORY=off or service_config "
    "plan_dispatch prio 0."
)


# ── Model ──────────────────────────────────────────────────────────


@dataclass
class _Node:
    id: int
    kind: str  # 'todo' | 'job'
    parent_id: int | None
    title: str
    meta: dict[str, Any]
    status: str
    created_at: datetime
    prio: int | None
    depth: int
    root: int
    #: open tag value -> when it was set (todos only).
    open_tags: dict[str, datetime] = field(default_factory=dict)
    children: list[int] = field(default_factory=list)


@dataclass
class _Blocker:
    id: int
    status: str
    is_ask: bool
    created_at: datetime


@dataclass
class _Plan:
    roots: list[int]
    nodes: dict[int, _Node]
    #: blocked todo id -> its ``blocked-by`` targets.
    blockers: dict[int, list[_Blocker]]

    def todos(self) -> list[_Node]:
        return sorted(
            (n for n in self.nodes.values() if n.kind == "todo"), key=lambda n: n.id
        )

    def child_todos(self, n: _Node) -> list[_Node]:
        return [self.nodes[c] for c in n.children if self.nodes[c].kind == "todo"]

    def jobs(self, n: _Node) -> list[_Node]:
        """Child jobs, oldest first."""
        return sorted(
            (self.nodes[c] for c in n.children if self.nodes[c].kind == "job"),
            key=lambda j: (j.created_at, j.id),
        )

    def live_job(self, n: _Node) -> bool:
        return any(j.status not in _JOB_TERMINAL for j in self.jobs(n))

    def status_of(self, b: _Blocker) -> str:
        node = self.nodes.get(b.id)
        return node.status if node is not None else b.status

    def ancestors(self, n: _Node) -> list[_Node]:
        out: list[_Node] = []
        seen = {n.id}
        cur = self.nodes.get(n.parent_id) if n.parent_id is not None else None
        while cur is not None and cur.id not in seen:
            out.append(cur)
            seen.add(cur.id)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
        return out

    def blocked(self, n: _Node) -> bool:
        """A live ``blocked-by`` target on ``n`` or any ancestor in the plan."""
        for node in (n, *self.ancestors(n)):
            for b in self.blockers.get(node.id, []):
                if self.status_of(b) not in BLOCKER_TERMINAL_STATUSES:
                    return True
        return False

    def paused(self, n: _Node) -> bool:
        return any(a.status == "paused" for a in (n, *self.ancestors(n)))

    def open_asks(self, n: _Node) -> list[_Blocker]:
        return [
            b
            for b in self.blockers.get(n.id, [])
            if b.is_ask and self.status_of(b) not in BLOCKER_TERMINAL_STATUSES
        ]


def _closed(n: _Node) -> bool:
    return n.status in TERMINAL_TODO_STATUSES


def _candidate_roots(conn: Any) -> list[int]:
    """Live, non-closed todos that root a plan (see the module docstring)."""
    ids: set[int] = set()
    for (value,) in conn.execute(
        """
        SELECT DISTINCT t.value
          FROM ref_tags rt JOIN tags t USING (tag_id)
          JOIN refs r ON r.ref_id = rt.ref_id
         WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
           AND r.kind = 'todo' AND r.retired_at IS NULL
        """
    ).fetchall():
        tail = str(value)[5:]
        if tail.isdigit():
            ids.add(int(tail))
    for ref_id, meta in conn.execute(
        """
        SELECT ref_id, meta FROM refs
         WHERE kind = 'todo' AND retired_at IS NULL
           AND meta ?| array['gate', 'loop', 'generate', 'auto_close']
        """
    ).fetchall():
        if node_kind(meta) not in (None, "task"):
            ids.add(int(ref_id))
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.ref_id = ANY(%s) AND r.kind = 'todo' AND r.retired_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) <> ALL(%s)
         ORDER BY r.ref_id
        """,
        (sorted(ids), sorted(TERMINAL_TODO_STATUSES)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _load(store: Any) -> _Plan:
    """The in-memory model of every plan subtree."""
    with store.pool.connection() as conn:
        candidates = _candidate_roots(conn)
    subtrees = {root: _fetch_subtree(store, root) for root in candidates}
    contained: set[int] = set()
    for root, rows in subtrees.items():
        contained.update(r["id"] for r in rows if r["id"] != root)
    roots = [r for r in candidates if r not in contained]
    placement: dict[int, tuple[int, int]] = {}  # id -> (root, depth)
    for root in roots:
        for row in subtrees[root]:
            placement.setdefault(row["id"], (root, int(row["depth"])))
    if not placement:
        return _Plan(roots=roots, nodes={}, blockers={})
    ids = sorted(placement)
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.parent_id, r.title, r.meta, r.created_at,
                   r.prio,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r
             WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
        tag_rows = conn.execute(
            """
            SELECT rt.ref_id, t.value, rt.created_at
              FROM ref_tags rt JOIN tags t USING (tag_id)
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
            """,
            (ids,),
        ).fetchall()
        link_rows = conn.execute(
            """
            SELECT l.src_ref_id, b.ref_id, b.created_at, b.meta ? 'ask',
                   EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                              AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')),
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = ANY(%s)
               AND b.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
    nodes: dict[int, _Node] = {}
    for ref_id, kind, parent_id, title, meta, created_at, prio, status in ref_rows:
        root, depth = placement[int(ref_id)]
        nodes[int(ref_id)] = _Node(
            id=int(ref_id),
            kind=str(kind),
            parent_id=int(parent_id) if parent_id is not None else None,
            title=str(title or ""),
            meta=dict(meta or {}),
            status=str(status or ("open" if kind == "todo" else "queued")),
            created_at=created_at,
            prio=int(prio) if prio is not None else None,
            depth=depth,
            root=root,
        )
    for ref_id, value, set_at in tag_rows:
        node = nodes.get(int(ref_id))
        if node is not None:
            node.open_tags[str(value)] = set_at
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node.id)
    blockers: dict[int, list[_Blocker]] = {}
    for src, dst, created_at, has_ask, ask_tag, status in link_rows:
        blockers.setdefault(int(src), []).append(
            _Blocker(
                id=int(dst),
                status=str(status or "open"),
                is_ask=bool(has_ask) or bool(ask_tag),
                created_at=created_at,
            )
        )
    return _Plan(roots=roots, nodes=nodes, blockers=blockers)


# ── Writes (no-ops when read-only) ─────────────────────────────────


@dataclass
class PassResult:
    """What one pass did (or, read-only, would have done)."""

    dry_run: bool
    reason: str | None = None
    roots: int = 0
    gates_opened: int = 0
    gates_closed: int = 0
    loops_minted: int = 0
    loops_closed: int = 0
    phases_closed: int = 0
    dispatched: int = 0
    released: int = 0
    asks: int = 0
    #: Ladder-exhausted ``run_slice`` nodes handed to the rescue path
    #: (td584381 S1a).
    rescued: int = 0
    reopened: int = 0
    #: Reviewer verdicts acted on (LAND / FIX / REVERT).
    reviews: int = 0
    #: Why the pass did nothing (another tick held the lock); ``None`` = ran.
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "roots": self.roots,
            "gates_opened": self.gates_opened,
            "gates_closed": self.gates_closed,
            "loops_minted": self.loops_minted,
            "loops_closed": self.loops_closed,
            "phases_closed": self.phases_closed,
            "dispatched": self.dispatched,
            "released": self.released,
            "asks": self.asks,
            "rescued": self.rescued,
            "dry_run": self.dry_run,
            "reopened": self.reopened,
            "reviews": self.reviews,
        }
        if self.skipped:
            out["skipped"] = self.skipped
        return out

    def summary(self) -> str:
        if self.skipped:
            return f"plan_dispatch: skipped — {self.skipped}"
        return (
            f"plan_dispatch: roots={self.roots} "
            f"gates={self.gates_opened}/{self.gates_closed} "
            f"loops={self.loops_minted}/{self.loops_closed} "
            f"phases={self.phases_closed} dispatched={self.dispatched} "
            f"released={self.released} asks={self.asks} dry_run={self.dry_run} "
            f"reopened={self.reopened} reviews={self.reviews}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.reason:
            lines.append(f"read-only ({self.reason}): the counts are what it would do")
        shown = self.notes[:_NOTES_CAP]
        lines += [f"- {n}" for n in shown]
        if len(self.notes) > len(shown):
            lines.append(f"- … {len(self.notes) - len(shown)} more")
        return "\n".join(lines)


class _Writer:
    """Store writes for one pass; every method is a no-op when ``dry_run``."""

    def __init__(self, store: Any, dry_run: bool) -> None:
        self.store = store
        self.dry_run = dry_run

    def set_status(self, n: _Node, value: str) -> None:
        n.status = value
        if self.dry_run:
            return
        self.store.add_tag(
            n.id, Tag.closed("STATUS", value), set_by="system", replace_prefix=True
        )
        if value == "done":
            # Picks-7d accounting, as the todo handler's tag path writes it.
            self.store.append_event(n.id, source=NAME, event="status:done")

    def stamp_meta(self, n: _Node, updates: dict[str, Any]) -> None:
        n.meta.update(updates)
        if not self.dry_run:
            self.store.stamp_ref_meta(n.id, updates)

    def remove_tags(self, n: _Node, values: list[str]) -> None:
        for value in values:
            n.open_tags.pop(value, None)
            if not self.dry_run:
                self.store.remove_tag(n.id, Tag.open(value))

    def comment(self, n: _Node, text: str) -> None:
        if self.dry_run:
            return
        existing = list(self.store.chunks.list_chunks_for_ref(n.id))
        ord_ = max((int(c.ord) for c in existing), default=0) + 1
        self.store.chunks.insert_chunks(
            n.id,
            [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": _COMMENT_KIND})],
        )


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── (1) gates ──────────────────────────────────────────────────────


def _step_gates(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "gate" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        if not w.dry_run and (n.meta.get("ask") or {}).get("kind") == "approval":
            # The standing approval: the tick may post the 'deploy r<N>'
            # answer itself — before the answer gate below evaluates, so
            # the same tick can open it.
            _answered, note = _release.auto_answer_approval(store, n.id, now=now)
            if note:
                res.notes.append(note)
        verdict = _gates.evaluate(store, n.meta["gate"], todo_id=n.id, now=now)
        prev = n.meta.get("gate_result") or {}
        if not isinstance(prev, dict) or (prev.get("open"), prev.get("why")) != (
            verdict.open,
            verdict.why,
        ):
            w.stamp_meta(
                n,
                {
                    "gate_result": {
                        "open": verdict.open,
                        "why": verdict.why,
                        "at": _iso(now),
                    }
                },
            )
        if verdict.open:
            w.set_status(n, "done")
            res.gates_opened += 1
            res.notes.append(f"gate td{n.id} open: {verdict.why}")
        else:
            res.gates_closed += 1


# ── (2) loops ──────────────────────────────────────────────────────


def _close_loop(w: _Writer, res: PassResult, n: _Node, why: str) -> None:
    w.comment(n, f"plan_dispatch: loop closed — {why}")
    w.set_status(n, "done")
    res.loops_closed += 1
    res.notes.append(f"loop td{n.id} closed: {why}")


def _step_loops(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "loop" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        if not all(_closed(c) for c in plan.child_todos(n)):
            continue
        spec = dict(n.meta.get("loop") or {})
        try:
            count = int(spec.get("count") or 0)
            cap = int(spec.get("max") or 0)
        except (TypeError, ValueError):
            _close_loop(w, res, n, f"meta.loop count/max unreadable: {spec!r}")
            continue
        body = spec.get("body")
        if body == "release-fix":
            # me573231: the automatic cap governs, not the stamped max.
            cap = min(cap, _RELEASE_FIX_AUTO_CAP)
        until = _gates.evaluate(store, spec.get("until") or {}, todo_id=n.id, now=now)
        if until.open:
            _close_loop(w, res, n, f"until is met ({until.why})")
            continue
        if count >= cap:
            if body == "release-fix" and not spec.get("exhausted_ask"):
                if w.dry_run:
                    res.notes.append(
                        f"loop td{n.id}: would file the exhausted release-fix ask"
                    )
                else:
                    ask_id = _mint_loop_ask(store, n, count, until.why)
                    w.stamp_meta(n, {"loop": {**spec, "exhausted_ask": ask_id}})
                    res.notes.append(
                        f"loop td{n.id}: {count} red release-fix iteration(s), "
                        f"asked in td{ask_id}"
                    )
                res.asks += 1
            _close_loop(w, res, n, f"max {cap} iteration(s) reached ({until.why})")
            continue
        if not isinstance(body, str) or body not in _templates.BODIES:
            _close_loop(
                w,
                res,
                n,
                f"loop body {body!r} has no minter in "
                f"precis.plans.templates.BODIES (known: {sorted(_templates.BODIES)}); "
                "register it, then reopen this loop",
            )
            continue
        if not w.dry_run:
            try:
                _templates.mint_body(store, n.id, body, iteration=count + 1)
            except BadInput as exc:
                _close_loop(w, res, n, f"loop body {body!r} refused: {exc.cause}")
                continue
        w.stamp_meta(n, {"loop": {**spec, "count": count + 1}})
        res.loops_minted += 1
        res.notes.append(f"loop td{n.id}: minted {body} iteration {count + 1}/{cap}")


# ── (2a) reviews ───────────────────────────────────────────────────


def _is_reviewer(n: _Node) -> bool:
    gen = n.meta.get("generate")
    return (
        node_kind(n.meta) == "generator"
        and isinstance(gen, dict)
        and gen.get("role") == "reviewer"
    )


def _review_from(n: _Node) -> int | None:
    """The ``td<id>`` a reviewer's ``meta.generate.from`` names."""
    m = _TD_FROM_RE.fullmatch(str((n.meta.get("generate") or {}).get("from") or ""))
    return int(m.group(1)) if m else None


def _review_sha(n: _Node) -> str | None:
    """The sha a sha-keyed reviewer's ``meta.generate.from`` names — a
    hand-landed engine commit with no linked todo (gr597625), ``None``
    for a todo-keyed reviewer."""
    m = _SHA_FROM_RE.fullmatch(str((n.meta.get("generate") or {}).get("from") or ""))
    return m.group(1).lower() if m else None


def _review_targets(plan: _Plan, n: _Node) -> list[_Node]:
    """The nodes whose land the reviewer judges: ``from`` itself, or its
    child todos (asks excluded) when it has them (a slicer's slices)."""
    target_id = _review_from(n)
    target = plan.nodes.get(target_id) if target_id is not None else None
    if target is None or target.kind != "todo":
        return []
    kids = [c for c in plan.child_todos(target) if "ask" not in c.meta]
    return kids or [target]


def _builder_vendors(
    plan: _Plan, n: _Node, lanes: tuple[_lanes.Lane, ...]
) -> tuple[str, ...]:
    """The vendors whose lanes ran a build job on the reviewed nodes, oldest
    job first (so ``[0]`` is the original builder's), each once.

    Every one of them is excluded from the review, not only the newest
    job's: td565332 (2026-11-05) read the newest job — sonnet's lint repair
    on top of codex's eight-file fix — as anthropic, and the "other vendor"
    pick sent the review to codex, which then reviewed its own build.

    Only jobs that left commits on the branch count (``run_slice.poll``
    stamps ``meta.branch_commits``): a claude-sub attempt that died with no
    commit before a codex build is not a builder, and counting it would make
    codex the "original builder"'s alternative and review its own work
    (BUILDER 2026-11-05). When no job carries the stamp (jobs before the
    field, or a reviewed node whose land came another way) every laned job
    counts."""
    laned: list[tuple[int, str, bool]] = []
    for target in _review_targets(plan, n):
        for j in plan.jobs(target):
            lane_name = j.meta.get("lane")
            if not lane_name:
                continue
            lane = next((ln for ln in lanes if ln.name == lane_name), None)
            if lane is None or not lane.vendor:
                continue
            laned.append((j.id, lane.vendor, bool(j.meta.get("branch_commits"))))
    builders = [row for row in laned if row[2]] or laned
    out: list[str] = []
    for _id, vendor, _committed in sorted(builders):
        if vendor not in out:
            out.append(vendor)
    return tuple(out)


def _comments(store: Any, todo_id: int) -> list[Any]:
    """The todo's comment chunks, newest first."""
    return sorted(
        (
            c
            for c in store.chunks.list_chunks_for_ref(todo_id)
            if c.chunk_kind == _COMMENT_KIND and c.text
        ),
        key=lambda c: (int(c.ord), int(c.id)),
        reverse=True,
    )


def _token_boundary(text: str, start: int) -> bool:
    """A fallback token counts only at a line start or directly after
    sentence-final punctuation (``…main.review: FIX — …``); a token
    mid-sentence (``I will post review: LAND if…``) is narration. Markdown
    line-start markers — ``- ``/``* `` bullets, ``**`` bold, ``#``
    headings, ``> `` quotes — may sit between the line start and the
    token."""
    i = start - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0 or text[i] == "\n" or text[i] in _SENTENCE_END:
        return True
    j = i
    while j >= 0 and text[j] in _MD_MARKERS:
        j -= 1
    return j < 0 or text[j] == "\n"


def _token_enclosed(text: str, start: int) -> bool:
    """Inside backticks or parens the token quotes the grammar (the
    brief's ``review: LAND | review: FIX — <list>`` echo), it is not a
    verdict."""
    head = text[:start]
    return bool(head.count("`") % 2) or head.count("(") > head.count(")")


def _verdict_template(body: str) -> bool:
    """``body`` holds a placeholder ``review: FIX|REVERT — <…>`` template
    line — the grammar echoed back, which suppresses every LAND in the
    same hand-off."""
    for m in _VERDICT_TEMPLATE_RE.finditer(body):
        lead = _VERDICT_LEAD_RE.match(body, m.end())
        if body[lead.end() if lead else m.end() :].strip().startswith("<"):
            return True
    return False


def _verdict_candidates(body: str) -> list[tuple[str, str]]:
    """The surviving fallback ``(verdict, rest)`` tokens in ``body``, in
    order — the strict set the coordinator ruled on (a wrong LAND hides a
    bug, a wrong REVERT is destructive)."""
    out: list[tuple[str, str]] = []
    suppress_land = _verdict_template(body)
    for m in _VERDICT_TOKEN_RE.finditer(body):
        if not _token_boundary(body, m.start()) or _token_enclosed(body, m.start()):
            continue
        lead = _VERDICT_LEAD_RE.match(body, m.end())
        rest = body[lead.end() if lead else m.end() :].strip()
        if rest.startswith("<"):  # a grammar placeholder, not findings
            continue
        if m.group(1).upper() == "LAND" and (
            suppress_land
            or rest.lower().startswith("(clean")  # the grammar's own gloss
            or rest.startswith("|")  # echo of the ``A | B`` grammar line
        ):
            continue
        out.append((m.group(1).upper(), rest))
    return out


def _verdict_lines(body: str) -> list[tuple[str, str]]:
    """The ``(verdict word, rest)`` pairs of ``body``'s verdict LINES — a
    ``review:`` token at a line start, markdown ``- ``/``* ``/``**``/``#``/
    ``> `` markers allowed before it (the line-start half of
    :func:`_token_boundary`), outside backticks/parens like a fallback
    token (:func:`_token_enclosed`): inside a multiline span the token
    quotes the grammar, and an enclosed REVERT must never reach the
    strictest pick (td591267). ``rest`` is that line's own text after the
    word and its lead, to end of line. A token glued mid-line after
    sentence-final punctuation is narration here, not a verdict line
    (gr574380)."""
    out: list[tuple[str, str]] = []
    for m in _VERDICT_LINE_RE.finditer(body):
        i = m.start() - 1
        while i >= 0 and body[i] in _MD_MARKERS:
            i -= 1
        if (i < 0 or body[i] == "\n") and not _token_enclosed(body, m.start()):
            end = body.find("\n", m.end())
            line = body[m.end() : end if end >= 0 else len(body)]
            lead = _VERDICT_LEAD_RE.match(line)
            out.append((m.group(1).upper(), line[lead.end() if lead else 0 :].strip()))
    return out


#: Verdict strictness, strictest first: disagreeing verdict lines in one
#: message resolve to the earliest of these present (coordinator ruling
#: 2026-11-08, gr574380).
_VERDICT_STRICTNESS = ("REVERT", "FIX", "LAND")


def parse_verdict(text: str) -> tuple[str, str] | None:
    """``(LAND|FIX|REVERT, rest)`` of one ``review:`` line, or ``None``
    when it is not a verdict. ``FIX`` and ``REVERT`` need a non-empty rest
    (the list / the why); a ``handoff:`` prefix is stripped first. Verdict
    lines that disagree resolve strictest-first (REVERT, then FIX, then
    LAND): the winner's own line is the rest, with a note naming the other
    verdict(s) present; a placeholder ``<…>`` line among them marks a
    grammar echo and is no verdict — a later ``review:`` line is never swallowed into
    the rest (gr574380); lines that agree keep it.
    When the text as a whole is no verdict line — a hand-off that glues
    the token to its narration (``… on landed main.review: FIX — …``,
    gr573564) — the fallback's surviving candidates rule: a FIX with
    findings wins over a LAND; REVERT never comes from a token; anything
    else is a miss."""
    body = text.strip()
    if body.lower().startswith("handoff:"):
        body = body[len("handoff:") :].strip()
    body = _READONLY_MARKER_RE.sub("", body)
    lines = _verdict_lines(body)
    words = {word for word, _ in lines}
    if len(words) > 1:
        if any(r.startswith("<") for _, r in lines):
            return None  # a grammar echo (``FIX — <list>``), not a verdict
        winner = next(w for w in _VERDICT_STRICTNESS if w in words)
        rest = next((r for w, r in lines if w == winner and r), "")
        if not rest:
            return None
        others = "/".join(w for w in _VERDICT_STRICTNESS if w in words and w != winner)
        return winner, f"{rest} (also a {others} line in the same message)"
    m = _VERDICT_RE.fullmatch(body)
    if m is not None:
        verdict, rest = m.group(1).upper(), m.group(2).strip()
        if verdict != "LAND" and not rest:
            return None
        return verdict, rest
    candidates = _verdict_candidates(body)
    fixes = [rest for verdict, rest in candidates if verdict == "FIX" and rest]
    if fixes:
        return "FIX", fixes[-1]
    if candidates and all(verdict == "LAND" for verdict, _ in candidates):
        return "LAND", candidates[-1][1]
    return None


def _is_review_line(text: str) -> bool:
    """A comment the verdict is read from: one starting ``review:``, or a
    ``handoff:`` whose own body starts ``review:`` or carries a surviving
    fallback token (gr573564). Any other comment is never scanned — the
    dispatcher's own retry note quotes the grammar."""
    body = text.strip().lower()
    if body.startswith("review:"):
        return True
    if not body.startswith("handoff:"):
        return False
    inner = body[len("handoff:") :].strip()
    return inner.startswith("review:") or bool(_verdict_candidates(inner))


def _read_verdict(
    store: Any, todo_id: int, since: datetime
) -> tuple[tuple[str, str] | None, str | None]:
    """``(verdict, raw line)`` from the comments posted at or after
    ``since``; ``(None, None)`` when there is none, ``(None, line)`` when
    it is malformed. A comment STARTING ``review:`` — the reviewer's own
    verdict — always wins over a token glued inside a ``handoff:``, newest
    first within each class."""
    comments: list[Any] = []
    for c in _comments(store, todo_id):
        if c.created_at < since:
            break
        comments.append(c)
    for c in comments:
        text = str(c.text)
        if text.strip().lower().startswith("review:"):
            return parse_verdict(text), text.strip()
    for c in comments:
        text = str(c.text)
        if _is_review_line(text):
            return parse_verdict(text), text.strip()
    return None, None


def _landed_sha(store: Any, todo_id: int) -> str | None:
    """The sha of the todo's newest ``landed: <sha>`` comment."""
    for c in _comments(store, todo_id):
        m = _LANDED_RE.match(str(c.text).strip())
        if m:
            return m.group(1).lower()
    return None


def _first_line(text: str, cap: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= cap else line[: cap - 1] + "…"


def _mint_task(
    store: Any, plan: _Plan, w: _Writer, root: int, title: str, body: str, now: datetime
) -> int:
    """Mint a ``run_slice`` task under the plan root (read-only: nothing) and
    add it to the in-memory plan, so the root phase does not close over it
    this pass. Returns its id (a negative stand-in when read-only)."""
    if w.dry_run:
        new_id = -1 - len([i for i in plan.nodes if i < 0])
    else:
        new_id = _templates.mint_plan_task(store, root, title, body)
    parent = plan.nodes.get(root)
    plan.nodes[new_id] = _Node(
        id=new_id,
        kind="todo",
        parent_id=root,
        title=title,
        meta={"op": DEFAULT_OP},
        status="open",
        created_at=now,
        prio=None,
        depth=(parent.depth + 1) if parent is not None else 1,
        root=root,
        open_tags={f"plan:{root}": now},
    )
    if parent is not None:
        parent.children.append(new_id)
    return new_id


def _review_fix(
    store: Any,
    plan: _Plan,
    w: _Writer,
    n: _Node,
    handle: str,
    findings: str,
    now: datetime,
) -> int:
    intent = (
        f"`git show {handle}` is the intent"
        if _review_sha(n) is not None
        else f"{handle}'s brief is the intent"
    )
    body = (
        f"{findings}\n\nFindings of the review td{n.id} of {handle}'s land on "
        f"main. Fix them on a fresh slice and land it; {intent}."
    )
    return _mint_task(
        store, plan, w, n.root, f"Fix review findings for {handle}", body, now
    )


def _review_revert(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    handle: str,
    why: str,
    now: datetime,
) -> int:
    """Mint the revert task and requeue each reviewed node behind it."""
    targets = _review_targets(plan, n)
    landed = [(t, _landed_sha(store, t.id)) for t in targets]
    shas = [sha for _t, sha in landed if sha]
    if not shas:
        # A sha-keyed reviewer names the commit to revert directly.
        from_sha = _review_sha(n)
        if from_sha is not None:
            shas = [from_sha]
    if len(shas) == 1:
        title = f"Revert {shas[0]} on main"
    elif shas:
        title = f"Revert {len(shas)} landed slices of {handle} on main"
    else:
        title = f"Revert the {handle} land on main"
    named = (
        ", ".join(f"`{s}`" for s in shas)
        if shas
        else f"the squash commit(s) naming {handle} (`git log --grep {handle} origin/main`)"
    )
    body = (
        f"Revert {named} on main: `git revert --no-edit <sha>` for each, newest "
        "first; resolve conflicts against the current main; commit with a "
        f"`revert(...)` subject and land it as a normal slice (scripts/ship).\n\n"
        f"Why (review td{n.id}): {why}"
    )
    revert_id = _mint_task(store, plan, w, n.root, title, body, now)
    requeue = [t for t, sha in landed if sha] or targets
    for t in requeue:
        claims = _claims(t)
        if claims:
            w.remove_tags(t, claims)
        # The floor is the newest ENDED job (td584381 S1d, the gr586688
        # rule): a still-queued job — a re-minted land that never ran —
        # is no re-arm boundary, and the ended land must be floored or
        # the phases backstop would close the todo under it.
        ended = [j for j in plan.jobs(t) if j.status in _JOB_ENDED]
        if ended:
            w.stamp_meta(t, {"rearmed_after_job": ended[-1].id})
        w.comment(
            t,
            f"handoff: review: REVERT — {why}\n\n(review td{n.id}; reverted by "
            f"td{revert_id} — rebuild after it lands, from the brief, with this "
            "finding in mind)",
        )
        if t.status != "open":
            w.set_status(t, "open")
        plan.blockers.setdefault(t.id, []).append(
            _Blocker(id=revert_id, status="open", is_ask=False, created_at=now)
        )
        if not w.dry_run:
            store.add_link(src_ref_id=t.id, dst_ref_id=revert_id, relation="blocked-by")
    if not requeue and _review_sha(n) is None:
        # A reviewer whose `from` target is not in this plan has no
        # landed todo to hold it through the revert: the reviewer
        # itself waits on the task — else it re-dispatches next tick
        # and re-reviews the same land in a loop (gr597625). A
        # sha-keyed reviewer closes at the verdict instead
        # (_step_reviews) — its land has no rebuild to re-read.
        if not w.dry_run:
            store.add_link(src_ref_id=n.id, dst_ref_id=revert_id, relation="blocked-by")
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=revert_id, status="open", is_ask=False, created_at=now)
        )
    res.notes.append(
        f"td{n.id}: requeued {', '.join(f'td{t.id}' for t in requeue) or 'nothing'} "
        f"behind the revert"
    )
    _reset_gates(plan, w, res, n, shas)
    return revert_id


def _reset_gates(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, shas: list[str]
) -> None:
    """Put every ``sha-in-prod`` gate of ``n``'s plan whose sha is one of the
    reverted ``shas`` back to :data:`~precis.plans.templates.PENDING_SHA`, so
    it does not open (or stay open) on a sha that is no longer the fix; a
    gate already ``done`` is reopened. The rebuild's land fills it again."""
    for g in plan.todos():
        spec = g.meta.get("gate")
        if (
            g.root != n.root
            or node_kind(g.meta) != "gate"
            or not isinstance(spec, dict)
        ):
            continue
        args = spec.get("args")
        if spec.get("kind") != "sha-in-prod" or not isinstance(args, dict):
            continue
        sha = str(args.get("sha") or "").strip().lower()
        if not sha or sha == _templates.PENDING_SHA:
            continue
        if not any(s.startswith(sha) or sha.startswith(s) for s in shas):
            continue
        if _closed(g) and g.status != "done":
            continue
        w.stamp_meta(
            g, {"gate": {**spec, "args": {**args, "sha": _templates.PENDING_SHA}}}
        )
        w.comment(g, "sha reset after review REVERT")
        if g.status == "done":
            w.set_status(g, "open")
        res.notes.append(f"gate td{g.id}: sha {sha[:9]} reset after review REVERT")


def _step_reviews(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
) -> None:
    for n in plan.todos():
        if not _is_reviewer(n) or _closed(n) or plan.paused(n):
            continue
        if plan.child_todos(n) or plan.live_job(n):
            continue
        jobs = plan.jobs(n)
        if not jobs or jobs[-1].status != "succeeded":
            continue
        newest = jobs[-1]
        seen = n.meta.get("review")
        if isinstance(seen, dict) and seen.get("job") == newest.id:
            continue
        verdict, raw = _read_verdict(store, n.id, newest.created_at)
        claims = _claims(n)
        if claims:
            w.remove_tags(n, claims)
        # The reviewer is read-only: every verdict is a diff read, never
        # test evidence — the dispatcher stamps that fact itself.
        record: dict[str, Any] = {
            "job": newest.id,
            "at": _iso(now),
            "tests_run": False,
        }
        target_id = _review_from(n)
        handle = _review_sha(n) or (f"td{target_id if target_id is not None else n.id}")
        if verdict is None:
            what = (
                f"a malformed verdict {_first_line(raw)!r}"
                if raw
                else "no `review:` comment"
            )
            job_type_, params_ = _resolve_op(n)
            cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type_, params_))
            w.stamp_meta(n, {"review": {**record, "verdict": None}})
            w.comment(
                n,
                f"plan_dispatch: job:{newest.id} succeeded with {what}; want "
                f"{_templates.REVIEW_GRAMMAR}. Retrying (retry cap {cap}), "
                "then rescue.",
            )
            if n.status != "open":
                w.set_status(n, "open")
            res.notes.append(f"td{n.id}: review job:{newest.id} left {what}")
            continue
        kind, rest = verdict
        record["verdict"] = kind
        marker = _templates.REVIEW_READONLY_MARKER
        if kind == "LAND":
            w.set_status(n, "done")
            res.notes.append(f"td{n.id}: review LAND for {handle} {marker}; closed")
        elif kind == "FIX":
            fix_id = _review_fix(store, plan, w, n, handle, rest, now)
            record["task"] = fix_id
            w.set_status(n, "done")
            res.notes.append(
                f"td{n.id}: review FIX for {handle} {marker}; minted td{fix_id}, closed"
            )
        else:
            revert_id = _review_revert(store, plan, w, res, n, handle, rest, now)
            record["task"] = revert_id
            if _review_sha(n) is not None:
                # A sha-keyed land has no rebuild to re-read: the
                # reviewer closes like a FIX — the gate tracks the
                # revert task it minted (gr597625). Kept open it would
                # re-dispatch once the revert lands, re-review the same
                # static commit, REVERT again and mint the same revert
                # in a loop (a second `git revert` re-applies it).
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: review REVERT for {handle} {marker}; "
                    f"minted td{revert_id}, closed"
                )
            else:
                w.stamp_meta(n, {"rearmed_after_job": newest.id})
                if n.status != "open":
                    w.set_status(n, "open")
                res.notes.append(
                    f"td{n.id}: review REVERT for {handle} {marker}; "
                    f"minted td{revert_id}, "
                    "reviewer stays open for the rebuild"
                )
        w.stamp_meta(n, {"review": record})
        res.reviews += 1


# ── (3) phases ─────────────────────────────────────────────────────


def _op_type(n: _Node) -> str:
    op = n.meta.get("op")
    if isinstance(op, dict):
        return str(op.get("type") or DEFAULT_OP)
    if isinstance(op, str) and op.strip():
        return op.strip()
    return DEFAULT_OP


def _step_reopen(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """A generator tagged ``STATUS:done`` (its session's own close) while a
    child todo is open goes back to ``STATUS:open``: it closes as a phase
    once every child is closed, and until then must keep blocking whatever is
    ``blocked-by`` it."""
    for n in plan.todos():
        if node_kind(n.meta) != "generator" or n.status != "done":
            continue
        open_kids = [c for c in plan.child_todos(n) if not _closed(c)]
        if not open_kids:
            continue
        w.set_status(n, "open")
        res.reopened += 1
        res.notes.append(
            f"generator td{n.id} reopened: {len(open_kids)} child(ren) still open"
        )


def _step_rearm(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """An answered retry-cap ask re-arms its node with a fresh budget whatever
    ran between the ask and its answer (gr561075). :func:`_attempts` counts
    the ended jobs after the newest ask; five hand-run lands between a cap
    ask and its ``retry`` once kept the node capped and minted a second ask
    the moment the first closed. The first tick that sees the newest ask
    terminal stamps ``meta.rearmed_after_job`` with the newest ended job
    (the floor the review-REVERT path already uses) and records which ask
    did it (``meta.rearmed_by_ask``), so the stamp happens once per ask —
    except when that newest ended job is a succeeded ``land_slice``: the
    slice already landed, there is nothing to retry, and flooring the
    todo on its own land would hold it open for a duplicate build of
    landed work — ``_step_phases``' backstop closes it this pass instead
    (gr586688 review)."""
    for n in plan.todos():
        if _closed(n) or not _dispatchable(plan, n):
            continue
        asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
        if not asks:
            continue
        newest_ask = max(asks, key=lambda b: (b.created_at, b.id))
        if plan.status_of(newest_ask) not in BLOCKER_TERMINAL_STATUSES:
            continue
        if n.meta.get("rearmed_by_ask") == newest_ask.id:
            continue
        ended = [j for j in plan.jobs(n) if j.status in _JOB_ENDED]
        # A succeeded ``land_slice`` as the newest ended job means the
        # slice already landed — nothing to retry: no floor, and the
        # phases backstop closes the todo this pass. Everything else the
        # re-arm writes (``rearmed_by_ask`` here, the answer record and
        # the ``rescue:`` clear below) still applies — safe: with no
        # floor the todo is done before dispatch would ever see it.
        landed = bool(
            ended
            and ended[-1].status == "succeeded"
            and ended[-1].meta.get("job_type") == "land_slice"
        )
        stamp: dict[str, Any] = {"rearmed_by_ask": newest_ask.id}
        if ended and not landed:
            stamp["rearmed_after_job"] = ended[-1].id
        # A re-land ask's answer decides what the next dispatch may be
        # (gr570187): "land again (no builder)" over an untouched brief
        # re-lands the old branch; a rewritten brief — or any other
        # answer — mints a builder on the task's brief. A non-reland ask
        # clears the record so a fresh infra-land run re-lands freely.
        ask_node = plan.nodes.get(newest_ask.id)
        opts = (
            (ask_node.meta.get("ask") or {}).get("options")
            if ask_node is not None
            else None
        )
        if isinstance(opts, list) and _RELAND_ASK_OPTIONS[0] in opts:
            stamp["rearmed_answer"] = (
                _release._newest_answer(w.store, newest_ask.id) or ""
            )
            stamp["rearmed_ask_at"] = newest_ask.created_at.isoformat()
        else:
            stamp["rearmed_answer"] = None
            stamp["rearmed_ask_at"] = None
        # A node whose rescue ended in the rescue pass's own ask: the
        # human's answer IS the clearing (td584381 S1a F1) — the
        # ``rescue:<cls>`` tag would otherwise keep it excluded from
        # dispatch and hygiene forever. ``meta.rescue.n`` stays: a
        # second exhaustion re-enters rescue and asks the human again.
        # ``rescue_answered`` records the episode for the rescue pass
        # (gr598110): a re-tag of the answered class with no newer job
        # re-arms instead of re-asking.
        cleared = [v for v in n.open_tags if v.startswith("rescue:")]
        if cleared:
            stamp["rescue_answered"] = {
                "cls": cleared[0][len("rescue:") :],
                "ask": newest_ask.id,
                "job": ended[-1].id if ended else None,
            }
        w.stamp_meta(n, stamp)
        if cleared:
            w.remove_tags(n, cleared)
            if n.status == "doing":
                w.set_status(n, "open")
        # gr598110: the re-arm must also drop the dead ``claimed-by:``
        # lease — ``_step_dispatch`` skips every claimed node, so a
        # re-armed node whose newest job had ended sat claimed until
        # hygiene's CLAIM_STALE wait, and the stall watchdog re-tagged it
        # (td598406 asked the same question again on the re-tag).
        released: list[str] = []
        if not plan.live_job(n):
            released = _claims(n)
            if released:
                w.remove_tags(n, released)
                if n.status == "doing":
                    w.set_status(n, "open")
        res.notes.append(
            f"td{n.id}: ask td{newest_ask.id} answered; retry budget reset"
            + (f" after job:{ended[-1].id}" if ended and not landed else "")
            + (
                f"; newest job:{ended[-1].id} already landed — no floor"
                if landed
                else ""
            )
            + ("; rescue tag cleared" if cleared else "")
            + (f"; released {', '.join(released)}" if released else "")
        )


def _moot_descendants(plan: _Plan, n: _Node) -> list[_Node]:
    """The open todos under ``n.root`` ``blocked-by``-dependent on ``n``,
    transitively — the review / wait-for-prod / validate chain a task
    moot closes (td584381 S1d review). A closed link in the chain still
    transmits: the closure walks through it, it is just not re-closed."""
    dependents: dict[int, set[int]] = {}
    for src, targets in plan.blockers.items():
        for b in targets:
            dependents.setdefault(b.id, set()).add(src)
    seen = {n.id}
    frontier = [n.id]
    out: list[_Node] = []
    while frontier:
        cur = frontier.pop()
        for dep_id in dependents.get(cur, ()):
            if dep_id in seen:
                continue
            seen.add(dep_id)
            m = plan.nodes.get(dep_id)
            if m is None or m.kind != "todo" or m.root != n.root:
                continue
            frontier.append(dep_id)
            if not _closed(m):
                out.append(m)
    return sorted(out, key=lambda m: m.id)


def _is_ask_node(m: _Node) -> bool:
    """An ask todo (``meta.ask`` or the ``ask-user`` tag): a question to a
    human, not build work the moot close waits on."""
    return isinstance(m.meta.get("ask"), dict) or any(
        t.startswith("ask-user") for t in m.open_tags
    )


def _informational_ask(m: _Node) -> bool:
    """An ask with ``meta.ask.blocks == "nothing"``: a record for the
    human, not a gate — it never holds a phase or generator open
    (gr589823). An ask that names a todo handle in ``blocks`` still does."""
    ask = m.meta.get("ask")
    return isinstance(ask, dict) and ask.get("blocks") == "nothing"


def _step_moot(store: Any, plan: _Plan, w: _Writer, res: PassResult) -> None:
    """A node closed on a ``moot:`` verdict carries ``MOOT_TAG``
    (gr548813): the plan's reason is gone (its gripe already resolved,
    its section deleted). Since td584381 S1d a CODE task can carry it
    too — ``run_slice`` only tags one whose ``moot:`` claim VERIFIED on
    the worker host and stamps the job's ``meta.moot_verified =
    {sha, line}``; a generator's first-line ``moot:`` still rides on
    trust (no record).

    A GENERATOR moot keeps the whole-plan close: every open node of the
    plan and its root go won't-do. A TASK moot is scoped (review round
    1): only the moot node's ``blocked-by`` descendants — review,
    wait-for-prod, validate — close won't-do, then the root, and only
    when no other open work node under the root remains (a second open
    build refuses with ``plan td<root> not closed: td<N> still open``);
    a node with a live job is never closed (``td<N> has a live job;
    moot close deferred`` — the tag stays and the next tick retries).
    The gripe stamp happens only once the root closed, and BEFORE the
    tag drops: ``_notify_gripe`` is idempotent, so a raise leaves the
    tag for the next tick instead of losing the resolution. The retry
    is bounded (review round 2): ``meta.moot_stamp_failures`` counts
    consecutive raises — at ``_MOOT_STAMP_ASK_AFTER`` one ask files on
    the plan root (:func:`_mint_stamp_ask`) and the stamp parks until
    the ask is answered or the key is cleared. A verified sha resolves
    the plan's gripe exactly as a land does (the same
    ``resolved: <sha>`` + ``STATUS:verify`` ``land_slice`` writes);
    one alert on the root tells a human the plan went away."""
    from precis.workers.job_types import land_slice as _land_slice

    for n in plan.todos():
        if MOOT_TAG not in n.open_tags:
            continue
        why = f"td{n.id} reported `moot:` (its handoff comment has the why)"
        jobs = plan.jobs(n)
        mv = jobs[-1].meta.get(MOOT_VERIFIED_KEY) if jobs else None
        sha: str | None = None
        mv_line: Any = None
        if isinstance(mv, dict) and mv.get("sha"):
            sha, mv_line = str(mv["sha"]), mv.get("line")
        gid = _land_slice._plan_gripe(store, f"plan:{n.root}")
        stamped = False
        if node_kind(n.meta) == "generator":
            # Whole-plan close (gr548813, unchanged): every open node of
            # the plan and its root go won't-do.
            closed = 0
            for m in plan.todos():
                if m.root != n.root or m.id == n.id or _closed(m):
                    continue
                w.comment(m, f"plan_dispatch: plan closed — {why}")
                w.set_status(m, "won't-do")
                closed += 1
            res.phases_closed += closed
            res.notes.append(
                f"plan td{n.root} closed as won't-do: {why}; {closed} node(s)"
            )
        else:
            # A task moot is scoped to its own chain (review round 1):
            # its ``blocked-by`` descendants close won't-do — a node
            # with a live job defers to the next tick — then the root
            # once no other open work node under it remains.
            closed = 0
            for m in _moot_descendants(plan, n):
                if plan.live_job(m):
                    res.notes.append(f"td{m.id} has a live job; moot close deferred")
                    continue
                w.comment(m, f"plan_dispatch: plan closed — {why}")
                w.set_status(m, "won't-do")
                closed += 1
            res.phases_closed += closed
            open_work = [
                m
                for m in plan.todos()
                if m.root == n.root
                and m.id not in (n.id, n.root)
                and not _closed(m)
                and not _is_ask_node(m)
            ]
            if open_work:
                res.notes.append(
                    f"plan td{n.root} not closed: td{open_work[0].id} still open"
                )
                continue
            root = plan.nodes.get(n.root)
            if root is not None and not _closed(root) and plan.live_job(root):
                res.notes.append(f"td{root.id} has a live job; moot close deferred")
                continue
            if w.dry_run:
                if sha is not None and gid is not None:
                    res.notes.append(
                        f"would resolve gripe gr{gid} on verified moot sha {sha[:9]}"
                    )
                continue
            if sha is not None and gid is not None:
                # The verified moot IS the fix's land — resolve the
                # plan's gripe on its sha exactly as ``land_slice``
                # would (td584381 S1d). Stamped BEFORE the root closes
                # (review round 1): a raise leaves the root open — the
                # plan stays a candidate — and ``MOOT_TAG`` for the next
                # tick's retry instead of losing the resolution. The
                # retry is bounded (review round 2): consecutive raises
                # count on ``meta.moot_stamp_failures``; at
                # ``_MOOT_STAMP_ASK_AFTER`` one ask files on the root and
                # the stamp parks — a silent res.notes loop would page
                # nobody. The park lifts when that ask is answered or a
                # human clears the meta key.
                fails_raw = n.meta.get(MOOT_STAMP_KEY)
                try:
                    fails = int(fails_raw or 0)
                except (TypeError, ValueError):
                    fails = 0
                if fails >= _MOOT_STAMP_ASK_AFTER:
                    root_node = plan.nodes.get(n.root)
                    if root_node is not None and plan.open_asks(root_node):
                        res.notes.append(
                            f"td{n.id}: gripe stamp parked after {fails} "
                            f"failed ticks — asked on td{n.root}"
                        )
                        continue
                    # The parked ask was answered (or never filed): the
                    # human's ruling re-arms the stamp retry.
                    w.stamp_meta(n, {MOOT_STAMP_KEY: 0})
                    fails = 0
                try:
                    res.notes.append(
                        _land_slice._notify_gripe(
                            store, gid, sha, str(mv_line or n.title)[:200], n.id
                        )
                    )
                    stamped = True
                    if fails:
                        w.stamp_meta(n, {MOOT_STAMP_KEY: 0})
                except Exception as exc:
                    fails += 1
                    w.stamp_meta(n, {MOOT_STAMP_KEY: fails})
                    res.notes.append(
                        f"td{n.id}: gripe stamp failed ({exc}); moot close deferred"
                    )
                    root_node = plan.nodes.get(n.root)
                    if (
                        fails >= _MOOT_STAMP_ASK_AFTER
                        and root_node is not None
                        and not plan.open_asks(root_node)
                    ):
                        ask_id = _mint_stamp_ask(
                            w.store, n, gid, sha, type(exc).__name__
                        )
                        res.asks += 1
                        res.notes.append(
                            f"td{n.id}: gripe stamp raised {fails} ticks "
                            f"running — asked in td{ask_id}"
                        )
                    continue
            if root is not None and not _closed(root):
                w.comment(root, f"plan_dispatch: plan closed — {why}")
                w.set_status(root, "won't-do")
                closed += 1
                res.phases_closed += 1
            res.notes.append(
                f"plan td{n.root} closed as won't-do: {why}; {closed} node(s)"
            )
        if w.dry_run:
            if sha is not None and gid is not None:
                res.notes.append(
                    f"would resolve gripe gr{gid} on verified moot sha {sha[:9]}"
                )
            continue
        if not stamped and sha is not None and gid is not None:
            # Generator path: the close ran above, so a raise here is
            # not retried — the plan is gone from the candidates. Kept
            # for parity; the task path stamps before its root close.
            try:
                res.notes.append(
                    _land_slice._notify_gripe(
                        store, gid, sha, str(mv_line or n.title)[:200], n.id
                    )
                )
            except Exception as exc:
                res.notes.append(
                    f"td{n.id}: gripe stamp failed ({exc}); moot close deferred"
                )
                continue
        w.remove_tags(n, [MOOT_TAG])
        root = plan.nodes.get(n.root)
        raise_alert(
            store,
            source=NAME,
            fingerprint=f"plan-moot:{n.root}",
            title=f"plan td{n.root} closed as moot: {root.title if root else ''}".strip(),
            detail=f"{why}; {closed} open node(s) set to won't-do.",
            severity="info",
            subject_ref_id=n.root,
        )


def _analysis_posted(plan: _Plan, store: Any, n: _Node) -> bool:
    """An ``analyst`` generator (td543749) whose newest job succeeded and
    left an ``analysis:`` comment (bare or as the ``handoff:``) since it
    was minted: its work is done."""
    gen = n.meta.get("generate")
    if not isinstance(gen, dict) or gen.get("role") != "analyst":
        return False
    jobs = plan.jobs(n)
    if not jobs or jobs[-1].status != "succeeded":
        return False
    for c in _comments(store, n.id):
        if c.created_at < jobs[-1].created_at:
            break
        body = str(c.text).strip().lower()
        if body.startswith("handoff:"):
            body = body[len("handoff:") :].strip()
        if body.startswith(_templates.ANALYSIS_PREFIX):
            return True
    return False


def _step_phases(plan: _Plan, w: _Writer, res: PassResult) -> None:
    # A root whose plan still carries an open ``MOOT_TAG`` is never
    # auto-closed here: ``_step_moot`` keeps the tag (and the root open)
    # when the gripe stamp fails so the next tick can retry — a
    # ``done`` root would drop the whole plan out of the candidates and
    # the retained tag could never fire (td584381 review round 1).
    mooted_roots = {n.root for n in plan.todos() if MOOT_TAG in n.open_tags}
    for n in sorted(plan.todos(), key=lambda x: (-x.depth, x.id)):
        if _closed(n) or n.status == "paused":
            continue
        kind = node_kind(n.meta)
        kids = plan.child_todos(n)
        if n.id == n.root and n.root in mooted_roots:
            continue
        if kind in ("phase", "generator"):
            if kind == "generator" and plan.live_job(n):
                continue
            # An informational ask (``blocks: "nothing"``) is a record, not
            # a gate: it stays open for the human and does not hold the
            # phase (gr589823).
            if kids and all(_closed(c) or _informational_ask(c) for c in kids):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"{kind} td{n.id} closed: every child is closed")
            elif not kids and _analysis_posted(plan, w.store, n):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"analyst td{n.id} closed: analysis: comment posted")
        elif kind == "task" and not kids:
            # A run_slice task closes through its land: the newest job is
            # then a succeeded land_slice (its ship closes the todo first;
            # this is the backstop). A succeeded run_slice alone never does.
            jobs = plan.jobs(n)
            if _op_type(n) == DEFAULT_OP and not (
                jobs and jobs[-1].meta.get("job_type") == "land_slice"
            ):
                continue
            # ``meta.rearmed_after_job`` — the floor ``_attempts`` honours,
            # stamped by a review REVERT or an answered ask — supersedes
            # the jobs at or below it: a newest job still under the floor
            # is the OLD land, and the todo stays open for its rebuild
            # (gr586688).
            floor = _rearm_floor(n)
            if jobs and jobs[-1].status == "succeeded":
                job_type = jobs[-1].meta.get("job_type") or _op_type(n)
                if floor is not None and jobs[-1].id <= floor:
                    res.notes.append(
                        f"task td{n.id} held open: newest {job_type} "
                        f"job:{jobs[-1].id} is at or below re-arm floor {floor}"
                    )
                    continue
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(
                    f"task td{n.id} closed: {job_type} job:{jobs[-1].id} succeeded"
                )


# ── (4) dispatch ───────────────────────────────────────────────────


def _dispatchable(plan: _Plan, n: _Node) -> bool:
    """Whether ``n`` is a node the tick runs jobs for (by shape only)."""
    if "ask" in n.meta:
        return False
    kind = node_kind(n.meta)
    kids = plan.child_todos(n)
    if kind == "task" or kind == "generator":
        return not kids
    return kind is None and not kids and n.id not in plan.roots


def _excluded(n: _Node) -> bool:
    for value in n.open_tags:
        for t in _EXCLUSIONS:
            if (t.endswith(":") and value.startswith(t)) or value == t:
                return True
    return False


def _claims(n: _Node) -> list[str]:
    return [v for v in n.open_tags if v.startswith("claimed-by:")]


def _attempt_tag(n: _Node) -> int:
    best = 0
    for value in n.open_tags:
        m = _ATTEMPT_RE.match(value)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _retries(n: _Node) -> int:
    raw = n.meta.get("retries", DEFAULT_RETRIES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES


def _no_attempt(j: _Node) -> bool:
    """An ended job that burned no attempt: the lane's host refused the
    launch (``meta.host_refusal``, gr571395) or it failed outside the
    diff's control (``meta.attempt_free``: a lane that never freed a slot,
    gr572118; a land the gate infrastructure killed, gr570187)."""
    return bool(j.meta.get("host_refusal") or j.meta.get(ATTEMPT_FREE_KEY))


def _attempt_jobs(plan: _Plan, n: _Node) -> list[_Node]:
    """The ended jobs counting as attempts on ``n``, oldest first —
    what :func:`_attempts` counts."""
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    since = max((b.created_at for b in asks), default=None)
    floor = _rearm_floor(n)
    jobs = plan.jobs(n)
    return [
        j
        for i, j in enumerate(jobs)
        # Ended only: a job still queued — never claimed, e.g. waiting for
        # a host with its placement capabilities (td543917) — is no attempt.
        if j.status in _JOB_ENDED
        and not _no_attempt(j)
        and (since is None or j.created_at > since)
        and (floor is None or j.id > floor)
        # A build that handed off to the lander is one attempt with its
        # land_slice, not two.
        and not (
            j.status == "succeeded"
            and j.meta.get("job_type") == DEFAULT_OP
            and any(k.meta.get("job_type") == "land_slice" for k in jobs[i + 1 :])
        )
    ]


def _attempts(plan: _Plan, n: _Node) -> int:
    """Ended jobs since the newest ask on ``n`` (all of them, floored by the
    ``attempt:<n>`` tag, when there was never an ask). A REVERT verdict and
    an answered ask (:func:`_step_rearm`) re-arm a node: only jobs after
    ``meta.rearmed_after_job`` count. A job carrying ``meta.host_refusal``
    or ``meta.attempt_free`` never counts: nothing on the diff was tried
    (:func:`_no_attempt`)."""
    ended = _attempt_jobs(plan, n)
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    if not asks and _rearm_floor(n) is None:
        return max(len(ended), _attempt_tag(n))
    return len(ended)


def _human_hit(plan: _Plan, n: _Node) -> list[str]:
    """The ``lanes.HUMAN_ONLY_PATHS`` paths the newest attempt's branch
    touched, or ``[]`` — the job-meta stamp :data:`HUMAN_ONLY_KEY`
    ``run_slice`` writes (td584381 S1a). Only the newest attempt job's
    stamp counts: a stamped job below the re-arm floor or the newest ask
    was already answered for."""
    ended = _attempt_jobs(plan, n)
    if not ended or ended[-1].status != "failed":
        return []
    hits = ended[-1].meta.get(HUMAN_ONLY_KEY)
    if isinstance(hits, list):
        return sorted(str(p) for p in hits)
    return [str(hits)] if hits else []


def _no_commit(plan: _Plan, n: _Node) -> bool:
    """The newest attempt ended ``failed`` carrying ``run_slice``'s
    :data:`NO_COMMIT_KEY` stamp — a clean exit that left no commit on
    ``slice/<id>`` (td584381 S1a F4). One such attempt exhausts the
    cap: no rung fixes a session that wrote nothing."""
    ended = _attempt_jobs(plan, n)
    return bool(
        ended and ended[-1].status == "failed" and ended[-1].meta.get(NO_COMMIT_KEY)
    )


def _rearm_floor(n: _Node) -> int | None:
    raw = n.meta.get("rearmed_after_job")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _infra_red_lands(plan: _Plan, n: _Node) -> int:
    """The run of consecutive failed ``land_slice`` jobs marked
    ``attempt_free == "infra-land"`` at the tail of ``n``'s jobs, counting
    only jobs after ``meta.rearmed_after_job`` (an answered ask re-arms,
    gr570187). A land red on the diff itself — or any other job — ends the
    run: the node goes back through a build, not another land."""
    floor = _rearm_floor(n)
    count = 0
    for j in reversed(plan.jobs(n)):
        if floor is not None and j.id <= floor:
            break
        if not (
            j.status == "failed"
            and j.meta.get("job_type") == "land_slice"
            and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        ):
            break
        count += 1
    return count


def _brief_rewritten(store: Any, todo_id: int, since: datetime) -> bool:
    """A ``brief:``/``handoff:`` comment on the task newer than ``since``
    (the re-land ask's mint): the brief changed, so a builder on the new
    brief — not a re-land of the old branch — is what the answer means."""
    for c in _comments(store, todo_id):
        if c.created_at <= since:
            break
        if str(c.text).lstrip().lower().startswith(("brief:", "handoff:")):
            return True
    return False


def _reland_params(plan: _Plan, n: _Node, store: Any) -> dict[str, Any] | None:
    """The failed land job's own ``params`` when the tick should re-mint
    ``land_slice`` — the newest job is an infra-land failure and the run
    is under :data:`LAND_AGAIN_CAP` — else ``None``. The lander re-runs
    ship on the same branch; no builder is asked to fix what the gate's
    environment broke. On the first tick after an answered re-land ask
    (the newest ended job is still ``meta.rearmed_after_job``) the
    re-land fires only when the answer names ``land again (no builder)``
    AND the brief is untouched — a rewritten brief mints a builder on
    the new brief instead (gr570187)."""
    jobs = plan.jobs(n)
    if not jobs:
        return None
    j = jobs[-1]
    if not (
        j.status == "failed"
        and j.meta.get("job_type") == "land_slice"
        and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
    ):
        return None
    if _infra_red_lands(plan, n) >= LAND_AGAIN_CAP:
        return None
    if n.meta.get("rearmed_ask_at") and _rearm_floor(n) == j.id:
        answer = str(n.meta.get("rearmed_answer") or "").strip().lower()
        if not answer.startswith("land again"):
            return None
        try:
            since = datetime.fromisoformat(str(n.meta["rearmed_ask_at"]))
        except ValueError:  # a stamp we cannot read must not park the land
            since = None
        if since is not None and _brief_rewritten(store, n.id, since):
            return None
    params = j.meta.get("params")
    return dict(params) if isinstance(params, dict) else None


def _land_again_capped(plan: _Plan, n: _Node) -> bool:
    """The infra-land run is done on its own: ``LAND_AGAIN_CAP``
    consecutive infra-red lands, or the newest job stamped
    ``meta.land_again_cap`` by the lander itself (a capped exit-137 —
    its gate-kill retries are spent, so the run goes to the ask, not to
    another automatic land, gr570187). The mark only counts past
    ``meta.rearmed_after_job``: an answered "land again" re-arms it."""
    floor = _rearm_floor(n)
    if _infra_red_lands(plan, n) >= LAND_AGAIN_CAP:
        return True
    jobs = plan.jobs(n)
    j = jobs[-1] if jobs else None
    return bool(
        j is not None
        and j.status == "failed"
        and j.meta.get("job_type") == "land_slice"
        and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        and j.meta.get("land_again_cap")
        and (floor is None or j.id > floor)
    )


def _task_rungs(
    lane_table: tuple[_lanes.Lane, ...], n: _Node, params: dict[str, Any]
) -> tuple[_lanes.Rung, ...]:
    """The ladder ``n`` climbs: every enabled fit rung for its task
    class, narrowed to the lane its ``meta.op`` pins — the same
    narrowing :func:`_escalate` applies (td584381 S1a)."""
    task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
    rungs = _lanes.ladder(task_class, lanes=lane_table)
    pinned = str(params["lane"]) if params.get("lane") else None
    if pinned:
        rungs = tuple(r for r in rungs if r.lane == pinned)
    return rungs


def _cap_rungs(
    plan: _Plan,
    lane_table: tuple[_lanes.Lane, ...],
    n: _Node,
    job_type: str,
    params: dict[str, Any],
) -> tuple[_lanes.Rung, ...]:
    """The ladder the retry cap counts, in one place (td584381 S1a
    F2/F3): ``()`` for a non-run_slice op or a model-pinned one —
    ``_escalate`` never climbs those, so the cap falls back to
    :data:`DEFAULT_RETRIES`. A reviewer climbs inside the lane its
    newest attempt ran on (``_review_lane``'s pick): its cap is that
    lane's rung count, never the full review-class table's."""
    if job_type != DEFAULT_OP or "model" in params:
        return ()
    rungs = _task_rungs(lane_table, n, params)
    if _is_reviewer(n) and not params.get("lane"):
        ended = _attempt_jobs(plan, n)
        last = ended[-1] if ended else None
        # ``meta.lane`` is stamped at submit; the job's own params carry
        # the pick before that (and always for a minted-but-unrun job).
        lane = (
            last.meta.get("lane") or (last.meta.get("params") or {}).get("lane")
            if last is not None
            else None
        )
        if lane is not None:
            rungs = tuple(r for r in rungs if r.lane == lane)
    return rungs


def _retry_cap(n: _Node, rungs: tuple[_lanes.Rung, ...]) -> int:
    """The attempts budget (td584381 S1a): an explicit ``meta.retries``
    pins the number; else the node climbs its whole lane ladder — one
    rung per attempt — before the cap fires. No ladder at all keeps
    :data:`DEFAULT_RETRIES`, the no-lane case that still mints the ask."""
    if n.meta.get("retries") is not None:
        return _retries(n)
    return len(rungs) or DEFAULT_RETRIES


def _capped(plan: _Plan, n: _Node, cap: int) -> bool:
    jobs = plan.jobs(n)
    # The newest job need only be TERMINAL: a cancelled job burns no
    # attempt itself (:func:`_attempt_jobs` skips it), but the attempts
    # below it still cap — an operator's cancel is a free re-queue,
    # never a bypass of the retry budget (gr587772).
    if not jobs or jobs[-1].status not in _JOB_TERMINAL:
        return False
    # A human-only hit caps at once: no lane may write those paths, so
    # no rung and no rescue can fix the failure (td584381 S1a).
    if _human_hit(plan, n):
        return True
    # A clean exit that left no commit is likewise the last attempt:
    # the ladder cannot teach a session to write (td584381 S1a F4).
    if _no_commit(plan, n):
        return True
    if _land_again_capped(plan, n):
        return True
    attempts = _attempts(plan, n)
    # ``attempts > 0``: with ``meta.retries: 0`` a no-attempt job (a host
    # refusal) must not mint the cap ask — nothing was tried (gr571395).
    return attempts > 0 and attempts >= cap


def _resolve_op(n: _Node) -> tuple[str, dict[str, Any]]:
    """``meta.op`` as ``(job_type, params)``."""
    op = n.meta.get("op")
    if isinstance(op, dict):
        job_type = str(op.get("type") or DEFAULT_OP)
        params = dict(op.get("params") or {})
        if job_type == DEFAULT_OP:
            params.setdefault("todo_id", n.id)
        return job_type, params
    if isinstance(op, str) and op.strip():
        return op.strip(), {"todo_id": n.id}
    return DEFAULT_OP, {"todo_id": n.id}


def _params_problem(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    """The submit path's static check: required keys, no unknown keys."""
    missing = [k for k in schema.get("required", []) if k not in params]
    if missing:
        return f"params miss {missing}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(params) - set(schema.get("properties", {})))
        if unknown:
            return f"params carry unknown keys {unknown}"
    return None


#: ``meta.task_class`` of a plain build node (mirrors ``run_slice``'s default).
_DEFAULT_TASK_CLASS = "code"


def _escalate(
    w: _Writer,
    plan: _Plan,
    n: _Node,
    params: dict[str, Any],
    lane_table: tuple[_lanes.Lane, ...],
) -> tuple[bool, str | None]:
    """Pin a retried ``run_slice`` one rung up the cross-lane ladder.

    Reto's ruling (td559457, 2026-11-03): when a slice fails, go up the
    chain before asking — free vendor lanes first, then the Claude
    subscription's models. The chain is :func:`precis.plans.lanes.ladder`
    for the node's task class (every enabled fit lane in table order, its
    models from the default upward). The retry climbs one rung above where
    the newest ended attempt ran (its job's ``meta.lane``/``meta.model``);
    when that is unknown, attempt *k* takes rung *k* from the foot. Floored
    at the top rung. A node whose ``meta.op`` already pins ``model`` is left
    alone (gr549046: the triage's pick wins); one that pins ``lane`` climbs
    within that lane only. Rungs on lanes a host refusal backed off are
    skipped — an escalated retry would park there, not build (gr571395);
    the floor is read on the FULL ladder so a backed-off lane never drops
    the retry below the last real attempt's rung, and when nothing free
    sits at or above the floor the node parks (lane-wait) rather than
    climbing down.

    Returns ``(go, note)``: ``go=False`` parks the node this tick — no
    job is minted — and ``note`` is the action line for the summary.
    """
    if "model" in params:
        return True, None
    # A reviewer whose newest job left no readable verdict retries on the
    # lane ``_review_lane`` picked: a format miss says nothing about the
    # lane's strength, and the climb pinned swe-2-max while the verdict
    # itself was lost to a parse miss (gr573564). The attempt still
    # counts, so the retry cap and its ask are unchanged.
    seen = n.meta.get("review")
    jobs = plan.jobs(n)
    if (
        _is_reviewer(n)
        and isinstance(seen, dict)
        and seen.get("verdict") is None
        and jobs
        and seen.get("job") == jobs[-1].id
    ):
        return True, None
    attempts = _attempts(plan, n)
    if attempts <= 0:
        return True, None
    rungs = _task_rungs(lane_table, n, params)
    if not rungs:
        return True, None
    # A no-attempt job is no rung to climb from either (gr571395): the
    # retry starts from the newest job that actually attempted.
    ended = [j for j in plan.jobs(n) if j.status in _JOB_ENDED and not _no_attempt(j)]
    prev = (
        _lanes.rung_index(
            rungs, ended[-1].meta.get("lane"), ended[-1].meta.get("model")
        )
        if ended
        else None
    )
    start = min(prev + 1 if prev is not None else attempts, len(rungs) - 1)
    backed = set(_lanes.lane_backoffs(w.store))
    idx = next(
        (i for i in range(start, len(rungs)) if rungs[i].lane not in backed),
        None,
    )
    if idx is None:
        blocked_lanes = sorted({r.lane for r in rungs[start:]})
        return False, (
            f"td{n.id}: lane-wait — every rung from {start + 1}/{len(rungs)} "
            f"up sits on a backed-off lane ({', '.join(blocked_lanes)}); the "
            "retry waits, it never climbs down"
        )
    target = rungs[idx]
    params["lane"] = target.lane
    if target.model is not None:
        params["model"] = target.model
    return True, (
        f"td{n.id}: retry {attempts} climbs to lane {target.lane}"
        f"{f' model {target.model}' if target.model else ''} "
        f"(rung {idx + 1}/{len(rungs)})"
    )


def _pick_executor(compatible: frozenset[str]) -> str:
    for name in _EXECUTOR_ORDER:
        if name in compatible:
            return name
    return sorted(compatible)[0]


def _mint_job(
    store: Any,
    n: _Node,
    job_type: str,
    executor: str,
    params: dict[str, Any],
    *,
    idem_key: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    from precis.workers._mint import mint_job_once

    stale = [v for v in n.open_tags if v.startswith("child-failed:")]
    with store.tx() as conn:
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type=job_type,
            executor=executor,
            title=f"{job_type} td{n.id}: {n.title.splitlines()[0][:70] if n.title else ''}",
            params=params,
            parent_id=n.id,
            idem_key=idem_key or f"{NAME}:td{n.id}",
            prio=n.prio,
            meta_extra={"plan_root": n.root, **(meta_extra or {})},
        )
        for value in stale:
            store.remove_tag(n.id, Tag.open(value), conn=conn)
        store.append_event(
            n.id,
            source=NAME,
            event="job-minted",
            payload={"job_id": job_id, "job_type": job_type, "executor": executor},
            conn=conn,
        )
    for value in stale:
        n.open_tags.pop(value, None)
    return job_id


def _effective_prio(plan: _Plan, n: _Node) -> int:
    """``n``'s prio, else its nearest ancestor's, else :data:`DEFAULT_PRIO`."""
    for node in (n, *plan.ancestors(n)):
        if node.prio is not None:
            return node.prio
    return DEFAULT_PRIO


def _review_lane(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    lane_table: tuple[_lanes.Lane, ...],
) -> tuple[bool, str | None]:
    """``(go, lane name)`` for a reviewer's ``run_slice``: a lane of a vendor
    that did not build what it reviews; ``go=False`` when every fit lane is
    full.

    Three steps, each a pick over a table shrunk by hand (never
    ``exclude_vendor``, whose own fallback could hand back an excluded
    vendor): every vendor that built on the node is out; if that leaves no
    enabled fit lane, only the original builder's vendor is out (a codex
    build repaired by sonnet is reviewed by claude-sub, not by codex); only
    when even that leaves nothing does the full table apply, with the note.
    Codex-built fixes get a Claude review (Reto's yes to 27b357ee5, via
    BUILDER 2026-11-05)."""
    cls = _templates.REVIEW_TASK_CLASS
    vendors = _builder_vendors(plan, n, lane_table)
    steps: list[tuple[frozenset[str], str]] = [
        (frozenset(vendors), "every build vendor"),
        (frozenset(vendors[:1]), "the original builder's vendor"),
        (frozenset(), ""),
    ]
    for excluded, label in steps:
        table = tuple(ln for ln in lane_table if ln.vendor not in excluded)
        if not any(ln.enabled and not ln.parked and ln.fits(cls) for ln in table):
            continue  # nothing of another vendor is enabled: next step
        lane = _lanes.pick_lane(w.store, cls, lanes=table)
        if lane is None:
            # Fit lanes of the allowed vendors exist but are full: wait,
            # do not bypass the rule.
            res.notes.append(
                f"td{n.id}: review not dispatched — every lane fit for {cls!r} "
                f"outside {label or 'the table'} is at its cap"
            )
            return False, None
        if vendors and lane.vendor == vendors[0]:
            # Step three only: the original builder's own vendor reviews.
            res.notes.append(
                f"td{n.id}: review on lane {lane.name} shares the builder's vendor "
                f"{lane.vendor!r} (no other vendor's lane is enabled; the "
                "cross-vendor rule holds once one is)"
            )
        return True, lane.name
    res.notes.append(f"td{n.id}: review not dispatched — no enabled lane fits {cls!r}")
    return False, None


# ── (4a) the validated non-repro rerun (gr588028) ──────────────────


def _digest_of(plan: _Plan, n: _Node) -> _Node | None:
    """The live ``ci_red_digest`` todo ``n`` is ``blocked-by`` — the
    release-fix loop's digest node, re-armed when a rerun still reads
    red (``ci_red_digest._fix_targets`` read the other way)."""
    for b in plan.blockers.get(n.id, []):
        node = plan.nodes.get(b.id)
        if node is None or node.kind != "todo":
            continue
        op = node.meta.get("op")
        if op == "ci_red_digest" or (
            isinstance(op, dict) and op.get("type") == "ci_red_digest"
        ):
            return node
    return None


def _mint_rerun_ask(store: Any, n: _Node, why: str, continued: str) -> int | None:
    """The ONE ask a stalled rerun path files under the plan root
    (gr588028 item 4, review round 1): it RECORDS the stall — the loop
    already continued (or still waits) without it — so it is
    ``blocks: "nothing"`` with no ``blocked-by`` edge, and the only
    option is ``acknowledged``. ``why`` names the error TYPE only,
    never internals. Insert shape of :func:`_mint_loop_ask`; the
    ``ci_rerun_stalled`` marker on the fix node is the dedupe."""
    context = (
        f"the validated-flake rerun stalled: {why} (fix td{n.id}, plan td{n.root})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_RERUN_ASK_OPTIONS),
            "blocks": "nothing",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = f"plan td{n.root}: the validated-flake rerun stalled ({why[:80]})"
    body = (
        f"A `validated:` non-repro fix session on td{n.id} parked the node "
        f"for a one-shot rerun of the red check.yml run's failed legs; the "
        f"rerun path stalled: {why}. {continued} This ask only records "
        "the stall — close it to acknowledge."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
    return int(ref.id)


def _rerun_stall(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    why: str,
    *,
    unpark: bool = True,
    marker: str = "",
) -> None:
    """One stall comment on the fix node + the informational root ask
    (:func:`_mint_rerun_ask` — it blocks nothing; the loop continues or
    waits regardless), then — unless ``unpark`` is false — drop
    :data:`NOREPRO_WAIT_TAG` and re-arm the digest so the release-fix
    loop continues as it does today (gr588028 item 4: the stall never
    retries the rerun itself). The >2h wait-cap call keeps the park;
    ``marker`` (the run id, or a sha key when none is known) is stamped
    as ``meta.ci_rerun_stalled`` and is the dedupe — a still-parked node
    never files twice for the same stall."""
    if marker and n.meta.get("ci_rerun_stalled") == marker:
        res.notes.append(f"td{n.id}: rerun stall already recorded — {why}")
    else:
        continued = (
            "The red-fix loop has continued as before (the digest "
            "re-armed, or no rerun fired)."
            if unpark
            else "The fix node is still parked on the in-flight rerun; "
            "closing the ask changes nothing about the wait."
        )
        ask_id = None if w.dry_run else _mint_rerun_ask(store, n, why, continued)
        if ask_id is not None or w.dry_run:
            w.comment(
                n,
                f"validated: rerun path stalled — {why}; the plan root's "
                "ask records it.",
            )
        if marker:
            w.stamp_meta(n, {"ci_rerun_stalled": marker})
    if unpark:
        w.remove_tags(n, [NOREPRO_WAIT_TAG])
        digest = _digest_of(plan, n)
        if digest is not None and not plan.live_job(digest):
            ended = [j for j in plan.jobs(digest) if j.status in _JOB_ENDED]
            if digest.status != "open":
                w.set_status(digest, "open")
            if ended:
                # The same floor ``_step_rearm`` and ``_review_revert``
                # stamp: without it ``_step_phases``' backstop re-closes
                # the digest next pass on its OLD succeeded job and no
                # fresh ``ci_red_digest`` — no fresh ``brief:`` — mints.
                w.stamp_meta(digest, {"rearmed_after_job": ended[-1].id})
    res.notes.append(f"td{n.id}: rerun stalled — {why}")


def _step_ci_rerun(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """The ``validated:`` non-repro path (gr588028), for every fix node
    ``run_slice.poll`` parked on :data:`NOREPRO_WAIT_TAG`: mint the
    ``ci_rerun`` job once per run id (idem ``ci_rerun:<run_id>`` — the
    once-per-run rule also lives in :func:`_release.ci_rerun_fired`),
    then the plan's own ``ci_verdict_poll`` records the rerun's verdict
    (no second reader). Green closes the node moot-style so the loop's
    recut re-reads the run; red re-arms the digest — the loop continues
    as today. Anything blocking files one stall comment and the deduped
    root ask, never a retry. Runs between :func:`_step_phases` and
    :func:`_step_dispatch` — the park tag's ``waiting-for:`` prefix keeps
    the node out of dispatch while this step owns it."""
    for n in plan.todos():
        if NOREPRO_WAIT_TAG not in n.open_tags or _closed(n):
            continue
        jobs = plan.jobs(n)
        reruns = [j for j in jobs if j.meta.get("job_type") == "ci_rerun"]
        polls = [
            j
            for j in jobs
            if j.meta.get("job_type") == "ci_verdict_poll" and j.meta.get("rerun_of")
        ]
        live = [j for j in (*reruns, *polls) if j.status not in _JOB_TERMINAL]
        rel = _release.release_of(store, n.id) or {}
        sha = str(rel.get("sha") or "")
        rng = str(rel.get("range") or sha)
        if live:
            pass  # the rerun or its poll is in flight — the wait cap below
        elif polls and polls[-1].status == "succeeded":
            verdict = _gates._newest_verdict(store, sha, rng) or {}
            run_id = str(polls[-1].meta.get("rerun_of") or "?")
            if verdict.get("verdict") == "green":
                w.comment(n, f"validated: flake — rerun of run {run_id} green")
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: rerun of run {run_id} recorded green; fix "
                    "closed, the recut re-reads the run"
                )
            else:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    f"rerun of run {run_id} still red — the loop "
                    "continues as today (the digest re-arms)",
                    marker=run_id,
                )
            continue
        elif polls:
            _rerun_stall(
                store,
                plan,
                w,
                res,
                n,
                f"the rerun's verdict poll {polls[-1].status}",
                marker=str(polls[-1].meta.get("rerun_of") or "poll"),
            )
            continue
        elif reruns and reruns[-1].status in _JOB_TERMINAL:
            rerun = reruns[-1]
            if rerun.status == "succeeded":
                rid: str | None = str(
                    (rerun.meta.get("ci_rerun") or {}).get("run_id") or ""
                )
                if w.dry_run:
                    res.notes.append(
                        f"td{n.id}: would mint ci_verdict_poll for run {rid}"
                    )
                    continue
                job_id = _mint_job(
                    store,
                    n,
                    "ci_verdict_poll",
                    "job_inproc",
                    {"todo_id": n.id},
                    idem_key=f"{NAME}:ci_verdict_poll:td{n.id}",
                    meta_extra={
                        ATTEMPT_FREE_KEY: "ci-rerun",
                        "rerun_of": rid,
                        # The freshness baseline: the poll reads a
                        # verdict as the rerun's only when the run shows
                        # a higher run_attempt (gr588028 review, F4).
                        "rerun_attempt_before": (rerun.meta.get("ci_rerun") or {}).get(
                            "rerun_attempt_before"
                        ),
                    },
                )
                res.notes.append(
                    f"td{n.id}: rerun of run {rid} accepted — minted "
                    f"ci_verdict_poll job:{job_id} to record the verdict"
                )
            else:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    f"the ci_rerun job {rerun.status} — the rerun was "
                    "refused or the run was not found",
                    marker=str(
                        (rerun.meta.get("ci_rerun") or {}).get("run_id")
                        or sha
                        or "refused"
                    ),
                )
            continue
        else:
            # Mint phase: no rerun job yet.
            verdict = _gates._newest_verdict(store, sha, rng) or {}
            if verdict.get("verdict") == "green":
                # Green already (a hand rerun, a misread digest) — never
                # rerun a run that is not red.
                w.comment(
                    n,
                    "validated: flake — the newest verdict on the cut is "
                    "already green; no rerun needed",
                )
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: verdict already green; closed without a rerun"
                )
                continue
            rid = _release.newest_red_run(store, sha) if sha else None
            if not rid:
                _rerun_stall(
                    store,
                    plan,
                    w,
                    res,
                    n,
                    "no red verdict with a run id on the cut sha to rerun",
                    marker=f"no-red:{sha or 'no-cut'}",
                )
                continue
            if _release.ci_rerun_fired(store, rid):
                # Defensive — run_slice's spent ruling never parks on a
                # fired run; a tag placed by hand un-parks to the plain
                # no_commit path.
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                res.notes.append(
                    f"td{n.id}: ci_rerun already fired for run {rid}; "
                    "un-parked to the ordinary path"
                )
                continue
            if w.dry_run:
                res.notes.append(f"td{n.id}: would mint ci_rerun for run {rid}")
                continue
            # The run id is never a param — ``ci_rerun`` resolves the
            # cut's newest red verdict row itself (a supplied
            # ``params.run_id`` is refused outright).
            params: dict[str, Any] = {"todo_id": n.id}
            host = _release.pinned_host(store, n.id)
            if host:
                params["target_node"] = host
            job_id = _mint_job(
                store,
                n,
                "ci_rerun",
                "ssh_node",
                params,
                idem_key=f"ci_rerun:{rid}",
                meta_extra={ATTEMPT_FREE_KEY: "ci-rerun"},
            )
            res.notes.append(
                f"td{n.id}: minted ci_rerun job:{job_id} for run {rid} "
                "(the failed legs re-fire once)"
            )
            continue
        # Still in flight — only the >2h wait is a stall. The marker
        # dedupes it: a live job does not re-ask each tick.
        tagged = n.open_tags.get(NOREPRO_WAIT_TAG)
        if tagged is not None and now - tagged > RERUN_WAIT_CAP:
            live_run = str(
                (live[-1].meta.get("rerun_of") or "")
                or (live[-1].meta.get("ci_rerun") or {}).get("run_id")
                or "in-flight"
            )
            _rerun_stall(
                store,
                plan,
                w,
                res,
                n,
                "over two hours waiting on the rerun and its verdict",
                unpark=False,
                marker=live_run,
            )


def _step_dispatch(
    plan: _Plan, w: _Writer, res: PassResult, lane_table: tuple[_lanes.Lane, ...]
) -> None:
    for n in sorted(plan.todos(), key=lambda x: (_effective_prio(plan, x), x.id)):
        if not _dispatchable(plan, n) or n.status != "open":
            continue
        if _excluded(n) or _claims(n) or plan.live_job(n):
            continue
        job_type, params = _resolve_op(n)
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type, params))
        if plan.blocked(n) or plan.paused(n) or _capped(plan, n, cap):
            continue
        jobs = plan.jobs(n)
        newest = jobs[-1] if jobs else None
        requeued_cancel: int | None = None
        if newest is not None and newest.status == "cancelled":
            if newest.meta.get("job_type") not in _REQUEUE_ON_CANCEL:
                # The cancel the tick leaves alone: an op off the
                # re-queue allowlist may carry side effects a kill did
                # not roll back — a land killed mid-ship may already
                # have merged, a release step or ``pr_action`` may
                # already have fired — so a blind re-run is never safe
                # (gr587772). The rescue tick's ``cancelled`` class owns
                # the node.
                res.notes.append(
                    f"td{n.id}: newest job:{newest.id} was cancelled; not redispatched"
                )
                continue
            # gr587772: every guard above already passed, so the cancel
            # is a free re-queue — dispatch exactly as for an
            # ``attempt_free`` job (the job burns no attempt: it is not
            # in ``_JOB_ENDED``). Stopping the node stays a tag's job:
            # ``halt:``/``waiting-for:``, ``STATUS:paused``, won't-do.
            # The note lands at the mint below: a node that parks on the
            # way (no fit review lane, a refused submit) was not
            # re-queued this pass.
            requeued_cancel = newest.id
        free_reason = newest.meta.get(ATTEMPT_FREE_KEY) if newest else None
        if free_reason:
            res.notes.append(f"td{n.id}: re-queued after {free_reason}, not an attempt")
        reland = _reland_params(plan, n, w.store)
        if reland is not None:
            # An infra-red land re-runs ship on the same branch with the
            # failed job's own params — no builder, no escalate, no review
            # lane (gr570187).
            job_type, params = "land_slice", reland
        if _is_reviewer(n) and job_type == DEFAULT_OP and "lane" not in params:
            go, lane_name = _review_lane(plan, w, res, n, lane_table)
            if not go:
                continue
            params["lane"] = lane_name
        if job_type == DEFAULT_OP:
            go, climbed = _escalate(w, plan, n, params, lane_table)
            if climbed:
                res.notes.append(climbed)
            if not go:
                continue
        if (
            job_type
            in (
                "release_deploy",
                "release_cut",
                "ci_red_digest",
                "ci_rerun",
                "release_confirm",
            )
            and "target_node" not in params
        ):
            # The deploy, a re-cut, the red-run digest and the runtime
            # confirm run where the cut ran (meta.release.host, td543856,
            # td543855, td573431, td573432); the first cut has none and
            # stays unpinned.
            host = _release.pinned_host(w.store, n.id)
            if host:
                params["target_node"] = host
        spec = get_job_type(job_type)
        if spec is None:
            res.notes.append(
                f"td{n.id}: meta.op job_type {job_type!r} is not registered"
            )
            continue
        problem = _params_problem(spec.params_schema, params)
        if problem is None and not w.dry_run and spec.validate_submit is not None:
            problem = spec.validate_submit(w.store, gripe_id=None, params=params)
        if problem:
            res.notes.append(f"td{n.id}: {job_type} not dispatched — {problem}")
            continue
        executor = _pick_executor(spec.compatible_executors)
        if w.dry_run:
            res.notes.append(f"td{n.id}: would dispatch {job_type} on {executor}")
        else:
            job_id = _mint_job(
                w.store,
                n,
                job_type,
                executor,
                params,
                # The re-land shares run_slice's own land-mint key
                # (``land_slice:td<id>``), not the tick's — one lander per
                # slice, however it was minted.
                idem_key=(f"land_slice:td{n.id}" if reland is not None else None),
            )
            if requeued_cancel is not None:
                res.notes.append(
                    f"td{n.id}: newest job:{requeued_cancel} was cancelled; "
                    "re-queued, not an attempt"
                )
            res.notes.append(
                f"td{n.id}: dispatched {job_type} job:{job_id} on {executor}"
            )
        res.dispatched += 1


# ── (5) hygiene ────────────────────────────────────────────────────


def _job_event_text(store: Any, job_id: int) -> str:
    """The newest ``job_event`` chunk's text (the job's failure reason)."""
    try:
        chunks = [
            c
            for c in store.chunks.list_chunks_for_ref(job_id)
            if c.chunk_kind == "job_event" and c.text
        ]
    except Exception:  # a missing tail must not stop the ask
        log.warning("plan_dispatch: reading job:%d chunks failed", job_id)
        return ""
    if not chunks:
        return ""
    return str(max(chunks, key=lambda c: (c.ord, c.id)).text).strip()


def _failure_tail(store: Any, job_id: int, limit: int) -> str:
    """The tail of the job's newest ``job_event`` chunk (its failure reason)."""
    return _job_event_text(store, job_id)[-limit:]


#: A session's own no-commit verdict — the first log line starting
#: ``moot:`` or ``no-op:`` (td584381 S1a F4), carried into the rescue
#: evidence or the ask so a human or the diagnose pass sees the why.
_MOOT_OR_NOOP_RE = re.compile(r"^\s*(?:moot|no-op)\s*:", re.IGNORECASE)


def _verdict_line(text: str) -> str | None:
    """The first ``moot:``/``no-op:`` line in a job's event text, ≤300
    chars, or ``None`` (td584381 S1a F4)."""
    for ln in text.splitlines():
        if _MOOT_OR_NOOP_RE.match(ln):
            return ln.strip()[:300]
    return None


def _handoff_verdict(
    store: Any, todo_id: int, *, since: datetime | None = None
) -> str | None:
    """The ``moot:``/``no-op:`` line of the single NEWEST ``handoff:``
    comment (the ``handoff:`` prefix stripped first, the line read by
    :func:`_verdict_line`), or ``None`` (td584381 S1d). Scoped to the
    newest attempt (review round 1): older handoffs are never consulted
    — an earlier attempt's ``moot:`` cannot be read as this attempt's —
    and ``since`` (the newest attempt job's ``created_at``) rejects a
    ``handoff:`` older than it. The session's verdict survives on the
    todo when the job's event text never carried it — a JSON-log
    adapter parses the session's result out of the raw log, so the log
    tail alone can miss it."""
    newest = next(
        (
            c
            for c in _comments(store, todo_id)
            if str(c.text).strip().lower().startswith("handoff:")
        ),
        None,
    )
    if newest is None:
        return None
    created = getattr(newest, "created_at", None)
    # Fail closed (review round 2): with a ``since`` in hand a handoff
    # whose ``created_at`` cannot be read is NOT this attempt's — an
    # unprovable scope is no scope.
    if since is not None and (not isinstance(created, datetime) or created < since):
        return None
    text = str(newest.text).strip()
    return _verdict_line(text[len("handoff:") :])


#: Options of the ask a node parked at :data:`LAND_AGAIN_CAP` infra-red
#: lands gets (gr570187): the default retry ladder cannot help — nothing
#: on the diff failed.
_RELAND_ASK_OPTIONS: list[str] = [
    "land again (no builder)",
    "rewrite the brief",
    "won't do",
]

#: Options of the ask a human-only hit mints (td584381 S1a): no lane may
#: write those paths — the options are the human edit landing, a new
#: brief that stays off them, or dropping the node.
_HUMAN_ASK_OPTIONS: list[str] = [
    "a human made the edit",
    "rewrite the brief",
    "won't do",
]


def _mint_ask(
    store: Any,
    plan: _Plan,
    n: _Node,
    attempts: int,
    cap: int,
    *,
    relands: int = 0,
    human: list[str] | None = None,
    lane_gap: str | None = None,
) -> int:
    newest = plan.jobs(n)[-1]
    if relands:
        head = (
            f"job:{newest.id} {newest.status} — {relands} land(s) in a row "
            "died outside the diff (gate infrastructure)"
        )
        title = (
            f"td{n.id}: land failed outside the diff {relands} times: "
            "land again, rewrite the brief, or drop it?"
        )
        options = list(_RELAND_ASK_OPTIONS)
    elif human:
        head = (
            f"job:{newest.id} {newest.status} — the branch touched "
            "human-only path(s): " + ", ".join(f"`{p}`" for p in human)
        )
        title = (
            f"td{n.id}: the diff touched human-only path(s) — "
            "human edit, new brief, or drop it?"
        )
        options = list(_HUMAN_ASK_OPTIONS)
    else:
        head = (
            f"job:{newest.id} {newest.status} after {attempts} attempt(s) "
            f"(retry cap {cap})"
        )
        title = f"td{n.id} hit its retry cap: retry, rewrite the brief, or drop it?"
        options = list(_ASK_OPTIONS)
    if lane_gap:
        head = f"{head}; {lane_gap}"
    tail = _failure_tail(store, newest.id, _ASK_CONTEXT_CAP - len(head) - 3)
    context = f"{head}: {tail}" if tail else head
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": options,
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    if relands:
        if newest.meta.get("land_again_cap"):
            # A capped exit-137 (the land's own gate-kill retries spent):
            # land_slice's blocked-handoff wording — the 137 can be the
            # container's mem_limit on the branch's own memory, so the
            # ask must not clear the branch.
            verdict = (
                "The cause is the docker VM's memory or the branch's own "
                "memory use — a memory hog on the branch is the builder's "
                "to fix; otherwise an operator frees the docker VM."
            )
        else:
            verdict = "The branch is not at fault, so no builder was minted."
        body = (
            f"The plan tick stopped re-landing td{n.id} "
            f"({n.title.splitlines()[0] if n.title else ''}): {head}. "
            f"{verdict} Answer with "
            "the option you pick and close this todo; 'land again (no "
            "builder)' re-arms the re-land, 'rewrite the brief' rebuilds "
            f"from the new brief on td{n.id} (a `brief:` comment or an "
            f"edited body). Pick 'won't do' by closing "
            f"td{n.id} STATUS:won't-do."
        )
    elif human:
        body = (
            f"The plan tick stopped dispatching td{n.id} "
            f"({n.title.splitlines()[0] if n.title else ''}): {head} — "
            f"a human must edit {', '.join(f'`{p}`' for p in human)}; "
            "no lane may write those paths, so no retry and no rescue "
            "can fix this. Answer 'a human made the edit' once the edit "
            f"is in and close this todo — closing re-arms td{n.id} with a "
            "fresh retry budget; 'rewrite the brief' rebuilds on a new "
            "brief (a `brief:` comment or an edited body); pick "
            f"'won't do' by closing td{n.id} STATUS:won't-do."
        )
    else:
        gap = (
            f" {lane_gap} — a lane must come back before a retry or a rescue can run."
            if lane_gap
            else ""
        )
        body = (
            f"The plan tick stopped dispatching td{n.id} ({n.title.splitlines()[0] if n.title else ''}): "
            f"{head}.{gap} Answer with the option you pick and close this todo; closing "
            f"it re-arms td{n.id} (a fresh retry budget). Pick 'won't do' by closing "
            f"td{n.id} STATUS:won't-do."
        )
        if newest.meta.get(NO_COMMIT_KEY):
            # The session's own read on a no-commit exit — carried so a
            # human (or S1d) acts on the why, not just the what
            # (td584381 S1a F4).
            vline = _verdict_line(_job_event_text(store, newest.id))
            if vline is None:
                # The log tail missed it (a JSON-log adapter parses the
                # result out of the raw log) — the posted ``handoff:``
                # comment still carries it (td584381 S1d); scoped to the
                # newest attempt's handoff.
                vline = _handoff_verdict(store, n.id, since=newest.created_at)
            if vline is not None:
                body += f" The session left no commit; its verdict: {vline}."
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
    return int(ref.id)


def _mint_stamp_ask(store: Any, n: _Node, gid: int, sha: str, err: str) -> int:
    """The ONE ask a persistently failing verified-moot gripe stamp
    files on the plan root (td584381 S1d review round 2):
    ``_notify_gripe`` raised :data:`_MOOT_STAMP_ASK_AFTER` ticks in a
    row, so the retry becomes a human call — a silent ``res.notes``
    loop would page nobody. ``err`` is the exception TYPE only (the
    message may carry internals). Insert shape of :func:`_mint_ask`;
    the ``blocked-by`` lands on the ROOT — ``_Plan.open_asks`` then
    dedupes the ask and :func:`_step_moot` reads the park off it."""
    context = (
        f"moot verified but the gripe stamp fails: {err} "
        f"(moot td{n.id}, plan td{n.root}, gripe gr{gid}, sha {sha})"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_MOOT_STAMP_ASK_OPTIONS),
            "blocks": f"td{n.root}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = (
        f"plan td{n.root}: verified moot td{n.id} cannot stamp gr{gid} — "
        "retry or drop the plan?"
    )
    body = (
        f"moot verified but the gripe stamp fails: {err}. The tick "
        f"verified the `moot:` claim of td{n.id} (sha {sha}) and closed "
        f"its chain won't-do, but resolving the plan's gripe gr{gid} on "
        f"that sha raised {_MOOT_STAMP_ASK_AFTER} ticks running — plan "
        f"td{n.root} stays open and the stamp is not retried while this "
        "ask is open. Answer and close this todo to re-arm the stamp "
        f"retry (or clear td{n.id}'s meta.{MOOT_STAMP_KEY} to retry at "
        f"once); pick 'won't do' by closing td{n.root} STATUS:won't-do."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.root, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
    return int(ref.id)


def _mint_loop_ask(store: Any, n: _Node, count: int, why: str) -> int:
    """The ruling ask an exhausted ``release-fix`` loop files (me573231):
    ``count`` automatic iterations ended red — named with the round, the
    head sha and the failing CI — and a further fix never starts by
    itself. Insert shape of :func:`_mint_ask`, minus the ``blocked-by``
    link (the ask parks the whole pass, not one node). ``blocks`` names the
    pass root, not ``"nothing"``: an informational ask no longer holds its
    root open (gr589823), and this one must — the parking is the point."""
    rel = _release.release_of(store, n.id)
    rn = str((rel or {}).get("rN") or "?")
    sha12 = str((rel or {}).get("sha") or "")[: _release.LANDED_SHA_LEN] or "?"
    context = f"release {rn} at {sha12}: {why}"[:_ASK_CONTEXT_CAP]
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_LOOP_ASK_OPTIONS),
            "blocks": f"td{n.root}",
            "context": context,
        }
    }
    check_ask_meta(meta)
    title = (
        f"release {rn}: {count} fix iteration(s) ended red — "
        "close this pass or hand the release to a human?"
    )
    body = (
        f"The {rn} pass's release-fix loop minted {count} fix iteration(s) "
        f"and every one ended red ({why}). No further iteration is minted: "
        "two run unattended, a third is a human call (me573231). "
        "release_poll mints no fresh pass while this pass's root stays "
        "open, and this ask holds the root open until it is closed. Fix "
        "main by hand, then close this todo and the pass root "
        "STATUS:won't-do — release_poll starts a fresh pass on the fixed "
        "main — or answer 'hand release with /go' to run the release "
        "yourself."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
    return int(ref.id)


def _stale_gripe_claims(
    conn: Any, now: datetime, *, gripe_id: int | None = None
) -> dict[int, list[str]]:
    """``{gripe id: [expired claimed-by: values]}`` for live ``STATUS:fixing``
    gripes with no live job under them (a child, or a ``fixes``-linked job).

    A lease is expired once its ``expires_at`` (stamped at claim and every
    re-claim, ``stamp_claim_lease``) has passed; a legacy row without one is
    judged by its tag ``created_at`` against :data:`CLAIM_STALE`.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, t.value, rt.created_at, rt.expires_at
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'gripe' AND r.retired_at IS NULL
           AND (%s::bigint IS NULL OR r.ref_id = %s::bigint)
           AND t.namespace = 'OPEN' AND t.value LIKE 'claimed-by:%%'
           AND EXISTS (SELECT 1 FROM ref_tags s JOIN tags st USING (tag_id)
                        WHERE s.ref_id = r.ref_id AND st.namespace = 'STATUS'
                          AND st.value = 'fixing')
           AND NOT EXISTS (
                 SELECT 1 FROM refs j
                  WHERE j.kind = 'job' AND j.retired_at IS NULL
                    AND (j.parent_id = r.ref_id OR EXISTS (
                          SELECT 1 FROM links l
                           WHERE l.src_ref_id = j.ref_id
                             AND l.dst_ref_id = r.ref_id
                             AND l.relation = 'fixes'))
                    AND NOT EXISTS (
                          SELECT 1 FROM ref_tags js JOIN tags jt USING (tag_id)
                           WHERE js.ref_id = j.ref_id AND jt.namespace = 'STATUS'
                             AND jt.value = ANY(%s)))
         ORDER BY r.ref_id, t.value
        """,
        (gripe_id, gripe_id, sorted(_JOB_TERMINAL)),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for ref_id, value, created_at, expires_at in rows:
        lapsed = (
            expires_at <= now
            if expires_at is not None
            else now - created_at > CLAIM_STALE
        )
        if lapsed:
            out.setdefault(int(ref_id), []).append(str(value))
    return out


def _release_gripe(store: Any, gripe_id: int, now: datetime) -> list[str] | None:
    """Drop an expired gripe's lease and return it to ``triaged`` — the
    handler's lease-drop rule (``GripeHandler._claim_status``): a newest
    ``resolved:`` comment means the work is done, so the status stays.
    Re-checked under the row lock; ``None`` when the lease was refreshed or
    the gripe moved on since the scan."""
    with store.tx() as conn:
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (gripe_id,))
        held = _stale_gripe_claims(conn, now, gripe_id=gripe_id).get(gripe_id)
        if not held:
            return None
        row = conn.execute(
            "SELECT c.text FROM chunks c WHERE c.ref_id = %s "
            "AND c.chunk_kind = %s ORDER BY c.ord DESC LIMIT 1",
            (gripe_id, _GRIPE_COMMENT_KIND),
        ).fetchone()
        resolved = bool(row) and str(row[0]).lstrip().startswith("resolved:")
        release_claims(conn, gripe_id)
        if resolved:
            return held
        store.add_tag(
            gripe_id,
            Tag.closed("STATUS", "triaged"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        (ord_,) = conn.execute(
            "SELECT coalesce(max(ord), 0) + 1 FROM chunks WHERE ref_id = %s",
            (gripe_id,),
        ).fetchone()
        holder = ", ".join(v.removeprefix("claimed-by:") for v in held)
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=int(ord_),
                    text=(
                        f"lease expired after {int(CLAIM_STALE.total_seconds() // 3600)}"
                        f" h; back to triaged (held by {holder})"
                    ),
                    meta={"chunk_kind": _GRIPE_COMMENT_KIND},
                )
            ],
            conn=conn,
        )
    return held


def _step_gripe_leases(store: Any, w: _Writer, res: PassResult, now: datetime) -> None:
    """gr542086: a ``STATUS:fixing`` gripe whose ``claimed-by:`` lease lapsed
    with no live job goes back to ``triaged`` (counted in ``released``)."""
    with store.pool.connection() as conn:
        stale = _stale_gripe_claims(conn, now)
    for gripe_id, held in stale.items():
        if not w.dry_run and _release_gripe(store, gripe_id, now) is None:
            continue
        res.released += 1
        res.notes.append(f"gr{gripe_id}: released expired {', '.join(held)}")


def _step_unplaced(store: Any, plan: _Plan, res: PassResult, now: datetime) -> None:
    """td543917: a note per plan job queued past :data:`QUEUED_NOTE_AFTER`
    whose placement capabilities (``ServiceSpec.placement``) no live host
    advertises together — the claim gate holds it until one does. Read-only;
    the sweeper's ``scheduler/unschedulable:<id>`` alert is the page."""
    from precis.workers.executors._common import (
        advertised_by_host,
        placement_gap,
        placement_requires,
    )

    waiting = [
        (n, j)
        for n in plan.todos()
        for j in plan.jobs(n)
        if j.status == "queued"
        and now - j.created_at > QUEUED_NOTE_AFTER
        and placement_requires(j.meta)
    ]
    if not waiting:
        return
    with store.pool.connection() as conn:
        advertised = advertised_by_host(conn)
    for n, j in waiting:
        gap = placement_gap(j.meta, advertised)
        if not gap:
            continue
        minutes = int((now - j.created_at).total_seconds() // 60)
        res.notes.append(
            f"td{n.id}: job jo{j.id} queued {minutes} min — no host advertises "
            f"{{{', '.join(sorted(gap))}}}"
        )


def _in_rescue(n: _Node) -> bool:
    """An open ``rescue:<cls>`` tag — the rescue pass owns the node."""
    return any(v.startswith("rescue:") for v in n.open_tags)


def _rescue_node(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, cap: int, attempts: int
) -> None:
    """A ladder-exhausted ``run_slice`` node enters the rescue path
    (td584381 S1a) instead of the cap ask: ``enter_rescue`` tags it once,
    files or joins the signature gripe, and the rescue pass owns what
    follows — one diagnose-and-re-arm, then the rescue pass's own ask on
    a second exhaustion (``meta.rescue.n >= 1``), the end of the
    autonomous road."""
    from precis.workers import factory_rescue as _rescue

    cls = "review" if _is_reviewer(n) else "builder"
    ended = _attempt_jobs(plan, n)
    last = ended[-1] if ended else plan.jobs(n)[-1]
    text = _job_event_text(w.store, last.id)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # The signature key's error line is the failure's own first line —
    # skipping the ``run_slice:`` preamble and the ``--- log tail ---``
    # separator, else every same-class failure collapses into one
    # signature gripe. The preamble stays the fallback.
    error_line = next(
        (ln for ln in lines if not ln.lstrip().startswith(("run_slice:", "---"))),
        lines[0] if lines else None,
    )
    rungs_tried = " → ".join(
        f"{j.meta.get('lane')}/{j.meta.get('model') or 'default'}" for j in ended
    )
    step = (
        f"retry cap {cap}: the lane ladder is spent "
        f"({attempts} attempts; rungs tried: {rungs_tried or 'unrecorded'})"
    )
    verdict = _verdict_line(text)
    if verdict is None:
        # The log tail missed it — the posted ``handoff:`` comment still
        # carries the verdict (td584381 S1d); the log tail keeps
        # precedence when it has one, and only the newest attempt's
        # ``handoff:`` counts (``last`` is that attempt's job).
        verdict = _handoff_verdict(w.store, n.id, since=last.created_at)
    if verdict is not None:
        # The session's own read on a no-commit exit — what a human or
        # the diagnose task acts on (td584381 S1a F4).
        step += f"; the session's verdict: {verdict}"
    commits = last.meta.get("branch_commits") or []
    head = str(commits[0]).split()[0] if commits and str(commits[0]).split() else None
    try:
        r = _rescue.enter_rescue(
            w.store,
            n.id,
            cls,
            _rescue.RescueEvidence(
                step=step,
                job=f"jo{last.id}",
                log_tail=text,
                error_line=error_line[:300] if error_line else None,
                # ``run_slice.slice_branch``'s spelling, restated — the
                # dependency stays one-way.
                branch=f"slice/{n.id}",
                head_sha=head,
            ),
        )
    except Exception as exc:  # one failing node must not stop the pass
        log.warning("plan_dispatch: rescue of td%d raised", n.id, exc_info=True)
        res.notes.append(f"td{n.id}: rescue failed ({type(exc).__name__}: {exc})")
        return
    if r.tagged:
        res.rescued += 1
        res.notes.append(
            f"td{n.id}: lane ladder spent ({attempts} attempts, cap {cap}); "
            f"entered rescue ({cls}, gripe gr{r.gripe_id})"
        )
        log_plan_event(
            w.store,
            n.id,
            f"node td{n.id} entered rescue ({cls}) at retry cap "
            f"({attempts} attempts, cap {cap})",
            entry_type="observation",
            root=n.root,
        )
    else:
        res.notes.append(f"td{n.id}: rescue skipped ({r.skipped})")


def _step_hygiene(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
) -> None:
    _step_gripe_leases(w.store, w, res, now)
    _step_unplaced(w.store, plan, res, now)
    for n in plan.todos():
        if _closed(n):
            continue
        stale = [v for v in _claims(n) if now - n.open_tags[v] > CLAIM_STALE]
        if stale and not plan.live_job(n):
            w.remove_tags(n, stale)
            if n.status == "doing":
                w.set_status(n, "open")
            res.released += 1
            res.notes.append(f"td{n.id}: released stale {', '.join(stale)}")
    for n in plan.todos():
        if _closed(n) or not _dispatchable(plan, n) or _in_rescue(n):
            continue
        job_type, params = _resolve_op(n)
        rungs = _task_rungs(lane_table, n, params) if job_type == DEFAULT_OP else ()
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type, params))
        if not _capped(plan, n, cap):
            continue
        if plan.open_asks(n):
            continue
        attempts = _attempts(plan, n)
        human = _human_hit(plan, n)
        land_capped = _land_again_capped(plan, n)
        lane_gap: str | None = None
        if not human and not land_capped and job_type == DEFAULT_OP and not rungs:
            task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
            pinned = params.get("lane")
            lane_gap = (
                f"the op's pinned lane {pinned!r} is not enabled"
                if pinned
                else f"no enabled lane fits task class {task_class!r}"
            )
        rescue = (
            not human
            and not land_capped
            and job_type == DEFAULT_OP
            and lane_gap is None
        )
        if w.dry_run:
            what = (
                f"enter rescue ({'review' if _is_reviewer(n) else 'builder'})"
                if rescue
                else f"file an ask (retry cap {cap})"
            )
            res.notes.append(f"td{n.id}: would {what}")
            if rescue:
                res.rescued += 1
            else:
                res.asks += 1
            continue
        if rescue:
            _rescue_node(plan, w, res, n, cap, attempts)
            continue
        ask_id = _mint_ask(
            w.store,
            plan,
            n,
            attempts,
            cap,
            relands=_infra_red_lands(plan, n) if land_capped else 0,
            human=human,
            lane_gap=lane_gap,
        )
        why = (
            "human-only hit"
            if human
            else "re-land cap hit"
            if land_capped
            else "no enabled lane"
            if lane_gap
            else "retry cap hit"
        )
        res.notes.append(f"td{n.id}: {why}, asked in td{ask_id}")
        # gr548800: the park and the ask are quest-logbook events.
        log_plan_event(
            w.store,
            n.id,
            f"node td{n.id} parked ({why}; {attempts} attempts, retry cap {cap})",
            entry_type="observation",
            root=n.root,
        )
        log_plan_event(
            w.store,
            n.id,
            f"ask td{ask_id} raised (blocks td{n.id})",
            root=n.root,
        )
        res.asks += 1


# ── Pass + dispatch ────────────────────────────────────────────────


def read_only_reason(store: Any, params: dict[str, Any] | None = None) -> str | None:
    """Why this pass must not write, or ``None`` when it may."""
    if os.environ.get("PRECIS_FACTORY", "").strip().lower() == "off":
        return "PRECIS_FACTORY=off"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT host FROM service_config WHERE service = %s AND prio = 0 LIMIT 1",
            (SERVICE,),
        ).fetchone()
    if row is not None:
        return f"service_config {SERVICE} prio 0 on host {row[0]}"
    if (params or {}).get("dry_run"):
        return "params.dry_run"
    return None


def run_pass(
    store: Any,
    *,
    dry_run: bool = False,
    reason: str | None = None,
    now: datetime | None = None,
) -> PassResult:
    """One dispatcher pass over every plan subtree (module docstring).

    The whole tick runs under :data:`TICK_LOCK_KEY`; when another tick holds
    it this returns a ``skipped`` result and touches nothing.
    """
    now = now or datetime.now(UTC)
    res = PassResult(dry_run=dry_run, reason=reason)
    # The lock sits on a dedicated connection whose open transaction does no
    # writes: it exists only to pin one backend (pgbouncer-safe) and hold the
    # transaction-scoped lock while the steps use their own connections.
    with try_xact_advisory_lock(
        store.dsn,
        TICK_LOCK_KEY,
        idle_timeout=TICK_LOCK_IDLE_TIMEOUT,
        application_name=TICK_LOCK_APP_NAME,
    ) as got:
        if not got:
            res.skipped = "another tick holds the lock"
            return res
        plan = _load(store)
        res.roots = len(plan.roots)
        lane_table = _lanes.load_lanes(store)
        w = _Writer(store, dry_run)
        _step_gates(store, plan, w, res, now)
        _step_loops(store, plan, w, res, now)
        _step_reviews(store, plan, w, res, now, lane_table)
        _step_reopen(plan, w, res)
        _step_rearm(plan, w, res)
        _step_moot(store, plan, w, res)
        _step_phases(plan, w, res)
        _step_ci_rerun(store, plan, w, res, now)
        _step_dispatch(plan, w, res, lane_table)
        _step_hygiene(plan, w, res, now, lane_table)
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    reason = read_only_reason(ctx.store, params)
    res = run_pass(ctx.store, dry_run=reason is not None, reason=reason)
    ctx.set_meta(plan_dispatch=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A whole-graph pass: a second run while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live plan state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "CLAIM_STALE",
    "DEFAULT_RETRIES",
    "NAME",
    "PARAMS_SCHEMA",
    "QUEUED_NOTE_AFTER",
    "SERVICE",
    "SPEC",
    "PassResult",
    "load",
    "parse_verdict",
    "read_only_reason",
    "run_pass",
]
