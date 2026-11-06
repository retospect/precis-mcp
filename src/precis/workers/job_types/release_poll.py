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
   is judged against the ``release.stage_deadlines`` setting; a round
   past a stage's minutes gets one ``ask-user`` ruling todo, once per
   (round, stage), offering only the hand verbs — nothing automatic
   acts. Runs first, at any hour and inside its own try/except, so a
   stuck release is reported whether or not a new one could mint.
1. Clock: the ``release.no_release_windows`` setting names UTC
   ``HH:MM-HH:MM`` pairs no release may start inside (the default is the
   nightly backup window); an unparsable setting is itself a blocker —
   when unsure, do not mint.
2. The DB: a live todo tagged ``release-pass`` whose STATUS is not in
   ``precis.todo.TERMINAL_TODO_STATUSES`` is a pass already in flight,
   and the shared ``release.round`` record names a round still open
   (``state`` not in :data:`_ROUND_TERMINAL_STATES`, any owner). No
   GitHub read has happened yet.
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
   ``ahead``); the prod compare is skipped when there is no tag.
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
from datetime import UTC, datetime, time
from typing import Any

from precis.plans import pr_class
from precis.plans import release as _release
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
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

#: check.yml run statuses that mean CI is still moving on a head.
_LIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending"}
)

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

#: ``release.round`` record states that mean the round is closed — the
#: same set as ``scripts/round._TERMINAL_STATES`` and
#: ``scripts/lib/round_state.py::TERMINAL_STATES`` (a test pins it). Any
#: other state — or none — is a round still in flight: no mint.
_ROUND_TERMINAL_STATES = frozenset({"confirmed", "superseded", "abandoned"})

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


def _round_blocker(store: Any) -> str | None:
    """The shared ``release.round`` record as a blocker line — ``None``
    only for an absent record ("no round") or a closed one (terminal
    state). A record that cannot be read or parsed blocks: when unsure,
    do not mint."""
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
    if state not in _ROUND_TERMINAL_STATES:
        return f"release round r{rec.get('n', '?')} in flight ({state})"
    return None


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
) -> str:
    """The one write the watchdog makes: an ``ask-user`` ruling todo,
    deduped by its ``release-stuck:r<N>:<stage>`` tag over todos in any
    status inside one locked transaction, so a re-tick, a closed ask or
    two runners never file a second. ``run_head12`` is the head the
    newest branch run reported — it may differ from the record's base
    after a hand ``ship --release`` fix, and the ask says so."""
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
    body = (
        f"Release r{n} has been in stage {stage} for more than {deadline} "
        f"minutes — {over}m past the mark — and nothing will act on that by "
        f"itself (head {head_txt}). This ask is the only write the release "
        f"watchdog makes; answering it executes nothing — 'acknowledged' "
        f"and 'false alarm' both just close it.\n\n"
        f"Hand verbs that apply (a human's call, in the coordinator's "
        f"checkout): {verbs}."
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

    The record's ``state_at`` is the clock; a record without one (every
    round cut before td574370) is never judged, as is a future or
    unparsable clock. From the row: ``deploying`` judges stage
    ``deploying``; ``deployed`` without ``deploy.confirmed_at`` judges
    ``confirm``; ``cut`` derives its ruled stage from
    ``release.ci_verdicts`` — a green verdict on the head is the
    approval wait (no stage), a red is ``fixing`` from that verdict's
    ``at`` (a red on the round's older head counts until a verdict for
    the current head exists), and no verdict is ``cut`` past its
    deadline unless one GitHub read shows a run in flight, in which
    case it is ``gating`` from ``state_at``. Terminal states, an absent
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
                                and str(run.get("status") or "") in _LIVE_RUN_STATUSES
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
        prod_has_head: bool | None = None
        if has_tag:
            sha = str((heads[n].get("object") or {}).get("sha") or "")
            cmp_ = pr_class._get_json(f"{base}/compare/{sha}...prod", {})
            prod_has_head = isinstance(cmp_, dict) and cmp_.get("status") in (
                "identical",
                "ahead",
            )
        if branch_in_flight(has_tag, prod_has_head):
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
    blocked = _round_blocker(store)
    if blocked is not None:
        res.blocker = blocked
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
    "in_window",
    "load",
    "parse_windows",
    "run_pass",
]
