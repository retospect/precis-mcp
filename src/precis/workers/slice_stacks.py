"""``slice_stacks`` — the docker footprint of finished slice worktrees
(gr570133, td573206).

The leak: ``land_slice`` removes a slice's worktree and branch but never
brought its ``precis-test-slice-td<N>`` compose project down, and the
reapers (``scripts/reap-test-dbs``, ``scripts/reap-idle-gates``) only run
from a Claude ``SessionStart`` hook — which never fires on the headless
slice host — so warm gate stacks of finished slices piled up in the slice
host's 8 GiB docker VM: gate OOM kills (exit 137) and docker address-pool
exhaustion, with no operator to sweep. This module is the lander-side
reaper: :func:`land_slice` downs its own stack at cleanup and calls
:func:`sweep` before every ship launch.

Stdlib only; every docker call goes through the one seam :func:`_docker`
(bounded, ``text=True``, never raises) so tests run with no daemon.

Safe by construction — the same idle judgment ``scripts/reap-test-dbs``'
idle-stack pass uses, restricted to ``precis-test-slice-`` projects only:

* a stack is *live* (never evicted, even over the cap) when any signal
  says work is in flight: a running container whose service is not the
  ``precis-gate``/``precis-test-db`` pair, an exec attached to any
  container, or a live pid under ``<gitdir>/precis-test-runs/``;
* a stack whose worktree is gone is evicted with ``-v`` (nothing left to
  keep, including the ``gate-cache`` volume);
* a live-free stack whose newest activity — a container ``StartedAt``, the
  tree's git index/HEAD/reflog mtimes, or ``scratch/land_slice.log`` —
  is older than :data:`IDLE_S_DEFAULT` is evicted WITHOUT ``-v``, so its
  warm ``gate-cache`` (uv/mypy) volume survives for the next slice that
  reuses the tree;
* while more stacks remain than :data:`CAP_DEFAULT`, the oldest live-free
  ones go too — but never a stack whose newest activity is younger than
  :data:`CAP_FLOOR_S_DEFAULT`: scripts/ship writes no ``precis-test-runs``
  marker and gates through separate ``compose exec`` calls, so a quiet
  young stack may be a land mid-ship. Liveness is re-inspected on a fresh
  ``ps``/``inspect`` right before each ``down`` — the first scan is
  evictions-stale by then, and a stack that went busy since is kept;
* finally, a ``precis-test-slice-*`` network whose project has no
  container left is ``docker network rm``'d — it counts against the
  daemon's address-pool ceiling (gr311857).

Knobs: ``PRECIS_SLICE_STACK_IDLE_S`` (default 3600),
``PRECIS_SLICE_STACK_CAP`` (default 8),
``PRECIS_SLICE_STACK_CAP_FLOOR_S`` (default 900 — the cap pass's
recency-as-busy floor), ``PRECIS_SLICE_STACK_SWEEP=off`` (no-op).
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The compose project prefix every per-worktree dev stack gets
#: (``compose_project_for`` in scripts/lib/compose-project.sh).
PROJECT_PREFIX = "precis-test-"
#: The only projects this module ever touches: a slice worktree's
#: (``slice-td<id>``, ``slice-pr-<n>`` — the ``-`` sanitiser keeps both).
SLICE_PREFIX = "precis-test-slice-"
#: The services a parked stack may have RUNNING without meaning work: the
#: warm gate and its db (reap-test-dbs' pair). Any other running service —
#: a ``run --rm`` pytest, a dev stack — means a session is using it.
PAIR_SERVICES = frozenset({"precis-gate", "precis-test-db"})

L_PROJECT = "com.docker.compose.project"
L_SERVICE = "com.docker.compose.service"
L_CONFIG = "com.docker.compose.project.config_files"
COMPOSE_SUFFIX = "/docker/dev/compose.yaml"
#: scripts/test's run markers: ``<gitdir>/precis-test-runs/<pid>``.
RUN_MARKER_DIR = "precis-test-runs"

DOCKER_TIMEOUT_S = 120
IDLE_S_DEFAULT = 3600
CAP_DEFAULT = 8
#: The cap pass's recency floor: a quiet stack this young may be a land
#: mid-ship (ship writes no run marker and gates through separate
#: ``compose exec`` calls — between execs nothing looks busy), so the cap
#: treats recency as busy.
CAP_FLOOR_S_DEFAULT = 900


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def idle_seconds() -> int:
    """Idle window in seconds (``PRECIS_SLICE_STACK_IDLE_S``, default
    :data:`IDLE_S_DEFAULT`)."""
    return _env_int("PRECIS_SLICE_STACK_IDLE_S", IDLE_S_DEFAULT)


def stack_cap() -> int:
    """How many slice stacks may stay warm (``PRECIS_SLICE_STACK_CAP``,
    default :data:`CAP_DEFAULT`)."""
    return _env_int("PRECIS_SLICE_STACK_CAP", CAP_DEFAULT)


def cap_floor_seconds() -> int:
    """The cap pass's recency floor in seconds
    (``PRECIS_SLICE_STACK_CAP_FLOOR_S``, default
    :data:`CAP_FLOOR_S_DEFAULT`): a quiet stack whose newest activity is
    younger than this is treated as busy — never cap-evicted."""
    return _env_int("PRECIS_SLICE_STACK_CAP_FLOOR_S", CAP_FLOOR_S_DEFAULT)


def sweep_off() -> bool:
    """``PRECIS_SLICE_STACK_SWEEP=off`` (also ``0``/``no``/``false``)."""
    value = os.environ.get("PRECIS_SLICE_STACK_SWEEP", "").strip().lower()
    return value in ("off", "0", "no", "false")


def project_for_tree(tree: Path) -> str:
    """Python twin of ``compose_project_for`` in
    scripts/lib/compose-project.sh (and of the twin in
    scripts/reap-idle-gates): the physical path's basename sanitised to
    compose's charset and lowercased, prefixed ``precis-test-``."""
    base = re.sub(
        r"[^a-zA-Z0-9_-]", "-", os.path.basename(os.path.realpath(tree))
    ).lower()
    return PROJECT_PREFIX + base if base else ""


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """The one docker seam: a bounded call (120 s) that never raises —
    a missing binary, a dead socket or a timeout reads as a non-zero
    ``CompletedProcess`` so callers degrade quietly. ``UID``/``GID`` ride
    the env for compose's bind-mount uid mapping, as the reapers set
    them."""
    env = dict(os.environ)
    try:
        env["UID"], env["GID"] = str(os.getuid()), str(os.getgid())
    except (AttributeError, OSError):  # pragma: no cover — non-POSIX host
        pass
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=DOCKER_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            ["docker", *args],
            127,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )


def _inspect(*args: str) -> list[dict[str, Any]]:
    """``docker inspect <args>`` parsed as a JSON list of dicts; ``[]`` on
    any failure — an unanswerable inspect never convicts a stack."""
    out = _docker(list(args))
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _labels(obj: dict[str, Any]) -> dict[str, str]:
    labels = obj.get("Labels")
    if labels is None:
        labels = (obj.get("Config") or {}).get("Labels")
    return labels if isinstance(labels, dict) else {}


def _epoch(ts: str) -> float | None:
    """Docker's RFC 3339 UTC timestamp to epoch seconds; ``None`` if
    unusable (``0001-01-01`` = never started)."""
    try:
        t = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None
    return float(t) if t > 0 else None


@dataclass
class Container:
    id: str
    name: str
    service: str
    config: str
    exec_ids: list[str]
    running: bool
    started: float | None

    @classmethod
    def from_inspect(cls, d: dict[str, Any]) -> Container:
        labels = _labels(d)
        state = d.get("State") or {}
        started = _epoch(str(state.get("StartedAt") or "")) or _epoch(
            str(d.get("Created") or "")
        )
        exec_ids = d.get("ExecIDs")
        return cls(
            id=str(d.get("Id", "")),
            name=str(d.get("Name", "")).lstrip("/"),
            service=labels.get(L_SERVICE, ""),
            config=labels.get(L_CONFIG, "").split(",")[0],
            exec_ids=[str(e) for e in exec_ids] if isinstance(exec_ids, list) else [],
            running=state.get("Status") == "running",
            started=started,
        )


@dataclass
class Stack:
    """A ``precis-test-slice-*`` project judged against its tree."""

    project: str
    tree: Path | None  # None: the config labels name no single worktree
    containers: list[Container] = field(default_factory=list)
    live: bool = True  # a work signal forbids eviction (see _live)
    activity: float | None = None  # newest observed mtime/StartedAt


def _tree_of(containers: list[Container]) -> Path | None:
    """The worktree the stack's compose file lives under: the
    ``config_files`` label minus the known suffix (read off the container,
    never inverted from the project name — the sanitiser is not
    injective). ``None`` when the labels disagree or name another layout:
    leave such a stack alone rather than guess a path to judge on."""
    configs = {c.config for c in containers}
    if len(configs) != 1:
        return None
    cfg = configs.pop()
    if not cfg.endswith(COMPOSE_SUFFIX) or cfg == COMPOSE_SUFFIX:
        return None
    return Path(cfg[: -len(COMPOSE_SUFFIX)])


def _git_dir(tree: Path) -> Path | None:
    """The tree's real git dir — ``.git`` itself, or the ``gitdir:`` a
    worktree's ``.git`` file points at."""
    dotgit = tree / ".git"
    if dotgit.is_dir():
        return dotgit
    try:
        line = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not line.startswith("gitdir: "):
        return None
    gd = Path(line[len("gitdir: ") :])
    return gd if gd.is_absolute() else tree / gd


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    except (OverflowError, OSError):
        return False
    return True


def _live_run_marker(tree: Path) -> bool:
    """A live scripts/test run marker: ``<gitdir>/precis-test-runs/<pid>``
    whose pid is still alive — a run in its container-less phase (lint,
    sync) still owns the stack. A dead marker is stale evidence, not
    cleanup business."""
    gd = _git_dir(tree)
    if gd is None:
        return False
    try:
        names = os.listdir(gd / RUN_MARKER_DIR)
    except OSError:
        return False
    return any(name.isdigit() and _pid_alive(int(name)) for name in names)


def _live(containers: list[Container], tree: Path) -> bool:
    """Any signal that forbids eviction: a running non-pair container (a
    gate or ``run --rm`` pytest in flight), an attached exec (a gate
    ``compose exec``), or a live run marker."""
    for c in containers:
        if c.running and c.service not in PAIR_SERVICES:
            return True
        if c.exec_ids:
            return True
    return _live_run_marker(tree)


def _activity(tree: Path, containers: list[Container]) -> float | None:
    """The newest activity signal: a container ``StartedAt``, the tree's
    git index/HEAD/reflog mtimes, or ``scratch/land_slice.log``'s mtime.
    ``None`` when nothing readable exists — an unjudgeable stack is kept,
    never evicted on a guess."""
    mtimes = [c.started for c in containers if c.started is not None]
    gd = _git_dir(tree)
    if gd is not None:
        for p in (gd / "index", gd / "HEAD", gd / "logs" / "HEAD"):
            try:
                mtimes.append(os.path.getmtime(p))
            except OSError:
                continue
    try:
        mtimes.append(os.path.getmtime(tree / "scratch" / "land_slice.log"))
    except OSError:
        pass
    return max(mtimes) if mtimes else None


def _project_containers(project: str) -> list[Container] | None:
    """A fresh inspect of ONE project's containers — the cap pass's
    just-before-down re-check (the first ``ps`` is evictions-stale by
    then). ``None`` when the daemon cannot answer: stop judging, a blind
    ``down`` is how a live gate dies."""
    ps = _docker(["ps", "-a", "-q", "--filter", f"label={L_PROJECT}={project}"])
    if ps.returncode != 0:
        return None
    ids = ps.stdout.split()
    return [Container.from_inspect(d) for d in _inspect("inspect", *ids)] if ids else []


def evict_project(project: str, *, volumes: bool) -> bool:
    """``docker compose -p <project> down --remove-orphans`` (plus ``-v``
    when ``volumes``). Refuses any project not starting
    ``precis-test-slice-`` — this module never downs a sibling's or a
    hand-run stack."""
    if not project.startswith(SLICE_PREFIX):
        return False
    args = ["compose", "-p", project, "down", "--remove-orphans"]
    if volumes:
        args.append("-v")
    return _docker(args).returncode == 0


def _orphan_networks(live_projects: set[str]) -> list[str]:
    """``precis-test-slice-*`` networks whose project has no container
    left — each still counts against the daemon's address-pool ceiling
    (gr311857). ``live_projects`` is every project that still has (or is
    about to get) containers."""
    out = _docker(
        [
            "network",
            "ls",
            "--format",
            '{{.Name}}|{{.Label "com.docker.compose.project"}}',
        ]
    )
    if out.returncode != 0:
        return []
    removed: list[str] = []
    for line in out.stdout.splitlines():
        name, _, proj = line.partition("|")
        name, proj = name.strip(), proj.strip()
        if not name or not proj.startswith(SLICE_PREFIX) or proj in live_projects:
            continue
        if _docker(["network", "rm", name]).returncode == 0:
            removed.append(name)
    return removed


def sweep(*, keep: Path | None = None) -> list[str]:
    """Evict slice stacks that are done: gone trees with ``-v``, idle
    stacks older than the window without, then the oldest live-free stacks
    while over the cap; then orphan networks. ``keep``'s project is never
    touched (it is about to run). Returns the names it removed — project
    names for stacks, network names for orphans. ``[]`` when the daemon
    cannot answer: the land goes on, the pile-up is a smell, not a
    failure."""
    if sweep_off():
        return []
    keep_project = project_for_tree(keep) if keep is not None else ""
    ps = _docker(["ps", "-a", "-q", "--filter", f"label={L_PROJECT}"])
    if ps.returncode != 0:
        return []  # daemon unreachable: judge nothing, evict nothing
    ids = ps.stdout.split()
    groups: dict[str, list[Container]] = {}
    for d in _inspect("inspect", *ids) if ids else []:
        project = str(_labels(d).get(L_PROJECT, ""))
        if project.startswith(SLICE_PREFIX) and project != keep_project:
            groups.setdefault(project, []).append(Container.from_inspect(d))

    now = time.time()
    idle_s = idle_seconds()
    evicted: list[str] = []
    remaining: list[Stack] = []
    for project, containers in sorted(groups.items()):
        tree = _tree_of(containers)
        if tree is None:
            # No single readable worktree: count it against the cap (the
            # VM still holds it) but never evict on a guessed path.
            remaining.append(Stack(project, None, containers))
            continue
        if not tree.is_dir():
            if evict_project(project, volumes=True):
                evicted.append(project)
            continue
        stack = Stack(
            project,
            tree,
            containers,
            live=_live(containers, tree),
            activity=_activity(tree, containers),
        )
        if (
            not stack.live
            and stack.activity is not None
            and now - stack.activity >= idle_s
        ):
            if evict_project(project, volumes=False):
                evicted.append(project)
            continue
        remaining.append(stack)

    cap = stack_cap()
    floor_s = cap_floor_seconds()
    while len(remaining) > cap:
        now = time.time()
        # Recency is a busy signal here, not just liveness: ship writes
        # no precis-test-runs marker and gates through separate `compose
        # exec` calls, so a stack quiet between execs — or a builder's
        # stack raised minutes ago — must not be downed mid-ship.
        candidates = [
            s
            for s in remaining
            if not s.live and s.activity is not None and now - s.activity >= floor_s
        ]
        if not candidates:
            break
        oldest = min(candidates, key=lambda s: s.activity or 0)
        # The first scan is evictions-stale by now: re-judge on a fresh
        # inspect right before the down.
        if oldest.tree is not None:
            fresh = _project_containers(oldest.project)
            if fresh is None:
                break  # the daemon stopped answering: judge nothing further
            if _live(fresh, oldest.tree):
                oldest.live = True  # went busy since the scan
                continue
            seen = _activity(oldest.tree, fresh)
            if seen is None or now - seen < floor_s:
                # Unjudgeable now, or went young again: kept — the cap
                # pass never evicts on a guess either.
                oldest.activity = seen
                continue
        if evict_project(oldest.project, volumes=False):
            evicted.append(oldest.project)
        remaining.remove(oldest)

    live_projects = (set(groups) - set(evicted)) | (
        {keep_project} if keep_project else set()
    )
    return evicted + _orphan_networks(live_projects)


__all__ = [
    "CAP_DEFAULT",
    "CAP_FLOOR_S_DEFAULT",
    "DOCKER_TIMEOUT_S",
    "IDLE_S_DEFAULT",
    "PAIR_SERVICES",
    "PROJECT_PREFIX",
    "RUN_MARKER_DIR",
    "SLICE_PREFIX",
    "Container",
    "Stack",
    "cap_floor_seconds",
    "evict_project",
    "idle_seconds",
    "project_for_tree",
    "stack_cap",
    "sweep",
    "sweep_off",
]
