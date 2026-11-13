"""``release_poll`` — mint one ``release-pass`` plan when main is ahead of
prod and nothing is in flight (td573430, slice S6 of td570415).

The release loop starts itself: DB plus a handful of GitHub reads under
``job_inproc`` (the ``gripe_validate`` / ``pr_poll`` shape), armed by one
recurring todo every 10 minutes::

    put(kind='todo', text='release_poll tick',
        meta={'schedule': {'every': '10m'}, 'executor': 'job_inproc',
              'job_type': 'release_poll'})

One pass checks in order and stops at the first blocker — an idle steady
state (main == prod) costs a single GitHub request, which matters because
the api.github.com budget is shared with ``pr_poll`` and
``ci_verdict_poll`` (all send the factory token,
:func:`precis.workers.factory_git.github_api_headers`; without one the
budget is GitHub's unauthenticated 60/h per IP):

0. Stuck watchdog (td574370): the ``release.round`` row's ``state_at``
   is judged against the ``release.stage_deadlines`` setting — the
   clock is the later of the stage stamp and the newest ``job_event``
   of the stage's live leg job under the pass, so a parked
   ``ci_verdict_poll`` still being re-queued reads as waiting, not
   stuck (td583852, td584153); a round past a stage's minutes gets one
   ``ask-user`` ruling todo, once per (round, stage), offering only the
   hand verbs — nothing automatic acts. Runs first, at any hour and
   inside its own try/except, so a stuck release is reported whether or
   not a new one could mint.
1. Clock: the ``release.no_release_windows`` setting names UTC
   ``HH:MM-HH:MM`` pairs no release may start inside (the default is the
   nightly backup window); an unparsable setting is itself a blocker —
   when unsure, do not mint. Inside a window the engine-review gate's
   reviewer mints and asks still run (step 5, gr611217); only the pass
   waits.
2. The DB: a live todo tagged ``release-pass`` whose STATUS is not in
   ``precis.todo.TERMINAL_TODO_STATUSES`` is a pass already in flight,
   and the shared ``release.round`` record names a round still open
   (not :func:`precis.plans.release.is_terminal`, any owner — one
   exception: a ``cut`` row whose recorded ``pass`` root is terminal
   with no deploy evidence is a dropped pass and the poll abandons the
   row itself, through ``update_round`` — never while a non-terminal
   ``release_deploy`` job sits under the pass, and a dry run or
   ``PRECIS_FACTORY=off`` reports the would-abandon and writes nothing,
   gr575362). No GitHub read has happened yet. An open pass or round
   still runs the engine-review gate's mint-and-ask writes (step 5)
   before it returns (gr611217): over main ahead of prod, and over the
   open pass's own release (:func:`_release_lands`) once it is cut —
   that release's deploy waits on those reviews, and nothing else
   mints their reviewers. A first cut a refusal held
   (``meta.cut_hold`` beside ``meta.cut_retarget``) for
   :data:`CUT_HOLD_ASK_AFTER_MIN` files one
   ``release-stuck:td<root>:recut-hold`` ask, quoting the refusal.
3. The repo: ``params.repo``, else ``release.repo``, else the checkout's
   ``origin`` (:func:`precis.plans.pr_class.resolve_repo`).
4. ``GET /repos/<repo>/compare/prod...<pin>``: ``behind_by > 0`` or
   ``status == 'diverged'`` means prod holds commits main lacks — no
   mint, whatever ``ahead_by`` says; ``ahead_by == 0`` mints nothing,
   nor does an empty ``files`` list — main's tree equals prod's, the
   case right after a release's merge-back commit (release-train T3).
   The payload's ``commits`` (else the scan's own ordering, td601285)
   is the window the pass records as ``upto``.
5. The engine-review gate holds the deploy, never the cut (release
   train, Reto 2026-11-12): a release is cut once, at origin/main's
   tip, and needs no reviews to cut. The gate's per-land verdicts are
   still computed here on the DB and a git scan of ``origin/prod..<pin>``
   (:func:`_gate_lands`, gr597625) before any api.github.com read, so
   its reviewer mints and review-wait asks file before the release is
   cut; the verdicts only become a note. ``pin`` is the
   ``origin/main`` head the scan's fetch resolved — the ONE snapshot
   the scan, the compare and the minted target share (td601005). The
   hold itself is :func:`deploy_review_blockers`, read by
   ``release_deploy``: every engine land inside the release — the
   cut's recorded ``meta.release.range`` plus the commits on
   ``release/r<N>`` after the cut — must be cleared, and every
   corrective commit a FIX/REVERT clearance rests on must be on
   ``release/r<N>``. A land on main after the cut belongs to the next
   release and holds nothing here.
6. In flight on origin: ``GET .../git/matching-refs/heads/release/`` and
   ``.../git/matching-refs/tags/deployed/`` (each returns the whole
   prefix unpaginated — :data:`_HEAD_CAP` and :data:`_TAG_CAP` bound
   what a read may claim; over-cap heads is unreadable, over-cap tags
   degrade to "in flight"). A ``release/r<N>`` head is in flight unless
   its ``deployed/r<N>`` tag exists AND prod contains the head
   (``GET .../compare/<head sha>...prod`` answers ``identical`` or
   ``ahead``); the prod compare is skipped when there is no tag. A head
   that fails that test still clears when main already contains it
   (``GET .../compare/<head sha>...main`` answers ``identical`` or
   ``ahead``) — a superseded re-cut leaves its stale ref on origin and
   must not block the next pass forever (gr585785).
7. The sha claim — one pass per cut sha (td573430 fix round 1): inside
   one transaction under the ``_MINT_LOCK_KEY`` advisory lock the pass
   re-checks the open-pass gate and refuses when a
   ``release-main:<sha12>``-tagged root already exists in any state
   (open, done, failed, abandoned) — a new pass needs main's head
   to move. The lock makes check-then-mint atomic: two concurrent
   runners cannot both mint.
8. Only then: :func:`precis.plans.templates.mint_release_pass` stamps
   the root ``release-main:<sha12>`` and the pass records
   ``meta.release_target = {sha, upto}`` — the pinned head and the
   sha12 window at-or-before it; ``release_cut`` passes the sha to
   ``scripts/release --sha``. The plan itself runs when
   ``plan_dispatch`` is armed, so minting is safe while
   ``PRECIS_FACTORY=off`` — this op mints; it never cuts or deploys.

``params.dry_run`` reads and reports ``would mint``, mints nothing.
``GitHubUnreadable`` anywhere ends the pass with a note; the next tick
retries. Every outcome is one summary line —
``release_poll <repo>: minted td<root>`` or
``release_poll <repo>: blocked — <blocker>``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from precis.plans import pr_class
from precis.plans import release as _release
from precis.plans.nodes import finish_mint
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "release_poll"

#: The setting naming the no-release windows (see :func:`parse_windows`).
WINDOWS_SETTING = "release.no_release_windows"

#: The setting naming the per-stage stuck deadlines (a JSON object of
#: minutes; see :func:`_deadlines` and :func:`watch_stuck`).
STUCK_SETTING = "release.stage_deadlines"

_API = "https://api.github.com/repos/{repo}"
_HEAD_RE = re.compile(r"^refs/heads/release/r(\d+)$")
_TAG_RE = re.compile(r"^refs/tags/deployed/r(\d+)$")
_PAIR_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")

#: ``pg_advisory_xact_lock`` key (the "rel_stuk" bytes) serializing the
#: stuck-ask dedupe read and insert: two concurrent runners cannot both
#: file (:func:`watch_stuck`).
_STUCK_LOCK_KEY = 0x72656C5F7374756B

#: The dedupe tag on the stuck ask — one per ``(round, stage)``, ever.
_STUCK_TAG_PREFIX = "release-stuck"

#: Leg activity may hold a stage's clock at most this many marks: a leg
#: re-queued forever without progressing still files at 3x the stage's
#: deadline (:func:`watch_stuck`; td584381 S1c ruling).
_ACTIVITY_CAP_MARKS = 3

#: check.yml run statuses that mean CI is still moving on a head.
_LIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending"}
)

#: The job types whose live jobs are the leg a stage waits on —
#: ``cut``, ``gating`` and ``fixing`` are all the row's ``cut`` state
#: (the cut, the verdict poll and, while ``fixing``, the loop's
#: digest/fix/land/recut legs); ``deploying`` waits on
#: ``release_deploy``, ``confirm`` on ``release_confirm``. A live job
#: of the stage's set whose ``job_event`` moved recently means the leg
#: is working — a ``wake_runner: re-queued`` line on a parked poll
#: counts (td583852, td584153: re-queued behind a derived_drain
#: backlog, gr584136).
_STAGE_LEG_JOBS: dict[str, frozenset[str]] = {
    "cut": frozenset({"release_cut", "ci_verdict_poll"}),
    "gating": frozenset({"release_cut", "ci_verdict_poll"}),
    "fixing": frozenset(
        {
            "ci_red_digest",
            "ci_rerun",
            "run_slice",
            "land_slice",
            "release_cut",
            "ci_verdict_poll",
        }
    ),
    "deploying": frozenset({"release_deploy"}),
    "confirm": frozenset({"release_confirm"}),
}

#: The hand verbs a stuck stage's ask names — all of them a human's call.
_STAGE_VERBS = {
    "cut": "`scripts/round cut --abandon`",
    "gating": "`scripts/round cut --abandon`",
    "fixing": "`scripts/round cut --abandon`",
    "deploying": "`scripts/round deploy` (resume the rollout) or "
    '`scripts/round supersede --partial --reason "<why>"`',
    "confirm": "`scripts/round deploy` (resume/confirm) or "
    '`scripts/round supersede --partial --reason "<why>"`',
}

#: The hand verbs a first cut's ``recut-hold`` ask names — never
#: ``cut --abandon``: the pass cut nothing yet, so the abandon would land
#: on the other, live release the hold is waiting on (gr620480).
_FIRST_CUT_HOLD_VERBS = (
    "answer a holding land's `release-review-wait:` ask or finish its "
    "review — not `scripts/round cut --abandon`: the pass never cut, so "
    "it would abandon the other, live release instead"
)

#: A first cut held this long files one stuck ask quoting the refusal
#: (gr611217) — the hold itself never lifts.
CUT_HOLD_ASK_AFTER_MIN = 60

#: The cut hold's stage name on that ask's ``release-stuck:`` tag (the
#: name predates the release train; it stays the dedupe key).
_CUT_HOLD_STAGE = "recut-hold"

#: Lines a recorded hold keeps (:func:`_kept_hold_lines`), plus a count.
CUT_HOLD_LINES_CAP = 20

#: The carry-note shape of a hold line: a cleared land whose corrective
#: commit the cut must carry — context, not a holder (gr651885).
_CARRY_MARK = "cleared on a corrective commit"

#: Bounds on what one ``matching-refs`` read may claim. The endpoint
#: answers the whole prefix in one unpaginated payload (GitHub lists
#: ``per_page``/``page`` params; clients report them ignored), so a
#: truncated or bloated list is the failure shape, not a missing page:
#: more than ``_HEAD_CAP`` release heads and the read cannot prove no
#: in-flight branch hides past the bound — unreadable; a deployed tag
#: past ``_TAG_CAP`` is simply unseen, the safe direction (its head
#: reads in flight).
_HEAD_CAP = 50
_TAG_CAP = 500

#: ``pg_advisory_xact_lock`` key (the "rel_poll" bytes) serializing the
#: sha-claim check and the mint: one transaction under
#: :meth:`precis.store.Store.atomic`, so two concurrent runners cannot
#: both pass the check and double-mint (td573430 fix round 1).
_MINT_LOCK_KEY = 0x72656C5F706F6C6C

#: ``meta.generate.from`` on a reviewer todo — ``td<N>`` of the slicer or
#: build it reviews; a land is covered when it names the landed todo or
#: its parent (:func:`engine_review_blockers`, td574937 E1).
_REVIEW_FROM_RE = re.compile(r"\s*td(\d+)\s*")

#: ``meta.generate.from`` on a sha-keyed reviewer — ``sha:<sha>`` of a
#: hand-landed engine commit no job stamp or subject handle links a todo
#: to (gr597625); the gate covers and dedupes on the sha prefix.
_REVIEW_FROM_SHA_RE = re.compile(r"\s*sha:([0-9a-f]{7,40})\s*", re.IGNORECASE)

#: The trailing handle list a ship squash subject carries (restated from
#: ``land_slice._HANDLES_RE``): the ``td<id>`` handles a hand-landed
#: commit links its todos by — a mid-subject ``td`` mention is prose, not
#: a handle.
_LAND_HANDLES_RE = re.compile(r"\(((?:gr|td)\d+(?:[,\s]+(?:gr|td)\d+)*)\)\s*$")
_LAND_TD_RE = re.compile(r"td(\d+)")

#: The dedupe tag on the review-wait ask — one OPEN ask per LANDED todo:
#: ``release-review-wait:td<id>`` where id is the land's todo, and
#: answering the ask clears that one land — an ``answer:`` comment, or
#: an ``answer:`` line in its details body (gr645663). An ask closed
#: without one holds nothing — the gate refiles it.
_REVIEW_WAIT_TAG_PREFIX = "release-review-wait"

#: Todo statuses that close a review-wait ask for dedupe and blocker
#: purposes — :data:`precis.todo.TERMINAL_TODO_STATUSES` plus
#: ``cancelled`` (the same set :func:`_review_outcome` judges dead on).
_WAIT_ASK_CLOSED: tuple[str, ...] = tuple(
    sorted(TERMINAL_TODO_STATUSES | {"cancelled"})
)

#: How long an open reviewer may sit on an engine land before one
#: ``ask-user`` ruling files (tagged ``release-review-wait:td<id>``);
#: a wait that can never resolve — no covering reviewer, a dead verdict,
#: a dead root, a fix that ended unlanded — files it at once.
REVIEW_WAIT_AFTER = timedelta(hours=24)

#: ``pg_advisory_xact_lock`` key (the "rev_wait" bytes) serializing the
#: review-wait dedupe read and insert.
_REVIEW_WAIT_LOCK_KEY = 0x7265765F77616974

#: ``pg_advisory_xact_lock`` key (the "rev_mint" bytes) serializing the
#: no-covering-reviewer re-read and the reviewer insert: two concurrent
#: gate ticks mint at most one reviewer per landed todo (td584381 S1b).
_REVIEW_MINT_LOCK_KEY = 0x7265765F6D696E74

#: Reviewers one gate pass mints at most: the backlog of historical
#: uncovered lands drains a couple a tick instead of bursting onto the
#: review lanes all at once.
_MINT_PER_PASS = 2

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Read and report 'would mint', mint nothing.
        "dry_run": {"type": "boolean"},
        # owner/repo; else release.repo, else the checkout's origin.
        "repo": {"type": "string"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Release loop starter: mint one release-pass plan when GitHub shows "
    "main ahead of prod, no release-pass plan is open, no release/r<N> "
    "branch is in flight and the clock is outside the "
    "release.no_release_windows window; the plan runs once plan_dispatch "
    "is armed."
)


@dataclass
class PollResult:
    """What one pass did (or, ``dry_run``, would have done)."""

    dry_run: bool
    repo: str | None = None
    minted: int | None = None
    would_mint: bool = False
    blocker: str = ""
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "minted": self.minted or 0,
            "would_mint": self.would_mint,
            "blocked": bool(self.blocker),
            "blocker": self.blocker,
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        repo = self.repo or "(no repo)"
        if self.minted is not None:
            return f"{NAME} {repo}: minted td{self.minted}"
        if self.would_mint:
            return f"{NAME} {repo}: would mint (dry run)"
        if self.blocker:
            return f"{NAME} {repo}: blocked — {self.blocker}"
        return f"{NAME} {repo}: nothing minted"

    def render(self) -> str:
        return "\n".join([self.summary(), *self.notes])


def parse_windows(raw: str) -> list[tuple[time, time]] | None:
    """The ``release.no_release_windows`` value as UTC ``(start, end)``
    pairs — ``None`` on an empty list, a malformed pair, an out-of-range
    time or a pair whose start equals its end."""
    out: list[tuple[time, time]] = []
    for piece in raw.split(","):
        m = _PAIR_RE.fullmatch(piece.strip())
        if m is None:
            return None
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if max(h1, h2) > 23 or max(m1, m2) > 59:
            return None
        start, end = time(h1, m1), time(h2, m2)
        if start == end:
            return None
        out.append((start, end))
    return out or None


def in_window(now: datetime, windows: list[tuple[time, time]]) -> str | None:
    """The window ``now`` sits inside as ``HH:MM-HH:MM``, else ``None``.

    Each pair is half-open ``[start, end)``; a start later than the end
    crosses midnight (inside is ``now >= start or now < end``)."""
    t = (now.astimezone(UTC) if now.tzinfo is not None else now).time()
    for start, end in windows:
        inside = start <= t < end if start < end else t >= start or t < end
        if inside:
            return f"{start:%H:%M}-{end:%H:%M}"
    return None


def branch_in_flight(has_tag: bool, prod_has_head: bool | None) -> bool:
    """A ``release/r<N>`` head counts as in flight unless its
    ``deployed/r<N>`` tag exists AND prod contains the head. When unsure
    (no tag, or a tag but no compare verdict), it is in flight."""
    return not (has_tag and prod_has_head is True)


def _release_pass_ids(store: Any) -> list[int]:
    """Live todos tagged ``release-pass`` (any status), oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             ORDER BY r.ref_id
            """,
            ("release-pass",),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _open_pass_id(store: Any) -> int | None:
    """The oldest ``release-pass``-tagged todo whose STATUS is not
    terminal, else ``None``."""
    from precis.todo import TERMINAL_TODO_STATUSES

    for rid in _release_pass_ids(store):
        status = next(
            (str(t.value) for t in store.tags_for(rid) if t.prefix == "STATUS"),
            "open",
        )
        if status not in TERMINAL_TODO_STATUSES:
            return rid
    return None


def _round_raw(store: Any) -> str | None:
    """The ``app_settings`` row for ``release.round`` — raises on a
    database error (``settings._db_read`` swallows one as ``None``, which
    here would read as "no round")."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (_release.ROUND_KEY,),
        ).fetchone()
    return None if row is None else str(row[0])


#: The ``pass`` handle ``release_cut`` writes on the row it creates — a
#: ``td<root>`` naming the pass's plan root. Script-published rows carry
#: none: their round belongs to a checkout's journal, not a factory pass.
_PASS_REF_RE = re.compile(r"^td(\d+)$")


def _pass_ref(store: Any, rec: dict[str, Any]) -> tuple[int, str] | None:
    """``(pass root id, its STATUS)`` when the row's ``pass`` handle
    names a live todo, else ``None`` — a row without one, or naming a
    todo that no longer exists, cannot be judged this way."""
    m = _PASS_REF_RE.fullmatch(str(rec.get("pass") or ""))
    if m is None:
        return None
    rid = int(m.group(1))
    if store.get_ref(kind="todo", id=rid) is None:
        return None
    status = next(
        (str(t.value) for t in store.tags_for(rid) if t.prefix == "STATUS"),
        "open",
    )
    return rid, status


def _deploy_evidence(store: Any, rec: dict[str, Any], pass_id: int) -> bool:
    """Something may already have rolled out: ``meta.release.confirmed``
    on the pass root, a ``deploy.sha`` already on the row, or
    ``release.prod_head`` naming the row's base sha."""
    ref = store.get_ref(kind="todo", id=pass_id)
    rel = (ref.meta or {}).get("release") if ref is not None else None
    if isinstance(rel, dict) and isinstance(rel.get("confirmed"), dict):
        return True
    dep = rec.get("deploy")
    if isinstance(dep, dict) and dep.get("sha"):
        return True
    base = str(rec.get("base") or "").lower()[: _release.LANDED_SHA_LEN]
    head = _release.read_prod_head(store)
    return bool(
        len(base) == _release.LANDED_SHA_LEN
        and head is not None
        and str(head.get("head") or "").lower().startswith(base)
    )


def _live_deploy_job(store: Any, pass_id: int) -> bool:
    """A non-terminal ``release_deploy`` job under the pass's plan
    members — its own ``deployed`` write may still land, so the row
    must not be abandoned out from under it (gr575362)."""
    from precis.workers._mint import JOB_TERMINAL_STATUSES

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id FROM refs j
              JOIN refs m ON m.ref_id = j.parent_id
              JOIN ref_tags rt ON rt.ref_id = m.ref_id
              JOIN tags t USING (tag_id)
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta ->> 'job_type' = 'release_deploy'
               AND m.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (f"plan:{pass_id}",),
        ).fetchall()
    for (jid,) in rows:
        status = next(
            (str(t.value) for t in store.tags_for(int(jid)) if t.prefix == "STATUS"),
            "open",
        )
        if status not in JOB_TERMINAL_STATUSES:
            return True
    return False


def _abandon_dropped_round(
    store: Any, rec: dict[str, Any], pass_id: int, status: str, now: datetime
) -> list[str]:
    """The cut row of a pass that ended before any deploy: ``abandoned``
    through :func:`precis.plans.release.update_round`'s merge — a fresh
    read under the rank rules, so a concurrent ``deployed`` write
    survives. Returns the write's note."""
    return _release.update_round(
        store,
        {
            "n": rec.get("n"),
            "state": "abandoned",
            "abandoned_why": (
                f"pass td{pass_id} ended {status} with no deploy recorded; "
                f"release_poll abandoned the row"
            ),
        },
        now=now,
    )


def _terminal_pass_blocker(
    rec: dict[str, Any], state: str, pass_id: int, status: str
) -> str:
    n = rec.get("n", "?")
    return (
        f"release round r{n} in flight ({state}) but pass td{pass_id} is "
        f"{status} — nothing will move the row; next: `scripts/round "
        f"supersede r{n}` run where the publish works, or "
        f"`scripts/prod-precis settings set release.round` a row with a "
        f"terminal state"
    )


def _rejudge_after_abandon(store: Any, pass_id: int, status: str) -> str | None:
    """What the fresh row says after the abandon write: a terminal state
    (the write landed) is no block; a row a concurrent write moved
    reads like a same-shape row whose pass ended — ``cut`` the generic
    blocker, anything later the hand-verb one. An absent fresh row is
    "no round"; an unreadable one blocks — when unsure, do not mint."""
    try:
        raw = _round_raw(store)
    except Exception as exc:
        return f"{_release.ROUND_KEY} unreadable ({type(exc).__name__}): nothing minted"
    rec: dict[str, Any] | None = None
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return f"{_release.ROUND_KEY} is unparsable: nothing minted"
        if isinstance(parsed, dict):
            rec = parsed
    if rec is None:
        return None
    state = rec.get("state")
    if not isinstance(state, str) or not state:
        return f"{_release.ROUND_KEY} carries no state: nothing minted"
    if _release.is_terminal(rec):
        return None
    if state == "cut":
        return f"release round r{rec.get('n', '?')} in flight (cut)"
    return _terminal_pass_blocker(rec, state, pass_id, status)


def _round_blocker(
    store: Any, now: datetime, *, dry_run: bool, notes: list[str]
) -> str | None:
    """The shared ``release.round`` record as a blocker line — ``None``
    only for an absent record ("no round"), a closed one (terminal
    state), or a ``cut`` row whose recorded ``pass`` root ended before
    any deploy: the poll abandons that row itself, a dropped pass would
    otherwise block every later mint (gr575362). Three exceptions keep
    the row: ``dry_run`` or ``PRECIS_FACTORY=off`` notes the
    would-abandon and writes nothing (``watch_stuck``'s rule); a live
    ``release_deploy`` job under the pass keeps blocking — its own
    ``deployed`` write may still land. The abandon goes through
    ``update_round``'s fresh read and the row is re-judged on what the
    write left. A later-state row whose pass ended keeps blocking —
    something may have rolled out — and the blocker names the hand
    verbs. A record that cannot be read or parsed blocks: when unsure,
    do not mint."""
    from precis.todo import TERMINAL_TODO_STATUSES
    from precis.workers.job_types import run_slice

    try:
        raw = _round_raw(store)
    except Exception as exc:
        return f"{_release.ROUND_KEY} unreadable ({type(exc).__name__}): nothing minted"
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return f"{_release.ROUND_KEY} is unparsable: nothing minted"
    if not isinstance(rec, dict):
        return f"{_release.ROUND_KEY} is not a record: nothing minted"
    state = rec.get("state")
    if not isinstance(state, str) or not state:
        return f"{_release.ROUND_KEY} carries no state: nothing minted"
    if _release.is_terminal(rec):
        return None
    n = rec.get("n", "?")
    pass_ref = _pass_ref(store, rec)
    if pass_ref is not None:
        pass_id, status = pass_ref
        if status in TERMINAL_TODO_STATUSES:
            if state == "cut" and not _deploy_evidence(store, rec, pass_id):
                if _live_deploy_job(store, pass_id):
                    return (
                        f"release round r{n} in flight (cut); pass "
                        f"td{pass_id} is {status} but a release_deploy job "
                        "is still live — it may advance the row itself"
                    )
                if dry_run or run_slice.factory_off():
                    notes.append(
                        f"would abandon release round r{n}: pass td{pass_id} "
                        f"ended {status} with no deploy recorded"
                    )
                    return None
                try:
                    notes.extend(
                        _abandon_dropped_round(store, rec, pass_id, status, now)
                    )
                except Exception as exc:
                    log.warning("release_poll: abandon row write failed", exc_info=True)
                    return (
                        f"release round r{n} in flight (cut) but pass "
                        f"td{pass_id} is {status}; the abandon write failed "
                        f"({type(exc).__name__})"
                    )
                return _rejudge_after_abandon(store, pass_id, status)
            return _terminal_pass_blocker(rec, state, pass_id, status)
    return f"release round r{n} in flight ({state})"


# ── the observation-only stuck watchdog (td574370) ──────────────────
#
# A release that stops moving must reach a human as one ask. The
# watchdog runs first in every pass — a stuck release is reported at
# any hour, whether or not a new pass could mint — reads only the DB
# (plus one GitHub probe past the `cut` deadline), and its only write
# is a single `ask-user` ruling todo per (round, stage). It never
# deploys, supersedes, cuts, abandons or answers anything.


def _deadlines(store: Any) -> tuple[dict[str, int], str | None]:
    """``({stage: minutes}, note)`` from ``release.stage_deadlines`` — a
    JSON object of minutes per stage. A stage absent, non-integer or
    ``<= 0`` is not watched; an unparsable or non-object value watches
    nothing and returns the note (fail closed: no ask)."""
    from precis import settings

    raw = settings.get_str(STUCK_SETTING, store=store)
    if not raw:
        return {}, None
    try:
        obj = json.loads(raw)
    except ValueError:
        return {}, f"stuck watch: {STUCK_SETTING} is unparsable — watching nothing"
    if not isinstance(obj, dict):
        return {}, f"stuck watch: {STUCK_SETTING} is not an object — watching nothing"
    return {str(k): v for k, v in obj.items() if type(v) is int and v > 0}, None


def _minutes_since(now: datetime, stamp: Any) -> float | None:
    """Minutes from ``stamp`` (an ``...Z`` string) to ``now`` — ``None``
    when it is unparsable or in the future: a clock that cannot be
    read is never judged."""
    try:
        then = datetime.strptime(str(stamp or ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    delta = (now - then).total_seconds() / 60
    return delta if delta >= 0 else None


def _stuck_pass_root(store: Any, rn: str) -> int | None:
    """The oldest open ``release-pass`` root whose ``meta.release.rN``
    is ``rn`` — its ``meta.release.sha`` is the freshest head the DB
    knows, the same identification ``_auto_answer_approval`` uses —
    else ``None`` (a hand release has no pass)."""
    from precis.todo import TERMINAL_TODO_STATUSES

    for rid in _release_pass_ids(store):
        status = next(
            (str(t.value) for t in store.tags_for(rid) if t.prefix == "STATUS"),
            "open",
        )
        if status in TERMINAL_TODO_STATUSES:
            continue
        rel = _release.release_of(store, rid)
        if rel is not None and str(rel.get("rN")) == rn:
            return rid
    return None


def _branch_run(repo: str, branch: str) -> dict[str, Any] | None:
    """The newest workflow run on ``branch`` — the one read the
    watchdog pays, made only past the ``cut``/``gating`` mark to spare
    the unauthenticated budget. Branch-keyed (not head-sha) so a hand
    ``ship --release`` head move cannot produce a wrong stage. Raises
    :class:`precis.plans.pr_class.GitHubUnreadable`."""
    url = f"{_API.format(repo=repo)}/actions/runs"
    data = pr_class._get_json(url, {"branch": branch, "per_page": "1"})
    if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
        raise pr_class.GitHubUnreadable(f"GET {url}: no workflow_runs list")
    runs = data["workflow_runs"]
    return runs[0] if runs and isinstance(runs[0], dict) else None


def _stuck_tag_exists(store: Any, n: int, stage: str) -> bool:
    """``True`` when a todo already carries the
    ``release-stuck:r<N>:<stage>`` tag in any status — the read-only
    pre-check that keeps a filed stage from ever costing another
    GitHub read."""
    tag = f"{_STUCK_TAG_PREFIX}:r{n}:{stage}"
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             LIMIT 1
            """,
            (tag,),
        ).fetchall()
    return bool(row)


def _leg_job_activity(store: Any, root_id: int, stage: str) -> datetime | None:
    """The newest activity stamp of the stage's live leg jobs under the
    pass root — the newest ``job_event`` chunk's ``created_at``, else
    the job ref's ``updated_at``/``created_at`` — or ``None`` when no
    job of a type in :data:`_STAGE_LEG_JOBS` is non-terminal under a
    ``plan:<root>`` member. A parked ``ci_verdict_poll`` the wake runner
    keeps re-queueing counts: the leg is waiting, not wedged. May
    raise: the caller's fail-quiet rule applies."""
    from precis.workers._mint import JOB_TERMINAL_STATUSES

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id, j.created_at, j.updated_at,
                   (SELECT MAX(c.created_at) FROM chunks c
                     WHERE c.ref_id = j.ref_id AND c.retired_at IS NULL
                       AND c.chunk_kind = 'job_event') AS last_event
              FROM refs j
              JOIN refs m ON m.ref_id = j.parent_id
              JOIN ref_tags rt ON rt.ref_id = m.ref_id
              JOIN tags t USING (tag_id)
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta ->> 'job_type' = ANY(%s)
               AND m.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (sorted(_STAGE_LEG_JOBS[stage]), f"plan:{root_id}"),
        ).fetchall()
    newest: datetime | None = None
    for jid, created_at, updated_at, last_event in rows:
        status = next(
            (str(t.value) for t in store.tags_for(int(jid)) if t.prefix == "STATUS"),
            "open",
        )
        if status in JOB_TERMINAL_STATUSES:
            continue
        for stamp in (last_event, updated_at, created_at):
            if stamp is None:
                continue
            ts = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
            if newest is None or ts > newest:
                newest = ts
    return newest


def _file_stuck_ask(
    store: Any,
    n: int,
    stage: str,
    over: int,
    head12: str,
    deadline: int,
    root_id: int | None,
    *,
    dry_run: bool,
    run_head12: str | None = None,
    clock_txt: str,
    capped: bool = False,
    detail: str = "",
    label: str | None = None,
    verbs: str | None = None,
) -> str:
    """The one write the watchdog makes: an ``ask-user`` ruling todo,
    deduped by its ``release-stuck:r<N>:<stage>`` tag over todos in any
    status inside one locked transaction, so a re-tick, a closed ask or
    two runners never file a second. ``run_head12`` is the head the
    newest branch run reported — it may differ from the record's base
    after a hand ``ship --release`` fix, and the ask says so.
    ``clock_txt`` is the one-line ``clock:`` note naming which clock
    judged the stage (``stage entered`` vs ``last leg job event``);
    ``capped`` adds the ``capped:`` line — the ask filed only because
    the live leg's hold ran past ``_ACTIVITY_CAP_MARKS`` marks.
    ``detail`` is one more line for the body and context (the first-cut
    hold's refusal, gr611217). ``label`` replaces ``r<n>`` in the
    tag and the text for a pass with no release number yet (a first cut
    held before it cut: ``td<root>``). ``verbs`` overrides the stage's
    hand verbs — the first-cut hold's, which must not name
    ``cut --abandon`` (gr620480)."""
    from precis.workers.job_types import run_slice

    tok = label or f"r{n}"
    disp = f"pass {label}" if label else f"r{n}"
    tag = f"{_STUCK_TAG_PREFIX}:{tok}:{stage}"
    verbs = _STAGE_VERBS[stage] if verbs is None else verbs
    head_txt = (
        f"{run_head12}, the head the newest run on release/r{n} ran on — "
        f"it may differ from the record's base {head12}"
        if run_head12
        else head12
    )
    context = (
        f"release {disp} stage {stage}: {over}m past the {deadline}m "
        f"deadline; head {head_txt}; answering executes nothing"
        + (f"; {detail}" if detail else "")
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["acknowledged", "false alarm"],
            "blocks": "nothing",
            "context": context[:600],
        }
    }
    check_ask_meta(meta)
    title = (
        f"release {disp} looks stuck in stage {stage} — act on it, or is this "
        "a false alarm?"
    )
    capped_txt = "capped: leg active but stage exceeded 3× its mark\n" if capped else ""
    body = (
        f"Release {disp} has been in stage {stage} for more than {deadline} "
        f"minutes — {over}m past the mark — and nothing will act on that by "
        f"itself (head {head_txt}). This ask is the only write the release "
        f"watchdog makes; answering it executes nothing — 'acknowledged' "
        f"and 'false alarm' both just close it.\n\n"
        f"Hand verbs that apply (a human's call, in the coordinator's "
        f"checkout): {verbs}.\n\n"
        f"{capped_txt}"
        f"clock: {clock_txt}" + (f"\n{detail}" if detail else "")
    )
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_STUCK_LOCK_KEY,))
        dupes = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             LIMIT 1
            """,
            (tag,),
        ).fetchall()
        if dupes:
            return f"stuck watch: ask for {tok}/{stage} already filed (td{dupes[0][0]})"
        if dry_run or run_slice.factory_off():
            return f"stuck watch: would file {tag} ({title[:60]})"
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=root_id,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        values = ["ask-user", "showstopper", tag]
        if root_id is not None:
            values.append(f"plan:{root_id}")
        for value in values:
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    return f"stuck watch: filed {tag} as td{ref.id}"


def watch_stuck(store: Any, *, now: datetime, dry_run: bool) -> list[str]:
    """Judge the ``release.round`` row against its stage deadlines and
    file at most one ask (module contract above); returns note lines.

    The clock is the later of the stage stamp — the record's
    ``state_at`` (``fixing`` takes the red verdict's ``at``), a record
    without one (every round cut before td574370) never judged — and
    the newest ``job_event`` of the stage's live leg job under the pass
    (:func:`_leg_job_activity`): a leg that produced an event minutes
    ago is waiting, not stuck — but only up to
    ``_ACTIVITY_CAP_MARKS`` times the stage's deadline; past the cap
    the stage clock judges and a filing ask says ``capped:``. The
    activity read runs only past the deadline (it can only lower the
    judged elapsed). An erroring activity read files no ask
    this tick — one note, the same fail-quiet rule as the row read. A
    future or unparsable clock is never judged. From the row:
    ``deploying`` judges stage ``deploying``; ``deployed`` without
    ``deploy.confirmed_at`` judges
    ``confirm``; ``cut`` derives its ruled stage from
    ``release.ci_verdicts`` — a green verdict on the head is the
    approval wait (no stage), a red is ``fixing`` from that verdict's
    ``at`` (a red on the round's older head counts until a verdict for
    the current head exists), and no verdict is ``cut`` past its
    deadline unless one GitHub read shows a run in flight, in which
    case it is ``gating`` from ``state_at`` — and that read is spent
    only when the judged clock could file: ``cut`` and ``gating``
    share the leg set, so a live leg holding the stage under every
    mark spares the probe. Terminal states, an absent
    or unreadable record, and every error path file no ask.
    """
    notes: list[str] = []
    deadlines, bad = _deadlines(store)
    if bad:
        return [bad]
    if not deadlines:
        return notes
    try:
        raw = _round_raw(store)
    except Exception as exc:
        log.warning("release_poll: stuck watch round read failed", exc_info=True)
        return [f"stuck watch: release.round unreadable ({type(exc).__name__})"]
    if not raw:
        return notes
    try:
        rec = json.loads(raw)
    except ValueError:
        return ["stuck watch: release.round is unparsable"]
    if not isinstance(rec, dict):
        return ["stuck watch: release.round is not a record"]
    if not isinstance(rec.get("n"), int) or not isinstance(rec.get("state"), str):
        return ["stuck watch: release.round is not a round record"]
    n = int(rec["n"])
    rn = f"r{n}"
    state = str(rec["state"])
    state_at = rec.get("state_at")
    if not state_at:
        return notes  # a record without state_at is never judged
    root_id = _stuck_pass_root(store, rn)
    stage: str | None = None
    clock: Any = state_at
    head = ""
    stage_run_head: str | None = None
    leg_known = False
    activity: datetime | None = None
    if state == "deploying":
        stage = "deploying"
    elif state == "deployed":
        dep = rec.get("deploy")
        confirmed = isinstance(dep, dict) and bool(dep.get("confirmed_at"))
        if not confirmed:
            stage = "confirm"
    elif state == "cut":
        head = _stuck_head(store, rec, root_id)
        head12 = head[:12].lower()
        verdict = _release._newest_sha_verdict(store, head12) if head12 else None
        base12 = str(rec.get("base") or "")[:12].lower()
        if (
            verdict is None
            and base12
            and base12 != head12
            and (v := _release._newest_sha_verdict(store, base12)) is not None
            and v.get("verdict") == "red"
        ):
            # A red on the round's older head is fixing until a verdict
            # for the current head exists.
            verdict = v
        if verdict is not None:
            if verdict.get("verdict") == "red":
                stage, clock = "fixing", verdict.get("at")
            # green: the approval/deploy wait — already a human's item.
        else:
            elapsed = _minutes_since(now, state_at)
            # The probe fires on the smaller ENABLED cut/gating mark —
            # a stage set to 0 disables only that stage.
            probe_at = min(
                (deadlines[s] for s in ("cut", "gating") if s in deadlines),
                default=None,
            )
            if elapsed is not None and probe_at is not None and elapsed > probe_at:
                if _stuck_tag_exists(store, n, "cut") or _stuck_tag_exists(
                    store, n, "gating"
                ):
                    notes.append(
                        "stuck watch: a cut/gating ask for r"
                        f"{n} already exists — no runs probe"
                    )
                else:
                    # Spend the one GitHub read only when the judged
                    # clock could file: cut and gating share
                    # _STAGE_LEG_JOBS' set, so this read decides for
                    # both — a live leg holding the stage under the
                    # smallest mark spares the probe (the same cap
                    # rule applies, against probe_at).
                    judged = elapsed
                    if root_id is not None:
                        try:
                            activity = _leg_job_activity(store, root_id, "cut")
                        except Exception as exc:
                            log.warning(
                                "release_poll: stuck watch leg read failed",
                                exc_info=True,
                            )
                            notes.append(
                                "watch_stuck: job activity unreadable "
                                f"({type(exc).__name__})"
                            )
                            return notes
                        leg_known = True
                        if activity is not None:
                            quiet = max((now - activity).total_seconds() / 60, 0.0)
                            if (
                                quiet < judged
                                and elapsed <= _ACTIVITY_CAP_MARKS * probe_at
                            ):
                                judged = quiet
                    if judged > probe_at:
                        repo = pr_class.resolve_repo(store, None)
                        branch = str(rec.get("branch") or f"release/r{n}")
                        if repo is None:
                            notes.append("stuck watch: no repo for the runs probe")
                        else:
                            try:
                                run = _branch_run(repo, branch)
                            except pr_class.GitHubUnreadable as exc:
                                notes.append(f"stuck watch: GitHub unreadable ({exc})")
                            else:
                                run_head = (
                                    str((run or {}).get("head_sha") or "")[:12].lower()
                                    or None
                                )
                                stage = (
                                    "gating"
                                    if run is not None
                                    and str(run.get("status") or "")
                                    in _LIVE_RUN_STATUSES
                                    else "cut"
                                )
                                stage_run_head = run_head
    # terminal states, green verdicts and absent rows: no stage.
    if stage is None or stage not in deadlines:
        return notes
    deadline = deadlines[stage]
    elapsed = _minutes_since(now, clock)
    if elapsed is None:
        return notes  # a clock in the future or unparsable is not judged
    clock_txt = f"stage entered {clock}"
    capped = False
    if root_id is not None and stage in _STAGE_LEG_JOBS:
        # The read is skipped inside the deadline — activity can only
        # lower the judged elapsed, and a broken read must not note
        # during a healthy stage. A read the probe check already did
        # (cut/gating share the leg set) is not repeated.
        if not leg_known and elapsed > deadline:
            try:
                activity = _leg_job_activity(store, root_id, stage)
            except Exception as exc:
                # The row-read rule: an unreadable leg mints no ask — one
                # note, and this tick goes no further.
                log.warning("release_poll: stuck watch leg read failed", exc_info=True)
                notes.append(
                    f"watch_stuck: job activity unreadable ({type(exc).__name__})"
                )
                return notes
        if activity is not None:
            quiet = max((now - activity).total_seconds() / 60, 0.0)
            if quiet < elapsed:
                if elapsed <= _ACTIVITY_CAP_MARKS * deadline:
                    elapsed = quiet
                    clock_txt = (
                        f"last leg job event {activity:%Y-%m-%dT%H:%M:%SZ} "
                        f"(stage entered {clock})"
                    )
                elif quiet <= deadline:
                    # Past the cap the stage clock judges — the ask
                    # files only because of it.
                    capped = True
                    clock_txt = (
                        f"stage entered {clock} "
                        f"(last leg job event {activity:%Y-%m-%dT%H:%M:%SZ})"
                    )
    over = elapsed - deadline
    if over <= 0:
        return notes
    head12 = (head or str(rec.get("base") or ""))[:12].lower() or "?"
    return [
        *notes,
        _file_stuck_ask(
            store,
            n,
            stage,
            int(over),
            head12,
            deadline,
            root_id,
            dry_run=dry_run,
            run_head12=stage_run_head,
            clock_txt=clock_txt,
            capped=capped,
        ),
    ]


def _stuck_head(store: Any, rec: dict[str, Any], root_id: int | None) -> str:
    """The release's current head sha — the matching pass root's
    ``meta.release.sha`` (``release_cut`` re-stamps it on every cut),
    else the row's ``base`` (the cut's head). A release-branch fix that
    moved the head without a recut is invisible to the record."""
    if root_id is not None:
        rel = _release.release_of(store, root_id)
        sha = str((rel or {}).get("sha") or "").strip()
        if sha:
            return sha
    return str(rec.get("base") or "").strip()


def _ref_n(refs: Any, pattern: re.Pattern[str]) -> dict[int, Any]:
    """``{<N>: <entry>}`` for the ``release/r<N>`` / ``deployed/r<N>``
    refs in one ``matching-refs`` payload."""
    out: dict[int, Any] = {}
    if not isinstance(refs, list):
        return out
    for entry in refs:
        if not isinstance(entry, dict):
            continue
        m = pattern.fullmatch(str(entry.get("ref") or ""))
        if m:
            out[int(m.group(1))] = entry
    return out


def _matching_refs(
    url: str, pattern: re.Pattern[str], cap: int
) -> dict[int, Any] | None:
    """The ``{<N>: <entry>}`` map one ``matching-refs`` call answers —
    ``None`` on a non-list payload or more than ``cap`` matches (the
    endpoint is unpaginated, so the bound is all a caller can trust)."""
    data = pr_class._get_json(url, {})
    if not isinstance(data, list):
        raise pr_class.GitHubUnreadable(f"GET {url}: expected a list")
    refs = _ref_n(data, pattern)
    return refs if len(refs) <= cap else None


def _merged_to_main(base: str, sha: str) -> bool:
    """main already contains ``sha`` — the head is a stale ref (a
    superseded re-cut's branch left on origin), not a release in flight
    (gr585785). ``GET .../compare/<sha>...main`` answers ``identical`` or
    ``ahead`` when main contains the head; any other verdict — behind,
    diverged, an empty sha or an unreadable payload — keeps the block."""
    if not sha:
        return False
    cmp_ = pr_class._get_json(f"{base}/compare/{sha}...main", {})
    return isinstance(cmp_, dict) and cmp_.get("status") in ("identical", "ahead")


def _in_flight(repo: str) -> str | None:
    """The first in-flight ``release/r<N>`` on origin (``release/r<N>``),
    else ``None``. Raises :class:`precis.plans.pr_class.GitHubUnreadable`."""
    base = _API.format(repo=repo)
    heads_url = f"{base}/git/matching-refs/heads/release/"
    heads = _matching_refs(heads_url, _HEAD_RE, _HEAD_CAP)
    if heads is None:
        raise pr_class.GitHubUnreadable(
            f"GET {heads_url}: more than {_HEAD_CAP} release heads"
        )
    if not heads:
        return None
    tags_url = f"{base}/git/matching-refs/tags/deployed/"
    tagged = set(_matching_refs(tags_url, _TAG_RE, _TAG_CAP) or {})
    for n in sorted(heads):
        has_tag = n in tagged
        sha = str((heads[n].get("object") or {}).get("sha") or "")
        prod_has_head: bool | None = None
        if has_tag:
            cmp_ = pr_class._get_json(f"{base}/compare/{sha}...prod", {})
            prod_has_head = isinstance(cmp_, dict) and cmp_.get("status") in (
                "identical",
                "ahead",
            )
        if branch_in_flight(has_tag, prod_has_head) and not _merged_to_main(base, sha):
            return f"release/r{n}"
    return None


def _pass_for_sha(store: Any, sha12: str) -> tuple[int, str] | None:
    """``(id, STATUS)`` of the oldest live pass root tagged
    ``release-main:<sha12>``, in any state — a closed pass for this sha
    still counts: one pass per main sha, a new pass needs main to move."""
    from precis.plans import templates

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             ORDER BY r.ref_id
             LIMIT 1
            """,
            (f"{templates.RELEASE_MAIN_TAG_PREFIX}{sha12}",),
        ).fetchall()
    if not rows:
        return None
    rid = int(rows[0][0])
    status = next(
        (str(t.value) for t in store.tags_for(rid) if t.prefix == "STATUS"),
        "open",
    )
    return rid, status


# ── the engine-review gate (td574937 E1) ──────────────────────────
#
# A release never deploys an engine land its reviewer has not read:
# the factory may build engine paths once ``factory.deny_relaxed`` is
# flipped, but only through the review lane. Since the release train
# (Reto 2026-11-12) the gate holds the deploy, never the cut: a release
# is cut once at origin/main's tip, and :func:`deploy_review_blockers`
# judges the lands INSIDE it — the cut's recorded range plus the fixes
# on ``release/r<N>`` (:func:`_release_lands`). A land on main after
# the cut belongs to the next release. The poll runs the same gate over
# ``origin/prod..<pin>`` (:func:`_git_lands`) for its mint-and-ask
# writes only. The gate's lands come from
# GIT, not jobs (gr597625): every first-parent commit in the scope
# whose diff touches
# ``lanes.ENGINE_PATHS`` — the commits the release carries. A commit
# links its todos through the succeeded ``land_slice`` job whose
# ``meta.landed_sha`` matches it (the stamped path, kept as the fast
# lookup) or the ``td<id>`` handles in the subject's trailing list; a
# linked todo's covering reviewer is ``meta.generate.from`` naming the
# todo itself or its parent — the one level
# ``plan_dispatch._review_targets`` reviews (a slicer's slices). A
# commit nothing links — a hand land from a tmux tree or the
# coordinator seat mints no ``land_slice`` job — is its own unit, and
# a linked todo with no live plan root joins it: there is nothing to
# hang a td-keyed reviewer under, so the gate mints the sha-keyed
# reviewer (``generate.from`` = ``sha:<sha12>``, one per commit,
# covering every unit of the commit) and holds the deploy until it
# closes LAND.
# A covering reviewer clears the land when it closed ``LAND``, or
# ``FIX`` whose fix task landed — a sha-keyed reviewer's ``REVERT``
# tracks its revert task the same way, since a hand land has no todo
# to rebuild and re-read. A FIX/REVERT clearance is only half the
# story: the release must also CARRY the corrective commit — a fix
# whose ``landed:`` sha is not on ``release/r<N>`` still holds the
# deploy (:func:`deploy_review_blockers`, td601005). That corrective
# commit is itself
# cleared by the chain (gr611217): an engine land whose sha IS a
# cleared FIX/REVERT's recorded ``landed:`` sha needs no review of its
# own — a covering reviewer that reached a verdict still wins.
# While a reviewer is open under a live root
# the release waits, and once the NEWER of the land and its newest
# covering reviewer is :data:`REVIEW_WAIT_AFTER` old one ``ask-user``
# ruling files — at once when the wait can never resolve (a dead
# verdict, an unlanded fix that already ended, an open reviewer under
# a dead root).
# No covering reviewer is the factory's own job (td584381 S1b): the
# gate mints one — a plan reviewer node under the root the landed
# todo's own ``plan:<id>`` tag names, ``blocked-by`` that todo so a
# REVERT holds it through the rebuild, which ``plan_dispatch`` runs on
# a review lane of a vendor that did not build the land — and files the
# ask only when no minted reviewer could run: no enabled review-fit
# lane, or a mint that raised; when ``plan_dispatch`` itself is
# switched off the gate mints nothing and files the ask as before.
# (No ``plan:`` tag naming a live root used to ask too — a dead root
# is hopeless to this same gate — but the sha-keyed mint needs no
# root, so it answers instead.)
# An answered ask
# (``release-review-wait:td<land>`` carrying an ``answer:`` comment or
# an ``answer:`` line in its details body, gr645663) clears that one
# land; one closed without either holds nothing — the gate refiles it
# and the blocker names the live ask's id. "Fix
# reviewed" is not gated:
# ``plan_dispatch._review_fix`` mints no reviewer for a fix today
# (td574946 S5 adds the narrow re-review and tightens this gate).
# Any read error fails closed — the release waits.


def _reviewer_rows(store: Any) -> list[tuple[int, dict[str, Any]]]:
    """``(ref_id, meta)`` for every live todo minted with
    ``meta.generate.role == 'reviewer'``, oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->'generate'->>'role' = 'reviewer'
             ORDER BY ref_id
            """
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


@dataclass(frozen=True)
class _GitLand:
    """One first-parent commit in the gate's scope — ``origin/prod..<pin>``
    or a release's own commits — the gate's source of truth for what
    landed (gr597625): a ``scripts/ship`` run from a tmux tree or the
    coordinator seat mints no ``land_slice`` job, so a stamped-job scan
    alone lets a hand land ride the release unseen."""

    sha: str
    subject: str
    committed_at: datetime | None
    paths: tuple[str, ...]


#: ``-c safe.directory=*``: the checkout can sit under a mount owned by
#: a different uid — the gate container bind-mounts the host's git dir
#: at its own path, and a service-account worker's checkout — so git
#: otherwise refuses to read it at all; the same widening
#: ``precis.handlers.job._git`` uses for its mounted-checkout probes.
_GIT_SAFE = ("-c", "safe.directory=*")


def _git_lands() -> tuple[list[_GitLand], str]:
    """``(lands, pin)`` — every first-parent commit in
    ``origin/prod..<pin>``, oldest first, with its touched paths, and
    ``pin`` the ``origin/main`` head the same fetch resolved: the ONE
    commit snapshot the pass pins its scan, the compare and the recorded
    target to (td601005 — a ``git/ref/heads/main`` read AFTER the scan
    could record a cut sha carrying a land the scan never saw).
    ``origin`` is fetched first so a stale checkout cannot hide a hand
    land; any failure raises (the caller fails closed). Seam: tests
    patch it."""
    # Needs repo_checkout: slice_root() reads PRECIS_SLICE_ROOT, set only on
    # a host with the repo checkout — hence :func:`conditional_requires`
    # (gr610176).
    from precis.workers.job_types import run_slice as _rs

    root = _rs.slice_root()
    proc = _rs._git([*_GIT_SAFE, "fetch", "-q", "origin", "main", "prod"], root)
    if proc.returncode != 0:
        raise RuntimeError(f"git fetch origin main prod: {proc.stderr.strip()[:200]}")
    proc = _rs._git([*_GIT_SAFE, "rev-parse", "origin/main^{commit}"], root)
    pin = proc.stdout.strip().lower()
    if proc.returncode != 0 or not pin:
        raise RuntimeError(f"git rev-parse origin/main: {proc.stderr.strip()[:200]}")
    return _log_lands(root, f"origin/prod..{pin}"), pin


#: ``meta.release.range`` as ``release_cut`` records it from
#: ``scripts/release``'s ``RELEASE_RANGE: <prev prod>..<head>`` line.
_RANGE_RE = re.compile(r"^[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}$", re.IGNORECASE)


def _release_lands(rel: dict[str, Any]) -> list[_GitLand]:
    """Every first-parent commit inside the release ``rel``
    (``meta.release``), oldest first: the cut's recorded ``range``, then
    the commits on ``release/r<N>`` after the cut sha — the fixes landed
    on the branch. Main's commits after the cut are not in it. ``origin``
    is fetched first (main and prod for the range's ends, the release
    branch into its remote-tracking ref); a malformed record or any git
    failure raises (the caller fails closed). Seam: tests patch it."""
    from precis.workers.job_types import run_slice as _rs

    rn = str(rel.get("rN") or "")
    # release-train T1: a fix round's restamp (``release.record_head``)
    # moves ``sha``/``range`` onto the release head and keeps the cut as
    # ``cut_sha``/``cut_range`` — the scan is always the cut's own range
    # plus EVERY commit on the branch after the cut, the restamped head's
    # fixes included.
    sha = str(rel.get("cut_sha") or rel.get("sha") or "").strip().lower()
    rng = str(rel.get("cut_range") or rel.get("range") or "").strip()
    if not re.fullmatch(r"r\d+", rn) or not sha or _RANGE_RE.fullmatch(rng) is None:
        raise RuntimeError(
            f"meta.release {rn or '?'} has no rN/sha/<prev>..<head> range to scan"
        )
    tracking = f"refs/remotes/origin/release/{rn}"
    root = _rs.slice_root()
    proc = _rs._git(
        [
            *_GIT_SAFE,
            "fetch",
            "-q",
            "origin",
            "main",
            "prod",
            f"+refs/heads/release/{rn}:{tracking}",
        ],
        root,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git fetch origin release/{rn}: {proc.stderr.strip()[:200]}"
        )
    return _log_lands(root, rng) + _log_lands(root, f"{sha}..{tracking}")


def _log_lands(root: Any, rng: str) -> list[_GitLand]:
    """``git log <rng> --first-parent`` in ``root`` as lands, oldest
    first, with their touched paths; a failure raises."""
    from precis.workers.job_types import run_slice as _rs

    proc = _rs._git(
        [
            *_GIT_SAFE,
            "log",
            rng,
            "--first-parent",
            "--format=%x00%H%x1f%ct%x1f%s",
            "--name-only",
        ],
        root,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log {rng}: {proc.stderr.strip()[:200]}")
    out: list[_GitLand] = []
    for rec in proc.stdout.split("\x00"):
        lines = rec.strip("\n").splitlines()
        if not lines or not lines[0].strip():
            continue
        sha, _, rest = lines[0].partition("\x1f")
        stamp, _, subject = rest.partition("\x1f")
        try:
            committed = datetime.fromtimestamp(int(stamp.strip()), UTC)
        except ValueError:
            committed = None
        out.append(
            _GitLand(
                sha=sha.strip().lower(),
                subject=subject.strip(),
                committed_at=committed,
                paths=tuple(p.strip() for p in lines[1:] if p.strip()),
            )
        )
    out.reverse()
    return out


def _landed_jobs(store: Any) -> list[tuple[str, int, int]]:
    """``(landed sha, job ref_id, landed todo id)`` for every
    ``land_slice`` job that SUCCEEDED — the sha → todo lookup the git
    scan stitches its commits to (the fast lookup; the
    ``meta.engine_paths`` stamp itself is ignored: it only ever decided
    engine-ness, which the commit's own diff now answers).

    Succeeded = ``meta->>'landed_sha' IS NOT NULL``: a failed land has no
    key and a no-diff land stamps JSON null (``->>`` reads both as SQL
    NULL) — neither blocks. ALL jobs are read, not the newest: a later
    failed re-land must not mask an earlier land."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta FROM refs
             WHERE kind = 'job' AND retired_at IS NULL
               AND meta->>'job_type' = 'land_slice'
               AND meta->>'landed_sha' IS NOT NULL
             ORDER BY ref_id
            """
        ).fetchall()
    out: list[tuple[str, int, int]] = []
    for ref_id, meta in rows:
        m = meta if isinstance(meta, dict) else {}
        sha = m.get("landed_sha")
        raw = (m.get("params") or {}).get("todo_id")
        if not isinstance(sha, str) or not sha.strip():
            continue
        if not isinstance(raw, (int, str)):
            continue
        try:
            todo_id = int(raw)
        except ValueError:
            continue
        out.append((sha.strip().lower(), int(ref_id), todo_id))
    return out


def _land_tds(subject: str) -> list[int]:
    """The ``td<id>`` handles in a squash subject's trailing handle list —
    the todos a hand land links itself to (``land_slice.with_handle``
    writes the same list)."""
    m = _LAND_HANDLES_RE.search(subject)
    if m is None:
        return []
    return [int(x) for x in _LAND_TD_RE.findall(m.group(1))]


@dataclass(frozen=True)
class _GateLand:
    """One unit the engine-review gate judges: a linked todo of an
    engine commit in the cut range, or the commit itself (``todo_id``
    ``None``) when nothing links it."""

    sha12: str
    todo_id: int | None
    job_id: int | None
    committed_at: datetime | None

    @property
    def ask_key(self) -> str:
        """The ``release-review-wait:`` tag tail — one ask per unit."""
        return f"td{self.todo_id}" if self.todo_id is not None else f"sha:{self.sha12}"

    @property
    def land(self) -> str:
        """The land's name in blocker and ask text."""
        return f"td{self.todo_id}" if self.todo_id is not None else self.sha12

    @property
    def label(self) -> str:
        """``land`` plus the landed job when one claims it."""
        job = f" (jo{self.job_id})" if self.job_id is not None else ""
        return f"{self.land}{job}"


def _gate_lands(
    store: Any, rel: dict[str, Any] | None = None
) -> tuple[list[_GateLand], str, list[str]]:
    """``(units, pin, scanned)`` — the gate's units, oldest land first,
    the head they were scanned at and ``scanned`` every first-parent sha
    of the scope, oldest first. The scope is ``origin/prod..<pin>`` with
    ``pin`` the pinned main head (:func:`_git_lands`), or with ``rel`` a
    release's own commits (:func:`_release_lands`) with ``pin`` its cut
    sha; ``scanned`` is then what ``release/r<N>`` carries. A unit is
    every commit in the scope whose diff touches
    :data:`lanes.ENGINE_PATHS`, stitched to its linked todos — the
    succeeded ``land_slice`` job whose ``meta.landed_sha`` matches (the
    stamped path, kept as the fast lookup) or the ``td<id>`` handles in
    the subject's trailing list. A commit neither links is its own unit,
    and a sha-keyed reviewer minted for it (or for a linked todo with no
    live plan root) covers every unit of the commit."""
    from precis.plans import lanes as _lanes

    if rel is None:
        lands, pin = _git_lands()
    else:
        lands = _release_lands(rel)
        pin = str(rel.get("cut_sha") or rel.get("sha") or "").lower()
    stamped = _landed_jobs(store)
    out: list[_GateLand] = []
    for land in lands:
        sha12 = land.sha[:12]
        jobs = [
            (job_id, todo_id)
            for sha, job_id, todo_id in stamped
            if land.sha.startswith(sha) or sha.startswith(sha12)
        ]
        todos = {todo_id for _job_id, todo_id in jobs} | set(_land_tds(land.subject))
        job_id = jobs[0][0] if jobs else None
        engine = bool(_lanes.engine_hits(land.paths))
        if not engine:
            continue
        if todos:
            out.extend(
                _GateLand(sha12, t, job_id, land.committed_at) for t in sorted(todos)
            )
        else:
            out.append(_GateLand(sha12, None, None, land.committed_at))
    return out, pin, [land.sha for land in lands]


#: The sha a ``landed: <sha>`` comment records — land-notify's land
#: record; ``landed: nothing``/``landed: pending`` deliberately do not
#: match (the newest-wins land record, ``gripe_autoclose._LANDED_RE``'s
#: shape).
_FIX_LANDED_RE = re.compile(r"^\s*landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)


def _fix_landed_sha(store: Any, fix_id: int) -> str | None:
    """The sha the fix todo's NEWEST ``landed:`` comment records — the
    corrective commit the cut must carry for the clearance to hold —
    else ``None``: no ``landed:`` comment, or the newest records no sha
    (newest wins; ``landed: nothing`` after a real sha is no record)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT c.text FROM chunks c
             WHERE c.ref_id = %s AND c.chunk_kind = 'todo_comment'
               AND c.retired_at IS NULL AND c.text LIKE 'landed:%%'
             ORDER BY c.ord DESC LIMIT 1
            """,
            (fix_id,),
        ).fetchone()
    if row is None:
        return None
    m = _FIX_LANDED_RE.match(str(row[0]))
    return m.group(1).lower() if m is not None else None


def _status_of(store: Any, ref_id: int) -> str:
    """The ref's STATUS tag — ``open`` when it carries none."""
    return next(
        (str(t.value) for t in store.tags_for(ref_id) if t.prefix == "STATUS"),
        "open",
    )


def _parent_of(store: Any, ref_id: int) -> int | None:
    """The ref's ``parent_id`` — ``None`` when absent or the row is
    gone."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    return None if row is None else row[0]


def _root(store: Any, todo_id: int) -> tuple[int, str]:
    """``(top todo id, its STATUS tag)`` walking ``parent_id`` up —
    capped at 25 hops; a ``parent_id`` cycle, or a link pointing at a
    retired or non-todo ref mid-chain, ends the walk with status
    ``gone`` (fail closed: an unreadable root is a dead root)."""
    top = todo_id
    seen = {todo_id}
    for _ in range(25):
        with store.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT r.parent_id, p.kind, p.retired_at
                  FROM refs r LEFT JOIN refs p ON p.ref_id = r.parent_id
                 WHERE r.ref_id = %s AND r.retired_at IS NULL
                """,
                (top,),
            ).fetchone()
        if row is None:
            return top, "gone"  # `top` vanished mid-walk
        parent, p_kind, p_retired = row
        if parent is None:
            break  # `top` is the root
        if parent in seen or p_retired is not None:
            return top, "gone"
        if p_kind != "todo":
            break  # a non-todo parent: `top` is the top todo
        seen.add(parent)
        top = parent
    return top, _status_of(store, top)


def _answered_wait_ask(store: Any, ask_key: str) -> bool:
    """``True`` when the ``release-review-wait:<ask_key>`` ask carries
    an ``answer:`` — a comment starting ``answer:`` (the ``answer:``
    convention — ``plans.release`` ~296, ``handlers.todo`` ~66), or a
    line starting ``answer:`` in its details body:
    ``precis-todo-tree-help``'s ask close writes it there
    (``edit(mode='replace', body='…\\nAnswer: …')``) and it counts the
    same (gr645663). Either clears that one land."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
              JOIN chunks c ON c.ref_id = r.ref_id
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND c.retired_at IS NULL
               AND (
                 (c.chunk_kind = 'todo_comment'
                  AND lower(ltrim(c.text)) LIKE 'answer:%%')
                 OR (c.chunk_kind = 'todo_body'
                     AND EXISTS (
                       SELECT 1
                         FROM unnest(string_to_array(c.text, chr(10))) AS u(ln)
                        WHERE lower(ltrim(u.ln)) LIKE 'answer:%%'))
               )
             LIMIT 1
            """,
            (f"{_REVIEW_WAIT_TAG_PREFIX}:{ask_key}",),
        ).fetchone()
    return row is not None


def _open_wait_ask_id(store: Any, ask_key: str) -> int | None:
    """The id of the still-answerable ``release-review-wait:<ask_key>``
    ask — a live todo whose STATUS is not in :data:`_WAIT_ASK_CLOSED` —
    or ``None``. A closed (or retired) ask holds nothing whether or not
    it was answered; an answered one cleared the land upstream."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rs JOIN tags s USING (tag_id)
                  WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS'
                    AND s.value = ANY(%s))
             ORDER BY r.ref_id
             LIMIT 1
            """,
            (f"{_REVIEW_WAIT_TAG_PREFIX}:{ask_key}", list(_WAIT_ASK_CLOSED)),
        ).fetchone()
    return None if row is None else int(row[0])


def _sha_keyed(meta: dict[str, Any]) -> bool:
    """``True`` when the reviewer's ``generate.from`` is ``sha:<sha>`` —
    a hand-landed engine commit keyed on the sha (gr597625), not a
    ``td<N>`` land."""
    return (
        _REVIEW_FROM_SHA_RE.fullmatch(
            str((meta.get("generate") or {}).get("from") or "")
        )
        is not None
    )


def _review_outcome(
    store: Any, rid: int, meta: dict[str, Any], land: str
) -> tuple[str, str, tuple[str, ...]]:
    """``(outcome, blocker line, requires)`` for one covering reviewer
    on the ``land`` (``td<N>`` or the sha12) — ``satisfied`` clears it,
    ``requires`` naming the corrective commit shas the selected cut
    must CARRY for the clearance to hold: a FIX's fix commit, a
    sha-keyed REVERT's revert (td601005 — the review being done is not
    enough when the cut ends ahead of the land but behind its
    corrective commit); ``waiting`` is a live reviewer still holding
    it (the line is the blocker); ``hopeless`` can never resolve, so
    the wait ask files at once."""
    status = _status_of(store, rid)
    base = f"engine review td{rid} on the {land} land"
    if status in TERMINAL_TODO_STATUSES or status == "cancelled":
        review = meta.get("review")
        review = review if isinstance(review, dict) else {}
        verdict = review.get("verdict")
        if verdict == "LAND":
            return "satisfied", "", ()
        if verdict == "FIX" or (verdict == "REVERT" and _sha_keyed(meta)):
            task = review.get("task")
            if isinstance(task, int):
                noun = "revert" if verdict == "REVERT" else "fix"
                sha = _fix_landed_sha(store, task)
                if sha is not None:
                    return "satisfied", "", (sha,)
                task_status = _status_of(store, task)
                if (
                    task_status not in TERMINAL_TODO_STATUSES
                    and task_status != "cancelled"
                ):
                    return (
                        "waiting",
                        (
                            f"{base} was {verdict}; its {noun} td{task} is not "
                            f"landed yet — next: let td{task} land"
                        ),
                        (),
                    )
                return (
                    "hopeless",
                    (
                        f"{base} was {verdict}; its {noun} td{task} ended "
                        f"{task_status} with no `landed: <sha>` record — next: "
                        "acknowledge the review-wait ask or land a fix"
                    ),
                    (),
                )
        return (
            "hopeless",
            (
                f"{base} closed {status} without a usable verdict; the wait "
                "can never resolve — next: acknowledge the review-wait ask "
                f"or re-review {land}"
            ),
            (),
        )
    root_id, root_status = _root(store, rid)
    if root_status in TERMINAL_TODO_STATUSES or root_status in ("cancelled", "gone"):
        return (
            "hopeless",
            (
                f"{base} is open under a {root_status} root (td{root_id}); it "
                f"will never finish — next: close td{rid} or acknowledge the "
                "review-wait ask"
            ),
            (),
        )
    return (
        "waiting",
        (
            f"{base} is {status}; the release waits — next: let td{rid} finish or close it"
        ),
        (),
    )


def _file_review_wait_ask(
    store: Any,
    ask_key: str,
    *,
    land: str,
    rid: int | None = None,
    job_id: int | None = None,
    dry_run: bool,
    reason: str | None = None,
) -> tuple[str, int | None]:
    """The review-wait ask: one OPEN ``ask-user`` ruling todo per gate
    unit (a landed todo, or a sha-keyed hand land), deduped by its
    ``release-review-wait:<ask_key>`` tag over still-answerable todos
    inside one locked transaction — same shape as
    :func:`_file_stuck_ask`. Answering it clears that one land; one
    closed without an ``answer:`` (comment or body line, gr645663) is
    refiled. ``land`` names the
    land in the text (``td<N>`` or the sha12), ``job_id`` the landed job
    when one claims it.

    Returns ``(note, ask_id)`` — the live ask's todo id (filed or
    deduped) so the blocker can name it, ``None`` on dry-run/factory-off.
    """
    from precis.workers.job_types import run_slice

    tag = f"{_REVIEW_WAIT_TAG_PREFIX}:{ask_key}"
    job = f" (jo{job_id})" if job_id is not None else ""
    if rid is not None:
        what = f"engine review td{rid} on the {land} land"
        title = (
            f"engine review td{rid} on the {land} land will not "
            "resolve on its own — acknowledge it, or is this a false alarm?"
        )
        body = (
            f"The engine-review gate (td574937 E1) holds the deploy of "
            f"the release carrying the {land} engine land while it is "
            f"uncleared; reviewer td{rid} cannot settle it on its own. "
            "This ask is the only write the gate makes; answering it "
            "executes nothing — a comment starting `answer:` "
            "('acknowledged' or 'false alarm') just closes it, which "
            "clears this one land."
        )
    else:
        what = f"the {land} engine land{job}"
        title = (
            f"engine land {land}{job} has no reviewer — "
            "acknowledge it, or is this a false alarm?"
        )
        why = f" The gate could not mint one itself: {reason}." if reason else ""
        body = (
            f"The engine-review gate (td574937 E1) holds the deploy of "
            f"the release carrying the {land} engine land{job} while no "
            f"reviewer covers it — fix, revert and "
            f"plain lands mint none.{why} This ask is the only write the "
            "gate makes; answering it executes nothing — a comment "
            "starting `answer:` ('acknowledged' or 'false alarm') just "
            "closes it, which clears this one land."
        )
    context = (
        f"{what} holds the deploy of the release carrying it until "
        "cleared — answering executes nothing"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["acknowledged", "false alarm"],
            "blocks": "nothing",
            "context": context[:600],
        }
    }
    check_ask_meta(meta)
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_REVIEW_WAIT_LOCK_KEY,))
        dupes = conn.execute(
            """
            SELECT r.ref_id,
                   EXISTS (
                     SELECT 1 FROM ref_tags rs JOIN tags s USING (tag_id)
                      WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS'
                        AND s.value = ANY(%s)) AS closed
              FROM refs r
                JOIN ref_tags rt ON rt.ref_id = r.ref_id
                JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             ORDER BY r.ref_id
            """,
            (list(_WAIT_ASK_CLOSED), tag),
        ).fetchall()
        open_ids = [int(r[0]) for r in dupes if not r[1]]
        closed_ids = [int(r[0]) for r in dupes if r[1]]
        if open_ids:
            return (
                f"engine review: ask for {land} already filed (td{open_ids[0]})",
                open_ids[0],
            )
        if dry_run or run_slice.factory_off():
            return f"engine review: would file {tag} ({title[:60]})", None
        ref = store.insert_ref(
            kind="todo", slug=None, title=title, meta=meta, conn=conn
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", tag):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    verb = "filed"
    if closed_ids:
        verb = f"refiled after td{', td'.join(str(i) for i in closed_ids)} closed"
    return f"engine review: {verb} {tag} as td{ref.id}", int(ref.id)


def _todo_row(store: Any, ref_id: int) -> tuple[str, Any] | None:
    """``(STATUS tag, created_at)`` of a live todo, or ``None`` when the
    row is retired, missing or not a todo."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM refs WHERE ref_id = %s AND kind = 'todo' "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    return _status_of(store, ref_id), row[0]


def _plan_root_tag(store: Any, todo_id: int) -> int | None:
    """The id in td<todo_id>'s own ``plan:<id>`` tag — the root the plan
    tick reads it under (``plan_dispatch._candidate_roots``) — or
    ``None``. The tag decides, never the parent chain: minting a
    ``plan:`` tag under a non-plan epic would make the epic a plan
    root and its other open leaf todos dispatchable ``run_slice``
    tasks (td584381 S1b)."""
    for tag in store.tags_for(todo_id):
        value = str(tag.value)
        if (
            tag.namespace == "open"
            and value.startswith("plan:")
            and value[5:].isdigit()
        ):
            return int(value[5:])
    return None


def _reviewer_hang(
    store: Any, todo_id: int, lane_table: tuple[Any, ...]
) -> tuple[int | None, str | None]:
    """``(plan_root, None)`` when a reviewer minted for the td<todo_id>
    land could actually run, else ``(None, refusal)`` — the reason the
    ask's body names (td584381 S1b).

    Unrunnable means: no enabled lane in ``lane_table`` fits the
    ``review`` task class (``plan_dispatch._review_lane`` could never
    place it); td<todo_id> carries no ``plan:<id>`` tag of its own —
    no plan root to hang a reviewer under (and one parented to the
    reviewed todo itself would read as its own review target in
    ``_review_targets``); or the root the tag names is terminal,
    cancelled or gone — an open reviewer under a dead root is judged
    hopeless by this same gate, so minting it burns a review lane and
    asks anyway. The caller does not ask on a root refusal: it mints
    the sha-keyed reviewer under a fresh root instead (:func:`_sha_hang`)."""
    from precis.plans import templates as _templates

    if not any(
        ln.enabled and ln.fits(_templates.REVIEW_TASK_CLASS) for ln in lane_table
    ):
        return None, "no enabled lane fits the 'review' task class"
    root_id = _plan_root_tag(store, todo_id)
    if root_id is None or root_id == todo_id:
        return None, (
            f"td{todo_id} carries no plan:<id> tag — no plan root to "
            "hang a reviewer under"
        )
    row = _todo_row(store, root_id)
    root_status = "gone" if row is None else row[0]
    if root_status in TERMINAL_TODO_STATUSES or root_status in ("cancelled", "gone"):
        return None, (
            f"the plan root td{root_id} is {root_status} — a reviewer under "
            "a dead root is judged hopeless by this gate and can never settle"
        )
    return root_id, None


def _mint_engine_reviewer(
    store: Any, todo_id: int, plan_root: int
) -> tuple[str, int, bool]:
    """Mint the td<todo_id> land's covering reviewer under td<plan_root>;
    returns ``(note, reviewer id, fresh)`` — ``fresh`` is False when the
    locked re-read found a live reviewer already covering, a dedupe
    that minted nothing.

    One per landed todo: the dedupe re-read (a live reviewer whose
    ``generate.from`` is ``td<todo_id>``) and the insert share one
    :data:`_REVIEW_MINT_LOCK_KEY` advisory-locked transaction
    (:func:`templates.mint_reviewer`'s own ``store.tx()`` joins it as a
    savepoint), so two concurrent gate ticks mint at most one — a tick
    that lost the race reads the winner's row and reports it instead of
    adding a second."""
    from precis.plans import templates as _templates

    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_REVIEW_MINT_LOCK_KEY,))
        dup = conn.execute(
            """
            SELECT ref_id FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->'generate'->>'role' = 'reviewer'
               AND meta->'generate'->>'from' = %s
             ORDER BY ref_id
             LIMIT 1
            """,
            (f"td{todo_id}",),
        ).fetchone()
        if dup is not None:
            return (
                f"engine review: reviewer td{int(dup[0])} already covers td{todo_id}",
                int(dup[0]),
                False,
            )
        rid = _templates.mint_reviewer(store, plan_root, todo_id)
    return f"engine review: minted reviewer td{rid} for td{todo_id}", rid, True


def _sha_hang(lane_table: tuple[Any, ...]) -> str | None:
    """The only refusal a sha-keyed mint can hit before it starts —
    the sha land's reviewer mints its own fresh phase root (always
    live), so just the lane fit decides (the sha half of
    :func:`_reviewer_hang`, gr597625). It is also the fallback gate
    for a td-linked unit whose own hang refused on the root: a lane
    refusal is the same string either helper returns, a root refusal
    never blocks the sha mint."""
    from precis.plans import templates as _templates

    if not any(
        ln.enabled and ln.fits(_templates.REVIEW_TASK_CLASS) for ln in lane_table
    ):
        return "no enabled lane fits the 'review' task class"
    return None


def _mint_sha_reviewer(store: Any, sha12: str) -> tuple[str, int, bool]:
    """Mint the sha-keyed reviewer over a hand-landed engine commit —
    nothing links it, or the linked todo has no live plan root;
    returns ``(note, reviewer id, fresh)`` — one per commit: the
    locked re-read (a live reviewer whose ``generate.from`` is
    ``sha:<sha12>`` — either direction's prefix, so a fuller sha key
    also dedupes) and the insert share the
    :data:`_REVIEW_MINT_LOCK_KEY` advisory-locked transaction
    (:func:`templates.mint_sha_reviewer`'s ``store.tx()`` joins it as a
    savepoint). The dup scan matches only well-formed ``sha:<hex>``
    keys: a malformed ``from`` (``sha:``, a bare prefix) would
    prefix-match every key here while the coverage read
    (:data:`_REVIEW_FROM_SHA_RE`) counts it as covering nothing — a
    mint that never lands and a land that never unblocks."""
    from precis.plans import templates as _templates

    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_REVIEW_MINT_LOCK_KEY,))
        dup = conn.execute(
            """
            SELECT ref_id FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->'generate'->>'role' = 'reviewer'
               AND lower(meta->'generate'->>'from') ~ '^sha:[0-9a-f]{7,40}$'
               AND (%s LIKE lower(meta->'generate'->>'from') || '%%'
                    OR lower(meta->'generate'->>'from') LIKE %s || '%%')
             ORDER BY ref_id
             LIMIT 1
            """,
            (f"sha:{sha12}", f"sha:{sha12}"),
        ).fetchone()
        if dup is not None:
            return (
                f"engine review: reviewer td{int(dup[0])} already covers {sha12}",
                int(dup[0]),
                False,
            )
        rid = _templates.mint_sha_reviewer(store, sha12)
    return f"engine review: minted reviewer td{rid} for {sha12}", rid, True


@dataclass
class _LandVerdict:
    """One gate unit's verdict (:func:`_gate_lands`).

    ``cleared`` lands never hold a deploy for their own review's sake;
    ``line`` is the blocker an uncleared one carries. ``requires``
    names corrective commit shas the release must CARRY for the
    clearance to hold — a FIX's fix, a sha-keyed REVERT's revert
    (td601005; :func:`deploy_review_blockers`)."""

    unit: _GateLand
    cleared: bool
    line: str = ""
    requires: tuple[str, ...] = ()


def _same_sha(a: str, b: str) -> bool:
    """Two sha spellings name one commit — either is the other's prefix
    (case-blind; an empty spelling names nothing). The one prefix rule
    the corrective chain and :func:`deploy_review_blockers` share."""
    a, b = a.lower(), b.lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _holds_own(store: Any, rid: int, meta: dict[str, Any]) -> bool:
    """The covering reviewer judges the unit itself, over the corrective
    chain (gr611217): it recorded a ``meta.review.verdict``, or it is
    still running — not terminal, not cancelled — so a late FIX/REVERT
    cannot arrive after the release deployed."""
    review = meta.get("review")
    if isinstance(review, dict) and review.get("verdict"):
        return True
    status = _status_of(store, rid)
    return status not in TERMINAL_TODO_STATUSES and status != "cancelled"


def _chain_corrective(
    store: Any,
    unit: _GateLand,
    carried: set[tuple[str, int]],
    by_task: dict[int, list[tuple[int, dict[str, Any]]]],
) -> bool:
    """``True`` when ``unit`` is the recorded corrective of a cleared
    FIX/REVERT (gr611217): a ``(sha, fix task)`` pair an earlier unit of
    this pass cleared on (``carried``), or the ``landed:`` sha of the
    linked todo when a satisfied reviewer names that todo as its
    ``review.task`` — the reviewed land may already sit on prod, outside
    the scan. The task must match too: a commit linking the fix todo
    AND another todo (``(td10, td11)``) clears only its fix-todo unit,
    the other change in it still needs its own review; a unit nothing
    links (``todo_id`` ``None``) is the commit itself."""
    if any(
        _same_sha(unit.sha12, s) and unit.todo_id in (None, task) for s, task in carried
    ):
        return True
    if unit.todo_id is None:
        return False
    for rid, meta in by_task.get(unit.todo_id, ()):
        outcome, _line, requires = _review_outcome(store, rid, meta, unit.land)
        if outcome == "satisfied" and any(_same_sha(unit.sha12, r) for r in requires):
            return True
    return False


def _engine_verdicts(
    store: Any,
    *,
    rel: dict[str, Any] | None = None,
    now: datetime | None = None,
    file_asks: bool = False,
    dry_run: bool = False,
) -> tuple[list[_LandVerdict] | None, list[str], str | None, str | None, list[str]]:
    """``(verdicts, notes, fatal, pin, scanned)`` — the engine-review
    gate's per-land verdict plus the pass's side effects over the scope
    :func:`_gate_lands` reads: ``origin/prod..<pin>`` by default — ``pin``
    the ``origin/main`` head the scan resolved, ``scanned`` its
    first-parent shas oldest first, the one commit snapshot the poll
    pins its compare and recorded target to (td601005) and the ordering
    it falls back to when the compare's ``commits`` list is empty
    (td601285) — or, with ``rel`` (a plan root's ``meta.release``), the
    release's own commits. ``fatal`` non-``None`` is the fail-closed
    line any read error returns (``verdicts`` and ``pin`` are ``None``
    then, ``scanned`` empty).

    Per gate unit (:func:`_gate_lands`), oldest land first: an answered
    ``release-review-wait:<key>`` ask clears a gated unit; so does being
    a cleared FIX/REVERT's recorded corrective commit while no covering
    reviewer reached a verdict (:func:`_chain_corrective`, gr611217); no
    covering reviewer
    (``generate.from`` naming the landed todo or its parent, or the
    commit's ``sha:<sha12>`` — sha-keyed covers every unit of the
    commit) mints one —
    under the root a linked todo's own ``plan:<id>`` tag names, under a
    fresh phase root for a sha-keyed land — a commit nothing links, or
    a linked todo with no live plan root — at most
    :data:`_MINT_PER_PASS` a pass — the mint is covering next tick —
    and files the ask only when minting cannot run or ``plan_dispatch``
    itself is switched off (td584381 S1b); covering reviewers are judged
    satisfied > waiting > hopeless — a waiting land files its ask once
    the NEWER of the land and its newest covering reviewer is
    :data:`REVIEW_WAIT_AFTER` old, a hopeless one at once. An ask
    closed without an ``answer:`` (comment or body line, gr645663)
    does not dedupe — the next filing pass refiles it — and blocker
    lines name the live ask's id.
    ``file_asks`` limits the mint-and-ask writes to the pass
    (``release_deploy`` asks for nothing). Any exception anywhere returns
    a single fail-closed blocker.
    """
    from precis.plans import lanes as _lanes
    from precis.workers.job_types import plan_dispatch as _plan_dispatch

    now = now or datetime.now(UTC)
    notes: list[str] = []
    verdicts: list[_LandVerdict] = []
    pin: str | None = None
    scanned: list[str] = []
    try:
        lane_table = _lanes.load_lanes(store) if file_asks else ()
        # plan_dispatch's own kill switch — the env OR a service_config
        # prio-0 row, read through its helper, never re-checked here:
        # a mint nobody dispatches would sit "waiting" and hold the ask
        # off for 24 h, so "off" takes the pre-S1b path and asks at once.
        factory_reason = _plan_dispatch.read_only_reason(store) if file_asks else None
        minted = 0
        by_from: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        by_sha: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        by_task: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        carried: set[tuple[str, int]] = set()  # (corrective sha, fix task)
        for rid, meta in _reviewer_rows(store):
            review = meta.get("review")
            task = review.get("task") if isinstance(review, dict) else None
            if isinstance(task, int) and not isinstance(task, bool):
                by_task.setdefault(task, []).append((rid, meta))
            gen = meta.get("generate")
            if not isinstance(gen, dict):
                continue
            src = str(gen.get("from") or "")
            m = _REVIEW_FROM_RE.fullmatch(src)
            if m is not None:
                by_from.setdefault(int(m.group(1)), []).append((rid, meta))
                continue
            m = _REVIEW_FROM_SHA_RE.fullmatch(src)
            if m is not None:
                by_sha.setdefault(m.group(1).lower(), []).append((rid, meta))
        units, pin, scanned = _gate_lands(store, rel)
        for unit in units:
            verdict = _LandVerdict(unit, cleared=False)
            if _answered_wait_ask(store, unit.ask_key):
                verdict.cleared = True
                verdicts.append(verdict)
                continue
            covering: list[tuple[int, dict[str, Any]]] = []
            if unit.todo_id is not None:
                parent = _parent_of(store, unit.todo_id)
                covering += [
                    r
                    for fid in (unit.todo_id, parent)
                    if fid is not None
                    for r in by_from.get(fid, ())
                ]
            # A sha-keyed reviewer covers every unit of its commit —
            # including a td-linked one it was minted for when the linked
            # todo had no live plan root to hang under (td599062).
            covering += [
                r
                for key, rs in by_sha.items()
                if _same_sha(unit.sha12, key)
                for r in rs
            ]
            # gr611217: a FIX's own fix land (a REVERT's revert) rides the
            # chain that cleared it — no second review, no mint — unless
            # a covering reviewer reached a verdict on it or still runs.
            if not any(
                _holds_own(store, r, m) for r, m in covering
            ) and _chain_corrective(store, unit, carried, by_task):
                verdict.cleared = True
                verdicts.append(verdict)
                continue
            if not covering:
                ask_id = _open_wait_ask_id(store, unit.ask_key)
                if file_asks:
                    plan_root: int | None = None
                    refusal: str | None = None
                    mintable = False
                    if factory_reason is None:
                        if unit.todo_id is not None:
                            plan_root, refusal = _reviewer_hang(
                                store, unit.todo_id, lane_table
                            )
                            mintable = plan_root is not None
                        if plan_root is None:
                            # No linked todo — or the linked todo has no
                            # live plan root to hang under (the briefed
                            # case): the sha-keyed reviewer mints its own
                            # fresh root, so only the lane fit can refuse
                            # — a lane refusal is _reviewer_hang's too.
                            refusal = _sha_hang(lane_table)
                            mintable = refusal is None
                    if mintable and not dry_run:
                        if minted >= _MINT_PER_PASS:
                            verdict.line = (
                                f"engine land {unit.label} has "
                                "no reviewer; reviewer mint deferred; the "
                                "release waits — next: a later tick mints it"
                            )
                            verdicts.append(verdict)
                            continue
                        try:
                            if unit.todo_id is not None and plan_root is not None:
                                note, minted_id, fresh = _mint_engine_reviewer(
                                    store, unit.todo_id, plan_root
                                )
                            else:
                                note, minted_id, fresh = _mint_sha_reviewer(
                                    store, unit.sha12
                                )
                        except Exception as exc:
                            log.warning(
                                "release_poll: reviewer mint for %s raised",
                                unit.land,
                                exc_info=True,
                            )
                            refusal = (
                                f"the reviewer mint raised {type(exc).__name__}: {exc}"
                            )
                        else:
                            minted += int(fresh)
                            notes.append(note)
                            state = (
                                f"td{minted_id} was minted for it"
                                if fresh
                                else f"reviewer td{minted_id} is running"
                            )
                            verdict.line = (
                                f"engine land {unit.label} had "
                                f"no reviewer; {state} — the release waits "
                                f"— next: let td{minted_id} run"
                            )
                            verdicts.append(verdict)
                            continue
                    elif mintable:
                        notes.append(
                            f"engine review: would mint a reviewer for {unit.land}"
                        )
                        verdict.line = (
                            f"engine land {unit.label} has no "
                            "reviewer; the release waits — next: a live "
                            "tick with the factory on mints one"
                        )
                        verdicts.append(verdict)
                        continue
                    note, filed_id = _file_review_wait_ask(
                        store,
                        unit.ask_key,
                        land=unit.land,
                        job_id=unit.job_id,
                        dry_run=dry_run,
                        reason=refusal,
                    )
                    notes.append(note)
                    if filed_id is not None:
                        ask_id = filed_id
                if ask_id is not None:
                    next_step = f"answer the review-wait ask td{ask_id}"
                elif file_asks:
                    next_step = f"answer the review-wait ask on {unit.land}"
                else:
                    next_step = "the review gate holds the deploy"
                verdict.line = (
                    f"engine land {unit.label} has no "
                    f"reviewer; the release waits — next: {next_step}"
                )
                verdicts.append(verdict)
                continue
            waiting: tuple[int, str] | None = None
            hopeless: tuple[int, str] | None = None
            satisfied = False
            for rid, meta in covering:
                outcome, line, requires = _review_outcome(store, rid, meta, unit.land)
                if outcome == "satisfied":
                    verdict.requires = requires
                    # ``requires`` is non-empty only for a FIX/REVERT
                    # with an int ``review.task`` (:func:`_review_outcome`).
                    task = (meta.get("review") or {}).get("task")
                    if isinstance(task, int):
                        carried.update((r, task) for r in requires)
                    satisfied = True
                    break
                if outcome == "waiting" and waiting is None:
                    waiting = (rid, line)
                elif outcome == "hopeless" and hopeless is None:
                    hopeless = (rid, line)
            if satisfied:
                verdict.cleared = True
                verdicts.append(verdict)
                continue
            pick = waiting or hopeless
            if pick is None:
                verdict.cleared = True
                verdicts.append(verdict)
                continue
            rid, line = pick
            if hopeless is not None and waiting is None:
                due = True  # can never resolve — ask at once
            else:
                # A waiting land's clock runs from the NEWER of the land
                # and its newest covering reviewer: a reviewer minted
                # on a stale land gets its own 24 h before the ask, and
                # a missing stamp is not due (td584381 S1b).
                row = _todo_row(store, max(r for r, _m in covering))
                rstamp = row[1] if row is not None else None
                if rstamp is None:
                    due = False
                else:
                    base = max(
                        s if s.tzinfo is not None else s.replace(tzinfo=UTC)
                        for s in (unit.committed_at, rstamp)
                        if s is not None
                    )
                    due = now - base >= REVIEW_WAIT_AFTER
            ask_id = _open_wait_ask_id(store, unit.ask_key)
            if file_asks and due:
                note, filed_id = _file_review_wait_ask(
                    store, unit.ask_key, land=unit.land, rid=rid, dry_run=dry_run
                )
                notes.append(note)
                if filed_id is not None:
                    ask_id = filed_id
            if ask_id is not None:
                line = f"{line} — the open ask is td{ask_id}"
            verdict.line = line
            verdicts.append(verdict)
    except Exception as exc:
        log.warning("release_poll: engine review gate unreadable", exc_info=True)
        return (
            None,
            notes,
            f"engine review gate unreadable ({type(exc).__name__}); "
            "next: retry next tick — fail closed",
            None,
            [],
        )
    return verdicts, notes, None, pin, scanned


def engine_review_blockers(
    store: Any,
    *,
    rel: dict[str, Any] | None = None,
    now: datetime | None = None,
    file_asks: bool = False,
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    """``(blocker lines, notes)`` — the engine-review gate's verdict over
    main ahead of prod, or over the release ``rel`` (a plan root's
    ``meta.release``): every uncleared engine land's line, or the single
    fail-closed line. The poll's mint-and-ask reads pass ``file_asks``;
    :func:`deploy_review_blockers` is the hold."""
    verdicts, notes, fatal, _pin, _scanned = _engine_verdicts(
        store, rel=rel, now=now, file_asks=file_asks, dry_run=dry_run
    )
    if fatal is not None:
        return [fatal], notes
    return [v.line for v in verdicts or () if not v.cleared], notes


def deploy_review_blockers(store: Any, todo_id: int) -> list[str]:
    """Why the release ``todo_id``'s plan cut may not deploy yet, else
    ``[]`` — ``release_deploy``'s hold (release train, Reto 2026-11-12).

    The scope is the release itself (:func:`_release_lands`): the cut's
    recorded range plus the commits on ``release/r<N>`` after the cut. A
    land on main after the cut is the next release's and holds nothing.
    Every engine land in scope must be cleared, and a FIX/REVERT
    clearance holds only once its corrective commit (the fix todo's
    ``landed:`` sha) is on ``release/r<N>`` — a fix landed on main alone
    does not ship with this release (td601005). No cut recorded is no
    hold here (the approval check refuses that deploy first); any read
    error is the single fail-closed line. Files nothing: the poll's
    open-pass tick mints the reviewers and asks for this scope."""
    rel = _release.release_of(store, todo_id)
    if rel is None:
        return []
    verdicts, _notes, fatal, _pin, scanned = _engine_verdicts(store, rel=rel)
    if fatal is not None:
        return [fatal]
    rn = rel.get("rN")
    lines = [v.line for v in verdicts or () if not v.cleared]
    for v in verdicts or ():
        missing = [
            r[:12]
            for r in v.requires
            if v.cleared and not any(_same_sha(r, s) for s in scanned)
        ]
        if missing:
            lines.append(
                f"engine land {v.unit.label} {_CARRY_MARK} "
                f"({', '.join(missing)}) that is not on release/{rn} — next: "
                f"land the fix on release/{rn} or answer the review-wait ask"
            )
    return lines


def _commit_shas(cmp_: Any) -> list[str]:
    """The ``compare prod...<pin>`` payload's commits as full shas,
    oldest first — the window the pass records as ``upto``."""
    out: list[str] = []
    if not isinstance(cmp_, dict):
        return out
    for c in cmp_.get("commits") or []:
        if isinstance(c, dict) and c.get("sha"):
            out.append(str(c["sha"]).lower())
    return out


def _kept_hold_lines(lines: list[str]) -> list[str]:
    """The hold's lines as recorded (gr651885): every holder — "not
    landed", "doing", review-wait — ahead of the "cleared on a corrective
    commit" carry-notes, at most :data:`CUT_HOLD_LINES_CAP` of them plus a
    ``+N more`` count. A first-five cut once hid r291's real holders
    behind five carry-notes."""
    carry = [ln for ln in lines if _CARRY_MARK in ln]
    ordered = [ln for ln in lines if _CARRY_MARK not in ln] + carry
    kept = [ln[:300] for ln in ordered[:CUT_HOLD_LINES_CAP]]
    if len(ordered) > CUT_HOLD_LINES_CAP:
        kept.append(f"+{len(ordered) - CUT_HOLD_LINES_CAP} more")
    return kept


def note_cut_hold(
    store: Any,
    todo_id: int,
    lines: list[str] | None,
    *,
    now: datetime | None = None,
) -> None:
    """Record (``lines``) or clear (``None``/empty) the first cut's hold
    on the plan root's ``meta.cut_hold = {since, lines}`` (gr611217) —
    ``release_cut`` records the refusal a wait-class cut died on and
    clears it once a cut succeeds. ``since`` is kept while the hold
    persists, so :func:`_cut_hold_ask` measures the whole hold; a
    cleared hold resets it. The lines are :func:`_kept_hold_lines`. A
    write failure is a log line, never the cut's verdict."""
    try:
        root = _release.plan_root(store, todo_id)
        ref = store.get_ref(kind="todo", id=root)
        prior = (ref.meta or {}).get("cut_hold") if ref is not None else None
        if not lines:
            if prior:
                store.stamp_ref_meta(root, {"cut_hold": None})
            return
        since = prior.get("since") if isinstance(prior, dict) else None
        if not since:
            since = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.stamp_ref_meta(
            root, {"cut_hold": {"since": since, "lines": _kept_hold_lines(lines)}}
        )
    except Exception:
        log.warning(
            "release_poll: cut-hold stamp for td%s failed", todo_id, exc_info=True
        )


def _cut_hold_ask(
    store: Any, root_id: int, *, now: datetime, dry_run: bool
) -> str | None:
    """The open pass's first cut a refusal held
    (``release_cut._note_refusal``: ``meta.cut_hold`` beside
    ``meta.cut_retarget``, no ``meta.release`` yet) past
    :data:`CUT_HOLD_ASK_AFTER_MIN` files ONE stuck ask —
    :func:`_file_stuck_ask`, deduped by its
    ``release-stuck:td<root>:recut-hold`` tag (gr611217). With no
    release number yet the ask keys on the pass root and quotes the
    refusal that started the hold — and never names ``cut --abandon``:
    it would land on the other, live release (gr620480). Returns its
    note, ``None`` when nothing is due."""
    ref = store.get_ref(kind="todo", id=root_id)
    meta = (ref.meta or {}) if ref is not None else {}
    hold = meta.get("cut_hold")
    retarget = meta.get("cut_retarget")
    if (
        not isinstance(hold, dict)
        or not isinstance(retarget, dict)
        or isinstance(meta.get("release"), dict)
    ):
        return None
    held = _minutes_since(now, hold.get("since"))
    if held is None or held < CUT_HOLD_ASK_AFTER_MIN:
        return None
    lines = [str(ln) for ln in hold.get("lines") or []]
    return _file_stuck_ask(
        store,
        0,
        _CUT_HOLD_STAGE,
        int(held) - CUT_HOLD_ASK_AFTER_MIN,
        "unrecorded",
        CUT_HOLD_ASK_AFTER_MIN,
        root_id,
        dry_run=dry_run,
        clock_txt=f"first-cut hold since {hold.get('since')}",
        detail=(
            f"cut refused ({retarget.get('class')}): {retarget.get('line')}; "
            f"holding: {'; '.join(lines) or 'unrecorded'}"
        ),
        label=f"td{root_id}",
        verbs=_FIRST_CUT_HOLD_VERBS,
    )


def _mint_reviewers_only(
    store: Any,
    res: PollResult,
    *,
    now: datetime,
    dry_run: bool,
    rel: dict[str, Any] | None = None,
) -> None:
    """The engine gate's mint-and-ask writes on a pass that cannot cut
    — inside a no-release window, while a pass is open or a round is
    in flight (gr611217): reviews are latency best paid before a
    deploy needs them. Main ahead of prod is read every time; ``rel``
    (the open pass's cut release) adds its own scope
    (:func:`_release_lands`) — the fixes on ``release/r<N>`` are not
    on main, and that release's deploy waits on them. The
    advisory-locked dedupe keeps one reviewer per unit; the verdicts
    are dropped and a fail-closed line is a note, never the blocker."""
    for scope in (None, rel) if rel is not None else (None,):
        _verdicts, notes, fatal, _pin, _scanned = _engine_verdicts(
            store, rel=scope, now=now, file_asks=True, dry_run=dry_run
        )
        res.notes.extend(notes)
        if fatal is not None:
            res.notes.append(fatal)


def run_pass(
    store: Any,
    *,
    repo: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PollResult:
    """One poll pass (module docstring); stops at the first blocker."""
    from precis import settings
    from precis.plans import templates

    now = now or datetime.now(UTC)
    res = PollResult(dry_run=dry_run)
    try:
        res.notes.extend(watch_stuck(store, now=now, dry_run=dry_run))
    except Exception:
        # The watchdog never blocks a mint: one note, then the pass goes on.
        log.warning("release_poll: stuck watch raised", exc_info=True)
        res.notes.append("stuck watch raised — mint path unaffected")
    windows = parse_windows(settings.get_str(WINDOWS_SETTING, store=store) or "")
    if windows is None:
        res.blocker = f"{WINDOWS_SETTING} is unparsable: nothing minted"
        return res
    hit = in_window(now, windows)
    if hit is not None:
        _mint_reviewers_only(store, res, now=now, dry_run=dry_run)
        res.blocker = f"inside the no-release window {hit}Z"
        return res
    open_id = _open_pass_id(store)
    if open_id is not None:
        _mint_reviewers_only(
            store,
            res,
            now=now,
            dry_run=dry_run,
            rel=_release.release_of(store, open_id),
        )
        try:
            note = _cut_hold_ask(store, open_id, now=now, dry_run=dry_run)
        except Exception:
            log.warning("release_poll: cut-hold ask raised", exc_info=True)
            note = "cut-hold ask raised — next tick retries"
        if note is not None:
            res.notes.append(note)
        res.blocker = f"release-pass td{open_id} is open"
        return res
    blocked = _round_blocker(store, now, dry_run=dry_run, notes=res.notes)
    if blocked is not None:
        _mint_reviewers_only(store, res, now=now, dry_run=dry_run)
        res.blocker = blocked
        return res
    verdicts, notes, fatal, pin, scanned = _engine_verdicts(
        store, now=now, file_asks=True, dry_run=dry_run
    )
    res.notes.extend(notes)
    if fatal is not None:
        res.blocker = fatal
        return res
    uncleared = [v for v in verdicts or () if not v.cleared]
    res.repo = pr_class.resolve_repo(store, repo)
    if not res.repo:
        res.notes.append(
            "no GitHub owner/repo (params.repo, release.repo and the "
            "checkout's origin all empty); next: set release.repo"
        )
        return res
    base = _API.format(repo=res.repo)
    try:
        # td601005: the compare's head is the scan's own pin, so a main
        # move after the fetch can never smuggle an unreviewed land
        # inside the recorded sha — there is no second main-head read.
        cmp_ = pr_class._get_json(f"{base}/compare/prod...{pin}", {})
        if not isinstance(cmp_, dict):
            raise pr_class.GitHubUnreadable(
                f"GET {base}/compare/prod...{pin}: expected an object"
            )
        try:
            ahead = int(cmp_["ahead_by"])
            behind = int(cmp_["behind_by"])
        except (KeyError, TypeError, ValueError):
            raise pr_class.GitHubUnreadable(
                f"GET {base}/compare/prod...{pin}: no ahead_by/behind_by"
            ) from None
        status = str(cmp_.get("status") or "unknown")
        if behind > 0 or status == "diverged":
            res.blocker = f"prod has commits main lacks ({status})"
            return res
        if ahead <= 0:
            res.blocker = f"main is not ahead of prod ({status})"
            return res
        if cmp_.get("files") == []:
            # release-train T3: a merge-back commit alone puts main ahead
            # of prod with prod's own tree — a pass would cut, test,
            # deploy and bounce an identical tree.
            res.blocker = (
                f"main's tree equals prod's ({ahead} commit(s) ahead, no file "
                "differs — a merge-back alone): nothing to release"
            )
            return res
        # Release train (Reto 2026-11-12): the cut is the pinned tip and
        # needs no reviews — a pending review holds this release's
        # deploy (:func:`deploy_review_blockers`), never its cut. The
        # compare's ``commits`` is the recorded window, the scan's own
        # ordering standing in when the list comes back empty (td601285).
        cut = str(pin)
        upto = [c[: _release.LANDED_SHA_LEN] for c in _commit_shas(cmp_) or scanned]
        if uncleared:
            res.notes.append(
                f"cut {cut[:9]}: {len(uncleared)} engine land(s) await review "
                f"— they hold the deploy, not the cut "
                f"({', '.join(v.unit.land for v in uncleared)})"
            )
        flying = _in_flight(res.repo)
        if flying is not None:
            res.blocker = f"{flying} is in flight"
            return res
    except pr_class.GitHubUnreadable as exc:
        res.blocker = f"GitHub unreadable ({exc}); nothing minted, next tick retries"
        return res
    sha12 = cut.strip().lower()[:12]
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MINT_LOCK_KEY,))
        open_id = _open_pass_id(store)
        if open_id is not None:
            res.blocker = f"release-pass td{open_id} is open"
            return res
        prior = _pass_for_sha(store, sha12)
        if prior is not None:
            res.blocker = f"pass for {sha12} already ran: {prior[1]}"
            return res
        if dry_run:
            res.would_mint = True
            return res
        try:
            mint = templates.mint_release_pass(store, main_sha=cut)
        except Exception as exc:  # one error line, not a raise
            log.warning("release_poll: mint failed", exc_info=True)
            res.blocker = f"mint failed: {type(exc).__name__}: {exc}"
            return res
        # The recorded cut: ``release_cut`` passes ``sha`` to
        # ``scripts/release --sha`` (gr592271); ``upto`` is the window
        # at-or-before it.
        store.stamp_ref_meta(
            int(mint.root_id),
            {"release_target": {"sha": cut, "upto": upto}},
            conn=conn,
        )
        res.minted = int(mint.root_id)
    return res


#: A checkout-less process re-parks the poll at most this many times (two
#: minutes apiece, ~48 min) before the job fails with a named reason.
_NO_CHECKOUT_PARK_CAP = 24
_NO_CHECKOUT_PARK_S = 120


def _defer_without_checkout(ctx: Any) -> bool:
    """``True`` when this process has no repo checkout and the poll was
    handed back (gr623024).

    The claim filter keeps the poll off a checkout-less ``job_inproc``
    process (:func:`precis.workers.executors.job_inproc.
    process_unclaimable_job_types`); this is the backstop for a claim that
    still lands (an env change since the process probed, a reclaim). The
    gate scan would raise out of ``run_slice.slice_root`` and the poll end
    "blocked — engine review gate unreadable … fail closed", which reads
    as a finished tick and blocks minting for hours. Instead the job parks
    (``ctx.defer`` -> ``STATUS:waiting_time``, re-queued by the wake runner)
    for a capable process to pick up; these placement parks count under
    ``placement_parks``, not the pause-park cap. Past
    :data:`_NO_CHECKOUT_PARK_CAP` or without ``ctx.defer`` the job fails
    with the reason, never succeeds."""
    from precis.workers import capability_probe

    if capability_probe._probe_repo_checkout() != 0:
        return False
    reason = (
        "release_poll: no repo checkout on this worker process "
        "(run_slice.slice_root unresolved); next: a process with "
        "PRECIS_SLICE_ROOT claims it — set it on every job_inproc lane "
        "(deploy/playbooks/20d-precis-worker-drain.yml)"
    )
    parks = int(ctx.meta.get("placement_parks") or 0) + 1
    if parks <= _NO_CHECKOUT_PARK_CAP and ctx.defer is not None:
        ctx.set_meta(pause_parks=0, placement_parks=parks)
        wake = datetime.now(UTC) + timedelta(seconds=_NO_CHECKOUT_PARK_S)
        if ctx.defer(wake, reason):
            return True
    ctx.record_failure(reason)
    return True


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    if _defer_without_checkout(ctx):
        return
    params = dict(ctx.meta.get("params") or {})
    try:
        res = run_pass(
            ctx.store,
            repo=params.get("repo"),
            dry_run=bool(params.get("dry_run")),
        )
    except Exception as exc:  # a failed pass must not wedge the lane
        log.warning("release_poll: pass raised (%s)", type(exc).__name__, exc_info=True)
        ctx.record_failure(f"release_poll: pass raised: {type(exc).__name__}: {exc}")
        return
    ctx.set_meta(release_poll=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


#: Host-capability token the heartbeat advertises through ``resource_slots``
#: on a host with a repo checkout (``capability_probe._probe_repo_checkout``).
REPO_CHECKOUT_RESOURCE = "repo_checkout"


def conditional_requires(params: dict[str, Any]) -> frozenset[str]:
    """``{repo_checkout}`` always — :func:`_git_lands` fetches and scans the
    slice checkout (``run_slice.slice_root``, ``PRECIS_SLICE_ROOT``), so a
    checkout-less in-proc worker must leave the poll queued instead of
    claiming it and failing closed with "engine review gate unreadable"
    (gr610176). Declared here, not in ``requires``: ``job_inproc`` PROVIDES
    nothing, so a static ``requires`` would be rejected at submit; the
    claim-time gate (``claim_executor_jobs(check_job_type_requires=True)``)
    folds this in."""
    return frozenset({REPO_CHECKOUT_RESOURCE})


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A second poll while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live DB/GitHub state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
    conditional_requires=conditional_requires,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NAME",
    "PARAMS_SCHEMA",
    "REPO_CHECKOUT_RESOURCE",
    "SPEC",
    "WINDOWS_SETTING",
    "PollResult",
    "branch_in_flight",
    "conditional_requires",
    "deploy_review_blockers",
    "engine_review_blockers",
    "in_window",
    "load",
    "parse_windows",
    "run_pass",
]
