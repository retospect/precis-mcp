"""The factory rehearsal harness: its safety fences, its scripted session and
its world wrappers (``scripts/rehearsal/``, ``scripts/factory-rehearsal``).

Host-only: no container, no database, no network. The shim tests drive the
real hub (``_slice_ws.ensure_hub``: its real ``pre-receive`` hook and secret
scan) and the real lane adapters' argv (``_slice_adapters``), so a drift in
either shows here before a rehearsal run does.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: process groups, os.killpg, fcntl locks and sh wrappers",
)

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

from rehearsal import db as rdb
from rehearsal import fixtures, runner, safety
from rehearsal import scenarios as rscenarios
from rehearsal import worker as rworker
from rehearsal import world as rworld

SHIM = SCRIPTS / "rehearsal" / "shim.py"
GH_STUB = SCRIPTS / "rehearsal" / "gh_stub.py"
GOOD_DSN = "postgresql://postgres@127.0.0.1:54321/precis_rehearsal"


# ── the database fence ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pinned_port() -> Iterator[None]:
    """The port Docker 'published' for the run these tests pretend to have."""
    safety.pin_port(54321)
    yield
    safety.pin_port(None)


@pytest.mark.parametrize(
    "dsn",
    [
        GOOD_DSN,
        "postgresql://u@localhost:54321/precis_rehearsal",
        "postgres://postgres@[::1]:54321/precis_rehearsal",
        "postgresql://postgres@127.0.0.2:54321/precis_rehearsal",
    ],
)
def test_scratch_dsn_accepts_loopback_rehearsal(dsn: str) -> None:
    assert safety.assert_scratch_dsn(dsn) == dsn


@pytest.mark.parametrize(
    ("dsn", "why"),
    [
        ("postgresql://u@db.example.com:54321/precis_rehearsal", "not loopback"),
        ("postgresql://u@10.0.0.5:54321/precis_rehearsal", "not loopback"),
        ("postgresql://u@0.0.0.0:54321/precis_rehearsal", "not loopback"),
        ("postgresql://u@127.0.0.1:54321/precis", "is not 'precis_rehearsal'"),
        ("postgresql://u@127.0.0.1:54321/precis_test", "is not 'precis_rehearsal'"),
        # The name is judged whole, not by a substring.
        (
            "postgresql://u@127.0.0.1:54321/precis_prod_rehearsal",
            "is not 'precis_rehearsal'",
        ),
        ("postgresql://u@127.0.0.1:54321/rehearsal", "is not 'precis_rehearsal'"),
        # A production container published on another loopback port.
        ("postgresql://u@127.0.0.1:5432/precis_rehearsal", "Docker published"),
        ("postgresql://u@127.0.0.1/precis_rehearsal", "Docker published"),
        (
            "postgresql://u@127.0.0.1:54321/precis_rehearsal?host=db.example.com",
            "not loopback|carries",
        ),
        (
            "postgresql://u@127.0.0.1:54321/precis_rehearsal?service=prod",
            "carries",
        ),
        (
            "postgresql://u@127.0.0.1:54321/precis_rehearsal?hostaddr=10.1.2.3",
            "carries",
        ),
        (
            "postgresql://u@127.0.0.1:54321/precis_rehearsal?options=-c%20x%3D1",
            "carries",
        ),
        ("postgresql://u@127.0.0.1,db.example.com/precis_rehearsal", "several|port"),
        ("postgresql:///precis_rehearsal", "no host|libpq"),
        ("postgresql://u@/precis_rehearsal", "no host|libpq"),
        ("host=127.0.0.1 dbname=precis_rehearsal", "postgresql:// URL"),
        ("service=prod", "postgresql:// URL"),
        ("sqlite:///precis_rehearsal", "postgresql:// URL"),
        ("", "no database DSN"),
        ("   ", "no database DSN"),
    ],
)
def test_scratch_dsn_refuses(dsn: str, why: str) -> None:
    with pytest.raises(safety.Refused, match=why):
        safety.assert_scratch_dsn(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        # libpq splits userinfo at the first '@' (host evil.example), a
        # hand-rolled split at the last one would call the host loopback.
        "postgresql://u@evil.example,@127.0.0.1:54321/precis_rehearsal",
        "postgresql://u:p" + "@ss@127.0.0.1:54321/precis_rehearsal",
        "postgresql://u@evil.example@127.0.0.1:54321/precis_rehearsal",
        # A percent-encoded host is decoded before it is judged.
        "postgresql://u@%65vil.example:54321/precis_rehearsal",
        "postgresql://u@%31%30.0.0.5:54321/precis_rehearsal",
    ],
)
def test_scratch_dsn_is_judged_as_libpq_parses_it(dsn: str) -> None:
    from psycopg.conninfo import conninfo_to_dict

    with pytest.raises(safety.Refused):
        safety.assert_scratch_dsn(dsn)
    # Whatever libpq makes of it, the verdict never rests on a loopback host
    # for these shapes.
    try:
        host = str(conninfo_to_dict(dsn).get("host", ""))
    except Exception:
        host = ""
    assert host not in ("127.0.0.1", "localhost", "::1")


def test_an_unpinned_run_accepts_no_dsn() -> None:
    safety.pin_port(None)
    with pytest.raises(safety.Refused, match="no scratch database has been started"):
        safety.assert_scratch_dsn(GOOD_DSN)
    assert safety.assert_scratch_dsn(GOOD_DSN, port=54321) == GOOD_DSN


def test_worker_env_never_carries_the_callers_connection_or_tokens() -> None:
    prod = "postgresql://svc@db.prod.example.com:5432/precis"
    caller = {
        "PRECIS_DATABASE_URL": prod,
        "PRECIS_DB_PASSWORD": "x",
        "PGHOST": "db.prod.example.com",
        "PGPASSWORD": "x",
        "DATABASE_URL": prod,
        "PRECIS_FACTORY_GIT_TOKEN": "t",
        "GH_TOKEN": "t",
        "ANTHROPIC_API_KEY": "k",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "PRECIS_SECRETS_FILE_DIR": "/run/secrets",
        "HOME": "/Users/someone",
        "USER": "someone",
        "LANG": "en_US.UTF-8",
    }
    env = safety.worker_env(
        caller, own={"PATH": "/x", "HOME": "/scratch/home"}, dsn=GOOD_DSN
    )
    assert env["PRECIS_DATABASE_URL"] == GOOD_DSN
    assert env["HOME"] == "/scratch/home"
    assert env["USER"] == "someone"
    assert env["LANG"] == "en_US.UTF-8"
    for leaked in (
        "PRECIS_DB_PASSWORD",
        "PGHOST",
        "PGPASSWORD",
        "DATABASE_URL",
        "PRECIS_FACTORY_GIT_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "SSH_AUTH_SOCK",
        "PRECIS_SECRETS_FILE_DIR",
    ):
        assert leaked not in env
    assert prod not in env.values()


def test_worker_env_refuses_a_foreign_dsn_and_forbidden_own_names() -> None:
    with pytest.raises(safety.Refused):
        safety.worker_env({}, own={}, dsn="postgresql://u@db.example.com/precis")
    with pytest.raises(safety.Refused):
        safety.worker_env({}, own={"GH_TOKEN": "t"}, dsn=GOOD_DSN)
    with pytest.raises(safety.Refused):
        safety.worker_env({}, own={"PGHOST": "elsewhere"}, dsn=GOOD_DSN)
    with pytest.raises(safety.Refused):
        safety.worker_env(
            {},
            own={"PRECIS_DATABASE_URL": "postgresql://u@db.example.com/p"},
            dsn=GOOD_DSN,
        )


def test_scrub_process_env_removes_connection_and_credential_names() -> None:
    environ = {
        "PRECIS_DATABASE_URL": "x",
        "PRECIS_FACTORY_GIT_TOKEN": "x",
        "PGHOST": "x",
        "DATABASE_URL": "x",
        "GH_TOKEN": "x",
        "CLAUDE_CODE_OAUTH_TOKEN": "x",
        "KIMI_CODE_API_KEY": "x",
        "PATH": "/usr/bin",
        "HOME": "/h",
    }
    before = set(environ)
    gone = safety.scrub_process_env(environ)
    assert sorted(gone) == sorted(before - {"PATH", "HOME"})
    assert environ == {"PATH": "/usr/bin", "HOME": "/h"}


def test_runner_entry_script_scrubs_the_environment_and_leaves_the_cwd() -> None:
    """Run the entry script's top level (not its ``main``) in a child process
    that carries a production-shaped environment: by the time it has run, the
    connection names are gone, the working directory is the scratch directory
    and ``precis`` has not been imported."""
    probe = (
        "import os, runpy, sys\n"
        f"runpy.run_path({str(SCRIPTS / 'factory-rehearsal')!r}, run_name='probe')\n"
        "print(os.environ.get('PRECIS_DATABASE_URL'), os.environ.get('PGHOST'), "
        "os.environ.get('GH_TOKEN'), os.environ.get('PATH') is not None)\n"
        "print(os.getcwd())\n"
        "print('precis' in sys.modules)\n"
    )
    env = {
        **os.environ,
        "PRECIS_DATABASE_URL": "postgresql://svc@db.prod.example.com/precis",
        "PGHOST": "db.prod.example.com",
        "GH_TOKEN": "t",
    }
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
        cwd=str(REPO),
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[0] == "None None None True", lines
    assert lines[1].endswith("scratch/rehearsal"), lines
    assert lines[2] == "False", "precis was imported before the scrub"


def test_compose_refuses_a_project_outside_the_rehearsal_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(safety.Refused, match="precis-rehearsal"):
        rdb._compose("precis-dev", "down", "-v")
    assert called == []
    assert rdb.remove_project("some-other-project").startswith("not removed")
    with pytest.raises(safety.Refused, match="prefix"):
        rdb.other_projects("precis-test-")
    assert called == []


def test_project_names_carry_a_checkout_id(tmp_path: Path) -> None:
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    assert rdb.project_prefix(a) != rdb.project_prefix(b)
    assert rdb.project_prefix(a) == rdb.project_prefix(a)
    assert rdb.project_prefix(a).startswith("precis-rehearsal-")


def test_other_projects_lists_only_this_checkouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mine, theirs = rdb.project_prefix(tmp_path), "precis-rehearsal-deadbeef-"
    rows = json.dumps(
        [
            {"Name": f"{mine}20261120"},
            {"Name": f"{theirs}20261120"},
            {"Name": "precis-test-x"},
        ]
    )
    monkeypatch.setattr(rdb, "check_local_engine", lambda: None)
    monkeypatch.setattr(
        rdb.proc,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, rows, ""),
    )
    assert rdb.other_projects(mine) == [f"{mine}20261120"]


def test_container_engine_must_be_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rdb, "_engine_checked", False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://elsewhere.example:2375")
    with pytest.raises(safety.Refused, match="DOCKER_HOST"):
        rdb.check_local_engine()
    monkeypatch.delenv("DOCKER_HOST")

    def inspect(host: str) -> Any:
        body = json.dumps([{"Endpoints": {"docker": {"Host": host}}}])
        return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(rdb.proc, "run", inspect("ssh://deploy@remote.example"))
    with pytest.raises(safety.Refused, match="not a local unix socket"):
        rdb.check_local_engine()
    monkeypatch.setattr(rdb.proc, "run", inspect("unix:///var/run/docker.sock"))
    rdb.check_local_engine()
    assert rdb._engine_checked is True
    monkeypatch.setattr(rdb, "_engine_checked", False)


def test_a_failure_after_compose_up_removes_the_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_compose(
        project: str, *args: str, timeout: float = 0.0
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "port":
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:55555\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rdb, "_compose", fake_compose)
    monkeypatch.setattr(rdb, "other_projects", lambda prefix: [])
    # The published port does not match what the fence was pinned to later:
    # break the fence after 'up' so the DSN check raises.
    monkeypatch.setattr(
        rdb,
        "assert_scratch_dsn",
        lambda dsn: (_ for _ in ()).throw(safety.Refused("simulated")),
    )
    with pytest.raises(safety.Refused, match="simulated"):
        rdb.ScratchDb.up("20261120", tmp_path)
    assert ("down", "-v", "--remove-orphans") in calls


def test_scratch_db_refuses_to_migrate_a_foreign_dsn(tmp_path: Path) -> None:
    foreign = rdb.ScratchDb(
        project="precis-rehearsal-x", dsn="postgresql://u@db.example.com:54321/precis"
    )
    with pytest.raises(safety.Refused):
        foreign.migrate(tmp_path / "python", env={}, cwd=tmp_path)


def test_driver_refuses_a_foreign_dsn(tmp_path: Path) -> None:
    from rehearsal.driver import Driver

    w = rworld.World(run_dir=tmp_path, repo_root=tmp_path)
    with pytest.raises(safety.Refused):
        Driver(w, "postgresql://u@db.example.com:54321/precis_rehearsal")
    with pytest.raises(safety.Refused):
        Driver(w, "postgresql://u@127.0.0.1:54321/precis")
    with pytest.raises(safety.Refused):
        Driver(w, "postgresql://u@127.0.0.1:5432/precis_rehearsal")


# ── the worker's environment ───────────────────────────────────────


def test_build_env_is_scratch_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PRECIS_DATABASE_URL", "postgresql://u@db.prod.example.com/precis"
    )
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "t")
    monkeypatch.setenv(
        "PATH", f"/Users/someone/.local/bin:/opt/tools:{os.environ['PATH']}"
    )
    w = rworld.World(run_dir=tmp_path / "run", repo_root=tmp_path)
    env = rworker.build_env(w, GOOD_DSN)
    assert env["PRECIS_DATABASE_URL"] == GOOD_DSN
    assert env["HOME"] == str(w.home)
    assert env["TMPDIR"] == str(w.tmp)
    assert "PRECIS_FACTORY_GIT_TOKEN" not in env
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(w.bin)
    assert not [p for p in parts if p.startswith("/Users/")]
    assert env["PRECIS_SLICE_ROOT"] == str(w.host)
    assert env["PRECIS_FACTORY_ORIGIN_URL"] == rworld.FAKE_ORIGIN
    assert env["DOCKER_HOST"].endswith("rehearsal.sock")
    assert env["PRECIS_SLICE_STACK_SWEEP"] == "off"
    for lane in ("CLAUDE", "DEVIN", "KIMI", "CODEX"):
        assert env[f"PRECIS_{lane}_BIN"] == str(w.bin / lane.lower())


def test_worker_refuses_to_start_on_a_foreign_dsn(tmp_path: Path) -> None:
    w = rworld.World(run_dir=tmp_path / "run", repo_root=tmp_path)
    worker = rworker.Worker(
        w, {"PRECIS_DATABASE_URL": "postgresql://u@db.example.com/precis"}
    )
    with pytest.raises(safety.Refused):
        worker.start()
    assert worker.proc is None
    with pytest.raises(safety.Refused):
        rworker.Worker(w, {}).start()


def test_worker_argv_names_only_the_wanted_passes(tmp_path: Path) -> None:
    w = rworld.World(run_dir=tmp_path / "run", repo_root=tmp_path)
    worker = rworker.Worker(
        w, {"PRECIS_DATABASE_URL": GOOD_DSN}, passes=("heartbeat", "job_ssh_node")
    )
    argv = worker.argv()
    assert argv[1:4] == ["-m", "precis", "worker"]
    only = [argv[i + 1] for i, a in enumerate(argv) if a == "--only"]
    assert only == ["heartbeat", "job_ssh_node"]
    assert argv[argv.index("--database-url") + 1] == GOOD_DSN


# ── registry ───────────────────────────────────────────────────────


def test_registry_carries_the_seven_gate_scenarios() -> None:
    assert set(rscenarios.GATE) <= set(rscenarios.REGISTRY)
    assert len(rscenarios.GATE) == 7
    assert rscenarios.REGISTRY["smoke"].run is not None


def test_unimplemented_scenarios_skip_by_default_and_fail_when_selected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    only = {"later": rscenarios.Scenario("later", "not written", None)}
    monkeypatch.setattr(runner, "REGISTRY", only)
    assert runner.main([]) == 0
    assert "SKIP later" in capsys.readouterr().out
    assert runner.main(["-s", "later"]) == 1
    assert "FAIL later" in capsys.readouterr().out
    assert runner.main(["-s", "nonesuch"]) == 2


# ── the credential-shaped fixture ──────────────────────────────────


def test_credential_fixture_is_flagged_but_its_source_is_clean() -> None:
    from precis.utils import secret_scan

    assert secret_scan.find_secrets(fixtures.credential_shaped())
    for name in ("fixtures.py", "shim.py", "gh_stub.py", "world.py", "scenarios.py"):
        text = (SCRIPTS / "rehearsal" / name).read_text(encoding="utf-8")
        assert not secret_scan.find_secrets(text), name
    assert not secret_scan.find_secrets(Path(__file__).read_text(encoding="utf-8"))


# ── world wrappers ─────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=kw.pop("check", True),
        **kw,
    )


def _scratch_remote(tmp_path: Path) -> Path:
    work = tmp_path / "seed"
    work.mkdir()
    _git(["init", "-q", "-b", "main"], work)
    _git(["config", "user.name", "t"], work)
    _git(["config", "user.email", "t@example.invalid"], work)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-q", "-m", "seed"], work)
    remote = tmp_path / "remote.git"
    _git(["clone", "-q", "--bare", str(work), str(remote)], tmp_path)
    return remote


def test_git_wrapper_maps_the_stand_in_url_and_refuses_other_github(
    tmp_path: Path,
) -> None:
    remote = _scratch_remote(tmp_path)
    wrapper = tmp_path / "git"
    rworld._write_exec(wrapper, rworld._git_wrapper(rworld._real("git"), remote))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    ok = subprocess.run(
        [str(wrapper), "ls-remote", rworld.FAKE_ORIGIN, "refs/heads/main"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ok.returncode == 0 and ok.stdout.split()[1] == "refs/heads/main", ok.stderr
    bare = subprocess.run(
        [
            str(wrapper),
            "ls-remote",
            rworld.FAKE_ORIGIN.removesuffix(".git"),
            "refs/heads/main",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert bare.returncode == 0, bare.stderr
    other = subprocess.run(
        [str(wrapper), "ls-remote", "https://github.com/someone/else.git"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert other.returncode == 2 and "refusing git contact" in other.stderr
    # A call that already carries url rewrites (ship's mirror env) keeps them
    # and gets none of ours.
    steps_aside = subprocess.run(
        [str(wrapper), "ls-remote", rworld.FAKE_ORIGIN],
        cwd=str(tmp_path),
        env={
            **env,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url./nonexistent-mirror.insteadOf",
            "GIT_CONFIG_VALUE_0": rworld.FAKE_ORIGIN,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    # The call went to the rewrite it carried (a mirror that does not exist),
    # not to our scratch remote and not to the real github.com.
    assert steps_aside.returncode != 0
    assert "nonexistent-mirror" in steps_aside.stderr, steps_aside.stderr
    assert str(remote) not in steps_aside.stderr


#: What a hosted runner has and a developer machine does not: tokens, CI
#: markers, and a real ``gh`` earlier on PATH that answers nothing.
_RUNNER_ENV = {
    "GH_TOKEN": "runner-stand-in",
    "GITHUB_TOKEN": "runner-stand-in",
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "someone/else",
    "CI": "true",
}


@pytest.mark.parametrize("runner", [False, True], ids=["plain-env", "runner-env"])
def test_gh_stub_answers_the_drift_guard_and_nothing_else(
    tmp_path: Path, runner: bool
) -> None:
    remote = _scratch_remote(tmp_path)
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(clone)], tmp_path)
    # The helper chdirs to ITS repo root (``parents[2]``) and reads the local
    # history there: run a copy that lives in the scratch clone, never the
    # checkout's own (a hosted runner's checkout has no ``origin/main``, so the
    # stub answered an empty history and the helper printed nothing).
    helper = clone / "scripts" / "lib" / "last_gated_main_sha.py"
    helper.parent.mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "lib" / "last_gated_main_sha.py", helper)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rworld._write_exec(
        bindir / "gh", f'#!/bin/sh\nexec {sys.executable} {GH_STUB} "$@"\n'
    )
    outer = {k: v for k, v in os.environ.items() if k not in _RUNNER_ENV}
    path = os.environ["PATH"]
    if runner:
        outer.update(_RUNNER_ENV)
        fake = tmp_path / "fake-gh"
        fake.mkdir()
        rworld._write_exec(fake / "gh", "#!/bin/sh\nexit 0\n")
        path = f"{fake}{os.pathsep}{path}"
    env = {**outer, "PATH": f"{bindir}{os.pathsep}{path}"}
    ok = subprocess.run(
        [
            sys.executable,
            "-I",
            str(helper),
            "--age-hours",
            "--start",
            "origin/main",
            "--hours",
            "48",
            "--diag",
        ],
        cwd=str(clone),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    # One commit in the scratch clone's history: the stub read the clone, not
    # the checkout the test runs from.
    assert "local=1 " in ok.stderr and "matched=1 " in ok.stderr, ok.stderr
    assert 0.5 < float(ok.stdout.strip() or "nan") < 2.0, (ok.stdout, ok.stderr)
    other = subprocess.run(
        [str(bindir / "gh"), "pr", "list"],
        cwd=str(clone),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert other.returncode == 1


# ── the scripted session ───────────────────────────────────────────


class Hub:
    """A real hub (``_slice_ws``) over a scratch host checkout, and one clone."""

    def __init__(self, tmp_path: Path) -> None:
        from precis.workers.job_types import _slice_ws

        self.ws = _slice_ws
        self.host = tmp_path / "host"
        self.host.mkdir()
        _git(["init", "-q", "-b", "main"], self.host)
        _git(["config", "user.name", "t"], self.host)
        _git(["config", "user.email", "t@example.invalid"], self.host)
        (self.host / "README.md").write_text("seed\n", encoding="utf-8")
        _git(["add", "."], self.host)
        _git(["commit", "-q", "-m", "seed"], self.host)
        self.hub = _slice_ws.ensure_hub(self.host)
        tip = _slice_ws.sync_hub(self.host, url=str(self.host), base="main")
        assert tip, "the hub could not be synced from the scratch host"
        self.world = tmp_path / "world"
        (self.world / "scripts").mkdir(parents=True)
        self.n = 0

    def tree(self, todo: int = 7, job: int = 3) -> Path:
        tree = self.ws.attempt_dir(self.host, todo, job)
        self.ws.cut(self.hub, tree, base="main", branch=f"slice/{todo}")
        return tree

    def script(self, todo: int, attempts: list[dict[str, Any]], **kw: Any) -> None:
        (self.world / "scripts" / f"td{todo}.json").write_text(
            json.dumps({"attempts": attempts, **kw}), encoding="utf-8"
        )

    def env(self, todo: int, job: int) -> dict[str, str]:
        base = {
            k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PRECIS_"))
        }
        return {**base, "PRECIS_SLICE_ID": f"td{todo}", "PRECIS_SLICE_JOB": str(job)}

    def run(
        self,
        lane: str,
        tree: Path,
        argv: list[str],
        *,
        todo: int = 7,
        job: int = 3,
        brief: str = "Build td7.",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SHIM),
                "--world",
                str(self.world),
                "--lane",
                lane,
                "--",
                *argv,
            ],
            cwd=str(tree),
            env=self.env(todo, job),
            input=brief,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )

    def record(self, todo: int = 7, attempt: int = 1) -> dict[str, Any]:
        data = json.loads(
            (self.world / "sessions" / f"td{todo}-n{attempt}.json").read_text(
                encoding="utf-8"
            )
        )
        assert isinstance(data, dict)
        return data

    def ref(self, todo: int = 7, job: int = 3) -> str | None:
        return self.ws.hub_tip(self.hub, self.ws.build_ref(todo, job))


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path)


def adapter_argv(adapter: str, tree: Path) -> tuple[list[str], str]:
    """The real adapter's argv for a session (without the binary) and its brief."""
    from precis.workers.job_types._slice_adapters import get_adapter

    brief_path = tree / "scratch" / "brief.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text("Build td7. This is the brief.\n", encoding="utf-8")
    argv = get_adapter(adapter).argv(
        model="swe-2-high" if adapter == "devin_tree" else None,
        effort="high",
        tree=tree,
        brief_path=brief_path,
        deny_paths=(),
        max_turns=5,
        models=("swe-2-high",),
    )
    return argv[1:], brief_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("adapter", "lane"),
    [
        ("claude_p", "claude"),
        ("devin_tree", "devin"),
        ("kimi_tree", "kimi"),
        ("codex_exec", "codex"),
    ],
)
def test_default_builder_pushes_and_every_lane_parses_its_message(
    hub: Hub, adapter: str, lane: str
) -> None:
    """Each lane's real adapter argv is recognised as a session and the final
    message comes back in the shape that adapter's parser reads."""
    from precis.workers.job_types._slice_adapters import get_adapter

    tree = hub.tree()
    argv, brief = adapter_argv(adapter, tree)
    proc = hub.run(lane, tree, argv, brief=brief)
    assert proc.returncode == 0, proc.stderr
    result, errored = get_adapter(adapter).parse_result(proc.stdout, tree, exit_code=0)
    assert not errored
    assert result and "tests: ran" in result
    head = _git(["rev-parse", "HEAD"], tree).stdout.strip()
    assert hub.ref() == head
    rec = hub.record()
    assert rec["state"] == "exited" and rec["code"] == 0 and rec["lane"] == lane
    assert any(s["do"] == "verify" and s["ok"] for s in rec["steps"])
    assert (
        rec["env"]["PRECIS_SLICE_ID"] == "td7" and rec["env"]["PRECIS_SLICE_JOB"] == "3"
    )
    assert "PRECIS_DATABASE_URL" not in rec["env_names"]
    assert rec["head0"] == hub.ws.hub_tip(hub.hub, "refs/heads/main")
    assert (
        (hub.world / "briefs" / "td7-n1.md")
        .read_text(encoding="utf-8")
        .startswith("Build td7")
    )


@pytest.mark.parametrize(
    ("lane", "argv", "expect"),
    [
        ("claude", ["--version"], "rehearsal shim"),
        ("devin", ["auth", "status"], "Logged in"),
        ("codex", ["login", "status"], "Logged in"),
        ("devin", ["models", "list", "--format", "json"], "families"),
        ("kimi", ["--version"], "rehearsal shim"),
    ],
)
def test_probes_are_answered_without_a_session(
    hub: Hub, lane: str, argv: list[str], expect: str
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            lane,
            "--",
            *argv,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        input="",
    )
    assert proc.returncode == 0 and expect in proc.stdout
    assert not list((hub.world / "sessions").glob("td*"))


def test_devin_login_probe_reads_the_shim_as_logged_in(hub: Hub) -> None:
    from precis.workers.job_types._slice_adapters import devin_status_logged_in

    proc = subprocess.run(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            "devin",
            "--",
            "auth",
            "status",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert devin_status_logged_in(proc)


def test_step_builders_drive_the_shim(hub: Hub, tmp_path: Path) -> None:
    from rehearsal import steps

    tree = hub.tree()
    marker = str(tmp_path / "unused.marker")
    script = [
        steps.attempt(*steps.builder("docs/via-steps.md", "via steps\n")),
        steps.attempt(steps.hang_in_commit(marker), steps.wait_for("/nonexistent")),
        steps.attempt(steps.wip("why"), steps.exit_(0, "done"), say="x"),
        steps.attempt(steps.verdict("FIX", "nope", anchors="a.py::b")),
    ]
    json.dumps(script)  # every step is plain JSON
    hub.script(7, script[:1])
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 0, proc.stderr
    ref = hub.ref()
    assert (
        ref
        and _git(["show", f"{ref}:docs/via-steps.md"], hub.hub).stdout == "via steps\n"
    )
    assert steps.builder("p", "t")[0] == {
        "do": "write",
        "path": "p",
        "text": "t",
        "append": False,
    }
    assert steps.push(on_refused=[steps.amend()])["on_refused"] == [{"do": "amend"}]
    assert steps.attempt(steps.verify(), say="hi") == {
        "steps": [{"do": "verify"}],
        "say": "hi",
    }


def test_wip_snapshot_is_pushed_and_labelled(hub: Hub) -> None:
    tree = hub.tree()
    hub.script(
        7,
        [
            {
                "steps": [
                    {"do": "write", "path": "docs/partial.md", "text": "half done\n"},
                    {"do": "wip", "why": "the parser is not written"},
                ],
                "say": "Could not finish: the parser is missing.",
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 0, proc.stderr
    ref = hub.ref()
    assert ref
    subject = _git(["log", "-1", "--format=%s", ref], hub.hub).stdout.strip()
    assert (
        subject.startswith("wip: snapshot") and "the parser is not written" in subject
    )
    assert hub.ws._is_wip(hub.hub, ref)


def test_refused_push_is_fixed_in_the_same_session(hub: Hub) -> None:
    tree = hub.tree()
    hub.script(
        7,
        [
            {
                "steps": [
                    {
                        "do": "write",
                        "path": "docs/leaky.md",
                        "text": f"token {fixtures.credential_shaped()}\n",
                    },
                    {"do": "commit", "message": "docs(rehearsal): leaky (td7)"},
                    {
                        "do": "push",
                        "on_refused": [
                            {
                                "do": "write",
                                "path": "docs/leaky.md",
                                "text": "token removed\n",
                            },
                            {"do": "amend"},
                        ],
                    },
                    {"do": "verify"},
                ]
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 0, proc.stderr
    pushes = [s for s in hub.record()["steps"] if s["do"] == "push"]
    assert [p["refused"] for p in pushes] == [True, False]
    assert (
        hub.ws.HOOK_REFUSED in pushes[0]["output"]
        and hub.ws.HOOK_SECRET in pushes[0]["output"]
    )
    assert fixtures.credential_shaped() not in pushes[0]["output"]
    ref = hub.ref()
    assert ref and ref == _git(["rev-parse", "HEAD"], tree).stdout.strip()
    held = _git(["show", f"{ref}:docs/leaky.md"], hub.hub).stdout
    assert fixtures.credential_shaped() not in held


def test_refused_push_left_unfixed_reaches_nothing(hub: Hub) -> None:
    tree = hub.tree()
    hub.script(
        7,
        [
            {
                "steps": [
                    {
                        "do": "write",
                        "path": "docs/leaky.md",
                        "text": f"token {fixtures.credential_shaped()}\n",
                    },
                    {"do": "commit"},
                    {"do": "push", "allow_refused": True},
                    {
                        "do": "exit",
                        "code": 0,
                        "say": "I could not push: the hub refused it.",
                    },
                ]
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 0, proc.stderr
    assert hub.ref() is None
    pushes = [s for s in hub.record()["steps"] if s["do"] == "push"]
    assert len(pushes) == 1 and pushes[0]["refused"] is True
    assert tree.is_dir()


def test_exit_without_pushing_leaves_the_hub_alone(hub: Hub) -> None:
    tree = hub.tree()
    hub.script(
        7,
        [
            {
                "steps": [
                    {"do": "write", "path": "x.md", "text": "x\n"},
                    {"do": "exit", "code": 0},
                ]
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 0
    assert hub.ref() is None
    assert (tree / "x.md").is_file()


def test_a_failing_step_ends_non_zero_with_an_error_result(hub: Hub) -> None:
    from precis.workers.job_types._slice_adapters import parse_claude_result

    tree = hub.tree()
    hub.script(7, [{"steps": [{"do": "check", "cmd": "false"}]}])
    argv, brief = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, brief=brief)
    assert proc.returncode == 70
    text, errored = parse_claude_result(proc.stdout)
    assert errored and text and "check" in text
    assert hub.record()["state"] == "exited"


def test_the_kth_session_of_a_todo_runs_the_kth_attempt(hub: Hub) -> None:
    hub.script(
        7,
        [
            {"steps": [{"do": "exit", "code": 1, "say": "gave up"}]},
            {
                "steps": [
                    {"do": "write", "path": "docs/second.md", "text": "second\n"},
                    {"do": "commit"},
                    {"do": "push"},
                    {"do": "verify"},
                ]
            },
        ],
    )
    t1 = hub.tree(7, 3)
    argv, brief = adapter_argv("claude_p", t1)
    first = hub.run("claude", t1, argv, job=3, brief=brief)
    assert first.returncode == 1 and hub.ref(7, 3) is None
    t2 = hub.tree(7, 4)
    second = hub.run("claude", t2, argv, job=4, brief=brief)
    assert second.returncode == 0, second.stderr
    assert hub.ref(7, 4) and hub.record(7, 2)["job"] == "4"
    assert hub.record(7, 1)["code"] == 1


def test_hang_in_commit_holds_index_lock_until_the_group_is_killed(
    hub: Hub, tmp_path: Path
) -> None:
    tree = hub.tree()
    marker = tmp_path / "hang.marker"
    hub.script(
        7,
        [
            {
                "steps": [
                    {"do": "write", "path": "docs/h.md", "text": "h\n"},
                    {"do": "hang_in_commit", "marker": str(marker)},
                ]
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            "claude",
            "--",
            *argv,
        ],
        cwd=str(tree),
        env=hub.env(7, 3),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(brief)
        proc.stdin.close()
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), "the commit hook never ran"
        assert marker.read_text(encoding="utf-8").startswith("locked")
        assert (tree / ".git" / "index.lock").exists()
        assert proc.poll() is None
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=20)
        assert proc.returncode == -signal.SIGKILL
        assert (tree / ".git" / "index.lock").exists(), (
            "a killed commit leaves its lock"
        )
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=20)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    assert hub.record()["state"] == "running"


def test_wait_for_gates_a_session_on_a_file(hub: Hub, tmp_path: Path) -> None:
    tree = hub.tree()
    gate = tmp_path / "go"
    hub.script(
        7,
        [
            {
                "steps": [
                    {"do": "wait_for", "path": str(gate), "timeout": 60},
                    {"do": "exit", "code": 0},
                ]
            }
        ],
    )
    argv, brief = adapter_argv("claude_p", tree)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            "claude",
            "--",
            *argv,
        ],
        cwd=str(tree),
        env=hub.env(7, 3),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(brief)
        proc.stdin.close()
        time.sleep(1.0)
        assert proc.poll() is None
        gate.write_text("go", encoding="utf-8")
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


PRELAND_BRIEF = (
    "Pre-land review. Read `git diff origin/main...origin/slice/7`. "
    "Name the head you judged in the verdict comment (`verified head <sha>`)."
)


def reviewer_tree(hub: Hub) -> tuple[Path, str]:
    tree = hub.tree(8, 5)
    (tree / "f.md").write_text("reviewed\n", encoding="utf-8")
    _git(["add", "."], tree)
    _git(["commit", "-q", "-m", "docs: reviewed (td7)"], tree)
    head = _git(["rev-parse", "HEAD"], tree).stdout.strip()
    _git(["update-ref", "refs/remotes/origin/slice/7", head], tree)
    return tree, head


def test_reviewer_without_a_script_says_land_naming_the_judged_head(hub: Hub) -> None:
    from precis.workers.job_types import plan_dispatch

    tree, head = reviewer_tree(hub)
    argv, _ = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, todo=8, job=5, brief=PRELAND_BRIEF)
    assert proc.returncode == 0, proc.stderr
    msg = hub.record(8)["message"]
    assert msg.splitlines()[0] == "review: LAND (read-only, tests not run)"
    assert plan_dispatch._verdict_heads(msg) == [head]
    assert hub.ref(8, 5) is None


def test_reviewer_fix_verdict_carries_findings_anchors_and_head(hub: Hub) -> None:
    from precis.workers.job_types import plan_dispatch

    tree, head = reviewer_tree(hub)
    hub.script(
        8,
        [
            {
                "steps": [
                    {
                        "do": "verdict",
                        "verdict": "FIX",
                        "text": "f.md misses the heading",
                        "anchors": "f.md::top",
                    }
                ]
            }
        ],
    )
    argv, _ = adapter_argv("claude_p", tree)
    proc = hub.run("claude", tree, argv, todo=8, job=5, brief=PRELAND_BRIEF)
    assert proc.returncode == 0, proc.stderr
    lines = hub.record(8)["message"].splitlines()
    assert (
        lines[0] == "review: FIX — f.md misses the heading (read-only, tests not run)"
    )
    assert "anchors: f.md::top" in lines
    assert plan_dispatch._verdict_heads("\n".join(lines)) == [head]


def test_the_record_names_the_group_the_session_leads(hub: Hub) -> None:
    """The launcher gives a session a group of its own (``start_new_session``);
    the shim stays in it and records it, so a driver's kill reaches exactly
    that group."""
    tree = hub.tree()
    argv, brief = adapter_argv("claude_p", tree)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            "claude",
            "--",
            *argv,
        ],
        cwd=str(tree),
        env=hub.env(7, 3),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    out, err = proc.communicate(brief, timeout=120)
    assert proc.returncode == 0, err
    rec = hub.record()
    assert rec["pid"] == proc.pid
    assert rec["pgid"] == proc.pid, "the shim does not lead the group it was given"
    assert rec["pgid"] != os.getpgrp(), "the session shares the test's own group"
    assert rec["cwd"] == str(tree)


def test_session_env_built_by_the_engine_carries_no_connection(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env a session really gets is ``run_slice.slice_env``'s, built here
    from a worker environment that holds a production-shaped DSN and tokens;
    the shim records its env names."""
    from precis.workers.job_types import run_slice

    monkeypatch.setenv(
        "PRECIS_DATABASE_URL", "postgresql://svc@db.prod.example.com/precis"
    )
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("PGPASSWORD", "x")
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    env = run_slice.slice_env(
        7, "high", mcp=False, job_id=3, gate_slot_root=tmp_path / "gate"
    )
    tree = hub.tree()
    argv, brief = adapter_argv("claude_p", tree)
    proc = subprocess.run(
        [
            sys.executable,
            str(SHIM),
            "--world",
            str(hub.world),
            "--lane",
            "claude",
            "--",
            *argv,
        ],
        cwd=str(tree),
        env=env,
        input=brief,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    rec = hub.record()
    names = set(rec["env_names"])
    assert {"PRECIS_SLICE_ID", "PRECIS_SLICE_JOB", "PRECIS_GATE_SLOT_ROOT"} <= names
    for leaked in (
        "PRECIS_DATABASE_URL",
        "PRECIS_FACTORY_GIT_TOKEN",
        "GH_TOKEN",
        "PGPASSWORD",
    ):
        assert leaked not in names, leaked
    assert (
        rec["env"]["PRECIS_SLICE_JOB"] == "3" and rec["env"]["PRECIS_SLICE_ID"] == "td7"
    )


def _sleeper(tag: str) -> subprocess.Popen[bytes]:
    """A process that leads its own group and whose command line names ``tag``."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", tag],
        start_new_session=True,
    )


def test_kill_record_signals_only_a_group_that_belongs_to_the_run(
    tmp_path: Path,
) -> None:
    from rehearsal.driver import kill_record

    run_dir = tmp_path / "run-1"
    victim = _sleeper(f"{run_dir}/host/x")
    bystander = _sleeper(str(tmp_path / "somebody-else"))
    try:
        assert kill_record({"pgid": victim.pid}, run_dir) == victim.pid
        victim.wait(timeout=20)
        assert victim.returncode == -signal.SIGKILL
        # A stale record that now names a stranger's group is never signalled.
        with pytest.raises(AssertionError, match="does not belong"):
            kill_record({"pgid": bystander.pid, "_path": "x"}, run_dir)
        assert bystander.poll() is None
        with pytest.raises(AssertionError, match="no process group"):
            kill_record({"pgid": 0, "_path": "x"}, run_dir)
        with pytest.raises(AssertionError, match="no process group"):
            kill_record({"pgid": 1}, run_dir)
    finally:
        for p in (victim, bystander):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=20)


def test_procguard_finds_and_ends_only_the_runs_groups(tmp_path: Path) -> None:
    from rehearsal import procguard

    run_dir = tmp_path / "run-2"
    ours = [_sleeper(f"{run_dir}/a"), _sleeper(f"{run_dir}/host/ship")]
    stranger = _sleeper(str(tmp_path / "run-20"))  # a longer name is not this run
    try:
        time.sleep(0.5)
        # run-2 is a prefix of run-20: the stranger's command holds "run-20/..."
        # which contains "run-2": the needle must not be fooled by it.
        groups = procguard.run_groups(run_dir)
        assert {p.pid for p in ours} <= groups
        assert procguard.group_belongs(ours[0].pid, run_dir)
        procguard.end_run_processes(run_dir, grace=0.5)
        for p in ours:
            p.wait(timeout=20)
        assert all(p.returncode is not None for p in ours)
        assert procguard.run_groups(run_dir) == set()
        assert not procguard.signal_group(os.getpgrp(), signal.SIGTERM, run_dir)
    finally:
        for p in (*ours, stranger):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=20)


def test_end_run_processes_spares_the_excluded_group(tmp_path: Path) -> None:
    from rehearsal import procguard

    run_dir = tmp_path / "run-3"
    spared = _sleeper(f"{run_dir}/worker")  # stands in for the worker's group
    ended = _sleeper(f"{run_dir}/host/ship")
    try:
        time.sleep(0.5)
        assert {spared.pid, ended.pid} <= procguard.run_groups(run_dir)
        assert procguard.run_groups(run_dir, exclude=(spared.pid,)) >= {ended.pid}
        assert spared.pid not in procguard.run_groups(run_dir, exclude=(spared.pid,))
        signalled = procguard.end_run_processes(
            run_dir, grace=0.5, exclude=(spared.pid,)
        )
        assert signalled == [ended.pid]
        ended.wait(timeout=20)
        assert spared.poll() is None, "the excluded group was signalled"
        assert procguard.run_groups(run_dir) == {spared.pid}
    finally:
        for p in (spared, ended):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=20)


def test_credential_free_scripts_only() -> None:
    """The scenario scripts the shim runs are JSON the driver writes at run
    time; nothing credential-shaped is checked in anywhere under scripts/rehearsal."""
    from precis.utils import secret_scan

    hits: list[str] = []
    for path in (SCRIPTS / "rehearsal").glob("*"):
        if path.is_file() and path.suffix in {".py", ".yaml"}:
            if secret_scan.find_secrets(path.read_text(encoding="utf-8")):
                hits.append(path.name)
    assert not hits, hits


def check_callable(
    fn: Callable[..., Any],
) -> bool:  # keeps mypy's Callable import honest
    return callable(fn)


# ── phase 2: worker control, object scan, the invariant checker ────


def test_worker_kill_and_restart_do_not_need_a_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = rworld.World(run_dir=tmp_path / "run", repo_root=tmp_path)
    w.run_dir.mkdir()
    worker = rworker.Worker(w, {"PRECIS_DATABASE_URL": GOOD_DSN})
    worker.proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    assert worker.alive()
    with pytest.raises(RuntimeError, match="already running"):
        worker.start()
    assert "killed" in worker.kill()
    assert not worker.alive() and worker.proc.returncode == -signal.SIGKILL
    assert worker.kill() == "worker was not running"
    started: list[list[str]] = []

    def fake_popen(argv: Any, **kw: Any) -> subprocess.Popen[bytes]:
        started.append(list(argv))
        return subprocess.Popen(["sleep", "300"], start_new_session=True)

    monkeypatch.setattr(rworker.launch, "popen", fake_popen)
    pid = worker.restart()
    try:
        assert started and started[0][1:4] == ["-m", "precis", "worker"]
        assert (w.run_dir / "worker.pid").read_text(encoding="utf-8") == str(pid)
    finally:
        worker.kill()


def _bare_with_main(tmp_path: Path) -> tuple[Path, str]:
    remote = _scratch_remote(tmp_path)
    sha = _git(
        ["--git-dir", str(remote), "rev-parse", "refs/heads/main"], tmp_path
    ).stdout.strip()
    return remote, sha


def test_objscan_finds_a_string_in_any_object_not_only_reachable_ones(
    tmp_path: Path,
) -> None:
    from rehearsal import objscan

    remote, _ = _bare_with_main(tmp_path)
    seed = frozenset(objscan.object_names(remote))
    needle = fixtures.credential_shaped()
    assert objscan.objects_containing(remote, [needle], skip=seed) == []
    blob = subprocess.run(
        ["git", "--git-dir", str(remote), "hash-object", "-w", "--stdin"],
        input=f"unreferenced {needle} blob\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert objscan.objects_containing(remote, [needle], skip=seed) == [blob]
    assert objscan.objects_containing(remote, [needle], skip=seed | {blob}) == []
    assert objscan.objects_containing(remote, [], skip=seed) == []
    assert objscan.parse_batch(b"", [b"x"]) == []


def _world(tmp_path: Path) -> rworld.World:
    """A small real world: remote with main and prod, a host clone."""
    from rehearsal import objscan

    remote, sha = _bare_with_main(tmp_path)
    _git(["--git-dir", str(remote), "update-ref", "refs/heads/prod", sha], tmp_path)
    w = rworld.World(run_dir=tmp_path, repo_root=tmp_path)
    _git(["clone", "-q", "--branch", "main", str(remote), str(w.host)], tmp_path)
    _git(["config", "user.name", "t"], w.host)
    _git(["config", "user.email", "t@example.invalid"], w.host)
    w.seed_sha = sha
    w.host_head0 = _git(["rev-parse", "HEAD"], w.host).stdout.strip()
    w.seed_objects = frozenset(objscan.object_names(remote))
    w.folders.mkdir(parents=True)
    return w


def test_invariants_hold_on_a_quiet_world_and_name_each_breach(tmp_path: Path) -> None:
    from rehearsal import invariants as inv

    w = _world(tmp_path)
    expect = inv.Expect()
    assert inv.check_folders(w, expect) == ([], [])
    assert inv.check_hub(w, expect) == []
    assert inv.check_host(w) == []
    assert inv.check_remote(w, expect) == []
    assert inv.check_secrets(w, expect) == []
    # 1: a folder nobody declared, a declared one that is gone.
    (w.folders / "slice-td9-j3").mkdir()
    v, notes = inv.check_folders(w, expect)
    assert v and "slice-td9-j3" in v[0] and notes == []
    v, _ = inv.check_folders(w, inv.Expect(kept={"slice-td9-j3", "slice-td9-j4"}))
    assert len(v) == 1 and "slice-td9-j4" in v[0]
    # 3: dirty tree, extra worktree, slice branch, index.lock.
    (w.host / "stray.txt").write_text("x", encoding="utf-8")
    _git(["worktree", "add", "-q", "-b", "slice/9", str(tmp_path / "wt")], w.host)
    (w.host / ".git" / "index.lock").write_text("", encoding="utf-8")
    breaches = " | ".join(inv.check_host(w))
    for word in ("dirty", "worktree list", "slice branch", "index.lock"):
        assert word in breaches, breaches
    # 4: a stray ref and a commit that is neither a land nor declared.
    _git(
        [
            "--git-dir",
            str(w.remote),
            "update-ref",
            "refs/factory/build/1/2",
            w.seed_sha,
        ],
        tmp_path,
    )
    work = tmp_path / "pusher"
    _git(["clone", "-q", str(w.remote), str(work)], tmp_path)
    _git(["config", "user.name", "t"], work)
    _git(["config", "user.email", "t@example.invalid"], work)
    (work / "x.md").write_text("x", encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-q", "-m", "an unexplained change"], work)
    _git(["push", "-q", "origin", "HEAD:refs/heads/main"], work)
    breaches = " | ".join(inv.check_remote(w, inv.Expect()))
    assert "refs/factory/build/1/2" in breaches and "unexplained" in breaches
    w.competitors.append(_git(["rev-parse", "HEAD"], work).stdout.strip())
    assert "unexplained" not in " | ".join(inv.check_remote(w, inv.Expect()))
    # 5: the fixture string in an unreferenced object of the remote.
    needle = fixtures.credential_shaped()
    subprocess.run(
        ["git", "--git-dir", str(w.remote), "hash-object", "-w", "--stdin"],
        input=needle,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    hit = inv.check_secrets(w, inv.Expect(secrets=[needle]))
    assert hit and "5:" in hit[0] and "remote" in hit[0]


def test_written_paths_drops_what_a_script_deleted() -> None:
    from types import SimpleNamespace

    from rehearsal import invariants as inv

    recs = [
        {"steps": [{"do": "write", "path": "a.md"}, {"do": "write", "path": "b.md"}]},
        {
            "steps": [
                {"do": "delete", "path": "b.md"},
                {"do": "commit"},
                {"do": "write", "path": "c.md"},
            ]
        },
    ]
    driver = SimpleNamespace(sessions=lambda todo: recs)
    assert inv.written_paths(cast(Any, driver), 7) == {"a.md", "c.md"}


def test_ctx_declares_plans_and_run_scenario_reports_failures(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from rehearsal.ctx import Ctx
    from rehearsal.driver import Plan

    driver = SimpleNamespace(
        events=["19:00:00 something happened"], note=lambda t: None
    )
    ctx = Ctx(
        world=rworld.World(run_dir=tmp_path, repo_root=tmp_path),
        driver=cast(Any, driver),
        health=lambda: None,
    )
    plan = Plan(root=1, build=2, review=3)
    assert ctx.register(plan) is plan and ctx.expect.tasks == {2}
    ctx.landed(plan)
    assert ctx.expect.landed == {2}

    def boom(c: Ctx) -> str:
        raise AssertionError("expected X\nobserved Y\nmore evidence")

    def fine(c: Ctx) -> str:
        c.note("a fact")
        return "all good"

    status, detail, extra = runner.run_scenario(
        ctx, rscenarios.Scenario("boom", "s", boom, invariants=False)
    )
    assert status == "FAIL" and detail == "expected X"
    assert "observed Y" in extra and any("something happened" in e for e in extra)
    status, detail, extra = runner.run_scenario(
        ctx, rscenarios.Scenario("fine", "s", fine, invariants=False)
    )
    assert status == "PASS" and detail.startswith("all good")
    assert "fact: a fact" in extra


def test_settle_folders_waits_for_the_worker_pass_not_a_launch(tmp_path: Path) -> None:
    import shutil
    import threading
    import time
    from types import SimpleNamespace

    from rehearsal.ctx import Ctx
    from rehearsal.driver import Driver, Timeout

    w = rworld.World(run_dir=tmp_path, repo_root=tmp_path)
    w.folders.mkdir(parents=True)
    gone, kept = w.folders / "slice-td5-j8", w.folders / "slice-td5-j9"
    gone.mkdir()
    kept.mkdir()
    ticks: list[int] = []

    def wait_until(pred: Any, *, timeout: float, **_: Any) -> Any:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise Timeout("timed out")
            time.sleep(0.05)
        return True

    driver = SimpleNamespace(
        wait_until=wait_until,
        tick=lambda: ticks.append(1),
        folders=lambda todo: Driver.folders(cast(Any, SimpleNamespace(world=w)), todo),
    )
    ctx = Ctx(world=w, driver=cast(Any, driver), health=lambda: None)
    # The worker's tidy pass drops the folder a moment later; nothing ticks.
    threading.Timer(0.3, shutil.rmtree, args=(gone,)).start()
    assert ctx.settle_folders(5, keep=("slice-td5-j9",), timeout=10) == []
    assert not gone.exists() and kept.exists() and ticks == []
    # A folder that is never settled comes back as what is left.
    assert ctx.settle_folders(5, timeout=0.3) == ["slice-td5-j9"]
    ctx.expect.kept.add("slice-td5-j9")
    assert ctx.settle_folders(timeout=0.3) == []


def _job(job_id: int, status: str, job_type: str = "land_slice", **meta: Any) -> Any:
    return {"id": job_id, "type": job_type, "status": status, "meta": meta}


def test_gate_skips_an_attempt_free_deferral_and_trips_on_a_red_land() -> None:
    from types import SimpleNamespace

    from rehearsal import gate as rgate
    from rehearsal.ctx import Ctx
    from rehearsal.driver import Plan

    jobs = {
        2: [
            # The ship launch refused for capacity, and a ship already busy:
            # the engine re-mints the land, so neither is a failure.
            _job(10, "failed", attempt_free="infra-land", red_class="admission"),
            _job(11, "failed", attempt_free="infra-land", red_class="ship-busy"),
            _job(12, "failed", attempt_free="admission"),
            # The same shape of infra red outside the deferral classes stays one.
            _job(13, "failed", attempt_free="infra-land", red_class="gate-killed"),
            # A red land: no attempt_free stamp. red_class alone is no deferral.
            _job(14, "failed", red_class="admission"),
            _job(15, "failed"),
            _job(16, "succeeded", attempt_free="infra-land", red_class="admission"),
        ],
        3: [],
    }
    driver = SimpleNamespace(
        jobs=lambda todo: jobs[todo],
        describe=lambda *t: "described",
    )
    ctx = Ctx(
        world=cast(Any, SimpleNamespace()),
        driver=cast(Any, driver),
        health=lambda: None,
    )
    assert [j["id"] for j in rgate.failed_jobs(ctx, 2, 3)] == [13, 14, 15]
    assert [j["id"] for j in rgate.failed_jobs(ctx, 2, 3, allow=(13, 14))] == [15]
    plan = Plan(root=1, build=2, review=3)
    with pytest.raises(AssertionError, match=r"jo13 land_slice failed unexpectedly"):
        rgate.fail_fast(ctx, plan)
    # Only deferrals: nothing trips.
    jobs[2] = jobs[2][:3]
    assert rgate.failed_jobs(ctx, 2, 3) == []
    rgate.fail_fast(ctx, plan)
    # A red land after the deferrals still trips it.
    jobs[2].append(_job(20, "failed"))
    with pytest.raises(AssertionError, match=r"jo20 land_slice failed unexpectedly"):
        rgate.fail_fast(ctx, plan)


def test_slot_hold_invariant_names_each_leftover_hold() -> None:
    import datetime
    from types import SimpleNamespace

    from rehearsal import invariants as inv

    at = datetime.datetime(2026, 11, 22, 9, 30, tzinfo=datetime.UTC)
    state: dict[str, Any] = {"holds": [], "rows": []}
    driver = SimpleNamespace(
        slot_holds=lambda resources: state["holds"],
        slot_rows=lambda resources: state["rows"],
    )
    quiet = [
        {"host": "h", "resource": "cpu_milli", "capacity": 8000, "free": 8000},
        {"host": "h", "resource": "mem_mb", "capacity": 4096, "free": 4096},
    ]
    state["rows"] = quiet
    assert inv.check_slot_holds(cast(Any, driver), grace=0) == []
    # A SIGKILLed worker's spawn never releases: its hold and the shortfall.
    state["holds"] = [
        {
            "id": 1,
            "holder": "spawn:run_slice:td7",
            "host": "h",
            "resource": "cpu_milli",
            "units": 1000,
            "acquired_at": at,
        },
        {
            "id": 2,
            "holder": "spawn:run_slice:td7",
            "host": "h",
            "resource": "mem_mb",
            "units": 2048,
            "acquired_at": at,
        },
    ]
    state["rows"] = [
        {"host": "h", "resource": "cpu_milli", "capacity": 8000, "free": 7000},
        quiet[1] | {"free": 2048},
    ]
    v = inv.check_slot_holds(cast(Any, driver), grace=0)
    assert len(v) == 3 and all(x.startswith("9:") for x in v), v
    assert "2 resource_slot_hold(s)" in v[0], v[0]
    for needle in (
        "holder=spawn:run_slice:td7",
        "cpu_milli=1000",
        "mem_mb=2048",
        f"acquired_at={at}",
    ):
        assert needle in v[0], v[0]
    assert "cpu_milli free 7000 != capacity 8000" in v[1], v
    assert "mem_mb free 2048 != capacity 4096" in v[2], v
    # Against a baseline taken at the scenario's start, only what is new counts:
    # hold 1 and a 1000-unit cpu shortfall were already there.
    base = inv.SlotBaseline(
        holds=frozenset({1}),
        short={("h", "cpu_milli"): 1000},
    )
    v = inv.check_slot_holds(cast(Any, driver), baseline=base, grace=0)
    assert len(v) == 2, v
    assert "1 resource_slot_hold(s)" in v[0] and "mem_mb=2048" in v[0], v[0]
    assert "cpu_milli=1000" not in v[0], v[0]  # hold 1 is the baseline's
    assert "mem_mb free 2048" in v[1], v
    full = inv.SlotBaseline(holds=frozenset({1, 2}), short={("h", "cpu_milli"): 1000})
    full.short[("h", "mem_mb")] = 2048
    assert inv.check_slot_holds(cast(Any, driver), baseline=full, grace=0) == []


def test_slot_baseline_reads_only_the_launch_resources() -> None:
    from types import SimpleNamespace

    from rehearsal import invariants as inv

    asked: list[tuple[str, ...]] = []

    def holds(resources: tuple[str, ...]) -> list[dict[str, Any]]:
        asked.append(resources)
        return [{"id": 4}, {"id": 9}]

    driver = SimpleNamespace(
        slot_holds=holds,
        slot_rows=lambda resources: [
            {"host": "h", "resource": "mem_mb", "capacity": 10, "free": 4}
        ],
    )
    got = inv.slot_baseline(cast(Any, driver))
    assert got.holds == {4, 9} and got.short == {("h", "mem_mb"): 6}
    assert asked == [("cpu_milli", "mem_mb")]


def test_check_runs_rule_9_with_the_scenarios_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rehearsal import invariants as inv

    seen: list[Any] = []
    for name in (
        "check_folders",
        "check_hub",
        "check_host",
        "check_remote",
        "check_secrets",
        "check_database",
        "check_central",
        "check_landed",
    ):
        monkeypatch.setattr(
            inv,
            name,
            lambda *a, _n=name, **k: ([], []) if _n == "check_folders" else [],
        )

    def rule9(driver: Any, *, baseline: Any = None, **_: Any) -> list[str]:
        seen.append(baseline)
        return ["9: leaked"]

    monkeypatch.setattr(inv, "check_slot_holds", rule9)
    base = inv.SlotBaseline(holds=frozenset({3}))
    violations, _ = inv.check(
        cast(Any, None), cast(Any, None), inv.Expect(), baseline=base
    )
    assert violations == ["9: leaked"] and seen == [base]


def test_run_scenario_takes_the_baseline_before_the_scenario_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from rehearsal import invariants as inv
    from rehearsal.ctx import Ctx

    order: list[str] = []
    marker = inv.SlotBaseline(holds=frozenset({7}))

    def take(driver: Any) -> Any:
        order.append("baseline")
        return marker

    def check(
        world: Any, driver: Any, expect: Any, baseline: Any = None
    ) -> tuple[list[str], list[str]]:
        order.append(f"check:{baseline is marker}")
        return [], []

    def scenario(c: Any) -> str:
        order.append("run")
        return "ok"

    monkeypatch.setattr(inv, "slot_baseline", take)
    monkeypatch.setattr(inv, "check", check)
    ctx = Ctx(
        world=cast(Any, SimpleNamespace()),
        driver=cast(Any, SimpleNamespace(events=[])),
        health=lambda: None,
    )
    monkeypatch.setattr(ctx, "settle_folders", lambda *a, **k: [])
    status, _, _ = runner.run_scenario(ctx, rscenarios.Scenario("s", "s", scenario))
    assert status == "PASS"
    assert order == ["baseline", "run", "check:True"]


def test_quiesce_retires_open_plans_and_waits_for_their_jobs(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from rehearsal.ctx import Ctx
    from rehearsal.driver import Plan

    w = rworld.World(run_dir=tmp_path, repo_root=tmp_path)
    w.folders.mkdir(parents=True)
    retired: list[int] = []
    live = {"jobs": [_job(5, "running", "run_slice"), _job(6, "succeeded")]}

    def wait_until(pred: Any, *, timeout: float, **_: Any) -> Any:
        live["jobs"][0]["status"] = "failed"  # the stopped session's job ends
        assert pred()
        return True

    def retire(root: int) -> int:
        retired.append(root)
        return 2

    driver = SimpleNamespace(
        store=SimpleNamespace(retire_todo_subtree=retire),
        status=lambda todo: "done" if todo == 20 else "open",
        jobs=lambda *a: live["jobs"],
        wait_until=wait_until,
        folders=lambda todo: [],
    )
    ctx = Ctx(world=w, driver=cast(Any, driver), health=lambda: None)
    ctx.register(Plan(root=1, build=2, review=3))
    ctx.register(Plan(root=10, build=20, review=30))  # landed: left alone
    notes = runner.quiesce(ctx, timeout=5)
    assert retired == [1], retired
    assert any("retired open plan td1" in n for n in notes), notes
    assert not any("still live" in n or "failed" in n for n in notes), notes


def _quiesce_ctx(
    tmp_path: Path, worker: Any, *, jobs: list[Any] | None = None
) -> tuple[Any, list[int]]:
    from types import SimpleNamespace

    from rehearsal.ctx import Ctx
    from rehearsal.driver import Plan

    w = rworld.World(run_dir=tmp_path, repo_root=tmp_path)
    w.folders.mkdir(parents=True)
    retired: list[int] = []

    def retire(root: int) -> int:
        retired.append(root)
        return 0

    def wait_until(pred: Any, *, what: str, **_: Any) -> Any:
        if not pred():
            raise AssertionError(f"timed out waiting for {what}")
        return True

    driver = SimpleNamespace(
        store=SimpleNamespace(retire_todo_subtree=retire),
        status=lambda todo: "open",
        jobs=lambda *a: jobs or [],
        wait_until=wait_until,
        folders=lambda todo: [],
    )
    ctx = Ctx(world=w, driver=cast(Any, driver), health=lambda: None, worker=worker)
    ctx.register(Plan(root=1, build=2, review=3))
    return ctx, retired


def test_quiesce_never_signals_the_workers_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from rehearsal import procguard

    seen: list[dict[str, Any]] = []

    def end(run_dir: Path, **kw: Any) -> list[int]:
        seen.append({"run_dir": run_dir, **kw})
        return []

    monkeypatch.setattr(procguard, "end_run_processes", end)
    worker = SimpleNamespace(alive=lambda: True, proc=SimpleNamespace(pid=4321))
    ctx, retired = _quiesce_ctx(tmp_path, worker)
    runner.quiesce(ctx, timeout=1)
    assert seen == [{"run_dir": tmp_path, "exclude": (4321,)}], seen
    assert retired == [1]


def test_quiesce_still_retires_stops_and_waits_when_the_restart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from rehearsal import procguard

    stopped: list[Any] = []

    def end(run_dir: Path, **kw: Any) -> list[int]:
        stopped.append(kw)
        return []

    monkeypatch.setattr(procguard, "end_run_processes", end)

    def boom() -> float:
        raise RuntimeError("no host came back")

    worker = SimpleNamespace(alive=lambda: False, proc=SimpleNamespace(pid=77))
    ctx, retired = _quiesce_ctx(tmp_path, worker, jobs=[_job(5, "running")])
    monkeypatch.setattr(ctx, "restart_worker", boom)
    notes = runner.quiesce(ctx, timeout=1)
    assert any("worker restart failed" in n and "no host came back" in n for n in notes)
    assert retired == [1] and stopped == [{"exclude": (77,)}]
    assert any("still live: [5]" in n for n in notes), notes


def test_main_loop_quiesces_after_a_fail_only_when_another_scenario_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    order: list[str] = []
    outcome = {"a": "FAIL", "b": "PASS", "c": "FAIL"}
    ran = rscenarios.Scenario("x", "s", lambda c: "ok")
    monkeypatch.setattr(runner, "REGISTRY", {n: ran for n in outcome})

    todo = iter(outcome)

    def run_scenario(ctx: Any, scenario: Any) -> tuple[str, str, list[str]]:
        name = next(todo)
        order.append(name)
        return outcome[name], "d", []

    monkeypatch.setattr(runner, "run_scenario", run_scenario)

    def quiesce(ctx: Any) -> list[str]:
        order.append("q")
        return []

    monkeypatch.setattr(runner, "quiesce", quiesce)
    results: list[tuple[str, str, str]] = []
    failed = runner.run_named(
        cast(Any, SimpleNamespace()),
        ["a", "b", "c"],
        explicit=True,
        non_gating=[],
        results=results,
    )
    # a FAILed with b after it: quiesce. b passed. c FAILed last: no quiesce.
    assert order == ["a", "q", "b", "c"], order
    assert failed and [s for s, _, _ in results] == ["FAIL", "PASS", "FAIL"]


def test_a_tick_runs_the_wake_pass_before_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's ``--only`` passes carry no wake thread: without the
    driver's own wake pass a review parked on a full lane is never re-queued
    and the scenario waits out the park's 10-minute timer."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from rehearsal.driver import Driver

    from precis.workers import _mint, wake_runner

    order: list[str] = []

    @contextmanager
    def tx() -> Iterator[object]:
        yield object()

    store = SimpleNamespace(tx=tx)

    def wake(got: Any, **_: Any) -> dict[str, int]:
        assert got is store
        order.append("wake")
        return {"claimed": 0, "ok": 0, "failed": 0}

    def mint(got: Any, **kw: Any) -> tuple[int, bool]:
        order.append(f"mint {kw['job_type']}")
        return 41, True

    monkeypatch.setattr(wake_runner, "run_wake_pass", wake)
    monkeypatch.setattr(_mint, "mint_job_once", mint)
    driver = SimpleNamespace(
        _tick_n=0,
        store=store,
        status=lambda job: "succeeded",
        meta=lambda job: {"plan_dispatch": {"dispatched": 1}},
    )
    got = Driver.tick(cast(Any, driver))
    assert order == ["wake", "mint plan_dispatch"]
    assert got == {"status": "succeeded", "counts": {"dispatched": 1}, "job": 41}


def test_the_worker_env_has_the_second_fences(tmp_path: Path) -> None:
    w = rworld.World(run_dir=tmp_path / "run", repo_root=tmp_path)
    env = rworker.build_env(w, GOOD_DSN)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["PRECIS_LAND_NOTIFY_CLI"] == env["PRECIS_GRIPE_NOTE_CLI"]
    assert env["PRECIS_LAND_NOTIFY_CLI"] == str(w.bin / "notify-stub")


def test_notify_stub_records_calls_and_posts_nothing(tmp_path: Path) -> None:
    stub = SCRIPTS / "rehearsal" / "notify_stub.py"
    log = tmp_path / "logs" / "notify-calls.jsonl"
    for args in (["put", "kind=todo"], ["get"]):
        out = subprocess.run(
            [sys.executable, str(stub), str(log), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert out.returncode == 0 and out.stdout == ""
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    assert [c["argv"] for c in calls] == [["put", "kind=todo"], ["get"]]


def test_scratch_gitconfig_has_no_helper_and_a_catch_all_for_github(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(rworld.GITCONFIG_TEXT, encoding="utf-8")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    helper = subprocess.run(
        ["git", "config", "--get-all", "credential.helper"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
        cwd=str(tmp_path),
    )
    assert helper.stdout.strip() == "", "a credential helper is configured"
    # Real git (no wrapper) reaches for github.com and lands on the dead path.
    out = subprocess.run(
        ["git", "ls-remote", "https://github.com/someone/else.git"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
        cwd=str(tmp_path),
    )
    assert out.returncode != 0 and "nonexistent-rehearsal" in out.stderr, out.stderr
    # The wrapper's stand-in url is a longer prefix: its rewrite wins.
    remote = _scratch_remote(tmp_path)
    wrapper = tmp_path / "git"
    rworld._write_exec(wrapper, rworld._git_wrapper(rworld._real("git"), remote))
    ok = subprocess.run(
        [str(wrapper), "ls-remote", rworld.FAKE_ORIGIN, "refs/heads/main"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
        cwd=str(tmp_path),
    )
    assert ok.returncode == 0 and "refs/heads/main" in ok.stdout, ok.stderr


def test_seed_defaults_to_head_and_a_dirty_src_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    assert runner.parse_args([]).seed_ref == "HEAD"
    ref, sha, dirty = runner.resolve_seed_ref("HEAD", allow_dirty=False)
    assert (ref, dirty) == ("HEAD", False) and len(sha) == 40
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(rworld.WorldError, match="src/ has uncommitted"):
        runner.resolve_seed_ref("HEAD", allow_dirty=False)
    assert runner.resolve_seed_ref("HEAD", allow_dirty=True)[2] is True
    with pytest.raises(rworld.WorldError, match="does not resolve"):
        runner.resolve_seed_ref("nonesuch", allow_dirty=True)


def test_inproc_tick_is_gone_and_lanes_follow_the_builder_choice() -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(["--tick", "inproc"])
    assert runner.lanes_for("devin") == {"devin": 3, "codex": 1}
    assert runner.lanes_for("kimi") == {"kimi": 3, "codex": 1}
    assert runner.parse_args(["--builder-lane", "kimi"]).builder_lane == "kimi"
    with pytest.raises(SystemExit):
        runner.parse_args(["--builder-lane", "metered"])


def test_sigterm_and_sighup_are_handled_as_an_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        runner._terminate(signal.SIGTERM, None)
    import inspect

    src = inspect.getsource(runner.main)
    assert "signal.SIGTERM" in src and "signal.SIGHUP" in src


def test_gate_deps_are_pinned_to_the_lock() -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    for name in rworld.GATE_DEPS:
        version = rworld.locked_version(lock, name)
        assert version and version[0].isdigit(), name
    assert rworld.locked_version(lock, "no-such-package") is None
    sample = '[[package]]\nname = "fastapi"\nversion = "9.9.9"\n'
    assert rworld.locked_version(sample, "fastapi") == "9.9.9"


def test_default_run_leaves_out_smoke_and_covers_the_gate() -> None:
    defaults = [n for n, s in rscenarios.REGISTRY.items() if s.default]
    assert "smoke" not in defaults
    assert set(rscenarios.GATE) <= set(defaults)
    assert all(rscenarios.REGISTRY[n].run is not None for n in rscenarios.GATE)
