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
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.plans import release as _release
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


def verdict_of(
    runs: list[dict[str, Any]], sha: str, branch: str = ""
) -> tuple[str, str, str]:
    """``(green|red|pending, run_id, why)`` from the newest check.yml run on
    ``sha`` (on ``branch`` when given)."""
    mine = [
        r
        for r in runs
        if str(r.get("path") or "").endswith(f"/{WORKFLOW_FILE}")
        and str(r.get("head_sha") or "").lower().startswith(sha[:7])
        and (not branch or r.get("head_branch") == branch)
    ]
    if not mine:
        where = f" on {branch}" if branch else ""
        return "pending", "", f"no {WORKFLOW_FILE} run{where} for {sha[:9]} yet"
    run = max(mine, key=lambda r: int(r.get("id") or 0))
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


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher (module docstring)."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    try:
        target = resolve_target(ctx.store, params)
        runs = fetch_runs(target["repo"], target["sha"])
    except PollError as exc:
        ctx.record_failure(str(exc))
        return
    sha, rng = target["sha"], target["range"]
    if runs is None:
        state, run_id, why = "pending", "", "GitHub unreadable (network or rate limit)"
    else:
        state, run_id, why = verdict_of(runs, sha, target["branch"])
    ctx.set_meta(
        ci_verdict_poll={"sha": sha, "range": rng, "state": state, "run_id": run_id}
    )
    if state in ("green", "red"):
        line = _release.record_ci_verdict(ctx.store, sha, rng, state, run_id)
        ctx.append_chunk("job_summary", f"{NAME}: {sha[:9]} {state} ({why}); {line}")
        log.info("%s: %s %s", NAME, sha[:9], state)
        return
    note = f"{NAME}: pending on {sha[:9]} ({why}); next poll in {POLL_INTERVAL}"
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
