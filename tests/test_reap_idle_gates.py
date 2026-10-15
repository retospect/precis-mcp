"""``scripts/reap-idle-gates`` — the idle warm-gate pair sweep.

``scripts/ship`` leaves a ``precis-gate`` container running in every tree's
``precis-test-<tree>`` project, and ``scripts/reap-test-dbs`` skips any project
with a running gate, so gate/db pairs of dead sessions stayed up for days and
pinned old images on the VM disk (docs/backlog/docker-vm-disk-fills-silently.md).

These run the REAL script, staged into a throwaway git repo with registered
worktrees, against a synthetic ``docker`` (state in a JSON file, every call
logged) and a synthetic ``scripts/inflight``. The real daemon is never
contacted.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: git worktrees + a shebang'd fake docker",
)

REPO = Path(__file__).resolve().parent.parent
HOUR = 3600

_FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["FAKE_DOCKER_STATE"]
with open(state_path, encoding="utf-8") as fh:
    st = json.load(fh)
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
if os.environ.get("FAKE_DOCKER_DOWN"):
    sys.exit(1)


def save():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(st, fh)


def labels(o):
    return o.get("Labels") or (o.get("Config") or {}).get("Labels") or {}


def match(o, flt):
    k, _, v = flt.partition("=")
    lab = labels(o)
    return k in lab and (not v or lab[k] == v)


def filtered(items, args):
    out = items
    for i, a in enumerate(args):
        if a == "--filter":
            f = args[i + 1]
            assert f.startswith("label="), f
            out = [o for o in out if match(o, f[len("label="):])]
    return out


def inspect(items, key, wanted):
    found = [o for o in items if o[key] in wanted]
    if key == "Id":
        st["inspects"] = st.get("inspects", 0) + 1
        after = os.environ.get("FAKE_EXEC_AFTER_INSPECTS")
        if after and st["inspects"] > int(after):
            for o in found:
                o["ExecIDs"] = ["late-exec"]
        save()
    print(json.dumps(found))
    sys.exit(0 if len(found) == len(wanted) else 1)


def remove(kind, key, name):
    before = len(st[kind])
    st[kind] = [o for o in st[kind] if o[key] != name]
    save()
    if len(st[kind]) == before:
        sys.exit(1)
    print(name)


if argv[:1] == ["ps"]:
    print("\n".join(o["Id"] for o in filtered(st["containers"], argv)))
elif argv[:1] == ["inspect"]:
    inspect(st["containers"], "Id", argv[1:])
elif argv[:2] == ["rm", "-f"]:
    remove("containers", "Id", argv[2])
elif argv[:1] in (["volume"], ["network"]):
    kind = argv[0] + "s"
    key = "Name" if kind == "volumes" else "Id"
    verb = argv[1]
    if verb == "ls":
        print("\n".join(o[key] for o in filtered(st[kind], argv)))
    elif verb == "inspect":
        inspect(st[kind], key, argv[2:])
    elif verb == "rm":
        if kind == "networks":
            ids = [o["Id"] for o in st[kind] if o["Name"] == argv[2]]
            remove(kind, "Id", ids[0] if ids else argv[2])
        else:
            remove(kind, key, argv[2])
else:
    sys.exit(2)
"""

_FAKE_INFLIGHT = """#!/usr/bin/env bash
[ -n "${FAKE_INFLIGHT_FAIL:-}" ] && exit 1
cat "$FAKE_INFLIGHT_JSON"
"""


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123456789Z"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.now = time.time()
        self.primary = tmp_path / "precis-mcp"
        self.primary.mkdir()
        self._git(self.primary, "init", "-q", "-b", "main")
        (self.primary / "scripts").mkdir()
        script = self.primary / "scripts" / "reap-idle-gates"
        shutil.copy2(REPO / "scripts" / "reap-idle-gates", script)
        inflight = self.primary / "scripts" / "inflight"
        inflight.write_text(_FAKE_INFLIGHT, encoding="utf-8")
        inflight.chmod(0o755)
        self._git(self.primary, "add", "-A")
        self._git(self.primary, "commit", "-q", "-m", "init")
        self.script = script
        bindir = tmp_path / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(_FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.bindir = bindir
        self.state_path = tmp_path / "docker-state.json"
        self.log_path = tmp_path / "docker.log"
        self.inflight_path = tmp_path / "inflight.json"
        self.state: dict[str, list[dict]] = {
            "containers": [],
            "volumes": [],
            "networks": [],
        }
        self.sessions: dict[str, str] = {str(self.primary): "—"}

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    def tree(self, name: str, *, session: str = "—", idle_h: float = 30.0) -> Path:
        path = self.tmp / "wts" / name
        self._git(self.primary, "worktree", "add", "-q", "-b", name, str(path))
        self.sessions[str(path)] = session
        self.age_tree(path, idle_h)
        return path

    def age_tree(self, path: Path, idle_h: float) -> None:
        gitdir = Path(
            (path / ".git").read_text(encoding="utf-8").split("gitdir: ")[1].strip()
        )
        t = self.now - idle_h * HOUR
        for p in (gitdir / "index", gitdir / "HEAD", gitdir / "logs" / "HEAD"):
            if p.exists():
                os.utime(p, (t, t))

    def pair(
        self,
        project: str,
        tree: Path | str,
        *,
        up_h: float = 30.0,
        services: tuple[str, ...] = ("precis-test-db", "precis-gate"),
        exec_ids: list[str] | None = None,
    ) -> None:
        for svc in services:
            self.state["containers"].append(
                {
                    "Id": f"{project}-{svc}-id",
                    "Name": f"/{project}-{svc}-1",
                    "Created": _iso(self.now - up_h * HOUR),
                    "State": {
                        "Running": True,
                        "StartedAt": _iso(self.now - up_h * HOUR),
                    },
                    "ExecIDs": exec_ids if svc == "precis-gate" else None,
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": svc,
                            "com.docker.compose.project.config_files": (
                                f"{tree}/docker/dev/compose.yaml"
                            ),
                        }
                    },
                }
            )
        self.volume(project)
        self.state["networks"].append(
            {
                "Id": f"{project}-net-id",
                "Name": f"{project}_default",
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.network": "default",
                },
            }
        )

    def volume(self, project: str, key: str = "gate-cache") -> None:
        self.state["volumes"].append(
            {
                "Name": f"{project}_{key}",
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.volume": key,
                },
            }
        )

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.inflight_path.write_text(
            json.dumps(
                {
                    "worktrees": [
                        {"path": p, "session": s} for p, s in self.sessions.items()
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.log_path.write_text("", encoding="utf-8")
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
        full_env.update(
            PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_DOCKER_STATE=str(self.state_path),
            FAKE_DOCKER_LOG=str(self.log_path),
            FAKE_INFLIGHT_JSON=str(self.inflight_path),
            **env,
        )
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=full_env,
            timeout=60,
            check=False,
        )

    def removals(self) -> list[list[str]]:
        calls = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        return [c for c in calls if "rm" in c[:2]]

    def remaining(self) -> dict[str, list[str]]:
        st = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            "containers": [c["Id"] for c in st["containers"]],
            "volumes": [v["Name"] for v in st["volumes"]],
            "networks": [n["Name"] for n in st["networks"]],
        }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_idle_sessionless_pair_is_reaped_gate_first(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-test-idle-tree", tree)
    out = env.run()
    assert out.returncode == 0, out.stderr
    assert "reaped 1: precis-test-idle-tree" in out.stdout
    assert env.removals() == [
        ["rm", "-f", "precis-test-idle-tree-precis-gate-id"],
        ["rm", "-f", "precis-test-idle-tree-precis-test-db-id"],
        ["volume", "rm", "precis-test-idle-tree_gate-cache"],
        ["network", "rm", "precis-test-idle-tree_default"],
    ]
    assert env.remaining() == {"containers": [], "volumes": [], "networks": []}


def test_dry_run_lists_and_removes_nothing(env: Env) -> None:
    idle = env.tree("idle-tree")
    live = env.tree("live-tree", session="live#123")
    env.pair("precis-test-idle-tree", idle)
    env.pair("precis-test-live-tree", live)
    out = env.run("--dry-run")
    assert out.returncode == 0, out.stderr
    assert "would reap precis-test-idle-tree: idle 30.0h, session —" in out.stdout
    assert "keep precis-test-live-tree: session live#123" in out.stdout
    assert "--dry-run: would reap 1: precis-test-idle-tree" in out.stdout
    assert env.removals() == []


@pytest.mark.parametrize("session", ["live#4242", "locked"])
def test_live_or_unjudgeable_session_is_kept(env: Env, session: str) -> None:
    tree = env.tree("busy", session=session)
    env.pair("precis-test-busy", tree)
    out = env.run()
    assert "nothing to reap" in out.stdout
    assert env.removals() == []


def test_dead_lock_session_counts_as_sessionless(env: Env) -> None:
    tree = env.tree("crashed", session="dead-lock#99")
    env.pair("precis-test-crashed", tree)
    assert "reaped 1: precis-test-crashed" in env.run().stdout


def test_fresh_purpose_keeps_the_pair(env: Env) -> None:
    tree = env.tree("parked")
    (tree / ".claude").mkdir(exist_ok=True)
    (tree / ".claude" / "purpose").write_text(
        "parked: resume later\n", encoding="utf-8"
    )
    env.pair("precis-test-parked", tree)
    out = env.run("--dry-run")
    assert "keep precis-test-parked: fresh .claude/purpose" in out.stdout


def test_exec_in_flight_keeps_the_pair(env: Env) -> None:
    tree = env.tree("gating")
    env.pair("precis-test-gating", tree, exec_ids=["abc"])
    out = env.run()
    assert "nothing to reap" in out.stdout
    assert env.removals() == []


def test_exec_attaching_after_judgment_aborts_removal(env: Env) -> None:
    tree = env.tree("late")
    env.pair("precis-test-late", tree)
    out = env.run(FAKE_EXEC_AFTER_INSPECTS="1")
    assert "nothing to reap" in out.stdout
    assert env.removals() == []


def test_recently_started_pair_is_kept(env: Env) -> None:
    tree = env.tree("fresh-gate")
    env.pair("precis-test-fresh-gate", tree, up_h=3)
    assert (
        "keep precis-test-fresh-gate: idle 3.0h < 24.0h" in env.run("--dry-run").stdout
    )


def test_recent_git_activity_keeps_an_old_pair(env: Env) -> None:
    tree = env.tree("committed", idle_h=1)
    env.pair("precis-test-committed", tree, up_h=40)
    assert "keep precis-test-committed: idle 1.0h" in env.run("--dry-run").stdout


def test_agent_trees_use_the_short_threshold(env: Env) -> None:
    tree = env.tree("agent-abc123", idle_h=3)
    env.pair("precis-test-agent-abc123", tree, up_h=3)
    assert "reaped 1: precis-test-agent-abc123" in env.run().stdout


def test_threshold_is_configurable(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-test-idle-tree", tree)
    out = env.run("--dry-run", PRECIS_REAP_GATE_MIN_AGE_SECONDS=str(48 * HOUR))
    assert "keep precis-test-idle-tree: idle 30.0h < 48.0h" in out.stdout


def test_project_with_another_service_is_kept(env: Env) -> None:
    tree = env.tree("running")
    env.pair(
        "precis-test-running",
        tree,
        services=("precis-test-db", "precis-gate", "precis-dev"),
    )
    out = env.run("--dry-run")
    assert "keep precis-test-running: other service ['precis-dev']" in out.stdout


def test_db_only_project_is_left_to_reap_test_dbs(env: Env) -> None:
    tree = env.tree("dbonly")
    env.pair("precis-test-dbonly", tree, services=("precis-test-db",))
    assert "keep precis-test-dbonly: no gate container" in env.run("--dry-run").stdout


def test_non_test_projects_are_never_judged_or_touched(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-mcp-http", tree)
    env.pair("precis-code-search", tree)
    out = env.run()
    assert "nothing to reap" in out.stdout
    assert env.removals() == []


def test_unregistered_and_primary_trees_are_kept(env: Env) -> None:
    stray = env.tmp / "elsewhere" / "stray"
    stray.mkdir(parents=True)
    env.pair("precis-test-stray", stray)
    env.pair("precis-test-precis-mcp", env.primary)
    out = env.run("--dry-run")
    assert "keep precis-test-stray: not a registered worktree" in out.stdout
    assert "keep precis-test-precis-mcp: primary checkout" in out.stdout


def test_gone_tree_is_left_to_reap_test_dbs(env: Env) -> None:
    env.pair("precis-test-gone", env.tmp / "wts" / "gone")
    assert "keep precis-test-gone: tree gone" in env.run("--dry-run").stdout


def test_mismatched_project_name_is_kept(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-test-other-name", tree)
    out = env.run("--dry-run")
    assert "keep precis-test-other-name: project name does not match" in out.stdout


def test_no_inflight_answer_reaps_nothing(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-test-idle-tree", tree)
    out = env.run(FAKE_INFLIGHT_FAIL="1")
    assert "nothing to reap" in out.stdout
    assert env.removals() == []


def test_orphan_gate_cache_volume_is_reaped_only_when_unminted(env: Env) -> None:
    env.tree("parked-tree")
    env.volume("precis-test-long-gone")  # no tree, no container
    env.volume("precis-test-parked-tree")  # a registered tree still mints it
    env.volume("precis-test-long-gone", key="other")  # not the gate cache
    env.volume("someone-else")  # not a test project
    out = env.run()
    assert "reaped 1: precis-test-long-gone_gate-cache" in out.stdout
    assert env.removals() == [["volume", "rm", "precis-test-long-gone_gate-cache"]]


def test_no_autoreap_is_a_no_op(env: Env) -> None:
    tree = env.tree("idle-tree")
    env.pair("precis-test-idle-tree", tree)
    out = env.run(PRECIS_NO_AUTOREAP="1")
    assert out.stdout == ""
    assert env.log_path.read_text(encoding="utf-8") == ""


def test_unreachable_daemon_is_silent(env: Env) -> None:
    out = env.run(FAKE_DOCKER_DOWN="1")
    assert out.returncode == 0
    assert out.stdout == ""


@pytest.mark.parametrize("name", ["idle-tree", "Foo.Bar baz", "agent-a1b2"])
def test_project_name_matches_the_bash_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    loader = importlib.machinery.SourceFileLoader(
        "reap_idle_gates", str(REPO / "scripts" / "reap-idle-gates")
    )
    spec = importlib.util.spec_from_loader("reap_idle_gates", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "reap_idle_gates", mod)  # dataclasses need it
    loader.exec_module(mod)
    path = tmp_path / name
    path.mkdir()
    bash = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; compose_project_for "$2"',
            "_",
            str(REPO / "scripts" / "lib" / "compose-project.sh"),
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert mod.compose_project_for(str(path)) == bash
