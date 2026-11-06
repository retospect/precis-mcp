"""slice_stacks — the lander-side reaper of finished slice test stacks
(gr570133, td573206).

Pure tests against a fake ``_docker`` seam and ``tmp_path`` trees: no
daemon, no real containers. The fake answers ``ps``/``inspect``/
``network ls`` from scripted state and records every argv.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: os.kill(pid, 0), UID/GID"
)

from precis.workers import slice_stacks


def _proc(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


class _Docker:
    """The fake ``_docker`` seam: argv dispatch + a call log."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: list[dict[str, Any]] = []
        self.networks: list[tuple[str, str]] = []  # (name, project label)
        self.rc = 0  # compose down / network rm exit code
        self.broken = False  # every call fails
        #: Optional hook run on every argv before dispatch — lets a test
        #: change container state mid-sweep (e.g. an exec attaches while a
        #: sibling's down runs).
        self.mutate: Any = None

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.mutate is not None:
            self.mutate(args)
        if self.broken:
            return _proc(rc=1)
        if args[:2] == ["ps", "-a"]:
            docs = self.containers
            if "--filter" in args:
                spec = args[args.index("--filter") + 1]
                key, _, val = spec.partition("label=")[2].partition("=")
                if val:
                    docs = [
                        c
                        for c in docs
                        if str(c["Config"]["Labels"].get(key, "")) == val
                    ]
            ids = [str(c["Id"]) for c in docs]
            return _proc("\n".join(ids) + "\n" if ids else "")
        if args[:1] == ["inspect"]:
            wanted = set(args[1:])
            docs = (
                [c for c in self.containers if str(c["Id"]) in wanted]
                if wanted
                else self.containers
            )
            return _proc(json.dumps(docs))
        if args[:2] == ["network", "ls"]:
            return _proc("".join(f"{n}|{p}\n" for n, p in self.networks))
        return _proc(rc=self.rc)  # compose down / network rm / anything

    def downs(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["compose"]]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> _Docker:
    fake = _Docker()
    monkeypatch.setattr(slice_stacks, "_docker", fake)
    for name in (
        "PRECIS_SLICE_STACK_SWEEP",
        "PRECIS_SLICE_STACK_IDLE_S",
        "PRECIS_SLICE_STACK_CAP",
        "PRECIS_SLICE_STACK_CAP_FLOOR_S",
    ):
        monkeypatch.delenv(name, raising=False)
    return fake


_ids = itertools.count(1)


def _container(
    project: str,
    tree: Path | None,
    *,
    service: str = "precis-test-db",
    running: bool = False,
    exec_ids: list[str] | None = None,
    started_s_ago: float = 7200,
) -> dict[str, Any]:
    """One ``docker inspect`` doc for a container in ``project`` whose
    compose file is ``<tree>/docker/dev/compose.yaml``."""
    started = datetime.now(UTC) - timedelta(seconds=started_s_ago)
    return {
        "Id": f"{next(_ids):064d}",
        "Name": f"/{project}-{service}-1",
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
                "com.docker.compose.project.config_files": (
                    f"{tree}/docker/dev/compose.yaml" if tree is not None else ""
                ),
            }
        },
        "State": {
            "Status": "running" if running else "exited",
            "StartedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "ExecIDs": list(exec_ids or []),
    }


def _tree(
    tmp_path: Path,
    name: str,
    *,
    git_age: float | None = None,
    log_age: float | None = None,
    marker_pids: list[int] | None = None,
) -> Path:
    """A present slice worktree: a ``.git`` dir (index/HEAD/logs/HEAD) at
    ``git_age`` seconds old, an optional ``scratch/land_slice.log``, and
    run markers ``<gitdir>/precis-test-runs/<pid>``."""
    tree = tmp_path / name
    gd = tree / ".git"
    (gd / "logs").mkdir(parents=True)
    now = time.time()
    for rel in ("index", "HEAD", "logs/HEAD"):
        p = gd / rel
        p.write_text("x", encoding="utf-8")
        if git_age is not None:
            os.utime(p, (now - git_age, now - git_age))
    if log_age is not None:
        log = tree / "scratch" / "land_slice.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("x", encoding="utf-8")
        os.utime(log, (now - log_age, now - log_age))
    for pid in marker_pids or []:
        runs = gd / "precis-test-runs"
        runs.mkdir(exist_ok=True)
        (runs / str(pid)).write_text("x", encoding="utf-8")
    return tree


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_project_for_tree_matches_shell_twin() -> None:
    """Same basename-sanitise-lowercase-prefix as
    scripts/lib/compose-project.sh's compose_project_for."""
    assert (
        slice_stacks.project_for_tree(Path("/x/y/slice-td42"))
        == "precis-test-slice-td42"
    )
    # trailing slash and characters outside [a-z0-9_-] map to dashes
    assert (
        slice_stacks.project_for_tree(Path("/x/y/slice-td42/"))
        == "precis-test-slice-td42"
    )
    assert (
        slice_stacks.project_for_tree(Path("/x/y/Slice TD 42!"))
        == "precis-test-slice-td-42-"
    )
    assert slice_stacks.project_for_tree(Path("/")) == ""


def test_evict_refuses_foreign_project(docker: _Docker) -> None:
    assert slice_stacks.evict_project("precis-test-other", volumes=True) is False
    assert slice_stacks.evict_project("precis-gate", volumes=False) is False
    assert slice_stacks.evict_project("postgres", volumes=True) is False
    assert docker.calls == []
    assert slice_stacks.evict_project("precis-test-slice-td42", volumes=True) is True
    assert docker.downs() == [
        [
            "compose",
            "-p",
            "precis-test-slice-td42",
            "down",
            "--remove-orphans",
            "-v",
        ]
    ]


def test_sweep_evicts_gone_tree_with_volumes(docker: _Docker, tmp_path: Path) -> None:
    gone = tmp_path / "slice-td7"  # the config label's tree, never created
    docker.containers = [_container("precis-test-slice-td7", gone)]
    assert slice_stacks.sweep() == ["precis-test-slice-td7"]
    assert docker.downs() == [
        [
            "compose",
            "-p",
            "precis-test-slice-td7",
            "down",
            "--remove-orphans",
            "-v",
        ]
    ]


def test_sweep_evicts_idle_keeps_volume(docker: _Docker, tmp_path: Path) -> None:
    """An idle present-tree stack comes down WITHOUT -v: the warm
    gate-cache (uv/mypy) volume survives for the next slice on the tree.
    A dead run marker is stale evidence, not liveness."""
    tree = _tree(tmp_path, "slice-td7", git_age=7200, marker_pids=[_dead_pid()])
    docker.containers = [_container("precis-test-slice-td7", tree, started_s_ago=7200)]
    assert slice_stacks.sweep() == ["precis-test-slice-td7"]
    assert docker.downs() == [
        ["compose", "-p", "precis-test-slice-td7", "down", "--remove-orphans"]
    ]


def test_sweep_never_touches_busy_exec_marker_or_running_run(
    docker: _Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three busy signals — a running non-pair container, an attached
    exec, a live run-marker pid — each keeps its stack even over the cap."""
    monkeypatch.setenv("PRECIS_SLICE_STACK_CAP", "0")
    run_tree = _tree(tmp_path, "slice-td1", git_age=99999)
    exec_tree = _tree(tmp_path, "slice-td2", git_age=99999)
    mark_tree = _tree(tmp_path, "slice-td3", git_age=99999, marker_pids=[os.getpid()])
    docker.containers = [
        _container(
            "precis-test-slice-td1",
            run_tree,
            service="precis-dev",  # a run --rm pytest, not the pair
            running=True,
        ),
        _container(
            "precis-test-slice-td2", exec_tree, running=True, exec_ids=["abc123"]
        ),
        _container("precis-test-slice-td3", mark_tree),
    ]
    assert slice_stacks.sweep() == []
    assert docker.downs() == []


def test_sweep_cap_evicts_oldest_idle_first(
    docker: _Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four quiet stacks all older than the cap floor but younger than
    the idle window (kept by the idle pass): two over the cap means the
    two oldest come down — without -v."""
    monkeypatch.setenv("PRECIS_SLICE_STACK_CAP", "2")
    floor = slice_stacks.CAP_FLOOR_S_DEFAULT
    ages = {
        "td1": floor + 100,
        "td2": floor + 500,
        "td3": floor + 900,
        "td4": floor + 1300,
    }
    docker.containers = [
        _container(
            f"precis-test-slice-{n}",
            _tree(tmp_path, f"slice-{n}", git_age=age),
            started_s_ago=age,
        )
        for n, age in ages.items()
    ]
    assert slice_stacks.sweep() == [
        "precis-test-slice-td4",
        "precis-test-slice-td3",
    ]
    assert all("-v" not in c for c in docker.downs())


def test_sweep_cap_keeps_young_quiet_stacks(
    docker: _Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recency is a busy signal for the cap pass: a quiet stack younger
    than the cap floor may be a land mid-ship (ship writes no run marker
    and gates through separate `compose exec` calls, so between execs
    nothing looks busy). Over the cap, the young still survive."""
    monkeypatch.setenv("PRECIS_SLICE_STACK_CAP", "1")
    ages = {"td1": 60, "td2": 240, "td3": 700}
    docker.containers = [
        _container(
            f"precis-test-slice-{n}",
            _tree(tmp_path, f"slice-{n}", git_age=age),
            started_s_ago=age,
        )
        for n, age in ages.items()
    ]
    assert slice_stacks.sweep() == []
    assert docker.downs() == []


def test_sweep_cap_rechecks_liveness_before_down(
    docker: _Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first scan is evictions-stale by the time a down runs: a stack
    that went busy since (an exec attached while a sibling's down ran)
    is re-inspected right before its own down and kept."""
    monkeypatch.setenv("PRECIS_SLICE_STACK_CAP", "1")
    floor = slice_stacks.CAP_FLOOR_S_DEFAULT
    ages = {"td1": floor + 1000, "td2": floor + 500}
    docker.containers = [
        _container(
            f"precis-test-slice-{n}",
            _tree(tmp_path, f"slice-{n}", git_age=age),
            started_s_ago=age,
        )
        for n, age in ages.items()
    ]

    def _busy_td2(args: list[str]) -> None:
        if args[:1] == ["compose"]:  # td1's down just ran
            for c in docker.containers:
                labels = c["Config"]["Labels"]
                if labels["com.docker.compose.project"] == "precis-test-slice-td2":
                    c["ExecIDs"] = ["abc123"]

    docker.mutate = _busy_td2
    assert slice_stacks.sweep() == ["precis-test-slice-td1"]
    assert docker.downs() == [
        ["compose", "-p", "precis-test-slice-td1", "down", "--remove-orphans"]
    ]


def test_sweep_keeps_the_keep_tree(docker: _Docker, tmp_path: Path) -> None:
    own = _tree(tmp_path, "slice-td42", git_age=99999)
    other = _tree(tmp_path, "slice-td7", git_age=99999)
    docker.containers = [
        _container("precis-test-slice-td42", own, started_s_ago=99999),
        _container("precis-test-slice-td7", other, started_s_ago=99999),
    ]
    assert slice_stacks.sweep(keep=own) == ["precis-test-slice-td7"]
    assert docker.downs() == [
        ["compose", "-p", "precis-test-slice-td7", "down", "--remove-orphans"]
    ]


def test_sweep_off_is_noop(
    docker: _Docker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SLICE_STACK_SWEEP", "off")
    docker.containers = [_container("precis-test-slice-td7", tmp_path / "slice-td7")]
    assert slice_stacks.sweep() == []
    assert docker.calls == []


def test_sweep_removes_orphan_networks(docker: _Docker, tmp_path: Path) -> None:
    """A slice network whose project has no container is removed; one
    whose project still has containers — and any non-slice network — is
    left."""
    young = _tree(tmp_path, "slice-td7", git_age=60)
    docker.containers = [_container("precis-test-slice-td7", young, started_s_ago=60)]
    docker.networks = [
        ("precis-test-slice-td7_default", "precis-test-slice-td7"),
        ("precis-test-slice-td9_default", "precis-test-slice-td9"),
        ("precis-test-other_default", "precis-test-other"),
    ]
    assert slice_stacks.sweep() == ["precis-test-slice-td9_default"]
    assert [c for c in docker.calls if c[:2] == ["network", "rm"]] == [
        ["network", "rm", "precis-test-slice-td9_default"]
    ]


def test_docker_failure_returns_empty(docker: _Docker, tmp_path: Path) -> None:
    """A dead daemon is a quiet no-op: the land goes on, nothing is
    judged or evicted."""
    docker.broken = True
    docker.containers = [_container("precis-test-slice-td7", tmp_path / "slice-td7")]
    assert slice_stacks.sweep() == []
    assert docker.calls == [
        ["ps", "-a", "-q", "--filter", "label=com.docker.compose.project"]
    ]
