"""``ci_verdict_poll`` — record check.yml's verdict on a release cut (td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``poll`` node of the ``release-pass`` template. Mechanical, DB + one
HTTPS read, under ``job_inproc`` (the ``gripe_validate`` shape), so the cut
session never waits on CI and no lane is held while CI runs.

One run: resolve ``{sha, range, branch, repo}`` — ``params.sha`` /
``params.range`` / ``params.repo`` when given, else the plan root's
``meta.release`` (written by ``release_cut``) for ``params.todo_id``; the
repo falls back to the ``release.repo`` setting, then to ``git remote
get-url origin`` of the repo checkout. Then ``GET
https://api.github.com/repos/<owner>/<repo>/actions/runs?head_sha=<sha>``
through :func:`precis.utils.safe_fetch.safe_get` (unauthenticated is fine
for the public repo at 60/h; ``GH_TOKEN`` from the env, else the factory's
vault token, is sent — :func:`precis.workers.factory_git.github_api_headers`).
The newest check.yml run (on the release branch when one is known):

- completed ``success``/``skipped``/``neutral`` → ``green``; any other
  completed conclusion → ``red``. The verdict row
  ``{sha, range, verdict, run_id, at}`` goes into ``release.ci_verdicts``
  (:func:`precis.plans.release.record_ci_verdict`, the row
  ``scripts/release`` writes) and the job succeeds — so the plan tick
  closes the poll node and the ``tests-green`` gate reads the row.
- no run yet, still running, or GitHub unreadable (rate limit, network) →
  **pending**: a ``pending`` summary, then the job parks itself
  (``ctx.defer``, ``STATUS:waiting_time``) for :func:`poll_interval`
  (10 min, 2 min once the run is 18+ min old — gr611302); the
  wake runner re-queues the same job, which polls again. A parked job is
  live, so the tick neither closes the node nor mints a second poll (one
  poll per node: the tick's ``plan_dispatch:td<id>`` idem key).

Pending splits two ways (gr656644):

- A run still going (``status != completed``) is an **attempt-free
  wait**: each park resets ``pause_parks`` first (the
  ``run_slice.wait_parks`` / ``release_poll.placement_parks`` shape —
  the shared six-park cap is for window pauses, not a healthy gate),
  so the job keeps polling on the run's own clock for as long as the
  run reports live. A soak gate runs ~60 min and ship's own CI budget
  is 150 min, while six 2-min late-interval parks cover ~12 — counting
  the wait against the cap spent a node retry per ~36 min and filed the
  retry-cap ask on a GREEN gate. A run still going past
  :data:`LIVE_STALL_ASK_AFTER` files ONE records-only ask on the plan
  root — the no-run ladder's rung shape, deduped on
  ``meta.ci_verdict_stall.run_asked`` — and keeps watching: a wedged
  run pages a human without a retry ever being spent.
- No run yet, an unreadable GitHub, or a stale pre-rerun verdict keep
  the park cap: past six parks the job fails "still pending"; the tick
  re-dispatches it up to the node's ``meta.retries`` (the template sets
  3), then files the usual retry-cap ask.

``dedupe_pending`` also holds a recurring-todo arming to one poll.

**Restamp mode** (``params.restamp``, the release-fix loop's ``restamp``
node between the fix nodes and the poll, release-train T1): no verdict
read — the job reads ``release/r<N>``'s head through the GitHub refs
API (:func:`fetch_head`) and moves ``meta.release`` onto it with no cut
(:func:`precis.plans.release.record_head`: sha, range end, the
``tests-green`` gate and the loop's ``until``). The poll after it then
waits on the run the fixes' push started; the digest of a second red
reads that run. A head equal to the recorded sha splits on the fix
nodes' closes (:func:`_restamp_owed`, gr664485): a close is not a push
— a hand close (gr663909) releases the restamp while the
``ship --onto`` push is still in flight. When a blocker may still owe
the branch a push — a recorded ``landed: <sha>`` the head does not
show, a held land, an open or bare-``done`` close with no legible
outcome — the restamp parks and re-reads the head like a pending poll,
then fails loudly at the park cap rather than leaving ``meta.release``
on the red cut and letting the poll judge the stale run. When every
blocker closed with no land owed (verdict closes, ``won't-do``,
``landed: nothing``) an unmoved head writes nothing, as before. The
no-run ladder's clock is ``meta.release.head_at`` once a
restamp set it, else ``cut_at``.

A poll the plan tick mints for a ``validated:`` non-repro rerun
(``meta.rerun_of``) carries ``meta.rerun_attempt_before`` — the run's
``run_attempt`` before ``gh run rerun`` fired. The run id survives a
rerun, so a completed verdict at or below that attempt is the OLD
run's verdict: the poll reads it as pending (same park-and-repoll
machinery), never records it. Only a verdict on a higher attempt —
the rerun's own — is recorded (gr588028 review, F4).

While GitHub answers but lists NO run on the cut's sha — a dropped or
very-late push event would otherwise pend forever (gr589270: r250's
run queued six minutes after its branch; a dropped one never lands) —
the poll walks the no-run ladder against ``meta.release.cut_at``: a
deduped stall note on the poll node at 20 minutes, one
``gh workflow run check.yml --ref release/r<N>`` workflow_dispatch at
40 (the release ops' factory ``gh`` credential, ``factory_git`` — its
Actions write scope is unverified, gr588028 — so a refusal is noted,
never retried), and one ``ask-user`` ruling on the plan root at 90.
Each rung records the sha it fired for on the node's
``meta.ci_verdict_stall``, so re-parks and re-dispatched jobs fire
once per cut; the note and the ask commit in one transaction with
their marker, and the dispatch claim is stamped before ``gh`` runs,
so no failure path re-fires the one-shot. A run appearing takes the
normal path. An unreadable GitHub (``runs`` ``None``) is not a
confirmed absence and walks no rung.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.plans import release as _release
from precis.plans.gates import sha_deployed
from precis.plans.nodes import finish_mint
from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo.guards import check_ask_meta
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "ci_verdict_poll"

#: Wait between polls while CI runs.
POLL_INTERVAL = timedelta(minutes=10)
#: Wait between polls once the run is :data:`POLL_FAST_AFTER` old —
#: check.yml is near its end then, so a fixed 10-minute wait adds ~4
#: minutes to the average gate for nothing (gr611302).
POLL_INTERVAL_LATE = timedelta(minutes=2)
POLL_FAST_AFTER = timedelta(minutes=18)
#: The workflow whose run is the release verdict (scripts/release's gate).
WORKFLOW_FILE = "check.yml"
OK_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_RUNS_URL = "https://api.github.com/repos/{repo}/actions/runs"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_TIMEOUT_S = 20.0
_GH_TIMEOUT_S = 60.0

#: The no-run ladder's rungs (gr589270, module docstring), measured
#: from ``meta.release.cut_at``: the stall note, the one
#: workflow_dispatch retry, the plan-root ask.
STALL_NOTE_AFTER = timedelta(minutes=20)
STALL_DISPATCH_AFTER = timedelta(minutes=40)
STALL_ASK_AFTER = timedelta(minutes=90)
#: The live-run rung's clock (gr656644), measured from the picked run's
#: ``created_at``: a gate still going past this age is past ship's own
#: CI budget (``PRECIS_REMOTE_CI_TIMEOUT_MIN``, 150 min) — likely wedged
#: or queued behind dead runners — so the poll files ONE records-only
#: ask on the plan root and keeps watching. The wait itself never fails
#: while the run reports live, so this is the human page for a stuck
#: gate, not a give-up.
LIVE_STALL_ASK_AFTER = timedelta(minutes=180)
#: The poll node's ``meta.<key>`` dedupe marker — ``{sha, noted?,
#: dispatched?, asked?, run_asked?}``: one fire per rung per cut,
#: across re-parks and re-dispatched jobs (a restamp moves
#: ``meta.release`` to a new sha, which resets the ladder).
STALL_META_KEY = "ci_verdict_stall"
_COMMENT_KIND = "todo_comment"
_ASK_CONTEXT_CAP = 600

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's poll node: sha/range/repo come from its root's meta.release.
        "todo_id": {"type": "integer"},
        # Explicit candidate (40-hex or abbreviated) and its verdict range.
        "sha": {"type": "string"},
        "range": {"type": "string"},
        # owner/repo on GitHub; else meta.release.repo / release.repo / git.
        "repo": {"type": "string"},
        # The release-fix loop's restamp node (release-train T1): read
        # origin/release/r<N>'s head and restamp meta.release onto it
        # (precis.plans.release.record_head) instead of polling a verdict.
        "restamp": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark release pass: read check.yml's GitHub Actions run on a release "
    "cut's sha and record green/red in release.ci_verdicts (the tests-green "
    "gate reads it); parks itself and re-polls every 10 min while CI runs "
    "(2 min once the run is 18+ min old, gr611302)."
)


class PollError(Exception):
    """A fault that ends the job failed (bad input, a 404 repo)."""


def _http_get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
    """Seam: one GET through the SSRF-pinned client under the politeness
    rules (``with_backoff``: retries, breaker, the per-host limiter);
    returns the final response."""
    from precis.utils.http import http_client, with_backoff
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=_TIMEOUT_S, headers=headers) as client:

        def send() -> Any:
            return safe_get(client, url, params=params)

        return with_backoff(send, service="github", url=url)


def _repo_from_git() -> str | None:
    try:
        from precis.workers.job_types import release_cut, run_slice

        return release_cut.origin_repo(run_slice.slice_root())
    except Exception:
        return None


def resolve_target(store: Any, params: dict[str, Any]) -> dict[str, str]:
    """``{sha, range, branch, repo}`` for this poll; raises :class:`PollError`."""
    rel: dict[str, Any] = {}
    todo_id = params.get("todo_id")
    if isinstance(todo_id, int) and store is not None:
        rel = _release.release_of(store, todo_id) or {}
    sha = str(params.get("sha") or rel.get("sha") or "").strip().lower()
    if not _SHA_RE.match(sha):
        where = f"td{todo_id}'s plan root meta.release" if todo_id else "params.sha"
        raise PollError(
            f"{NAME}: no candidate sha ({where} has none: {sha!r}); next: let "
            "release_cut record the cut, or pass params.sha"
        )
    rng = str(params.get("range") or rel.get("range") or sha).strip()
    rn = str(rel.get("rN") or "")
    from precis import settings

    repo = (
        _release.valid_repo(params.get("repo"))
        or _release.valid_repo(rel.get("repo"))
        or _release.valid_repo(settings.get_str("release.repo", store=store))
        or _repo_from_git()
    )
    if not repo:
        raise PollError(
            f"{NAME}: no GitHub owner/repo (params.repo, meta.release.repo, the "
            "release.repo setting and the checkout's origin all empty); next: "
            "set release.repo"
        )
    return {
        "sha": sha,
        "range": rng,
        "branch": f"release/{rn}" if rn else "",
        "repo": repo,
        # The ladder's clock (gr589270): absent or unparsable -> no rungs.
        # A restamped head (release-train T1) starts its own clock — its
        # run was pushed with the fix, not at the cut.
        "cut_at": str(rel.get("head_at") or rel.get("cut_at") or ""),
    }


_REF_URL = "https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fetch_head(repo: str, branch: str) -> str | None:
    """``branch``'s head sha on GitHub, else ``None`` when unreadable
    (network, rate limit, a missing ref — the caller parks and retries)."""
    headers = _factory_git.github_api_headers()
    try:
        resp = _http_get(_REF_URL.format(repo=repo, branch=branch), {}, headers)
    except Exception as exc:
        log.info("%s: GitHub ref read failed (%s)", NAME, type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    sha = str(((data or {}).get("object") or {}).get("sha") or "").lower()
    return sha if _FULL_SHA_RE.match(sha) else None


#: A ``landed: <sha>`` comment line — the record a fixer's land_slice
#: leaves on the fix todo (``land_slice._LANDED_COMMENT_RE``). An
#: all-zeros sha is the gate's PENDING_SHA placeholder, not a land.
_LANDED_SHA_RE = re.compile(r"^landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)
#: ``landed: nothing``/other non-sha ``landed:`` lines — a land that
#: pushed no commit owes the branch nothing.
_LANDED_ANY_RE = re.compile(r"^landed:", re.IGNORECASE)
#: The no-land closes a fix todo's comments may carry: the verdicts
#: (``no-op:``/``moot:``/``validated:`` — ``plans.nodes.any_verdict``'s
#: set), the release-fix ``verified:`` escape, and the digest's
#: ``waived:`` close. A close carrying one of these owes the release
#: branch no push.
_NO_LAND_RE = re.compile(
    r"^\s*(?:no-op|moot|validated|verified|waived)\s*:", re.IGNORECASE
)
#: Terminal todo statuses a fix node may close with owing no push —
#: ``precis.todo.TERMINAL_TODO_STATUSES`` minus ``done``.
_NO_LAND_STATUS = frozenset({"won't-do", "auto-timeout"})


def _restamp_blockers(store: Any, restamp_id: int) -> list[int]:
    """The todos the restamp node waits on — the iteration's fix nodes
    (``src`` waits on ``dst`` in a ``blocked-by`` link)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE relation = 'blocked-by' "
            "AND src_ref_id = %s ORDER BY dst_ref_id",
            (restamp_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _comment_outcome(text: str) -> tuple[str, str] | None:
    """The close outcome one comment records: ``('landed', sha)`` a push
    was recorded, ``('no-land', line)`` a close that owes the release
    branch nothing — a first-line ``landed:``/verdict comment, or a
    ``handoff:`` comment's outcome line. The finishing contract puts the
    verdict on the handoff's own first line (``handoff: no-op: …``,
    ``handoff: verified: …``), so the ``handoff:`` prefix is stripped
    before that line is read — not only its later lines. ``None`` —
    other comments never count: a brief quoting the verdict grammar is
    no outcome (td604234)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if lines[0].lower().startswith("handoff:"):
        embedded = lines[0][len("handoff:") :].strip()
        candidates = ([embedded] if embedded else []) + lines[1:]
    else:
        candidates = lines[:1]
    for s in candidates:
        m = _LANDED_SHA_RE.match(s)
        if m is not None and m.group(1).lstrip("0"):
            return "landed", m.group(1).lower()
        if _LANDED_ANY_RE.match(s) or _NO_LAND_RE.match(s):
            return "no-land", s[:80]
    return None


def _fix_owed(store: Any, fix_id: int) -> str | None:
    """Why a todo blocking the restamp may still owe ``release/r<N>`` a
    push, else ``None``: a held land (``meta.pending_land``) or a
    ``landed: <sha>`` comment the head does not show, a still-open node
    (never reached — the restamp is blocked-by it), or a bare ``done``
    with no legible outcome — a hand close (gr663909) whose
    ``ship --onto`` may still be in flight. A ``won't-do``/verdict/
    ``landed: nothing`` close owes nothing.

    The outcome scan reads only the CURRENT attempt's ``todo_comment``
    chunks — newest-first down to (not through) the second
    ``handoff:``/``brief:`` boundary, the ``run_slice._posted_verdict``
    convention — so an earlier attempt's ``no-op:`` close cannot read as
    this attempt's outcome and hide a bare ``done`` whose push is still
    in flight. A ``landed:``/verdict line inside a job summary or the
    todo body is quoted grammar (td604234), not the close's outcome."""
    from precis.plans.templates import PENDING_LAND_KEY
    from precis.workers.job_types import run_slice as _rs

    ref = store.get_ref(kind="todo", id=fix_id)
    if ref is None:
        return None
    if isinstance((ref.meta or {}).get(PENDING_LAND_KEY), dict):
        return f"td{fix_id} holds a pending land"
    status = "open"
    for tag in store.tags_for(fix_id):
        if tag.prefix == "STATUS":
            status = str(tag.value)
            break
    if status in _NO_LAND_STATUS:
        return None
    if status != "done":
        return f"td{fix_id} is still {status}"
    comments = sorted(
        (
            c
            for c in store.chunks.list_chunks_for_ref(fix_id)
            if c.chunk_kind == _COMMENT_KIND and c.text
        ),
        key=lambda c: (int(c.ord), int(getattr(c, "id", 0))),
        reverse=True,
    )
    boundary_seen = False
    for c in comments:
        body = str(c.text).strip()
        lowered = body.lower()
        if (
            lowered.startswith("handoff:")
            or lowered.startswith("brief:")
            or body == _rs._HANDOFF_WITHHELD
        ):
            if boundary_seen:
                break
            boundary_seen = True
        hit = _comment_outcome(body)
        if hit is not None:
            kind, detail = hit
            if kind == "landed":
                return f"td{fix_id} recorded landed: {detail[:12]}"
            return None
    return f"td{fix_id} closed done with no recorded land or verdict"


def _restamp_owed(store: Any, restamp_id: int) -> list[str]:
    """One line per fix todo blocking the restamp whose close may still
    owe ``release/r<N>`` a push (:func:`_fix_owed`) — the head-equals-
    recorded-sha wait is for these. An unreadable blocker list or node
    counts as owed: guessing "no fix landed" is the failure gr664485
    files, so an unprovable close waits and then refuses loudly."""
    try:
        blockers = _restamp_blockers(store, restamp_id)
    except Exception:
        log.warning("%s: restamp blockers unreadable", NAME, exc_info=True)
        return ["the iteration's fix nodes are unreadable"]
    owed: list[str] = []
    for fix_id in blockers:
        try:
            why = _fix_owed(store, fix_id)
        except Exception:
            log.warning("%s: fix close td%s unreadable", NAME, fix_id, exc_info=True)
            why = f"td{fix_id}'s close outcome is unreadable"
        if why is not None:
            owed.append(why)
    return owed


def _restamp(ctx: Any, params: dict[str, Any]) -> None:
    """The restamp node (release-train T1, :data:`PARAMS_SCHEMA`
    ``restamp``): the fix round's lands moved ``origin/release/r<N>``,
    so ``meta.release`` moves onto its head
    (:func:`precis.plans.release.record_head`) — no cut. The next node,
    the verdict poll, then waits on the check.yml run the fixes' push
    started, and a red there digests that run, never the cut's. An
    unreadable head parks like a pending poll — and so does a head that
    has not moved while a fix node's close may still owe a push
    (:func:`_restamp_owed`, gr664485): the close released the restamp,
    it is not proof ``origin`` moved."""
    try:
        target = resolve_target(ctx.store, params)
    except PollError as exc:
        ctx.record_failure(str(exc))
        return
    if not target["branch"]:
        ctx.record_failure(f"{NAME}: restamp needs a recorded cut (meta.release.rN)")
        return
    head = fetch_head(target["repo"], target["branch"])
    if head is None:
        note = f"{NAME}: restamp — {target['branch']} head unreadable; retrying"
        ctx.append_chunk("job_summary", note)
        defer = getattr(ctx, "defer", None)
        if defer is not None and defer(datetime.now(UTC) + POLL_INTERVAL_LATE, note):
            return
        ctx.record_failure(note)
        return
    if head.startswith(target["sha"]) or target["sha"].startswith(head):
        todo_id = params.get("todo_id")
        owed: list[str]
        if (
            isinstance(todo_id, int)
            and not isinstance(todo_id, bool)
            and ctx.store is not None
        ):
            owed = _restamp_owed(ctx.store, todo_id)
        elif ctx.store is None:
            owed = []
        else:
            owed = ["the restamp node's todo_id is missing"]
        if not owed:
            ctx.append_chunk(
                "job_summary",
                f"{NAME}: restamp — {target['branch']} head {head[:9]} is the "
                "recorded sha (no fix landed); meta.release unchanged",
            )
            ctx.set_meta(ci_verdict_restamp={"head": head, "moved": False})
            return
        why = "; ".join(owed)
        note = (
            f"{NAME}: restamp — {target['branch']} head {head[:9]} is the "
            f"recorded sha but {why}: the push may still be in flight; "
            f"re-checking in {POLL_INTERVAL_LATE}"
        )
        ctx.append_chunk("job_summary", note)
        ctx.set_meta(ci_verdict_restamp={"head": head, "moved": False, "owed": owed})
        defer = getattr(ctx, "defer", None)
        if defer is not None and defer(datetime.now(UTC) + POLL_INTERVAL_LATE, note):
            return
        ctx.record_failure(
            f"{NAME}: restamp — {target['branch']} head is still the "
            f"recorded sha {target['sha'][:9]} and {why}: cannot move "
            f"meta.release; next: push the fix to {target['branch']} "
            f"(scripts/ship --onto {target['branch']}), or close the fix "
            "node(s) with a no-land verdict (no-op:/moot: — a bare done "
            "never proves nothing was pushed)"
        )
        return
    notes = _release.record_head(ctx.store, int(params["todo_id"]), head)
    ctx.set_meta(ci_verdict_restamp={"head": head, "moved": True})
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: restamp — {target['branch']} {target['sha'][:9]} -> "
        f"{head[:9]}\n" + "\n".join(notes),
    )


def fetch_runs(repo: str, sha: str) -> list[dict[str, Any]] | None:
    """The workflow runs on ``sha``; ``None`` when GitHub is unreadable
    (network, rate limit, 5xx — the caller treats that as pending)."""
    from precis.utils.http import VendorError

    headers = _factory_git.github_api_headers()
    try:
        resp = _http_get(
            _RUNS_URL.format(repo=repo), {"head_sha": sha, "per_page": 50}, headers
        )
    except Exception as exc:
        # A VendorError's text is the status line (plus a rate limit's quota
        # and reset), never the request headers.
        why = str(exc) if isinstance(exc, VendorError) else type(exc).__name__
        log.info("%s: GitHub read failed (%s)", NAME, why)
        return None
    if resp.status_code == 404:
        raise PollError(
            f"{NAME}: GitHub has no repo {repo!r} (404); next: fix release.repo"
        )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else None


def _picked_run(
    runs: list[dict[str, Any]], sha: str, branch: str = ""
) -> dict[str, Any] | None:
    """The newest check.yml run on ``sha`` (on ``branch`` when given) —
    the run :func:`verdict_of` judges, or ``None``."""
    mine = [
        r
        for r in runs
        if str(r.get("path") or "").endswith(f"/{WORKFLOW_FILE}")
        and str(r.get("head_sha") or "").lower().startswith(sha[:7])
        and (not branch or r.get("head_branch") == branch)
    ]
    return max(mine, key=lambda r: int(r.get("id") or 0)) if mine else None


def _run_age(run: dict[str, Any] | None, now: datetime) -> timedelta | None:
    """The picked run's age from its ``created_at``; ``None`` without a
    run or a parsable stamp — no clock, the slow interval."""
    if run is None:
        return None
    try:
        created = datetime.strptime(
            str(run.get("created_at") or ""), "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return None
    return now - created.replace(tzinfo=UTC)


def poll_interval(run_age: timedelta | None) -> timedelta:
    """Wait to the next poll: :data:`POLL_INTERVAL_LATE` once the run is
    :data:`POLL_FAST_AFTER` old (check.yml is near its end; gr611302),
    else :data:`POLL_INTERVAL`."""
    if run_age is not None and run_age >= POLL_FAST_AFTER:
        return POLL_INTERVAL_LATE
    return POLL_INTERVAL


def _run_attempt(run: dict[str, Any]) -> int:
    """The run's ``run_attempt`` as an int; 0 when absent/unreadable —
    a verdict that cannot prove it is past the rerun baseline is not
    fresh (gr588028 review, F4)."""
    try:
        return int(run.get("run_attempt") or 0)
    except (TypeError, ValueError):
        return 0


def _rerun_baseline(meta: dict[str, Any]) -> int | None:
    """The ``rerun_attempt_before`` the tick stamped on a rerun poll
    job (``meta.rerun_attempt_before``), else ``None`` — ordinary polls
    carry no baseline and are untouched."""
    raw = meta.get("rerun_attempt_before")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def verdict_of(
    runs: list[dict[str, Any]], sha: str, branch: str = ""
) -> tuple[str, str, str]:
    """``(green|red|pending, run_id, why)`` from the newest check.yml run on
    ``sha`` (on ``branch`` when given)."""
    run = _picked_run(runs, sha, branch)
    if run is None:
        where = f" on {branch}" if branch else ""
        return "pending", "", f"no {WORKFLOW_FILE} run{where} for {sha[:9]} yet"
    run_id = str(run.get("id") or "")
    status = str(run.get("status") or "")
    if status != "completed":
        return "pending", run_id, f"run {run_id} is {status or 'unknown'}"
    conclusion = str(run.get("conclusion") or "")
    if conclusion in OK_CONCLUSIONS:
        return "green", run_id, f"run {run_id} concluded {conclusion}"
    return (
        "red",
        run_id,
        f"run {run_id} concluded {conclusion or 'without a conclusion'}",
    )


# ── the no-run ladder (gr589270) ──────────────────────────────────


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Seam: ``gh <args>`` as the factory's GitHub identity (``GH_TOKEN``
    from the vault token, ``factory_git`` — the release ops' credential;
    the host's ``gh auth`` when there is none). Never raises on a
    non-zero exit."""
    try:
        return subprocess.run(
            [os.environ.get("PRECIS_GH_BIN") or "gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GH_TIMEOUT_S,
            env=_factory_git.git_auth_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _node_comment(store: Any, todo_id: int, text: str, *, conn: Any = None) -> None:
    """Append ``text`` as a ``todo_comment`` on the poll node (the
    run_slice ``_append_comment`` shape, worker-stamped)."""
    existing = list(store.chunks.list_chunks_for_ref(todo_id, conn=conn))
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=ord_,
                text=text,
                meta={"chunk_kind": _COMMENT_KIND, **worker_comment_meta()},
            )
        ],
        conn=conn,
    )


def _cut_stamp(value: Any) -> datetime | None:
    """``meta.release.cut_at`` as a datetime; ``None`` when absent or
    unparsable — no clock, no ladder."""
    try:
        parsed = datetime.strptime(str(value or ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _stall_marker(store: Any, todo_id: int, sha: str) -> dict[str, Any] | None:
    """The poll node's stall marker for ``sha`` — a fresh ``{sha: sha}``
    when the marker names an older cut or is absent; ``None`` when the
    node is unreadable."""
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None:
        return None
    marker = (ref.meta or {}).get(STALL_META_KEY)
    if not isinstance(marker, dict) or marker.get("sha") != sha:
        return {"sha": sha}
    return dict(marker)


def _mint_stall_ask(
    store: Any,
    node_id: int,
    root: int,
    *,
    title: str,
    context: str,
    body: str,
    marker: dict[str, Any],
) -> int:
    """The ONE ruling todo a stall rung files under the plan root (the
    ``_mint_rerun_ask`` shape): it RECORDS the stall — ``blocks:
    "nothing"``, ``acknowledged`` the only option. The poll node's
    marker commits in the same transaction, so a failed write leaves
    neither and the rung retries next tick instead of filing a second
    ask."""
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["acknowledged"],
            "blocks": "nothing",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.stamp_ref_meta(node_id, {STALL_META_KEY: marker}, conn=conn)
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _mint_no_run_ask(
    store: Any,
    node_id: int,
    root: int,
    target: dict[str, str],
    mins: int,
    marker: dict[str, Any],
) -> int:
    """The no-run ladder's ask at 90 minutes (gr589270): a confirmed-
    absent run past every retry is a dropped push event."""
    branch, sha = target["branch"], target["sha"]
    where = branch or "the release branch"
    title = f"{NAME}: no {WORKFLOW_FILE} run on {where} for {sha[:9]} ({mins}m)"
    context = (
        f"no {WORKFLOW_FILE} run on {where} for {sha[:9]} {mins} min after "
        "the cut — the push event was likely dropped; answering executes "
        "nothing"
    )
    body = (
        f"A release cut pushed {where} at {sha[:9]}, but GitHub still "
        f"shows no {WORKFLOW_FILE} run for it {mins} minutes later — the "
        "push event was likely dropped (gr589270: a late event queues "
        "minutes behind the branch, a dropped one never lands). The "
        "verdict poll already fired its one workflow_dispatch retry and "
        "keeps polling; this ask only records the stall — a push or a "
        "restamp fixes it, answering 'acknowledged' closes it."
    )
    return _mint_stall_ask(
        store,
        node_id,
        root,
        title=title,
        context=context,
        body=body,
        marker=marker,
    )


def _mint_live_ask(
    store: Any,
    node_id: int,
    root: int,
    target: dict[str, str],
    run: dict[str, Any],
    mins: int,
    marker: dict[str, Any],
) -> int:
    """The live-run rung's ask (gr656644): a gate still going past
    :data:`LIVE_STALL_ASK_AFTER` — past ship's own 150-min CI budget —
    is likely wedged or queued behind dead runners."""
    branch, sha = target["branch"], target["sha"]
    where = branch or "the release branch"
    run_id = str(run.get("id") or "")
    status = str(run.get("status") or "unknown")
    title = (
        f"{NAME}: {WORKFLOW_FILE} run {run_id} still {status} {mins}m "
        f"after the cut ({sha[:9]})"
    )
    context = (
        f"{WORKFLOW_FILE} run {run_id} on {where} for {sha[:9]} is still "
        f"{status} {mins} min after the cut — past ship's own 150-min CI "
        "budget; the run is likely wedged; answering executes nothing"
    )
    body = (
        f"A release cut pushed {where} at {sha[:9]}, and its {WORKFLOW_FILE} "
        f"run {run_id} is still {status} {mins} minutes later (gr656644: "
        "a live gate's wait is attempt-free — it never spends a retry — "
        "so this ask is the human page for a stuck run). The verdict "
        "poll keeps watching and records the verdict the moment the run "
        "completes; this ask only records the stall — answering "
        "'acknowledged' closes it."
    )
    return _mint_stall_ask(
        store,
        node_id,
        root,
        title=title,
        context=context,
        body=body,
        marker=marker,
    )


def _no_run_stall(
    ctx: Any, todo_id: Any, target: dict[str, str], *, now: datetime
) -> list[str]:
    """The gr589270 ladder for a confirmed-absent run (module docstring):
    note at :data:`STALL_NOTE_AFTER`, one dispatch at
    :data:`STALL_DISPATCH_AFTER`, one ask at :data:`STALL_ASK_AFTER`,
    each deduped by the node's :data:`STALL_META_KEY` marker. Each rung
    persists its own marker: the note and the ask commit with the write
    they dedupe, and the dispatch claim is stamped BEFORE ``gh`` runs —
    a later failure (comment write, job crash) can never re-fire the
    one-shot. Returns the actions taken, for the summary; ``[]`` without
    a poll node, a clock or a readable marker — and on any write failure
    (the poll's park is the verdict path; a ladder fault degrades to
    next tick, never a job failure)."""
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or ctx.store is None:
        return []
    cut_at = _cut_stamp(target.get("cut_at"))
    if cut_at is None:
        return []
    age = now - cut_at
    if age < STALL_NOTE_AFTER:
        return []
    sha, branch, repo = target["sha"], target["branch"], target["repo"]
    where = f" on {branch}" if branch else ""
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        marker = _stall_marker(ctx.store, todo_id, sha)
    except Exception:
        log.warning("%s: stall marker read failed", NAME, exc_info=True)
        return []
    if marker is None:
        return []
    actions: list[str] = []
    try:
        if "noted" not in marker:
            marker["noted"] = stamp
            with ctx.store.tx() as conn:
                _node_comment(
                    ctx.store,
                    todo_id,
                    f"{NAME}: no {WORKFLOW_FILE} run{where} for {sha[:9]} after "
                    "20 min — the push event may have been dropped; the poll "
                    "keeps watching and re-fires the gate once at 40 min",
                    conn=conn,
                )
                ctx.store.stamp_ref_meta(todo_id, {STALL_META_KEY: marker}, conn=conn)
            actions.append("posted the 20-min stall note on the poll node")
        if age >= STALL_DISPATCH_AFTER and "dispatched" not in marker and branch:
            argv = [
                "workflow",
                "run",
                WORKFLOW_FILE,
                "--ref",
                branch,
                "--repo",
                repo,
            ]
            shown = f"gh {' '.join(argv)}"
            # The one-shot claim must persist BEFORE gh fires — an
            # unrecorded dispatch (a later comment-write failure or a
            # crash) is the repeat-dispatch bug, including after a 403.
            marker["dispatched"] = stamp
            ctx.store.stamp_ref_meta(todo_id, {STALL_META_KEY: marker})
            proc = _gh(argv)
            if proc.returncode == 0:
                detail = (
                    f"{NAME}: {shown} — the gate re-fires under "
                    "workflow_dispatch (once per sha; the poll keeps watching)"
                )
            else:
                lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = f": {lines[-1][:160]}" if lines else ""
                detail = (
                    f"{NAME}: {shown} refused (exit {proc.returncode}{tail}) "
                    "— not retried; the poll keeps watching"
                )
            actions.append(f"fired the one-shot gate dispatch ({shown})")
            _node_comment(ctx.store, todo_id, detail)
        if age >= STALL_ASK_AFTER and "asked" not in marker:
            root = _release.plan_root(ctx.store, todo_id)
            mins = int(age.total_seconds() // 60)
            marker["asked"] = stamp
            ask_id = _mint_no_run_ask(ctx.store, todo_id, root, target, mins, marker)
            actions.append(f"filed the stall ask td{ask_id} on plan td{root}")
    except Exception:
        log.warning("%s: no-run stall ladder failed", NAME, exc_info=True)
    return actions


def _live_run_stall(
    ctx: Any,
    todo_id: Any,
    target: dict[str, str],
    run: dict[str, Any],
    *,
    now: datetime,
) -> list[str]:
    """The gr656644 rung for a still-running gate: past
    :data:`LIVE_STALL_ASK_AFTER` (run age from ``created_at``) ONE
    records-only ask lands on the plan root — deduped by the node's
    :data:`STALL_META_KEY` ``run_asked`` marker, committed with the ask —
    while the poll keeps its attempt-free watch. ``[]`` without a poll
    node, a run clock or a readable marker, and on any write failure
    (the park is the verdict path; a rung fault degrades to next tick,
    never a job failure)."""
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or ctx.store is None:
        return []
    age = _run_age(run, now)
    if age is None or age < LIVE_STALL_ASK_AFTER:
        return []
    try:
        marker = _stall_marker(ctx.store, todo_id, target["sha"])
    except Exception:
        log.warning("%s: stall marker read failed", NAME, exc_info=True)
        return []
    if marker is None or "run_asked" in marker:
        return []
    try:
        marker["run_asked"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        root = _release.plan_root(ctx.store, todo_id)
        mins = int(age.total_seconds() // 60)
        ask_id = _mint_live_ask(ctx.store, todo_id, root, target, run, mins, marker)
    except Exception:
        log.warning("%s: live-run stall ask failed", NAME, exc_info=True)
        return []
    return [f"filed the live-run stall ask td{ask_id} on plan td{root}"]


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher (module docstring)."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    if params.get("restamp"):
        _restamp(ctx, params)
        return
    try:
        target = resolve_target(ctx.store, params)
    except PollError as exc:
        ctx.record_failure(str(exc))
        return
    sha, rng = target["sha"], target["range"]
    if ctx.store is not None:
        try:
            deployed = sha_deployed(ctx.store, sha)
        except Exception:
            deployed = None  # an unreadable prod head cannot prove shipping
        if deployed is not None and deployed.open:
            # gr582590: a verdict recorded for a sha prod already runs arms
            # the deploy ask on a shipped sha — the pass is stale, not green.
            ctx.record_failure(
                f"{NAME}: {sha[:9]} is already on prod ({deployed.why}); "
                "recording a verdict would arm a deploy of a shipped sha; "
                "next: drop this release pass — the cut is stale"
            )
            return
    try:
        runs = fetch_runs(target["repo"], sha)
    except PollError as exc:
        ctx.record_failure(str(exc))
        return
    picked = _picked_run(runs, sha, target["branch"]) if runs else None
    if runs is None:
        state, run_id, why = "pending", "", "GitHub unreadable (network or rate limit)"
    else:
        state, run_id, why = verdict_of(runs, sha, target["branch"])
    meta: dict[str, Any] = {"sha": sha, "range": rng, "state": state, "run_id": run_id}
    if ctx.meta.get("rerun_of") is not None and picked is not None:
        # A rerun poll records the run's freshness evidence too — the
        # tick and the stall path see what the poll saw (F4).
        meta["run_attempt"] = picked.get("run_attempt")
        meta["updated_at"] = picked.get("updated_at")
    before = _rerun_baseline(ctx.meta)
    if before is not None and picked is not None and state in ("green", "red"):
        # A completed verdict at or below the pre-rerun attempt is the
        # OLD run's verdict — GitHub has not flipped the rerun yet:
        # pending, not the rerun's verdict (gr588028 review, F4; the
        # run id stays the same across a rerun).
        attempt = _run_attempt(picked)
        if attempt <= before:
            state = "pending"
            meta["state"] = state
            why = (
                f"run {run_id} attempt {attempt} is the pre-rerun "
                f"baseline (<= {before}) — the rerun's own verdict has "
                "not landed"
            )
    ctx.set_meta(ci_verdict_poll=meta)
    if state in ("green", "red"):
        line = _release.record_ci_verdict(ctx.store, sha, rng, state, run_id)
        ctx.append_chunk("job_summary", f"{NAME}: {sha[:9]} {state} ({why}); {line}")
        log.info("%s: %s %s", NAME, sha[:9], state)
        return
    stall: list[str] = []
    now = datetime.now(UTC)
    live = picked is not None and str(picked.get("status") or "") != "completed"
    if runs is not None and picked is None:
        # GitHub answered and no run exists for the sha: the confirmed-
        # absent case the gr589270 ladder covers (an in-progress run or
        # an unreadable GitHub is ordinary pending, no rungs).
        stall = _no_run_stall(ctx, params.get("todo_id"), target, now=now)
    elif live and picked is not None:
        stall = _live_run_stall(ctx, params.get("todo_id"), target, picked, now=now)
    interval = poll_interval(_run_age(picked, now))
    note = f"{NAME}: pending on {sha[:9]} ({why}); next poll in {interval}"
    if stall:
        note += "; " + "; ".join(stall)
    ctx.append_chunk("job_summary", note)
    defer = getattr(ctx, "defer", None)
    wake = now + interval
    if live:
        # gr656644: a still-running gate is an attempt-free wait, not a
        # spent retry — reset the shared park counter (it budgets window
        # pauses, not a healthy CI run) and park on the run's own clock;
        # LIVE_STALL_ASK_AFTER above is the human page if it wedges. A
        # refused or absent defer still fails attempt-free, so the tick
        # re-queues the poll without counting a node attempt.
        if defer is not None:
            ctx.set_meta(pause_parks=0)
            if defer(wake, note):
                return
        from precis.workers.job_types import run_slice as _rs

        ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: "ci-wait"})
        ctx.record_failure(
            f"{NAME}: CI still pending on {sha[:9]} and the park was "
            f"refused ({why}); re-queued attempt-free — the poll keeps "
            "watching on the next dispatch"
        )
        return
    if defer is not None and defer(wake, note):
        return
    ctx.record_failure(
        f"{NAME}: CI still pending on {sha[:9]} after the park cap ({why}); "
        "the plan tick re-dispatches within the node's retries"
    )


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    dedupe_pending=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NAME",
    "PARAMS_SCHEMA",
    "POLL_INTERVAL",
    "POLL_INTERVAL_LATE",
    "SPEC",
    "fetch_head",
    "fetch_runs",
    "load",
    "poll_interval",
    "resolve_target",
    "verdict_of",
]
