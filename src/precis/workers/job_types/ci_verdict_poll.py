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
for the public repo at 60/h; ``GH_TOKEN`` from the env is sent when set).
The newest check.yml run (on the release branch when one is known):

- completed ``success``/``skipped``/``neutral`` → ``green``; any other
  completed conclusion → ``red``. The verdict row
  ``{sha, range, verdict, run_id, at}`` goes into ``release.ci_verdicts``
  (:func:`precis.plans.release.record_ci_verdict`, the row
  ``scripts/release`` writes) and the job succeeds — so the plan tick
  closes the poll node and the ``tests-green`` gate reads the row.
- no run yet, still running, or GitHub unreadable (rate limit, network) →
  **pending**: a ``pending`` summary, then the job parks itself
  (``ctx.defer``, ``STATUS:waiting_time``) for :data:`POLL_INTERVAL`; the
  wake runner re-queues the same job, which polls again. A parked job is
  live, so the tick neither closes the node nor mints a second poll (one
  poll per node: the tick's ``plan_dispatch:td<id>`` idem key). Past the
  park cap (six parks) the job fails "still pending"; the tick re-dispatches
  it up to the node's ``meta.retries`` (the template sets 3), then files the
  usual retry-cap ask.

``dedupe_pending`` also holds a recurring-todo arming to one poll.

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
from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo.guards import check_ask_meta
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "ci_verdict_poll"

#: Wait between polls while CI runs.
POLL_INTERVAL = timedelta(minutes=10)
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
#: The poll node's ``meta.<key>`` dedupe marker — ``{sha, noted?,
#: dispatched?, asked?}``: one fire per rung per cut, across re-parks
#: and re-dispatched jobs (a re-cut restamps ``meta.release`` with a
#: new sha, which resets the ladder).
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
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark release pass: read check.yml's GitHub Actions run on a release "
    "cut's sha and record green/red in release.ci_verdicts (the tests-green "
    "gate reads it); parks itself and re-polls every 10 min while CI runs."
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
        "cut_at": str(rel.get("cut_at") or ""),
    }


def fetch_runs(repo: str, sha: str) -> list[dict[str, Any]] | None:
    """The workflow runs on ``sha``; ``None`` when GitHub is unreadable
    (network, rate limit, 5xx — the caller treats that as pending)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = _http_get(
            _RUNS_URL.format(repo=repo), {"head_sha": sha, "per_page": 50}, headers
        )
    except Exception as exc:
        log.info("%s: GitHub read failed (%s)", NAME, type(exc).__name__)
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


def _mint_no_run_ask(
    store: Any,
    node_id: int,
    root: int,
    target: dict[str, str],
    mins: int,
    marker: dict[str, Any],
) -> int:
    """The ONE ruling todo the ladder files under the plan root at 90
    minutes (the ``_mint_rerun_ask`` shape): it RECORDS the missing run —
    ``blocks: "nothing"``, ``acknowledged`` the only option. The poll
    node's ``asked`` marker commits in the same transaction, so a failed
    write leaves neither and the rung retries next tick instead of
    filing a second ask."""
    branch, sha = target["branch"], target["sha"]
    where = branch or "the release branch"
    context = (
        f"no {WORKFLOW_FILE} run on {where} for {sha[:9]} {mins} min after "
        "the cut — the push event was likely dropped; answering executes "
        "nothing"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["acknowledged"],
            "blocks": "nothing",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = f"{NAME}: no {WORKFLOW_FILE} run on {where} for {sha[:9]} ({mins}m)"
    body = (
        f"A release cut pushed {where} at {sha[:9]}, but GitHub still "
        f"shows no {WORKFLOW_FILE} run for it {mins} minutes later — the "
        "push event was likely dropped (gr589270: a late event queues "
        "minutes behind the branch, a dropped one never lands). The "
        "verdict poll already fired its one workflow_dispatch retry and "
        "keeps polling; this ask only records the stall — a push or a "
        "re-cut fixes it, answering 'acknowledged' closes it."
    )
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
    return int(ref.id)


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


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher (module docstring)."""
    del spec
    params = dict(ctx.meta.get("params") or {})
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
    if runs is not None and picked is None:
        # GitHub answered and no run exists for the sha: the confirmed-
        # absent case the gr589270 ladder covers (an in-progress run or
        # an unreadable GitHub is ordinary pending, no rungs).
        stall = _no_run_stall(ctx, params.get("todo_id"), target, now=datetime.now(UTC))
    note = f"{NAME}: pending on {sha[:9]} ({why}); next poll in {POLL_INTERVAL}"
    if stall:
        note += "; " + "; ".join(stall)
    ctx.append_chunk("job_summary", note)
    defer = getattr(ctx, "defer", None)
    wake = datetime.now(UTC) + POLL_INTERVAL
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
    "SPEC",
    "fetch_runs",
    "load",
    "resolve_target",
    "verdict_of",
]
