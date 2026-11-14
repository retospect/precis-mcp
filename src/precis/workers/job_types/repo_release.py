"""``repo_release``: cut a release of a second repo (td659522 step 3).

The job bumps ``[project].version`` in the repo's ``pyproject.toml`` and the
project's own entry in ``uv.lock``, commits and pushes that, waits for the CI
run on the pushed commit, and tags it when the run is green. The profile
fields it reads are ``RepoProfile.releaser`` / ``release_check_cmds`` /
``ci_workflow`` / ``tag_prefix`` (``precis.plans.lanes``).

The module holds the pure helpers first: no I/O, no git, no network, no
store. The rule they serve: the committed bytes are produced by these text
edits and never by running a tool in the tree (no ``uv lock``, no ``uv
version``), so the diff is exactly the version lines and each edit is checked
by re-parsing both texts with ``tomllib``.

Below them is the job (td659522 step 3 R5 and R6), :mod:`~precis.workers.
repo_land`'s sibling, which takes its shapes from it. Submit refuses a repo
whose profile does not name this job as its ``releaser`` (catpath's does)
and one ``factory.open_repos`` does not list.
Phases, in the handle's ``phase``:

* ``lease`` (parked): :func:`submit` only guards and parks. :func:`poll`
  states the want, then takes the repo lease (``kind="release"``); taken, it
  runs *prepare* in the same poll.
* *prepare* (never a stored phase): fetch the base tip, resume a ``pushed``
  record if the todo carries one, read ``pyproject.toml`` and ``uv.lock`` at
  the tip, pick the version, edit both texts with the pure helpers, commit
  them in a detached release tree and assert the commit is a child of the tip
  touching exactly those two files by one line each. The commit sha is fixed
  in the handle before the verifier is launched.
* ``check``: the profile's ``release_check_cmds`` run detached under
  :func:`repo_land.gate_env`, so the verifier (which executes repo-controlled
  code and config) has no credential and cannot change what is pushed. Green,
  and after the admission and lease re-reads, the todo's ``meta.repo_release``
  is stamped ``state: "pushed"`` and only then is the handle's sha pushed with
  :func:`~precis.workers.factory_git_mirror.push_cas`. A raced push prepares
  again on the new tip (at most :data:`MAX_ROUNDS` rounds per job).
* ``ci`` (parked, the lease held): each due poll reads the workflow runs of
  the pushed sha through :func:`_read_runs` and acts on :func:`ci_verdict`:
  ``pending`` re-parks, ``green`` goes on to the tag, ``red`` stamps the
  record ``red`` and fails charged, ``cancelled`` waits for the branch to be
  quiet and re-runs the run (:func:`_rerun`, at most :data:`MAX_RERUNS`).
  A failure that needs the coordinator (no run ever appeared, the re-runs are
  spent, the token lacks Actions access) carries
  :data:`COORDINATOR_ASK_KEY` in the job meta, which ``plan_dispatch``
  reads (``_coordinator_hit``) to route the node's cap ask.
* tag and record (same poll as the green verdict): admission and lease are
  re-read, the annotated tag is created at the handle's sha with
  :func:`~precis.workers.factory_git_mirror.push_tag_new`, the record becomes
  ``tagged`` with its ``ci`` block, one ``released:`` comment is written, the
  lease is released and the job succeeds. A resume that finds the tag
  already at the recorded sha goes straight to the record.

Failures use :func:`_fail`, not ``land_slice._fail``: a release node is a
mechanical task, not a slice, so there is no claim, ``attempt:<n>`` tag or
pre-land review to touch. A charged failure leaves a ``handoff:`` comment and
fails the job; an attempt-free one also sets ``meta.attempt_free``.
Every failure before the push leaves no ``state: "pushed"`` record.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.plans import pr_class as _pr_class
from precis.plans import release as _release
from precis.plans.nodes import node_kind
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
from precis.workers import factory_git_mirror as _mirror
from precis.workers import repo_land as _rl
from precis.workers import repo_lease as _repo_lease
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import land_slice as _ls
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

__all__ = [
    "COORDINATOR_ASK_KEY",
    "DEADLINE_KILL_META",
    "DESCRIPTION",
    "MAX_ROUNDS",
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "ReleaseError",
    "api_base",
    "bump_version",
    "ci_verdict",
    "kill",
    "pick_bump",
    "pick_run",
    "poll",
    "project_name_version",
    "set_lock_version",
    "set_project_version",
    "submit",
    "tag_name",
    "validate_submit",
]


class ReleaseError(ValueError):
    """A release helper refused its input; the message is one line."""


_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_FEAT = re.compile(r"feat[(:!]")
#: A TOML table header line: ``[a.b]`` or ``[[a.b]]`` with an optional
#: comment. No ``=`` and no nested brackets, so an array continuation such
#: as ``[1, 2],`` inside a multi-line value does not match.
_HEADER = re.compile(r"\s*(\[\[?)\s*([^\[\]=]*?)\s*\]\]?\s*(?:#.*)?")
_VERSION_LINE = re.compile(r"(\s*version\s*=\s*)([\"'])([^\"'\r\n]*)\2(.*)", re.DOTALL)
_NAME_LINE = re.compile(r"\s*name\s*=\s*\"([^\"\r\n]*)\"\s*(?:#.*)?")
_LOCAL_SOURCE = re.compile(
    r"\s*source\s*=\s*\{\s*(?:editable|virtual)\s*=\s*\"\.\"\s*\}\s*(?:#.*)?"
)


def _split_version(current: str) -> tuple[int, int, int]:
    m = _SEMVER.fullmatch(current)
    if m is None:
        raise ReleaseError(f"version {current!r} is not exactly X.Y.Z")
    return int(m[1]), int(m[2]), int(m[3])


def bump_version(current: str, bump: str) -> str:
    """``current`` (exactly ``X.Y.Z``) bumped: ``minor`` is X.Y+1.0, ``patch``
    is X.Y.Z+1. Never major."""
    x, y, z = _split_version(current)
    if bump == "minor":
        return f"{x}.{y + 1}.0"
    if bump == "patch":
        return f"{x}.{y}.{z + 1}"
    raise ReleaseError(f"bump {bump!r} is not 'minor' or 'patch'")


def pick_bump(subjects: Iterable[str]) -> str:
    """``minor`` when any subject is a conventional ``feat`` (``feat`` then
    ``(``, ``:`` or ``!``), else ``patch``."""
    return "minor" if any(_FEAT.match(s) for s in subjects) else "patch"


def tag_name(profile_tag_prefix: str, version: str) -> str:
    """The release tag for ``version`` (must be ``X.Y.Z``)."""
    _split_version(version)
    return f"{profile_tag_prefix}{version}"


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{what} does not parse as TOML: {exc}") from exc


def _replace_version(line: str, new: str) -> str | None:
    """``line`` with its quoted version value replaced, quote style, indent,
    trailing comment and line ending kept; ``None`` when it is no version
    line."""
    m = _VERSION_LINE.fullmatch(line)
    if m is None:
        return None
    rest = m[4]
    return f"{m[1]}{m[2]}{new}{m[2]}{rest}"


def set_project_version(pyproject_text: str, new: str) -> str:
    """``pyproject_text`` with the one static ``version`` line of the
    ``[project]`` table set to ``new``, every other byte kept."""
    _split_version(new)
    old = _parse(pyproject_text, "pyproject.toml")
    lines = pyproject_text.splitlines(keepends=True)
    in_project = False
    hits: list[int] = []
    for i, line in enumerate(lines):
        h = _HEADER.fullmatch(line.rstrip("\r\n"))
        if h is not None:
            in_project = h[1] == "[" and h[2] == "project"
            continue
        if in_project and _VERSION_LINE.fullmatch(line) is not None:
            hits.append(i)
    if not hits:
        raise ReleaseError("[project] has no static version line")
    if len(hits) > 1:
        raise ReleaseError("[project] has more than one version line")
    replaced = _replace_version(lines[hits[0]], new)
    assert replaced is not None  # matched above
    lines[hits[0]] = replaced
    out = "".join(lines)
    expect = copy.deepcopy(old)
    expect["project"]["version"] = new
    if _parse(out, "edited pyproject.toml") != expect:
        raise ReleaseError("pyproject edit changed more than project.version")
    return out


def project_name_version(pyproject_text: str) -> tuple[str, str]:
    """``([project].name, [project].version)`` as strings."""
    project = _parse(pyproject_text, "pyproject.toml").get("project")
    if not isinstance(project, dict):
        raise ReleaseError("pyproject.toml has no [project] table")
    name, version = project.get("name"), project.get("version")
    if not isinstance(name, str):
        raise ReleaseError("[project].name is missing or not a string")
    if not isinstance(version, str):
        raise ReleaseError("[project].version is missing or not a string")
    return name, version


def _norm(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def set_lock_version(lock_text: str, package: str, new: str) -> str:
    """``lock_text`` (a uv lockfile) with the version line of the project's
    own ``[[package]]`` block (name equal to ``package`` after PEP 503
    normalisation, source ``{ editable = "." }`` or ``{ virtual = "." }``) set
    to ``new``, every other byte kept."""
    _split_version(new)
    old = _parse(lock_text, "uv.lock")
    want = _norm(package)
    lines = lock_text.splitlines(keepends=True)
    # (index of the [[package]] block among all of them, version line index)
    hits: list[tuple[int, int | None]] = []
    block = -1
    name: str | None = None
    local = False
    ver: int | None = None
    active = False  # inside the top-level keys of a [[package]] block

    def close() -> None:
        if active and name is not None and _norm(name) == want and local:
            hits.append((block, ver))

    for i, line in enumerate(lines):
        h = _HEADER.fullmatch(line.rstrip("\r\n"))
        if h is not None:
            close()
            active = h[1] == "[[" and h[2] == "package"
            if active:
                block += 1
            name, local, ver = None, False, None
            continue
        if not active:
            continue
        m = _NAME_LINE.fullmatch(line.rstrip("\r\n"))
        if m is not None:
            name = m[1]
        elif _LOCAL_SOURCE.fullmatch(line.rstrip("\r\n")) is not None:
            local = True
        elif _VERSION_LINE.fullmatch(line) is not None:
            ver = i
    close()
    if not hits:
        raise ReleaseError(f"uv.lock has no local package block for {package!r}")
    if len(hits) > 1:
        raise ReleaseError(f"uv.lock has more than one local block for {package!r}")
    block_no, ver = hits[0]
    if ver is None:
        raise ReleaseError(f"uv.lock block for {package!r} has no version line")
    replaced = _replace_version(lines[ver], new)
    assert replaced is not None  # matched above
    lines[ver] = replaced
    out = "".join(lines)
    expect = copy.deepcopy(old)
    try:
        expect["package"][block_no]["version"] = new
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseError("uv.lock package list does not match its blocks") from exc
    if _parse(out, "edited uv.lock") != expect:
        raise ReleaseError("uv.lock edit changed more than one block's version")
    return out


_GITHUB_REMOTE = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
_API_REPOS = "https://api.github.com/repos"


def api_base(remote: str) -> str:
    """The REST base of the repo at ``remote``: ``https://github.com/<owner>/
    <repo>`` (an optional ``.git`` or trailing slash) becomes ``https://api.
    github.com/repos/<owner>/<repo>``. Anything else (another host, ``http``,
    userinfo, a port, a path below the repo) raises :class:`ReleaseError`: the
    workflow reads and the re-run POST go to GitHub and nowhere else."""
    m = _GITHUB_REMOTE.fullmatch(remote.strip())
    if m is None or m[2] in (".", ".."):
        raise ReleaseError(
            "the remote is not a plain https://github.com/<owner>/<repo>"
        )
    return f"{_API_REPOS}/{m[1]}/{m[2]}"


def _in_workflow(run: Mapping[str, Any], workflow: str) -> bool:
    """Whether ``run`` is a run of the workflow file ``workflow`` (its
    ``path`` is that name, or ends in ``/`` plus it)."""
    path = run.get("path")
    return isinstance(path, str) and (path == workflow or path.endswith("/" + workflow))


def pick_run(
    runs: Sequence[Mapping[str, Any]], *, sha: str, workflow: str
) -> Mapping[str, Any] | None:
    """From a "list workflow runs" payload's ``workflow_runs``: the run on
    exactly ``sha`` of the workflow file ``workflow``; the highest
    ``run_attempt`` then the highest ``id`` when several match; ``None`` when
    none do."""
    found = [r for r in runs if r.get("head_sha") == sha and _in_workflow(r, workflow)]
    if not found:
        return None
    return max(
        found,
        key=lambda r: (int(r.get("run_attempt") or 1), int(r.get("id") or 0)),
    )


def ci_verdict(run: Mapping[str, Any] | None, *, rerun_attempt_before: int = 0) -> str:
    """``pending``, ``green``, ``cancelled`` or ``red`` for a picked run. A
    completed run whose ``run_attempt`` is at or below ``rerun_attempt_before``
    is still pending: a re-run was requested and its new attempt has not shown
    up (gr588028, see ``ci_verdict_poll``)."""
    if run is None or run.get("status") != "completed":
        return "pending"
    if int(run.get("run_attempt") or 1) <= rerun_attempt_before:
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "green"
    if conclusion == "cancelled":
        return "cancelled"
    return "red"


# ── The job: spec ──────────────────────────────────────────────────

NAME = "repo_release"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The release node's todo: it carries meta.repo_release.
        "todo_id": {"type": "integer"},
        # The repo to release; its profile's ``releaser`` must be this job.
        "repo": {"type": "string"},
        # ``auto`` picks minor when a ``feat`` landed since the last release.
        "bump": {"type": "string", "enum": ["auto", "minor", "patch"]},
    },
    "required": ["todo_id", "repo"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Cut a release of a second repo, mechanically (no LLM): bump the version "
    "in pyproject.toml and the project's uv.lock entry as text, commit it on "
    "the base tip, verify it credential-free, push it, wait for the CI run on "
    "that commit and tag it when green. Holds the repo's land lease from the "
    "first poll to the end. Refused while PRECIS_FACTORY=off, for a repo "
    "whose profile names no releaser, and for a repo that is not open."
)

#: ``meta.attempt_free`` of this job's attempt-free failures.
ATTEMPT_FREE_MARK = "infra-release"

#: The wall-deadline kill's verdict: attempt-free, nothing to re-land.
DEADLINE_KILL_META: dict[str, Any] = {
    _rs.ATTEMPT_FREE_KEY: ATTEMPT_FREE_MARK,
    "red_class": _rl.TIMED_OUT_CLASS,
}

#: Handle phases besides :data:`repo_land.LEASE_PHASE`.
CHECK_PHASE = "check"
CI_PHASE = "ci"
#: Seconds a parked CI wait sleeps between polls (one runs read per poll;
#: the repo lease is held throughout, so the wait is kept short).
CI_PARK_S = 120
#: Seconds after ``pushed_at`` with no workflow run at all before the
#: coordinator is asked (the push event was dropped; ci.yml has no
#: workflow_dispatch to recover it).
NO_RUN_CUTOFF_S = 1800
#: Seconds of CI waiting (from the first ``ci`` poll of this job) before the
#: job gives up attempt-free; the record stays ``pushed`` for the next one.
CI_WALL_S = 6000
#: Re-runs of a cancelled run before the coordinator is asked.
MAX_RERUNS = 2
#: Seconds GitHub may stay unreadable before the job gives up attempt-free.
UNREADABLE_CAP_S = 1800
#: Seconds a GitHub request may take (read or re-run).
_HTTP_TIMEOUT_S = 20.0
#: Run statuses that still hold the branch: a re-run beside one of them
#: would cancel it or be cancelled by it.
_ACTIVE_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)

#: ``job.meta`` key naming why a failed job needs the coordinator, not a
#: blind retry. Restated from ``ci_red_digest.COORDINATOR_ASK_KEY`` (the
#: same one-way import rule ``plan_dispatch`` follows); ``plan_dispatch.
#: _coordinator_hit`` reads it off the node's newest failed attempt.
COORDINATOR_ASK_KEY = "coordinator_ask"
#: Prepare rounds per job: a raced push prepares again on the new tip.
MAX_ROUNDS = 3

#: ``todo.meta`` key of the release record (read by step 4).
RECORD_KEY = "repo_release"

#: The two files a release commit changes, one line each.
RELEASE_PATHS: tuple[str, str] = ("pyproject.toml", "uv.lock")
#: The commit identity; passed with ``-c``, never read from any config.
RELEASE_IDENT: tuple[str, str] = (
    "precis factory",
    "factory-release@users.noreply.github.com",
)
#: Verifier script, log and exit file under the run dir.
CHECK_SCRIPT = "release_check.sh"
CHECK_LOG = "release_check.log"
CHECK_EXIT = "release_check.exit"

#: ``red_class`` values besides the ones :mod:`repo_land` defines.
FETCH_CLASS = "origin-fetch"
TAG_ORACLE_CLASS = "tag-oracle"
TREE_CLASS = "release-tree"
READ_CLASS = "release-read"
RECORD_CLASS = "release-record"
TAG_PUSH_CLASS = "tag-push"
CI_UNREADABLE_CLASS = "github-unreadable"
CI_WALL_CLASS = "ci-wall"
BATCH_CLASS = "batch-open"

_BODY_SUBJECTS = 100
_BUMPS = frozenset({"auto", "minor", "patch"})


# ── Admission ──────────────────────────────────────────────────────


def _release_refusal(store: Any, params: dict[str, Any]) -> str | None:
    """Why this job may not release (td659522), or ``None``: the factory is
    on, the todo is live, ``params.repo`` names a profile whose ``releaser``
    is this job, ``factory.open_repos`` lists it, the profile has a remote, a
    ``ci_workflow`` and a release check, and the todo's ``repo:`` tag agrees
    with the param. Read at submit, when a parked job wakes, and again right
    before the push."""
    lead = "repo_release: "
    if _rs.factory_off():
        return lead + "the factory is off (PRECIS_FACTORY=off); next: unset it"
    reason = _rs._todo_rejection(store, params.get("todo_id"))
    if reason is not None:
        return reason.replace("run_slice: ", lead, 1)
    if params.get("bump", "auto") not in _BUMPS:
        return lead + "params.bump must be 'auto', 'minor' or 'patch'"
    repo = params.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return lead + "the job has no params.repo; next: pass the repo to release"
    repo = repo.strip()
    profile = _lanes.repo_profile(repo)
    if profile is None or profile.releaser != NAME:
        return (
            lead + f"the profile for {repo!r} names no releaser; next: release by hand"
        )
    if repo not in _lanes.open_repos(store):
        return (
            lead + f"factory.open_repos does not list {repo!r}; next: release by hand"
        )
    if not profile.remote or not profile.ci_workflow or not profile.release_check_cmds:
        return lead + (
            f"the profile for {repo!r} lacks a remote, a ci_workflow or a "
            "release check; next: release by hand"
        )
    try:
        api_base(profile.remote)
    except ReleaseError as exc:
        return lead + f"{exc} ({repo!r}); next: release by hand"
    if store is None:
        return None
    todo_id = params["todo_id"]
    tagged = ",".join(_rs.slice_repos(store, todo_id)) or _lanes.DEFAULT_REPO
    if tagged != repo:
        return lead + (
            f"todo {todo_id} resolves to repo {tagged!r} but params.repo is "
            f"{repo!r}; next: resubmit with params.repo={tagged!r}"
        )
    return None


def _plan_rows(store: Any, root: int) -> list[dict[str, Any]]:
    """``[{id, parent_id, kind}]`` of the plan subtree at ``root`` (seam)."""
    from precis.todo.views import _fetch_subtree

    return _fetch_subtree(store, root)


def _is_slice_node(meta: Mapping[str, Any]) -> bool:
    """A build node: no ``meta.op`` or a ``run_slice`` one, not a gate, loop,
    generator or phase."""
    if node_kind(dict(meta)) not in (None, "task"):
        return False
    op = meta.get("op")
    if isinstance(op, Mapping):
        op = op.get("type")
    return not op or str(op).strip() == _rs.NAME


def _is_reviewer_node(meta: Mapping[str, Any]) -> bool:
    gen = meta.get("generate")
    return (
        node_kind(dict(meta)) == "generator"
        and isinstance(gen, Mapping)
        and gen.get("role") == "reviewer"
    )


def _blocked_edges(store: Any, ids: Sequence[int]) -> list[tuple[int, int]]:
    """``(waiter, target)`` of every ``blocked-by`` link among ``ids`` (seam)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'blocked-by' "
            "AND src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s)",
            (list(ids), list(ids)),
        ).fetchall()
    return [(int(src), int(dst)) for src, dst in rows]


def _waits_on(
    todo_id: int,
    edges: Sequence[tuple[int, int]],
    parent_of: Mapping[int, int | None],
) -> set[int]:
    """The nodes that wait on ``todo_id``: ``blocked-by`` it, a child of a
    node that is, and so on. A ``blocked-by`` on a phase gates its children,
    so the walk follows both."""
    found: set[int] = set()
    frontier = {todo_id}
    while frontier:
        nxt = {src for src, dst in edges if dst in frontier}
        nxt |= {nid for nid, parent in parent_of.items() if parent in frontier}
        frontier = nxt - found - {todo_id}
        found |= frontier
    return found


def _open_siblings(store: Any, todo_id: int, repo: str) -> tuple[int, list[int]]:
    """``(plan root, ids)`` of the OPEN slice and reviewer nodes of ``repo``
    under the plan root of ``todo_id``, other than the node itself, ask nodes,
    phases (a node with child todos) and nodes that wait on this one (a later
    batch ``blocked-by`` this release must not hold it). A node's repo is its
    own ``repo:`` tag, else its nearest tagged ancestor's, else the default
    repo."""
    root = _release.plan_root(store, todo_id)
    rows = [r for r in _plan_rows(store, root) if r.get("kind") == "todo"]
    parent_of = {int(r["id"]): r.get("parent_id") for r in rows}
    parents = set(parent_of.values())
    after = _waits_on(todo_id, _blocked_edges(store, list(parent_of)), parent_of)
    held: list[int] = []
    for row in rows:
        nid = int(row["id"])
        if nid in (todo_id, root) or nid in after:
            continue
        ref = store.get_ref(kind="todo", id=nid)
        if ref is None or _rs._terminal_status(store, nid) is not None:
            continue
        meta = ref.meta or {}
        if isinstance(meta.get("ask"), Mapping) or any(
            str(t).startswith("ask-user") for t in store.tags_for(nid)
        ):
            continue
        if _is_reviewer_node(meta):
            pass
        elif nid in parents or not _is_slice_node(meta):
            continue
        if (",".join(_rs.slice_repos(store, nid)) or _lanes.DEFAULT_REPO) == repo:
            held.append(nid)
    return root, held


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Refuses everything :func:`_release_refusal` does, and holds (a
    ``holding:`` refusal, so no job is minted) while an open slice or
    reviewer node of the repo remains under the release node's plan root.
    The batch backstop: a reviewer's FIX gates the release node itself, but
    the fix's own reviewer is minted elsewhere with no edge to it."""
    del gripe_id
    refusal = _release_refusal(store, params)
    if refusal is not None or store is None:
        return refusal
    repo = str(params["repo"]).strip()
    node = store.get_ref(kind="todo", id=params["todo_id"])
    if (getattr(node, "meta", None) or {}).get("retries") != 1:
        # plan_dispatch retries a failed op node blind until meta.retries
        # failures; a blind retry of a release spends a version.
        return (
            f"repo_release: todo {params['todo_id']} must carry meta.retries = 1 "
            "(one failed release asks; a blind retry spends a version); next: "
            "set it on the release node"
        )
    root, held = _open_siblings(store, params["todo_id"], repo)
    if not held:
        return None
    shown = ", ".join(f"td{i}" for i in held[:8]) + (", ..." if len(held) > 8 else "")
    return (
        f"holding: {len(held)} open slice or reviewer node(s) of {repo!r} remain "
        f"under plan td{root} ({shown}); next: the release waits until they close"
    )


# ── Failures ───────────────────────────────────────────────────────


def _fail(
    ctx: Any,
    todo_id: int,
    reason: str,
    *,
    handoff: str,
    attempt_free: str | None = None,
) -> None:
    """The failure recorder. Unlike ``land_slice._fail`` it never touches the
    todo's claim, ``STATUS`` or ``attempt:<n>`` tag (a release node is a
    mechanical task; the dispatcher owns its state): it leaves a ``handoff:``
    comment on the node and fails the job. ``attempt_free`` is the red's class
    when the host or the remote, not the base, is at fault: it sets
    ``meta.attempt_free`` and ``meta.red_class``."""
    try:
        _rs._append_comment(ctx.store, todo_id, f"handoff: {handoff}")
    except Exception:  # pragma: no cover - the job outcome still lands
        log.warning("repo_release: handoff comment on td%d failed", todo_id)
    suffix = ""
    if attempt_free is not None:
        ctx.set_meta(
            **{_rs.ATTEMPT_FREE_KEY: ATTEMPT_FREE_MARK}, red_class=attempt_free
        )
        suffix = f" (no attempt: {attempt_free})"
    ctx.set_meta(compute_handle_released=True)
    ctx.record_failure(f"repo_release: {reason}{suffix}")


def _stamp_record(ctx: Any, todo_id: int, record: dict[str, Any] | None) -> bool:
    """Write (or, with ``None``, clear) the todo's release record. ``False``
    when the store refuses."""
    try:
        ctx.store.stamp_ref_meta(todo_id, {RECORD_KEY: record})
    except Exception:
        log.warning("repo_release: record stamp on td%d failed", todo_id, exc_info=True)
        return False
    return True


def _stored_record(store: Any, todo_id: int) -> dict[str, Any] | None:
    ref = store.get_ref(kind="todo", id=todo_id)
    record = (getattr(ref, "meta", None) or {}).get(RECORD_KEY) if ref else None
    return record if isinstance(record, dict) else None


# ── Git ────────────────────────────────────────────────────────────


def _auth_env() -> dict[str, str]:
    """The env of the credentialed oracle and push calls: only the
    :mod:`factory_git_mirror` helpers ever see it."""
    env = _factory_git.git_auth_env(base=_ls.land_env())
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _tree_path(root: Path, todo_id: int) -> Path:
    return _rs.worktrees_dir(root) / f"release-td{todo_id}"


def _drop_tree(root: Path, todo_id: int) -> None:
    """Remove the release tree and its worktree record; never raises."""
    _rl._drop_land_tree(root, _tree_path(root, todo_id))


def _release_tree(root: Path, todo_id: int, tip: str) -> Path | None:
    """A fresh detached worktree of ``tip`` at ``<root>/.claude/worktrees/
    release-td<id>``, any stale one removed first. ``None`` when git fails."""
    tree = _tree_path(root, todo_id)
    _drop_tree(root, todo_id)
    tree.parent.mkdir(parents=True, exist_ok=True)
    add = _ls._git(["worktree", "add", "--detach", str(tree), tip], root)
    return tree if add.returncode == 0 else None


def _show(cwd: Path, rev: str, path: str) -> str | None:
    """``path`` at ``rev`` as text, byte for byte (no newline translation, so
    a CRLF file is edited as it is). Same scrubbed env and hooks-off args as
    ``land_slice._git``. ``None`` when git fails or the bytes are not UTF-8."""
    proc = subprocess.run(
        ["git", *_mirror.TRUSTED_ARGS, "show", f"{rev}:{path}"],
        cwd=str(cwd),
        env=_mirror.local_env(_ls.land_env()),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _object_dirs(root: Path) -> tuple[Path, ...] | None:
    """The clone's object store, for the push helpers' scratch repo
    alternates; ``None`` when git cannot say."""
    common = _ls._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    if common.returncode != 0 or not common.stdout.strip():
        return None
    return (Path(common.stdout.strip()) / "objects",)


def _is_ancestor(root: Path, ancestor: str, sha: str) -> bool:
    return (
        _ls._git(["merge-base", "--is-ancestor", ancestor, sha], root).returncode == 0
    )


def _on_base(root: Path, sha: str, tip: str) -> bool | None:
    """Whether ``sha`` is an ancestor of the fetched ``tip``; ``None`` when
    git could not say. A sha the clone does not hold is not on the base (the
    fetch of ``tip`` would have brought it)."""
    rc = _ls._git(["merge-base", "--is-ancestor", sha, tip], root).returncode
    if rc in (0, 1):
        return rc == 0
    if _ls._git(["cat-file", "-e", f"{sha}^{{commit}}"], root).returncode != 0:
        return False
    return None


def _numstat_ok(cwd: Path, tip: str, sha: str) -> bool:
    """Whether ``tip..sha`` adds exactly one line and removes exactly one in
    each of :data:`RELEASE_PATHS` and nothing else (``-z``: paths are not
    C-quoted)."""
    out = _rl._read(cwd, ["diff", "--numstat", "--no-renames", "-z", tip, sha])
    if out is None:
        return False
    rows: dict[str, tuple[str, str]] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) != 3:
            return False
        rows[parts[2]] = (parts[0], parts[1])
    return rows == {p: ("1", "1") for p in RELEASE_PATHS}


def _commit_violation(cwd: Path, tip: str, sha: str) -> str | None:
    """Why ``sha`` is not a release commit on ``tip``, else ``None``: its
    parent is the tip, it changes exactly :data:`RELEASE_PATHS`, one line
    each. Read before the verifier runs and again before the push."""
    if not sha or not tip:
        return "the handle names no sha or tip"
    parent = _rl._read(cwd, ["rev-parse", f"{sha}^"])
    if parent is None or parent.strip() != tip:
        return f"{sha[:9]} is not a child of {tip[:9]}"
    paths = _rl._changed_paths(cwd, tip, sha)
    if paths is None:
        return "changed-path enumeration failed"
    if paths != sorted(RELEASE_PATHS):
        return "the commit changes " + ", ".join(paths) + " rather than the two files"
    if not _numstat_ok(cwd, tip, sha):
        return "the commit changes more than one line in each file"
    return None


def _commit_body(
    package: str,
    tag: str,
    bump: str,
    why: str,
    prev_tag: str | None,
    subjects: list[str],
) -> str:
    """The commit body: the bump and its reason, then the first-parent
    subjects since the previous release (when it is on the base)."""
    lines = [f"Release {package} {tag}: {bump} bump ({why})."]
    if prev_tag and subjects:
        lines += ["", f"Since {prev_tag}:"]
        lines += [f"- {s}" for s in subjects[:_BODY_SUBJECTS]]
        if len(subjects) > _BODY_SUBJECTS:
            lines.append(f"- ... and {len(subjects) - _BODY_SUBJECTS} more")
    return "\n".join(lines)


def _commit(tree: Path, ident: list[str], subject: str, body: str) -> str | None:
    """Commit the staged release edit and return its sha, ``None`` when git
    fails."""
    args = [*ident, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", subject]
    if body:
        args += ["-m", body]
    if _ls._git(args, tree).returncode != 0:
        return None
    sha = (_rl._read(tree, ["rev-parse", "HEAD"]) or "").strip()
    return sha or None


# ── Submit ─────────────────────────────────────────────────────────


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: guards only, no outward write and no git.
    Returns a parked ``phase: "lease"`` handle (due at once), or ``None``
    after failing the job."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    refusal = _release_refusal(ctx.store, params)
    if refusal is not None:
        ctx.record_failure(refusal)
        return None
    repo = str(params["repo"]).strip()
    try:
        _rs.slice_root_for(repo)
    except RuntimeError as exc:
        ctx.record_failure(f"repo_release: {exc}")
        return None
    handle: dict[str, Any] = {
        "op": NAME,
        "repo": repo,
        "phase": _rl.LEASE_PHASE,
        "lease_since": _ls._now().timestamp(),
    }
    _rl.park_until(ctx, handle, 0)
    return handle


# ── Prepare ────────────────────────────────────────────────────────


def _ci_handle(
    profile: _lanes.RepoProfile, record: Mapping[str, Any], rounds: int
) -> dict[str, Any]:
    """A parked ``ci`` handle for the pushed commit ``record`` names."""
    handle: dict[str, Any] = {
        "op": NAME,
        "repo": profile.name,
        "phase": CI_PHASE,
        "rounds": rounds,
        # This job's CI wait starts now (a resumed push may be hours old).
        "ci_since": _ls._now().timestamp(),
        "reruns": 0,
        "rerun_attempt_before": 0,
        "unreadable_since": None,
    }
    for key in (
        "package",
        "version",
        "tag",
        "sha",
        "base",
        "prev_tag",
        "prev_sha",
        "pushed_at",
    ):
        handle[key] = record.get(key)
    return handle


def _resume(
    ctx: Any,
    profile: _lanes.RepoProfile,
    root: Path,
    params: dict[str, Any],
    tip: str,
    env: dict[str, str],
    rounds: int,
) -> tuple[bool, dict[str, Any] | None]:
    """Sort out a ``state: "pushed"`` record left by an earlier job (or by
    this job's earlier round). ``(False, None)``: go on fresh (no usable
    record, or the push never happened and the record is cleared).
    ``(True, handle)``: a parked ``ci`` handle for the pushed sha.
    ``(True, None)``: the job was driven terminal here (failed, or recorded
    and succeeded when the tag was already at the pushed sha)."""
    todo_id = params["todo_id"]
    record = _stored_record(ctx.store, todo_id)
    if record is None or record.get("state") != "pushed":
        return False, None
    sha = _rl._hex(record.get("sha"))
    version, tag = str(record.get("version") or ""), str(record.get("tag") or "")
    try:
        sane = tag == tag_name(profile.tag_prefix, version)
    except ReleaseError:
        sane = False
    if not sha or not sane or record.get("repo") != profile.name:
        _stamp_record(ctx, todo_id, None)  # unreadable: not a push we can vouch for
        return False, None
    at = _mirror.ls_remote_tag(profile.remote, tag, env=env)
    if at is None:
        _fail(
            ctx,
            todo_id,
            f"tag {tag} could not be read on {profile.name}'s remote",
            handoff=f"release FAILED: the remote's tag {tag} could not be read "
            "(infrastructure); the release resumes the pushed commit.",
            attempt_free=TAG_ORACLE_CLASS,
        )
        return True, None
    if at and at != sha:
        _stamp_record(ctx, todo_id, {**record, "state": "tag-conflict"})
        _fail(
            ctx,
            todo_id,
            f"tag {tag} exists at {at[:9]}, not at the pushed release commit {sha[:9]}",
            handoff=f"release FAILED: tag {tag} already points at {at[:9]}, not "
            f"at the release commit {sha[:9]} this node pushed. Tags are never "
            "moved; a human decides whether this version is spent.",
        )
        return True, None
    if at == sha:
        # CI was green and the tag landed before the earlier job could record
        # it: record now, with the run's block when one read is cheap.
        _conclude(
            ctx,
            profile,
            root,
            todo_id,
            {**record, "sha": sha},
            ci=_cheap_ci(profile, sha),
        )
        return True, None
    on_base = _on_base(root, sha, tip)
    if on_base is None:
        _fail(
            ctx,
            todo_id,
            f"git could not say whether {sha[:9]} is on origin/{profile.base}",
            handoff="release FAILED: git could not read the clone's history "
            "(infrastructure); the release resumes the pushed commit.",
            attempt_free=READ_CLASS,
        )
        return True, None
    if on_base:
        # The commit is on the base but untagged: the push happened, so wait
        # for its CI run rather than cut another.
        handle = _ci_handle(profile, {**record, "sha": sha}, rounds)
        _rl.park_until(ctx, handle, 0)
        ctx.append_chunk(
            "job_event",
            f"repo_release: {profile.name} resumes pushed {tag} at {sha[:9]}",
        )
        return True, handle
    _stamp_record(ctx, todo_id, None)  # the push never happened
    return False, None


def _carried_prev(
    ctx: Any,
    profile: _lanes.RepoProfile,
    todo_id: int,
    current: str,
    env: dict[str, str],
) -> tuple[str | None, str] | None:
    """``(tag, sha)`` of the release this node's untagged release commit of
    version ``current`` was cut from: the stored record's ``prev_tag`` /
    ``prev_sha``, taken only when the record is for that version and the
    remote's tag still points at that sha. ``(None, "")`` when there is none
    to carry; ``None`` when the remote could not be read."""
    record = _stored_record(ctx.store, todo_id) or {}
    tag, sha = record.get("prev_tag"), _rl._hex(record.get("prev_sha"))
    if (
        record.get("repo") != profile.name
        or record.get("version") != current
        or not isinstance(tag, str)
        or not tag.startswith(profile.tag_prefix)
        or not sha
    ):
        return None, ""
    try:
        if tag_name(profile.tag_prefix, tag[len(profile.tag_prefix) :]) != tag:
            return None, ""
    except ReleaseError:
        return None, ""
    at = _mirror.ls_remote_tag(profile.remote, tag, env=env)
    if at is None:
        return None
    return (tag, sha) if at == sha else (None, "")


def _no_op(
    ctx: Any,
    profile: _lanes.RepoProfile,
    root: Path,
    todo_id: int,
    *,
    package: str,
    tag: str,
    version: str,
    tip: str,
) -> None:
    """Nothing landed since the last release: stamp the tagged record, say so
    and succeed. No commit, no push."""
    record = {
        "repo": profile.name,
        "package": package,
        "version": version,
        "tag": tag,
        "sha": tip,
        "base": profile.base,
        "prev_tag": tag,
        "prev_sha": tip,
        "state": "tagged",
        "no_op": True,
        "job": _rl._job_id(ctx),
        "tagged_at": _ls._now().isoformat(),
    }
    _conclude(
        ctx,
        profile,
        root,
        todo_id,
        record,
        text=f"released: {tag} {tip} (no-op: nothing landed since the last release)",
    )


def _conclude(
    ctx: Any,
    profile: _lanes.RepoProfile,
    root: Path,
    todo_id: int,
    record: dict[str, Any],
    *,
    ci: dict[str, Any] | None = None,
    text: str | None = None,
) -> None:
    """The success exit: stamp ``record`` (the tagged one), write the one
    ``released:`` comment (never ``landed:``), drop any release tree, release
    the lease and succeed the job. A refused stamp fails attempt-free: the
    tag is already out, and the resume finds it and records."""
    tag, sha = str(record.get("tag") or ""), str(record.get("sha") or "")
    record = {
        "tagged_at": _ls._now().isoformat(),
        **record,
        "state": "tagged",
        "job": _rl._job_id(ctx),
    }
    if ci:
        record["ci"] = ci
    if text is None:
        text = f"released: {tag} {sha}" + (f" (CI run {ci['run_id']})" if ci else "")
    if not _stamp_record(ctx, todo_id, record):
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            f"could not stamp the release record on td{todo_id}",
            handoff="release FAILED: the node could not be marked "
            "(infrastructure); the release is retried.",
            attempt_free=RECORD_CLASS,
        )
        return
    try:
        _rs._append_comment(ctx.store, todo_id, text)
    except Exception:  # pragma: no cover - the record is written; report it
        log.warning("repo_release: released comment on td%d failed", todo_id)
    _drop_tree(root, todo_id)
    try:
        _repo_lease.release(ctx.store, profile.name, job_id=_rl._job_id(ctx))
    except Exception:  # pragma: no cover - the terminal status frees it anyway
        log.warning("repo_release: lease release for td%d failed", todo_id)
    ctx.set_meta(
        exit_code=0, release_tag=tag, release_sha=sha, compute_handle_released=True
    )
    ctx.append_chunk("job_summary", f"repo_release td{todo_id}: {text}")
    ctx.set_status("succeeded")


def _prepare(
    ctx: Any,
    profile: _lanes.RepoProfile,
    root: Path,
    params: dict[str, Any],
    rounds: int,
) -> dict[str, Any] | None:
    """Everything from the base fetch to the verifier's launch, with the
    lease held. Returns the handle to carry on with (``check``, or a parked
    ``ci`` for a resumed push), or ``None`` after driving the job terminal
    (failed, or succeeded as a no-op)."""
    todo_id = params["todo_id"]
    tip = _rs.fetch_base_tip(profile, root)
    if tip is None:
        _fail(
            ctx,
            todo_id,
            f"could not fetch origin/{profile.base} of {profile.name}",
            handoff=f"release FAILED: origin/{profile.base} of {profile.name} "
            "could not be fetched (infrastructure); the release is retried.",
            attempt_free=FETCH_CLASS,
        )
        return None
    env = _auth_env()
    done, resumed = _resume(ctx, profile, root, params, tip, env, rounds)
    if done:
        return resumed

    def charged(why: str, handoff: str) -> None:
        _drop_tree(root, todo_id)
        _fail(ctx, todo_id, why, handoff=f"release FAILED: {handoff}")

    def free(why: str, handoff: str, red_class: str) -> None:
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            why,
            handoff=f"release FAILED: {handoff}",
            attempt_free=red_class,
        )

    # The two texts at the tip.
    texts: dict[str, str] = {}
    for path in RELEASE_PATHS:
        text = _show(root, tip, path)
        if text is None:
            if _rl._read(root, ["cat-file", "-e", f"{tip}:{path}"]) is None:
                charged(
                    f"{path} is missing or not UTF-8 at {tip[:9]}",
                    f"{path} is missing or not UTF-8 on origin/{profile.base}.",
                )
            else:
                free(
                    f"git could not read {path} at {tip[:9]}",
                    f"git could not read {path} (infrastructure); retried.",
                    READ_CLASS,
                )
            return None
        texts[path] = text
    try:
        package, current = project_name_version(texts["pyproject.toml"])
        current_tag = tag_name(profile.tag_prefix, current)
    except ReleaseError as exc:
        charged(str(exc), f"{exc}; fix pyproject.toml by hand.")
        return None
    prev_sha = _mirror.ls_remote_tag(profile.remote, current_tag, env=env)
    if prev_sha is None:
        free(
            f"tag {current_tag} could not be read on {profile.name}'s remote",
            f"the remote's tag {current_tag} could not be read "
            "(infrastructure); the release is retried.",
            TAG_ORACLE_CLASS,
        )
        return None
    if prev_sha == tip:
        _no_op(
            ctx,
            profile,
            root,
            todo_id,
            package=package,
            tag=current_tag,
            version=current,
            tip=tip,
        )
        return None
    prev_tag: str | None = current_tag if prev_sha else None
    if not prev_sha:
        # The tip's version was never tagged. When it is this node's own red
        # or conflicted release commit, count from the release that one was
        # cut from, so a retry after fixes is not read as a first release.
        carried = _carried_prev(ctx, profile, todo_id, current, env)
        if carried is None:
            free(
                f"the previous release tag could not be read on {profile.name}'s remote",
                "the remote's previous release tag could not be read "
                "(infrastructure); the release is retried.",
                TAG_ORACLE_CLASS,
            )
            return None
        prev_tag, prev_sha = carried
    # The bump: params.bump, or from the first-parent subjects since the
    # previous tag (none to read when it is absent or off the base).
    on_base = bool(prev_sha) and _is_ancestor(root, prev_sha, tip)
    subjects: list[str] = []
    if on_base:
        listed = _rl._read(
            root, ["log", "--first-parent", "--format=%s", f"{prev_sha}..{tip}"]
        )
        subjects = (listed or "").splitlines()
    bump = str(params.get("bump") or "auto")
    if bump != "auto":
        why = "params.bump"
    elif not on_base:
        bump, why = "minor", "no previous release on the base"
    else:
        bump = pick_bump(subjects)
        why = "a feat landed since the last release" if bump == "minor" else "no feat"
    try:
        version = bump_version(current, bump)
        tag = tag_name(profile.tag_prefix, version)
    except ReleaseError as exc:
        charged(str(exc), f"{exc}; fix the version by hand.")
        return None
    taken = _mirror.ls_remote_tag(profile.remote, tag, env=env)
    if taken is None:
        free(
            f"tag {tag} could not be read on {profile.name}'s remote",
            f"the remote's tag {tag} could not be read (infrastructure); retried.",
            TAG_ORACLE_CLASS,
        )
        return None
    if taken:
        charged(
            f"tag {tag} already exists on {profile.name}'s remote",
            f"tag {tag} already exists ({taken[:9]}); a version is never "
            "reused. A human decides which version to cut.",
        )
        return None
    try:
        new_py = set_project_version(texts["pyproject.toml"], version)
        new_lock = set_lock_version(texts["uv.lock"], package, version)
    except ReleaseError as exc:
        charged(str(exc), f"{exc}; the files need a hand edit.")
        return None
    # Cut the release tree, write the two files, commit exactly them.
    tree = _release_tree(root, todo_id, tip)
    if tree is None:
        free(
            f"git worktree add failed for the release tree of td{todo_id}",
            "the release tree could not be created; the release is retried.",
            TREE_CLASS,
        )
        return None
    try:
        (tree / "pyproject.toml").write_bytes(new_py.encode("utf-8"))
        (tree / "uv.lock").write_bytes(new_lock.encode("utf-8"))
    except OSError as exc:
        free(
            f"writing the release files failed: {exc}",
            "the release tree could not be written (infrastructure); retried.",
            TREE_CLASS,
        )
        return None
    ident = [
        "-c",
        f"user.name={RELEASE_IDENT[0]}",
        "-c",
        f"user.email={RELEASE_IDENT[1]}",
    ]
    body = _commit_body(
        package, tag, bump, why, prev_tag if on_base else None, subjects
    )
    subject = f"chore(release): {tag} (td{todo_id})"
    staged = _ls._git(["add", "--", *RELEASE_PATHS], tree)
    sha = _commit(tree, ident, subject, body) if staged.returncode == 0 else None
    if sha is None:
        charged(
            f"the release commit for {tag} failed",
            "git could not commit the release edit; the base may be unusual.",
        )
        return None
    violation = _commit_violation(tree, tip, sha)
    if violation is None and _show(tree, sha, "pyproject.toml") != new_py:
        violation = "the committed pyproject.toml is not the edited text"
    if violation is None and _show(tree, sha, "uv.lock") != new_lock:
        violation = "the committed uv.lock is not the edited text"
    if violation is not None:
        charged(
            f"release commit refused: {violation}",
            f"the release commit is not a pure version bump ({violation}).",
        )
        return None
    return _launch_check(
        ctx,
        profile,
        root,
        tree,
        params,
        tip=tip,
        sha=sha,
        package=package,
        version=version,
        tag=tag,
        prev_tag=prev_tag,
        prev_sha=prev_sha or None,
        rounds=rounds,
    )


def _launch_check(
    ctx: Any,
    profile: _lanes.RepoProfile,
    root: Path,
    tree: Path,
    params: dict[str, Any],
    *,
    tip: str,
    sha: str,
    package: str,
    version: str,
    tag: str,
    prev_tag: str | None,
    prev_sha: str | None,
    rounds: int,
) -> dict[str, Any] | None:
    """Fix the commit in the handle, persist it, then launch the verifier
    detached in the release tree under the credential-free gate env. The sha
    is on the handle before any repo-controlled code runs; what is pushed is
    that sha and nothing read back from the tree."""
    todo_id = params["todo_id"]
    run = _rs.run_dir(todo_id)
    script, log_path, exit_path = run / CHECK_SCRIPT, run / CHECK_LOG, run / CHECK_EXIT
    launched = _ls._now()
    handle: dict[str, Any] = {
        "op": NAME,
        "repo": profile.name,
        "phase": CHECK_PHASE,
        "release_tree": str(tree),
        "tip": tip,
        "sha": sha,
        "package": package,
        "version": version,
        "tag": tag,
        "base": profile.base,
        "prev_tag": prev_tag,
        "prev_sha": prev_sha,
        "rounds": rounds,
        "log": str(log_path),
        "exit_file": str(exit_path),
        "started_at": launched.strftime(_ls._TS_FMT),
        "deadline": launched.timestamp() + _ls.wall_seconds(),
    }
    ctx.set_meta(compute_handle=handle, compute_handle_released=False)
    try:
        script.write_text(
            _rl.commands_script(("check", c) for c in profile.release_check_cmds),
            encoding="utf-8",
        )
        exit_path.unlink(missing_ok=True)
        pid = _rs._launch(
            ["/bin/sh", str(script)],
            cwd=tree,
            stdin=Path(os.devnull),
            log=log_path,
            exit_file=exit_path,
            env=_rl.gate_env(profile),
        )
    except Exception as exc:
        log.warning(
            "repo_release: verifier launch for td%d failed", todo_id, exc_info=True
        )
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            f"verifier launch failed: {exc}",
            handoff=f"release FAILED: the verifier did not start ({exc}); retried.",
            attempt_free=_rl.GATE_INFRA_CLASS,
        )
        return None
    handle["pid"] = pid
    handle["pgid"] = pid
    _rl.stamp_identity(handle, pid)
    ctx.append_chunk(
        "job_event",
        f"repo_release: {profile.name} {tag} commit {sha[:9]} on tip {tip[:9]}; "
        f"verifier pid {pid} in {tree}",
    )
    return handle


def _adopt(ctx: Any, handle: dict[str, Any], fresh: dict[str, Any]) -> None:
    """Swap ``fresh`` in place of ``handle`` and persist it (poll cannot hand
    a handle back, so it goes to ``meta.compute_handle``)."""
    handle.clear()
    handle.update(fresh)
    deadline = fresh.get("deadline")
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=deadline
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
        else _ls._now().timestamp() + _ls.wall_seconds(),
    )


# ── Poll ───────────────────────────────────────────────────────────


def _due(handle: dict[str, Any]) -> bool:
    """Whether a parked handle's ``not_before`` has passed (an unreadable
    marker counts as due: do not park forever)."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _ls._TS_FMT).replace(
            tzinfo=UTC
        )
    except (KeyError, ValueError):
        return True
    return _ls._now() >= due


def _admission_gap(
    ctx: Any, profile: _lanes.RepoProfile, params: dict[str, Any]
) -> str | None:
    """Why the handle's repo is no longer admitted, else ``None``."""
    if _rs.job_repo(params) != profile.name:
        return f"the handle's repo is not the job's ({_rs.job_repo(params)!r})"
    return _release_refusal(ctx.store, params)


def _batch_open(ctx: Any, todo_id: int, profile: _lanes.RepoProfile, what: str) -> bool:
    """The submit backstop, read again before the push: a slice or reviewer of
    the repo that opened under the plan root since submit (a review's fix)
    fails the job attempt-free, and the next dispatch holds on it. ``True``
    when the job was failed. Never read once the commit is pushed: a later
    fix must not keep a pushed release from its tag."""
    _root, held = _open_siblings(ctx.store, todo_id, profile.name)
    if not held:
        return False
    shown = ", ".join(f"td{i}" for i in held[:8])
    _fail(
        ctx,
        todo_id,
        f"{len(held)} slice or reviewer node(s) of {profile.name!r} opened under "
        f"the plan since submit ({shown}); {what}",
        handoff=f"release FAILED: the batch is open again ({shown}); the release "
        "waits until they close.",
        attempt_free=BATCH_CLASS,
    )
    return True


def _poll_lease(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    profile: _lanes.RepoProfile,
    root: Path,
) -> bool:
    """A parked job, due: state the want (so new lands queue behind it), take
    the lease and prepare. Blocked: park again, or fail attempt-free past
    :data:`repo_land.LEASE_WAIT_CAP_S`. ``True`` only when the job is
    terminal."""
    todo_id = params["todo_id"]
    closed = _admission_gap(ctx, profile, params)
    if closed is not None:
        _fail(
            ctx,
            todo_id,
            f"repo {profile.name!r} is no longer admitted ({closed}); not releasing",
            handoff="release FAILED: the repo was closed to the factory while the "
            "release waited; it is retried once the repo is open.",
            attempt_free=_rl.CLOSED_CLASS,
        )
        return True
    pushed = (_stored_record(ctx.store, todo_id) or {}).get("state") == "pushed"
    if not pushed and _batch_open(ctx, todo_id, profile, "not releasing"):
        return True
    _repo_lease.want(ctx.store, profile.name, job_id=_rl._job_id(ctx))
    blocker = _repo_lease.acquire(
        ctx.store, profile.name, job_id=_rl._job_id(ctx), kind="release"
    )
    if blocker is not None:
        since = handle.get("lease_since")
        waited = (
            _ls._now().timestamp() - float(since)
            if isinstance(since, (int, float)) and not isinstance(since, bool)
            else 0.0
        )
        if waited > _rl.LEASE_WAIT_CAP_S:
            _fail(
                ctx,
                todo_id,
                f"the {profile.name!r} lease stayed with job #{blocker.get('job_id')} "
                f"({blocker.get('reason')}) for {int(waited)}s; giving up this try",
                handoff="release FAILED: the repo lease was held by another job for "
                "too long; the release is retried.",
                attempt_free=_rl.HELD_CLASS,
            )
            return True
        _rl._park(ctx, handle, blocker)
        return False
    fresh = _prepare(ctx, profile, root, params, 1)
    if fresh is None:
        return True
    _adopt(ctx, handle, fresh)
    return False


def _poll_check(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    profile: _lanes.RepoProfile,
    root: Path,
) -> bool:
    """The verifier's phase: keep waiting while it runs, classify its end."""
    todo_id = params["todo_id"]
    exit_code = _rs._read_exit(handle)
    pid = _rl._pid(handle.get("pid"))
    if exit_code is None and pid > 1 and _ls._process_alive(pid):
        deadline = handle.get("deadline")
        past = (
            isinstance(deadline, (int, float))
            and not isinstance(deadline, bool)
            and _ls._now().timestamp() > float(deadline)
        )
        if not past:
            ctx.set_meta(compute_handle=handle, compute_handle_released=False)
            return False
        _rl._signal_gate(handle)
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            f"the release verifier of td{todo_id} passed its wall deadline",
            handoff="release FAILED: the verifier passed its wall-clock deadline "
            "(infrastructure); the release is retried.",
            attempt_free=_rl.TIMED_OUT_CLASS,
        )
        return True
    if exit_code == 0:
        return _poll_green(ctx, handle, params, profile, root)
    masked = mask_secrets(_rs._read_log(handle)) or ""
    if (
        # No exit file: the launcher never wrote one, so the verifier was
        # killed from outside (a worker restart, a reboot), not failed.
        exit_code is None
        or exit_code == 137
        or _ls.gate_kill_line(masked, exit_code) is not None
    ):
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            "the release verifier was killed or lost",
            handoff="release FAILED: the verifier was killed from outside "
            "(infrastructure); nothing was pushed and the release is retried.",
            attempt_free=_rl.GATE_INFRA_CLASS,
        )
        return True
    found = _rl._GATE_FAIL_RE.findall(masked)
    command = found[-1][1] if found else "the release check"
    excerpt = _ls.gate_red_excerpt(masked)
    handoff = (
        f"release FAILED: `{command}` failed (exit {exit_code}) on the release "
        f"commit for {handle.get('tag')}. The lockfile on origin/{profile.base} "
        "is stale beyond its version line, so a version-only edit cannot pass "
        "the check: it needs a relock by a slice or a human. Nothing was "
        "pushed and no version was spent."
    )
    if excerpt:
        handoff += f"\n\nThe verifier's output before the verdict:\n{excerpt}"
    ctx.set_meta(exit_code=exit_code)
    _drop_tree(root, todo_id)
    tail = masked[-_ls._TAIL_CAP :].strip()
    _fail(
        ctx,
        todo_id,
        f"release check red: {command}"
        + (f"\n--- log tail ---\n{tail}" if tail else ""),
        handoff=handoff,
    )
    return True


def _poll_green(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    profile: _lanes.RepoProfile,
    root: Path,
) -> bool:
    """Exit 0: re-check the handle's commit, the admission and the lease,
    stamp the record, push the handle's sha. ``True`` when the job is
    terminal; ``False`` once parked in ``ci`` (or prepared again)."""
    todo_id = params["todo_id"]
    sha, tip = _rl._hex(handle.get("sha")), _rl._hex(handle.get("tip"))
    tag = str(handle.get("tag") or "")

    def free(why: str, handoff: str, red_class: str) -> bool:
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            why,
            handoff=f"release FAILED: {handoff}",
            attempt_free=red_class,
        )
        return True

    violation = _commit_violation(root, tip, sha)
    if violation is not None:
        return free(
            f"release commit refused before the push: {violation}",
            "the release commit no longer sits on the tip it was verified "
            "against (infrastructure); the release is retried.",
            _rl.HANDLE_CLASS,
        )
    closed = _admission_gap(ctx, profile, params)
    if closed is not None:
        return free(
            f"repo {profile.name!r} is no longer admitted ({closed}); not pushing",
            "the repo was closed to the factory while the verifier ran; the "
            "release is retried once it is open.",
            _rl.CLOSED_CLASS,
        )
    if _batch_open(ctx, todo_id, profile, "not pushing"):
        _drop_tree(root, todo_id)
        return True
    held = _repo_lease.holder(ctx.store, profile.name)
    if held is None or held.get("job_id") != _rl._job_id(ctx):
        by = f"job #{held.get('job_id')}" if held else "no job"
        return free(
            f"the {profile.name!r} lease is held by {by}, not this job; not pushing",
            "the repo lease was lost while the verifier ran; the release is retried.",
            _rl.HELD_CLASS,
        )
    pushed_at = _ls._now().isoformat()
    record = {
        "repo": profile.name,
        "package": handle.get("package"),
        "version": handle.get("version"),
        "tag": tag,
        "sha": sha,
        "base": profile.base,
        "prev_tag": handle.get("prev_tag"),
        "prev_sha": handle.get("prev_sha"),
        "state": "pushed",
        "job": _rl._job_id(ctx),
        "pushed_at": pushed_at,
    }
    # Stamped BEFORE the push: once the sha can be on the remote's base, a
    # resumed job must be able to tell it was pushed whatever fails next.
    if not _stamp_record(ctx, todo_id, record):
        return free(
            f"could not stamp {RECORD_KEY} on td{todo_id}; not pushing",
            "the node could not be marked before the push (infrastructure); "
            "the release is retried.",
            RECORD_CLASS,
        )
    objects = _object_dirs(root)
    if objects is None:
        _stamp_record(ctx, todo_id, None)  # nothing was pushed
        return free(
            "could not locate the clone's object store",
            "the object store was not found (infrastructure); retried.",
            _rl.PUSH_ERROR_CLASS,
        )
    outcome, detail = _mirror.push_cas(
        profile.remote,
        profile.base,
        sha,
        tip,
        ops_parent=_mirror.mirror_root(_rs.slice_root()),
        env=_auth_env(),
        object_dirs=objects,
    )
    if outcome == "raced":
        # The record stays "pushed": the push may have landed with someone
        # else's commit on top. The resume check in _prepare reads the new tip
        # and either waits for this sha's CI or clears the record.
        _drop_tree(root, todo_id)
        rounds = _int(handle.get("rounds"), 1)
        if rounds >= MAX_ROUNDS:
            return free(
                f"origin/{profile.base} moved under {rounds} release rounds: {detail}",
                f"origin/{profile.base} moved on every one of {rounds} tries; "
                "the release is retried.",
                _rl.RACED_CLASS,
            )
        fresh = _prepare(ctx, profile, root, params, rounds + 1)
        if fresh is None:
            return True
        _adopt(ctx, handle, fresh)
        return False
    if outcome != "ok":
        # The push may have happened: the record stays "pushed" and the
        # resume check sorts it out against the remote.
        return free(
            f"push of {sha[:9]} to origin/{profile.base} failed: {detail}",
            "the push could not be verified (infrastructure); the release "
            "resumes the commit if it landed.",
            _rl.PUSH_ERROR_CLASS,
        )
    _drop_tree(root, todo_id)
    try:
        _rs._append_comment(
            ctx.store, todo_id, f"release commit pushed: {tag} {sha}; waiting for CI"
        )
    except Exception:  # pragma: no cover - the push is real; the record says so
        log.warning("repo_release: pushed comment on td%d failed", todo_id)
    ci = _ci_handle(profile, record, _int(handle.get("rounds"), 1))
    _rl.park_until(ctx, ci, CI_PARK_S)
    _adopt(ctx, handle, ci)
    return False


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# ── CI: the runs read, the re-run, the tag ─────────────────────────


class _RunsDenied(Exception):
    """GitHub answered 404 to a runs read: the token cannot see Actions on
    this repo."""


class _RunsUnreadable(Exception):
    """GitHub did not answer usefully (network, rate limit, 5xx, bad body)."""


def _read_runs(url: str, params: dict[str, Any]) -> Any:
    """Seam: one GET of ``url`` as parsed JSON, through
    :func:`precis.plans.pr_class._get_json` (the SSRF-pinned client under the
    ``github`` politeness policy; the token is in the ``Authorization``
    header :func:`factory_git.github_api_headers` builds inside that call,
    nowhere else). Raises ``pr_class.GitHubUnreadable`` whose text is the
    status line, never a header."""
    return _pr_class._get_json(url, params)


def _runs(api: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``workflow_runs`` of ``GET <api>/actions/runs``. Raises
    :class:`_RunsDenied` on a 404, :class:`_RunsUnreadable` otherwise."""
    try:
        payload = _read_runs(f"{api}/actions/runs", params)
    except _pr_class.GitHubUnreadable as exc:
        if str(exc).endswith("HTTP 404"):
            raise _RunsDenied(str(exc)) from exc
        raise _RunsUnreadable(str(exc)) from exc
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise _RunsUnreadable("the runs payload has no workflow_runs list")
    return [r for r in runs if isinstance(r, dict)]


def _rerun(api: str, run_id: int) -> tuple[bool, int]:
    """``POST <api>/actions/runs/<id>/rerun``: ``(ok, http status)``, the
    status ``0`` when no answer came. Never raises and never logs the
    request headers. A non-GET goes through ``safe_stream`` (the same pinned
    client as the reads) under ``with_backoff(idempotent=False)``: only a
    connect failure or a 429 is retried, so a re-run is never sent twice by
    the retry loop. 401/403 arrive as a ``VendorError`` carrying the
    status; a 404 comes back as a response."""
    from precis.utils.http import VendorError, http_client, with_backoff
    from precis.utils.safe_fetch import safe_stream

    url = f"{api}/actions/runs/{int(run_id)}/rerun"
    headers = _factory_git.github_api_headers()

    def send() -> Any:
        with http_client(timeout=_HTTP_TIMEOUT_S, headers=headers) as client:
            with safe_stream(client, "POST", url) as resp:
                resp.read()
                return resp

    try:
        resp = with_backoff(send, service="github", idempotent=False, url=url)
    except VendorError as exc:
        status = exc.status if isinstance(exc.status, int) else 0
        log.info("repo_release: re-run POST %s refused (HTTP %s)", url, status or "-")
        return False, status
    except Exception as exc:
        log.info("repo_release: re-run POST %s failed (%s)", url, type(exc).__name__)
        return False, 0
    status = int(resp.status_code)
    return 200 <= status < 300, status


def _ci_block(run: Mapping[str, Any]) -> dict[str, Any]:
    """The record's ``ci`` block for a picked run."""
    return {
        "run_id": int(run.get("id") or 0),
        "attempt": int(run.get("run_attempt") or 1),
        "url": run.get("html_url"),
        "conclusion": run.get("conclusion"),
    }


def _runs_params(profile: _lanes.RepoProfile, sha: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"branch": profile.base, "event": "push", "per_page": 50}
    if sha:
        params["head_sha"] = sha
    return params


def _cheap_ci(profile: _lanes.RepoProfile, sha: str) -> dict[str, Any] | None:
    """The ``ci`` block of the sha's run from one best-effort read, else
    ``None`` (any trouble, or no run)."""
    try:
        runs = _runs(api_base(profile.remote), _runs_params(profile, sha))
    except (ReleaseError, _RunsDenied, _RunsUnreadable):
        return None
    run = pick_run(runs, sha=sha, workflow=profile.ci_workflow or "")
    return _ci_block(run) if run is not None else None


def _record_of(
    ctx: Any,
    profile: _lanes.RepoProfile,
    handle: Mapping[str, Any],
    todo_id: int,
    state: str,
    **extra: Any,
) -> dict[str, Any]:
    """The release record for ``state``: what the ``ci`` handle carries,
    under the stored one (the push's own), with this job as ``job``."""
    rec: dict[str, Any] = {"repo": profile.name, "base": profile.base}
    for key in (
        "package",
        "version",
        "tag",
        "sha",
        "prev_tag",
        "prev_sha",
        "pushed_at",
    ):
        rec[key] = handle.get(key)
    rec.update(_stored_record(ctx.store, todo_id) or {})
    rec.update(state=state, job=_rl._job_id(ctx), **extra)
    return rec


def _wait(
    ctx: Any,
    handle: dict[str, Any],
    todo_id: int,
    why: str,
    *,
    now: float,
) -> bool:
    """Park the ``ci`` handle for the next poll, or, past
    :data:`CI_WALL_S` of waiting, fail attempt-free (the record stays
    ``pushed``; the next job resumes the same sha). ``True`` when failed."""
    since = handle.get("ci_since")
    if isinstance(since, (int, float)) and now - float(since) > CI_WALL_S:
        _fail(
            ctx,
            todo_id,
            f"CI of {handle.get('tag')} not settled after {CI_WALL_S}s ({why})",
            handoff=f"release FAILED: CI on {handle.get('tag')} did not settle in "
            f"{CI_WALL_S}s ({why}); the pushed commit {str(handle.get('sha'))[:9]} "
            "is kept and the release resumes it.",
            attempt_free=CI_WALL_CLASS,
        )
        return True
    _rl.park_until(ctx, handle, CI_PARK_S)
    return False


def _coordinator(ctx: Any, todo_id: int, why: str, handoff: str) -> bool:
    """A charged failure the coordinator, not a blind retry, must answer:
    :data:`COORDINATOR_ASK_KEY` is stamped on the job so ``plan_dispatch``
    routes the node's cap ask to it. Always ``True`` (terminal)."""
    ctx.set_meta(**{COORDINATOR_ASK_KEY: why})
    _fail(ctx, todo_id, why, handoff=f"release FAILED: {handoff}")
    return True


def _poll_ci(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    profile: _lanes.RepoProfile,
    root: Path,
) -> bool:
    """One due poll of the CI wait (``poll`` checked ``not_before``).
    ``True`` when the job is terminal."""
    todo_id = params["todo_id"]
    sha = _rl._hex(handle.get("sha"))
    now = _ls._now().timestamp()

    def free(why: str, handoff: str, red_class: str) -> bool:
        _fail(
            ctx,
            todo_id,
            why,
            handoff=f"release FAILED: {handoff}",
            attempt_free=red_class,
        )
        return True

    if not sha:
        return free(
            "the ci handle names no sha",
            "the CI handle lost its commit (infrastructure); the release resumes "
            "from the record.",
            _rl.HANDLE_CLASS,
        )
    held = _repo_lease.holder(ctx.store, profile.name)
    if held is None or held.get("job_id") != _rl._job_id(ctx):
        by = f"job #{held.get('job_id')}" if held else "no job"
        return free(
            f"the {profile.name!r} lease is held by {by}, not this job; not waiting on CI",
            "the repo lease was lost while CI ran; the release resumes the pushed "
            "commit.",
            _rl.HELD_CLASS,
        )
    try:
        api = api_base(profile.remote)
    except ReleaseError as exc:
        return _coordinator(
            ctx,
            todo_id,
            f"{profile.name}: {exc}",
            f"{exc}; the profile's remote needs a human.",
        )
    denied = (
        f"GitHub answered 404 to the workflow-run read on {profile.name}; the "
        "factory token lacks Actions read on that repo"
    )
    try:
        runs = _runs(api, _runs_params(profile, sha))
    except _RunsDenied:
        return _coordinator(
            ctx,
            todo_id,
            denied,
            f"{denied}. The commit {sha[:9]} is pushed; a human grants the token "
            "Actions read.",
        )
    except _RunsUnreadable as exc:
        return _unreadable(ctx, handle, todo_id, str(exc), now)
    handle["unreadable_since"] = None
    run = pick_run(runs, sha=sha, workflow=profile.ci_workflow or "")
    verdict = ci_verdict(
        run, rerun_attempt_before=_int(handle.get("rerun_attempt_before"), 0)
    )
    if run is None:
        pushed = _pushed_ts(handle)
        if now - pushed > NO_RUN_CUTOFF_S:
            why = (
                f"no CI run appeared for {sha[:9]} on {profile.name} "
                f"{NO_RUN_CUTOFF_S}s after the push"
            )
            return _coordinator(
                ctx,
                todo_id,
                why,
                f"{why} (the push event was probably dropped; the workflow has "
                "no manual trigger). The version is spent: a human decides.",
            )
        return _wait(ctx, handle, todo_id, "no run yet", now=now)
    if verdict == "pending":
        return _wait(ctx, handle, todo_id, "run pending", now=now)
    if verdict == "green":
        return _tag_and_record(ctx, handle, params, profile, root, run)
    if verdict == "red":
        block = _ci_block(run)
        _stamp_record(
            ctx, todo_id, _record_of(ctx, profile, handle, todo_id, "red", ci=block)
        )
        _fail(
            ctx,
            todo_id,
            f"CI run {block['run_id']} on {sha[:9]} concluded {block['conclusion']}",
            handoff=f"release FAILED: CI run {block['run_id']} ({block['conclusion']}) "
            f"on the release commit {sha[:9]} is red: {block['url']}. No tag was "
            f"cut; the commit stays on {profile.base} and the next try takes a "
            "new version.",
        )
        return True
    return _poll_cancelled(ctx, handle, todo_id, profile, api, run, now)


def _pushed_ts(handle: Mapping[str, Any]) -> float:
    """``pushed_at`` as a timestamp; this wait's start when unreadable."""
    try:
        return datetime.fromisoformat(str(handle["pushed_at"])).timestamp()
    except (KeyError, ValueError):
        since = handle.get("ci_since")
        return (
            float(since) if isinstance(since, (int, float)) else _ls._now().timestamp()
        )


def _unreadable(
    ctx: Any, handle: dict[str, Any], todo_id: int, why: str, now: float
) -> bool:
    """GitHub could not be read: re-park, and past :data:`UNREADABLE_CAP_S` of
    continuous silence fail attempt-free (the record stays ``pushed``)."""
    since = handle.get("unreadable_since")
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        since = now
        handle["unreadable_since"] = since
    if now - float(since) > UNREADABLE_CAP_S:
        _fail(
            ctx,
            todo_id,
            f"GitHub unreadable for {int(now - float(since))}s: {why}",
            handoff="release FAILED: GitHub could not be read for "
            f"{UNREADABLE_CAP_S}s (infrastructure); the release resumes the "
            "pushed commit.",
            attempt_free=CI_UNREADABLE_CLASS,
        )
        return True
    return _wait(ctx, handle, todo_id, "GitHub unreadable", now=now)


def _poll_cancelled(
    ctx: Any,
    handle: dict[str, Any],
    todo_id: int,
    profile: _lanes.RepoProfile,
    api: str,
    run: Mapping[str, Any],
    now: float,
) -> bool:
    """A cancelled run: wait for the branch to be quiet, then re-run it, at
    most :data:`MAX_RERUNS` times."""
    sha = _rl._hex(handle.get("sha"))
    reruns = _int(handle.get("reruns"), 0)
    if reruns >= MAX_RERUNS:
        why = (
            f"CI run {run.get('id')} on {sha[:9]} was cancelled again after "
            f"{reruns} re-runs on {profile.name}"
        )
        return _coordinator(
            ctx,
            todo_id,
            why,
            f"{why}: {run.get('html_url')}. The version is spent: a human decides.",
        )
    try:
        branch = _runs(api, _runs_params(profile, None))
    except _RunsDenied:
        return _coordinator(
            ctx,
            todo_id,
            f"GitHub answered 404 to the workflow-run read on {profile.name}; the "
            "factory token lacks Actions read on that repo",
            "the token lacks Actions read; a human grants it.",
        )
    except _RunsUnreadable as exc:
        return _unreadable(ctx, handle, todo_id, str(exc), now)
    busy = [
        r
        for r in branch
        if _in_workflow(r, profile.ci_workflow or "")
        and r.get("status") in _ACTIVE_STATUSES
        and r.get("id") != run.get("id")
    ]
    if busy:
        return _wait(ctx, handle, todo_id, "another run holds the branch", now=now)
    ok, status = _rerun(api, int(run.get("id") or 0))
    if ok:
        # An answered POST ends the uncertainty a lost reply left: keep the
        # flag and a later genuine 403 reads as "probably requested".
        handle.pop("rerun_unsure", None)
        handle["reruns"] = reruns + 1
        handle["rerun_attempt_before"] = int(run.get("run_attempt") or 1)
        ctx.append_chunk(
            "job_event",
            f"repo_release: {profile.name} CI run {run.get('id')} was cancelled; "
            f"re-run {reruns + 1}/{MAX_RERUNS} requested",
        )
        return _wait(ctx, handle, todo_id, "re-run requested", now=now)
    if status == 403 and handle.pop("rerun_unsure", False):
        # The POST before this one got no answer and may have been accepted:
        # GitHub answers 403 for a run that is already re-running. Read it as
        # requested; were it a real refusal, the wait ends at the CI wall and
        # the next job's first POST says so.
        handle["reruns"] = reruns + 1
        handle["rerun_attempt_before"] = int(run.get("run_attempt") or 1)
        return _wait(ctx, handle, todo_id, "re-run probably requested", now=now)
    if status == 0:
        handle["rerun_unsure"] = True
    if status in (401, 403, 404):
        why = (
            f"GitHub refused the re-run of CI run {run.get('id')} on "
            f"{profile.name} (HTTP {status}); the factory token lacks Actions write"
        )
        return _coordinator(
            ctx,
            todo_id,
            why,
            f"{why}. A human re-runs {run.get('html_url')} or "
            "grants the token Actions write.",
        )
    return _wait(ctx, handle, todo_id, f"re-run POST failed (HTTP {status})", now=now)


def _tag_and_record(
    ctx: Any,
    handle: dict[str, Any],
    params: dict[str, Any],
    profile: _lanes.RepoProfile,
    root: Path,
    run: Mapping[str, Any],
) -> bool:
    """CI is green: re-read the admission and the lease, create the tag at
    the handle's sha, record, succeed. ``True`` when the job is terminal."""
    todo_id = params["todo_id"]
    sha, tag = _rl._hex(handle.get("sha")), str(handle.get("tag") or "")
    block = _ci_block(run)

    def free(why: str, handoff: str, red_class: str) -> bool:
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            why,
            handoff=f"release FAILED: {handoff}",
            attempt_free=red_class,
        )
        return True

    closed = _admission_gap(ctx, profile, params)
    if closed is not None:
        return free(
            f"repo {profile.name!r} is no longer admitted ({closed}); not tagging",
            "the repo was closed to the factory while CI ran; the release resumes "
            "the pushed commit once it is open.",
            _rl.CLOSED_CLASS,
        )
    held = _repo_lease.holder(ctx.store, profile.name)
    if held is None or held.get("job_id") != _rl._job_id(ctx):
        by = f"job #{held.get('job_id')}" if held else "no job"
        return free(
            f"the {profile.name!r} lease is held by {by}, not this job; not tagging",
            "the repo lease was lost while CI ran; the release resumes the pushed "
            "commit.",
            _rl.HELD_CLASS,
        )
    objects = _object_dirs(root)
    if objects is None:
        return free(
            "could not locate the clone's object store",
            "the object store was not found (infrastructure); retried.",
            TAG_PUSH_CLASS,
        )
    outcome, detail = _mirror.push_tag_new(
        profile.remote,
        tag,
        sha,
        message=tag,
        tagger=RELEASE_IDENT,
        ops_parent=_mirror.mirror_root(_rs.slice_root()),
        env=_auth_env(),
        object_dirs=objects,
    )
    if outcome == "exists":
        _stamp_record(
            ctx,
            todo_id,
            _record_of(ctx, profile, handle, todo_id, "tag-conflict", ci=block),
        )
        _drop_tree(root, todo_id)
        _fail(
            ctx,
            todo_id,
            f"tag {tag} already exists at {mask_secrets(detail)[:9]}, not at {sha[:9]}",
            handoff=f"release FAILED: tag {tag} points at {mask_secrets(detail)[:9]}, "
            f"not at the green release commit {sha[:9]} (someone released at the "
            "same time). Tags are never moved; a human decides.",
        )
        return True
    if outcome != "ok":
        return free(
            f"tagging {tag} at {sha[:9]} failed: {mask_secrets(detail)}",
            "the tag could not be verified (infrastructure); the release resumes "
            "the pushed commit and finds the tag if it landed.",
            TAG_PUSH_CLASS,
        )
    _conclude(
        ctx,
        profile,
        root,
        todo_id,
        _record_of(ctx, profile, handle, todo_id, "tagged"),
        ci=block,
    )
    return True


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half with ``land_slice.poll``'s contract: ``False``
    while the job waits (parked, or the verifier runs), ``True`` once the job
    is terminal."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"repo_release: malformed handle {handle!r}")
        return True
    params = dict((ctx.meta or {}).get("params") or {})
    phase = handle.get("phase")
    if phase in (CI_PHASE, _rl.LEASE_PHASE) and not _due(handle):
        return False
    profile = _lanes.repo_profile(str(handle.get("repo") or ""))
    root = _rl._handle_root(handle)
    if (
        root is None
        or profile is None
        or not profile.remote
        or not isinstance(params.get("todo_id"), int)
    ):
        ctx.record_failure(
            f"repo_release: no root or profile for {handle.get('repo')!r}"
        )
        return True
    if phase == _rl.LEASE_PHASE:
        return _poll_lease(ctx, handle, params, profile, root)
    if phase == CI_PHASE:
        return _poll_ci(ctx, handle, params, profile, root)
    if phase == CHECK_PHASE:
        return _poll_check(ctx, handle, params, profile, root)
    ctx.record_failure(f"repo_release: unknown phase {phase!r}")
    return True


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the verifier's process group
    (never the worker's own) and drop the release tree. A parked handle has
    neither, and nothing here raises."""
    if not isinstance(handle, dict) or handle.get("phase") != CHECK_PHASE:
        return
    _rl._signal_gate(handle)
    params = (getattr(ctx, "meta", None) or {}).get("params") or {}
    root = _rl._handle_root(handle)
    if root is not None:
        _drop_tree(root, _int(params.get("todo_id"), 0))


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate_submit=validate_submit,
    submit=submit,
    poll=poll,
    kill=kill,
    deadline_kill_meta=DEADLINE_KILL_META,
    wall_seconds=_ls.wall_seconds,
)
