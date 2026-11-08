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
the unauthenticated api.github.com budget (60/h) is shared with
``pr_poll`` and ``ci_verdict_poll``:

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
   when unsure, do not mint.
2. The DB: a live todo tagged ``release-pass`` whose STATUS is not in
   ``precis.todo.TERMINAL_TODO_STATUSES`` is a pass already in flight,
   and the shared ``release.round`` record names a round still open
   (not :func:`precis.plans.release.is_terminal`, any owner — one
   exception: a ``cut`` row whose recorded ``pass`` root is terminal
   with no deploy evidence is a dropped pass and the poll abandons the
   row itself, through ``update_round`` — never while a non-terminal
   ``release_deploy`` job sits under the pass, and a dry run or
   ``PRECIS_FACTORY=off`` reports the would-abandon and writes nothing,
   gr575362). No GitHub read has happened yet.
3. The repo: ``params.repo``, else ``release.repo``, else the checkout's
   ``origin`` (:func:`precis.plans.pr_class.resolve_repo`).
4. ``GET /repos/<repo>/compare/prod...main``: ``behind_by > 0`` or
   ``status == 'diverged'`` means prod holds commits main lacks — no
   mint, whatever ``ahead_by`` says; ``ahead_by == 0`` mints nothing.
5. In flight on origin: ``GET .../git/matching-refs/heads/release/`` and
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
6. The sha claim — one pass per main sha (td573430 fix round 1):
   ``GET .../git/ref/heads/main``, then inside one transaction under the
   ``_MINT_LOCK_KEY`` advisory lock the pass re-checks the open-pass
   gate and refuses when a ``release-main:<sha12>``-tagged root already
   exists in any state (open, done, failed, abandoned) — a new pass
   needs main to move. The lock makes check-then-mint atomic: two
   concurrent runners cannot both mint.
7. Only then: :func:`precis.plans.templates.mint_release_pass` stamps
   the root ``release-main:<sha12>``. The plan itself runs when
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

#: The dedupe tag on the review-wait ask — one OPEN ask per LANDED todo:
#: ``release-review-wait:td<id>`` where id is the land's todo, and
#: answering the ask clears that one land. An ask closed without an
#: ``answer:`` comment holds nothing — the gate refiles it.
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
    the live leg's hold ran past ``_ACTIVITY_CAP_MARKS`` marks."""
    from precis.workers.job_types import run_slice

    tag = f"{_STUCK_TAG_PREFIX}:r{n}:{stage}"
    verbs = _STAGE_VERBS[stage]
    head_txt = (
        f"{run_head12}, the head the newest run on release/r{n} ran on — "
        f"it may differ from the record's base {head12}"
        if run_head12
        else head12
    )
    context = (
        f"release r{n} stage {stage}: {over}m past the {deadline}m "
        f"deadline; head {head_txt}; answering executes nothing"
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
        f"release r{n} looks stuck in stage {stage} — act on it, or is this "
        "a false alarm?"
    )
    capped_txt = "capped: leg active but stage exceeded 3× its mark\n" if capped else ""
    body = (
        f"Release r{n} has been in stage {stage} for more than {deadline} "
        f"minutes — {over}m past the mark — and nothing will act on that by "
        f"itself (head {head_txt}). This ask is the only write the release "
        f"watchdog makes; answering it executes nothing — 'acknowledged' "
        f"and 'false alarm' both just close it.\n\n"
        f"Hand verbs that apply (a human's call, in the coordinator's "
        f"checkout): {verbs}.\n\n"
        f"{capped_txt}"
        f"clock: {clock_txt}"
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
            return f"stuck watch: ask for r{n}/{stage} already filed (td{dupes[0][0]})"
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


def _main_sha(base: str) -> str:
    """main's head sha on origin. ``GET .../git/ref/heads/main`` answers
    one ref object — a prefix list (no exact ``refs/heads/main`` match)
    or a payload without the sha is unreadable. Raises
    :class:`precis.plans.pr_class.GitHubUnreadable`."""
    url = f"{base}/git/ref/heads/main"
    data = pr_class._get_json(url, {})
    if isinstance(data, list):
        data = next(
            (
                e
                for e in data
                if isinstance(e, dict) and e.get("ref") == "refs/heads/main"
            ),
            None,
        )
    sha = ""
    if isinstance(data, dict):
        sha = str((data.get("object") or {}).get("sha") or "")
    if not sha:
        raise pr_class.GitHubUnreadable(f"GET {url}: no refs/heads/main sha")
    return sha


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
# A release never cuts over an engine land its reviewer has not read:
# the factory may build engine paths once ``factory.deny_relaxed`` is
# flipped, but only through the review lane. The gate is land-centric —
# every ``land_slice`` job that SUCCEEDED (``meta.landed_sha`` not null;
# a failed land has no key and a no-diff land a JSON null, so neither
# blocks, and a later failed re-land cannot mask an earlier success) with
# an engine ``meta.engine_paths`` stamp needs a reviewer covering the
# landed todo: ``meta.generate.from`` naming the todo itself or its
# parent — the one level ``plan_dispatch._review_targets`` reviews (a
# slicer's slices). A land with no stamp (pre-E1) counts as non-engine.
# A covering reviewer clears the land when it closed ``LAND``, or
# ``FIX`` whose fix task landed; while it is open under a live root the
# release waits, and once the NEWER of the land and its newest covering
# reviewer is :data:`REVIEW_WAIT_AFTER` old one ``ask-user`` ruling
# files — at once when the wait can never resolve (a dead verdict, an
# unlanded fix that already ended, an open reviewer under a dead root).
# No covering reviewer is the factory's own job (td584381 S1b): the
# gate mints one — a plan reviewer node under the root the landed
# todo's own ``plan:<id>`` tag names, ``blocked-by`` that todo so a
# REVERT holds it through the rebuild, which ``plan_dispatch`` runs on
# a review lane of a vendor that did not build the land — and files the
# ask only when no minted reviewer could run: no enabled review-fit
# lane, no ``plan:`` tag naming a live root (a dead root is hopeless to
# this same gate, and the reviewed todo itself would read as its own
# review target), or a mint that raised; when ``plan_dispatch`` itself
# is switched off the gate mints nothing and files the ask as before.
# An answered ask
# (``release-review-wait:td<land>`` with an ``answer:`` comment) clears
# that one land; one closed without the comment holds nothing — the
# gate refiles it and the blocker names the live ask's id. "Fix
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


def _engine_landed_jobs(store: Any) -> list[tuple[int, int, Any]]:
    """``(job ref_id, landed todo id, created_at)`` for every
    ``land_slice`` job that SUCCEEDED and stamped engine, oldest first.

    Succeeded = ``meta->>'landed_sha' IS NOT NULL``: a failed land has no
    key and a no-diff land stamps JSON null (``->>`` reads both as SQL
    NULL) — neither blocks. ALL jobs are read, not the newest: a later
    failed re-land must not mask an earlier engine land. Engine = the
    ``<unreadable>`` mark or a stamp holding a path engine TODAY (the
    globs re-check, so a glob added since the land still counts); no
    stamp (pre-E1) or non-engine paths skip the gate.
    """
    from precis.plans import lanes as _lanes

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta, created_at FROM refs
             WHERE kind = 'job' AND retired_at IS NULL
               AND meta->>'job_type' = 'land_slice'
               AND meta->>'landed_sha' IS NOT NULL
             ORDER BY ref_id
            """
        ).fetchall()
    out: list[tuple[int, int, Any]] = []
    for ref_id, meta, created_at in rows:
        m = meta if isinstance(meta, dict) else {}
        stamped = m.get("engine_paths")
        if not isinstance(stamped, list) or not stamped:
            continue
        if stamped != ["<unreadable>"] and not _lanes.engine_hits(stamped):
            continue
        raw = (m.get("params") or {}).get("todo_id")
        if not isinstance(raw, (int, str)):
            continue
        try:
            todo_id = int(raw)
        except ValueError:
            continue
        out.append((int(ref_id), todo_id, created_at))
    return out


def _fix_landed(store: Any, fix_id: int) -> bool:
    """``True`` once the fix todo carries a ``landed:`` comment."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM chunks c
             WHERE c.ref_id = %s AND c.chunk_kind = 'todo_comment'
               AND c.retired_at IS NULL AND c.text LIKE 'landed:%%'
             LIMIT 1
            """,
            (fix_id,),
        ).fetchone()
    return row is not None


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


def _answered_wait_ask(store: Any, todo_id: int) -> bool:
    """``True`` when the ``release-review-wait:td<todo_id>`` ask carries
    an ``answer:`` comment (the ``answer:`` convention —
    ``plans.release`` ~296, ``handlers.todo`` ~66); it clears that one
    land."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
              JOIN chunks c ON c.ref_id = r.ref_id
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND c.chunk_kind = 'todo_comment' AND c.retired_at IS NULL
               AND lower(ltrim(c.text)) LIKE 'answer:%%'
             LIMIT 1
            """,
            (f"{_REVIEW_WAIT_TAG_PREFIX}:td{todo_id}",),
        ).fetchone()
    return row is not None


def _open_wait_ask_id(store: Any, todo_id: int) -> int | None:
    """The id of the still-answerable ``release-review-wait:td<todo_id>``
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
            (f"{_REVIEW_WAIT_TAG_PREFIX}:td{todo_id}", list(_WAIT_ASK_CLOSED)),
        ).fetchone()
    return None if row is None else int(row[0])


def _review_outcome(
    store: Any, rid: int, meta: dict[str, Any], todo_id: int
) -> tuple[str, str]:
    """``(outcome, blocker line)`` for one covering reviewer on the
    td<todo_id> land — ``satisfied`` clears it; ``waiting`` is a live
    reviewer still holding it (the line is the blocker); ``hopeless``
    can never resolve, so the wait ask files at once."""
    status = _status_of(store, rid)
    base = f"engine review td{rid} on the td{todo_id} land"
    if status in TERMINAL_TODO_STATUSES or status == "cancelled":
        review = meta.get("review")
        review = review if isinstance(review, dict) else {}
        verdict = review.get("verdict")
        if verdict == "LAND":
            return "satisfied", ""
        if verdict == "FIX":
            task = review.get("task")
            if isinstance(task, int):
                if _fix_landed(store, task):
                    return "satisfied", ""
                task_status = _status_of(store, task)
                if (
                    task_status not in TERMINAL_TODO_STATUSES
                    and task_status != "cancelled"
                ):
                    return "waiting", (
                        f"{base} was FIX; its fix td{task} is not landed "
                        f"yet — next: let td{task} land"
                    )
                return "hopeless", (
                    f"{base} was FIX; its fix td{task} ended {task_status} "
                    "with no landed: comment — next: acknowledge the "
                    "review-wait ask or land a fix"
                )
        return "hopeless", (
            f"{base} closed {status} without a usable verdict; the wait "
            "can never resolve — next: acknowledge the review-wait ask "
            f"or re-review td{todo_id}"
        )
    root_id, root_status = _root(store, rid)
    if root_status in TERMINAL_TODO_STATUSES or root_status in ("cancelled", "gone"):
        return "hopeless", (
            f"{base} is open under a {root_status} root (td{root_id}); it "
            f"will never finish — next: close td{rid} or acknowledge the "
            "review-wait ask"
        )
    return "waiting", (
        f"{base} is {status}; the release waits — next: let td{rid} finish or close it"
    )


def _file_review_wait_ask(
    store: Any,
    todo_id: int,
    *,
    rid: int | None = None,
    job_id: int | None = None,
    dry_run: bool,
    reason: str | None = None,
) -> tuple[str, int | None]:
    """The review-wait ask: one OPEN ``ask-user`` ruling todo per LANDED
    todo, deduped by its ``release-review-wait:td<todo_id>`` tag over
    still-answerable todos inside one locked transaction — same shape as
    :func:`_file_stuck_ask`. Answering it clears that one land; one
    closed without an ``answer:`` comment is refiled.

    Returns ``(note, ask_id)`` — the live ask's todo id (filed or
    deduped) so the blocker can name it, ``None`` on dry-run/factory-off.
    """
    from precis.workers.job_types import run_slice

    tag = f"{_REVIEW_WAIT_TAG_PREFIX}:td{todo_id}"
    if rid is not None:
        what = f"engine review td{rid} on the td{todo_id} land"
        title = (
            f"engine review td{rid} on the td{todo_id} land will not "
            "resolve on its own — acknowledge it, or is this a false alarm?"
        )
        body = (
            f"The engine-review gate (td574937 E1) holds every release "
            f"pass and cut while the td{todo_id} engine land is "
            f"uncleared; reviewer td{rid} cannot settle it on its own. "
            "This ask is the only write the gate makes; answering it "
            "executes nothing — 'acknowledged' and 'false alarm' both "
            "just close it, which clears this one land."
        )
    else:
        what = f"the td{todo_id} engine land (jo{job_id})"
        title = (
            f"engine land td{todo_id} (jo{job_id}) has no reviewer — "
            "acknowledge it, or is this a false alarm?"
        )
        why = f" The gate could not mint one itself: {reason}." if reason else ""
        body = (
            f"The engine-review gate (td574937 E1) holds every release "
            f"pass and cut while the td{todo_id} engine land (job "
            f"jo{job_id}) has no reviewer covering it — fix, revert and "
            f"plain lands mint none.{why} This ask is the only write the "
            "gate makes; answering it executes nothing — 'acknowledged' "
            "and 'false alarm' both just close it, which clears this one "
            "land."
        )
    context = (
        f"{what} holds every release pass and cut until cleared — "
        "answering executes nothing"
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
                f"engine review: ask for td{todo_id} already filed (td{open_ids[0]})",
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
    asks anyway."""
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


def engine_review_blockers(
    store: Any,
    *,
    now: datetime | None = None,
    file_asks: bool = False,
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    """``(blocker lines, notes)`` — the engine-review gate's verdict.

    Per engine land — a ``land_slice`` job that SUCCEEDED with an engine
    stamp — oldest first: an answered ``release-review-wait:td<land>``
    ask clears it; no covering reviewer (``generate.from`` naming the
    landed todo or its parent) mints one under the root the land's own
    ``plan:<id>`` tag names, at most :data:`_MINT_PER_PASS` a pass —
    the mint is covering next tick — and files the ask only when
    minting cannot run or ``plan_dispatch`` itself is switched off
    (td584381 S1b); covering reviewers are judged satisfied > waiting >
    hopeless — a waiting land files its ask once the NEWER of the land
    and its newest covering reviewer is :data:`REVIEW_WAIT_AFTER` old,
    a hopeless one at once. An ask closed without an ``answer:``
    comment does not dedupe — the next filing pass refiles it — and
    blocker lines name the live ask's id. ``file_asks`` limits the
    mint-and-ask writes to the pass (``release_cut`` asks for nothing).
    Any exception anywhere returns a single fail-closed blocker.
    """
    from precis.plans import lanes as _lanes
    from precis.workers.job_types import plan_dispatch as _plan_dispatch

    now = now or datetime.now(UTC)
    notes: list[str] = []
    blockers: list[str] = []
    try:
        lane_table = _lanes.load_lanes(store) if file_asks else ()
        # plan_dispatch's own kill switch — the env OR a service_config
        # prio-0 row, read through its helper, never re-checked here:
        # a mint nobody dispatches would sit "waiting" and hold the ask
        # off for 24 h, so "off" takes the pre-S1b path and asks at once.
        factory_reason = _plan_dispatch.read_only_reason(store) if file_asks else None
        minted = 0
        by_from: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for rid, meta in _reviewer_rows(store):
            gen = meta.get("generate")
            if not isinstance(gen, dict):
                continue
            m = _REVIEW_FROM_RE.fullmatch(str(gen.get("from") or ""))
            if m is None:
                continue
            by_from.setdefault(int(m.group(1)), []).append((rid, meta))
        for job_id, todo_id, created_at in _engine_landed_jobs(store):
            if _answered_wait_ask(store, todo_id):
                continue
            parent = _parent_of(store, todo_id)
            covering = [
                r
                for fid in (todo_id, parent)
                if fid is not None
                for r in by_from.get(fid, ())
            ]
            if not covering:
                ask_id = _open_wait_ask_id(store, todo_id)
                if file_asks:
                    plan_root, refusal = (
                        _reviewer_hang(store, todo_id, lane_table)
                        if factory_reason is None
                        else (None, None)
                    )
                    if plan_root is not None and not dry_run:
                        if minted >= _MINT_PER_PASS:
                            blockers.append(
                                f"engine land td{todo_id} (jo{job_id}) has "
                                "no reviewer; reviewer mint deferred; the "
                                "release waits — next: a later tick mints it"
                            )
                            continue
                        try:
                            note, minted_id, fresh = _mint_engine_reviewer(
                                store, todo_id, plan_root
                            )
                        except Exception as exc:
                            log.warning(
                                "release_poll: reviewer mint for td%s raised",
                                todo_id,
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
                            blockers.append(
                                f"engine land td{todo_id} (jo{job_id}) had "
                                f"no reviewer; {state} — the release waits "
                                f"— next: let td{minted_id} run"
                            )
                            continue
                    elif plan_root is not None:
                        notes.append(
                            f"engine review: would mint a reviewer for td{todo_id}"
                        )
                        blockers.append(
                            f"engine land td{todo_id} (jo{job_id}) has no "
                            "reviewer; the release waits — next: a live "
                            "tick with the factory on mints one"
                        )
                        continue
                    note, filed_id = _file_review_wait_ask(
                        store,
                        todo_id,
                        job_id=job_id,
                        dry_run=dry_run,
                        reason=refusal,
                    )
                    notes.append(note)
                    if filed_id is not None:
                        ask_id = filed_id
                if ask_id is not None:
                    next_step = f"answer the review-wait ask td{ask_id}"
                elif file_asks:
                    next_step = f"answer the review-wait ask on td{todo_id}"
                else:
                    next_step = "the review gate holds the cut"
                blockers.append(
                    f"engine land td{todo_id} (jo{job_id}) has no "
                    f"reviewer; the release waits — next: {next_step}"
                )
                continue
            waiting: tuple[int, str] | None = None
            hopeless: tuple[int, str] | None = None
            satisfied = False
            for rid, meta in covering:
                outcome, line = _review_outcome(store, rid, meta, todo_id)
                if outcome == "satisfied":
                    satisfied = True
                    break
                if outcome == "waiting" and waiting is None:
                    waiting = (rid, line)
                elif outcome == "hopeless" and hopeless is None:
                    hopeless = (rid, line)
            if satisfied:
                continue
            pick = waiting or hopeless
            if pick is None:
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
                        for s in (created_at, rstamp)
                        if s is not None
                    )
                    due = now - base >= REVIEW_WAIT_AFTER
            ask_id = _open_wait_ask_id(store, todo_id)
            if file_asks and due:
                note, filed_id = _file_review_wait_ask(
                    store, todo_id, rid=rid, dry_run=dry_run
                )
                notes.append(note)
                if filed_id is not None:
                    ask_id = filed_id
            if ask_id is not None:
                line = f"{line} — the open ask is td{ask_id}"
            blockers.append(line)
    except Exception as exc:
        log.warning("release_poll: engine review gate unreadable", exc_info=True)
        return (
            [
                f"engine review gate unreadable ({type(exc).__name__}); "
                "next: retry next tick — fail closed"
            ],
            notes,
        )
    return blockers, notes


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
        res.blocker = f"inside the no-release window {hit}Z"
        return res
    open_id = _open_pass_id(store)
    if open_id is not None:
        res.blocker = f"release-pass td{open_id} is open"
        return res
    blocked = _round_blocker(store, now, dry_run=dry_run, notes=res.notes)
    if blocked is not None:
        res.blocker = blocked
        return res
    lines, notes = engine_review_blockers(
        store, now=now, file_asks=True, dry_run=dry_run
    )
    res.notes.extend(notes)
    if lines:
        res.blocker = "; ".join(lines)
        return res
    res.repo = pr_class.resolve_repo(store, repo)
    if not res.repo:
        res.notes.append(
            "no GitHub owner/repo (params.repo, release.repo and the "
            "checkout's origin all empty); next: set release.repo"
        )
        return res
    base = _API.format(repo=res.repo)
    try:
        cmp_ = pr_class._get_json(f"{base}/compare/prod...main", {})
        if not isinstance(cmp_, dict):
            raise pr_class.GitHubUnreadable(
                f"GET {base}/compare/prod...main: expected an object"
            )
        try:
            ahead = int(cmp_["ahead_by"])
            behind = int(cmp_["behind_by"])
        except (KeyError, TypeError, ValueError):
            raise pr_class.GitHubUnreadable(
                f"GET {base}/compare/prod...main: no ahead_by/behind_by"
            ) from None
        status = str(cmp_.get("status") or "unknown")
        if behind > 0 or status == "diverged":
            res.blocker = f"prod has commits main lacks ({status})"
            return res
        if ahead <= 0:
            res.blocker = f"main is not ahead of prod ({status})"
            return res
        flying = _in_flight(res.repo)
        if flying is not None:
            res.blocker = f"{flying} is in flight"
            return res
        main_sha = _main_sha(base)
    except pr_class.GitHubUnreadable as exc:
        res.blocker = f"GitHub unreadable ({exc}); nothing minted, next tick retries"
        return res
    sha12 = main_sha.strip().lower()[:12]
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
            mint = templates.mint_release_pass(store, main_sha=main_sha)
        except Exception as exc:  # one error line, not a raise
            log.warning("release_poll: mint failed", exc_info=True)
            res.blocker = f"mint failed: {type(exc).__name__}: {exc}"
            return res
        res.minted = int(mint.root_id)
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
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
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "WINDOWS_SETTING",
    "PollResult",
    "branch_in_flight",
    "engine_review_blockers",
    "in_window",
    "load",
    "parse_windows",
    "run_pass",
]
