"""repo_land, the second-repo lander's submit half (td659522 step 2).

Real temp git repos (gr741695): an "origin" bare repo standing in for the
second repository's GitHub, a clone of it that is the host checkout, the
host's real hub (``_slice_ws.ensure_hub``) and a separate builder clone whose
commits reach the host only through an attempt ref in the hub, as a build's
do. The credentialed fetch is replaced by a plain local fetch and the
detached launch by a recorder. The fake store and ctx are
``test_land_slice``'s.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.plans.templates import BUILD_FLOOR_KEY, BUILD_REF_KEY
from precis.workers import factory_git, factory_git_mirror, repo_land, repo_lease
from precis.workers.job_types import _slice_ws as ws
from precis.workers.job_types import land_slice, run_slice
from tests.test_land_slice import _Ctx, _Store

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the gate script runs under sh"
)

PYPROJECT = '[project]\nname = "demo"\nversion = "1.0.0"\ndependencies = []\n'
IDENT = {
    "GIT_AUTHOR_NAME": "Setup",
    "GIT_AUTHOR_EMAIL": "setup@example.invalid",
    "GIT_COMMITTER_NAME": "Setup",
    "GIT_COMMITTER_EMAIL": "setup@example.invalid",
}


def _g(cwd: Path, *args: str) -> str:
    """Plain git for test setup, with its own fixed identity."""
    import os

    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **IDENT},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


class FakeLease:
    """``repo_lease`` at the ``repo_land`` seam (its SQL is covered by
    ``test_repo_lease``): free by default, and held by the job once taken."""

    def __init__(self) -> None:
        self.blocker: dict[str, Any] | None = None
        self.holder_job: int | None = 901  # the fake ctx's ref_id
        self.acquires: list[tuple[str, int, str]] = []
        self.releases: list[tuple[str, int]] = []

    def acquire(
        self, store: Any, repo: str, *, job_id: int, kind: str
    ) -> dict[str, Any] | None:
        self.acquires.append((repo, job_id, kind))
        return self.blocker

    def holder(self, store: Any, repo: str) -> dict[str, Any] | None:
        if self.holder_job is None:
            return None
        return {"job_id": self.holder_job, "kind": "land", "at": "x"}

    def release(self, store: Any, repo: str, *, job_id: int) -> None:
        self.releases.append((repo, job_id))


TODO = 42
JOB = 901  # the fake ctx's ref_id: the land job
BUILD_JOB = 7  # the build that pushed the attempt


@dataclass
class Rig:
    origin: Path
    clone: Path
    builder: Path
    hub: Path
    launches: list[tuple[list[str], dict[str, Any]]]
    fetches: list[str]
    lease: FakeLease = field(default_factory=FakeLease)
    #: The attempt ``commit_on_branch`` last pushed: its hub ref and the
    #: builder's commit (the land's ``ref`` and ``head``).
    ref: str = ""
    head: str = ""

    def land_tree(self, job: int = JOB) -> Path:
        return ws.attempt_dir(self.clone, TODO, job, ws.ROLE_LAND)

    def push_attempt(self, sha: str, *, job: int = BUILD_JOB) -> str:
        """What a build leaves in the hub: the attempt's ref at ``sha``. A
        fetch into the bare hub from the builder's clone (no hook, as the
        worker's own writes)."""
        ref = ws.build_ref(TODO, job)
        _g(self.hub, "fetch", "-q", "--no-tags", str(self.builder), f"+{sha}:{ref}")
        assert ws.hub_tip(self.hub, ref) == sha
        return ref

    def commit_on_branch(
        self, files: dict[str, str], msg: str = "feat(x): widget"
    ) -> None:
        """The builder commits on its ``slice/42`` and pushes the attempt
        ref to the hub; the host checkout never sees the commit."""
        _g(self.builder, "checkout", "-q", "slice/42")
        for name, text in files.items():
            path = self.builder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _g(self.builder, "add", "-A")
        _g(self.builder, "commit", "-q", "-m", msg)
        self.head = _g(self.builder, "rev-parse", "HEAD")
        self.ref = self.push_attempt(self.head)

    def snapshot_on_top(self) -> str:
        """A ``wip:`` snapshot commit on top of the builder's head, pushed
        as the attempt's tip (what ``finalize`` makes for leftovers)."""
        (self.builder / "leftover.txt").write_text("unfinished\n", encoding="utf-8")
        _g(self.builder, "add", "leftover.txt")
        _g(
            self.builder,
            "commit",
            "-q",
            "-m",
            f"wip: snapshot session exited (td{TODO})",
        )
        snap = _g(self.builder, "rev-parse", "HEAD")
        self.push_attempt(snap)
        return snap

    def refs(self) -> dict[int, str]:
        return ws.hub_builds(self.hub, TODO)

    def advance_origin(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _g(self.clone, "add", "-A")
        _g(self.clone, "commit", "-q", "-m", "chore: main moves")
        _g(self.clone, "push", "-q", "origin", "main")
        _g(self.clone, "reset", "-q", "--hard", "HEAD~1")


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _g(tmp_path, "clone", "-q", str(origin), str(clone))
    _g(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    (clone / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (clone / "src").mkdir()
    (clone / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _g(clone, "add", "-A")
    _g(clone, "commit", "-q", "-m", "chore: seed")
    _g(clone, "push", "-q", "origin", "main")
    _g(clone, "fetch", "-q", "origin")
    # The builder's own clone: slice/42 at the seed. Its commits reach the
    # host only through the hub's attempt ref.
    builder = tmp_path / "builder"
    _g(tmp_path, "clone", "-q", str(origin), str(builder))
    _g(builder, "checkout", "-q", "-b", "slice/42")
    hub = ws.ensure_hub(clone)
    rig = Rig(origin, clone, builder, hub, [], [])

    def fake_fetch(
        dest: Path,
        url: str,
        ref: str,
        dest_ref: str,
        *,
        ops_parent: Path,
        env: Any,
        object_dirs: tuple[Path, ...] = (),
    ) -> str | None:
        rig.fetches.append(url)
        _g(dest, "fetch", "-q", str(origin), f"+refs/heads/{ref}:{dest_ref}")
        return _g(dest, "rev-parse", dest_ref)

    def fake_launch(argv: list[str], **kw: Any) -> int:
        rig.launches.append((argv, kw))
        return 4242

    monkeypatch.setattr(repo_lease, "acquire", rig.lease.acquire)
    monkeypatch.setattr(repo_lease, "holder", rig.lease.holder)
    monkeypatch.setattr(repo_lease, "release", rig.lease.release)
    monkeypatch.setattr(factory_git_mirror, "fetch_ref_into", fake_fetch)
    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    monkeypatch.setattr(run_slice, "slice_root_for", lambda repo: clone)
    monkeypatch.setattr(run_slice, "slice_root", lambda: clone)
    # The repo is admitted for the rig (land_slice's own tests cover the
    # admission rules); the closed cases patch this again.
    monkeypatch.setattr(land_slice, "_repo_refusal", lambda store, params: None)
    monkeypatch.setattr(factory_git, "factory_git_token", lambda: None)
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    for name in IDENT:
        monkeypatch.delenv(name, raising=False)
    return rig


def _params(rig: Rig, **extra: Any) -> dict[str, Any]:
    """The land's params as a build mints them: the hub ref and the commit,
    and no ``worktree``."""
    params: dict[str, Any] = {
        "todo_id": 42,
        "repo": "catpath",
        "branch": "slice/42",
        "lane": "claude-sub",
    }
    if rig.ref:
        params["ref"] = rig.ref
    if rig.head:
        params["head"] = rig.head
    return {**params, **extra}


def _submit(rig: Rig, **extra: Any) -> tuple[_Ctx, Any, _Store]:
    store = _Store()
    meta: dict[str, Any] = {}
    if rig.ref:  # what the build recorded when it pushed, and a land's conflict
        meta = {
            BUILD_REF_KEY: {"ref": rig.ref, "sha": rig.head, "host": "here"},
            land_slice.CONFLICT_KEY: {"head": rig.head, "ref": rig.ref},
        }
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "repo:catpath"), meta=meta)
    ctx = _Ctx(store, _params(rig, **extra))
    return ctx, repo_land.submit(ctx, None), store


def test_happy_path_squashes_one_commit_on_the_tip_and_launches_the_gate(
    rig: Rig,
) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n", "src/b.py": "y = 1\n"})
    rig.advance_origin({"README.md": "hi\n"})
    # the host checkout has neither the builder's commit nor a slice branch
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{rig.head}^{{commit}}"], cwd=rig.clone
    )
    assert probe.returncode != 0
    assert _g(rig.clone, "branch", "--list", "slice/42") == ""
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and handle is not None
    assert handle["repo"] == "catpath" and handle["pid"] == 4242
    tip = _g(rig.clone, "rev-parse", "refs/remotes/origin/main")
    assert handle["tip"] == tip
    sha = handle["sha"]
    assert _g(rig.clone, "rev-parse", f"{sha}^") == tip
    assert _g(rig.clone, "log", "-1", "--format=%s", sha) == "feat(x): widget (td42)"
    # the commit's change is the builder's change, nothing from main's move
    changed = _g(rig.clone, "diff", "--name-only", tip, sha).splitlines()
    assert changed == ["src/a.py", "src/b.py"]
    assert _g(rig.clone, "rev-parse", f"{sha}:src/a.py") == _g(
        rig.builder, "rev-parse", f"{rig.head}:src/a.py"
    )
    # identity: no env set, so the head's author (the setup identity)
    assert _g(rig.clone, "log", "-1", "--format=%an", sha) == "Setup"
    # the handle carries what the poll half needs: the job's own land tree,
    # a detached worktree of the host checkout, and the commit it took
    land_tree = Path(handle["land_tree"])
    assert land_tree == rig.land_tree() and land_tree.is_dir()
    assert land_tree.name == f"slice-td42-land-j{JOB}"
    assert handle["worktree"] == handle["land_tree"]
    assert "slice_tree" not in handle
    assert (handle["ref"], handle["head"]) == (rig.ref, rig.head)
    assert _g(land_tree, "rev-parse", "HEAD") == sha
    for key in ("branch", "log", "exit_file", "started_at", "deadline", "subject"):
        assert handle[key]
    # nothing is landed yet: the hub still holds the attempt
    assert rig.refs() == {BUILD_JOB: rig.head}
    # one detached launch: cwd the land tree, no credential, UV_PYTHON set
    assert len(rig.launches) == 1
    argv, kw = rig.launches[0]
    assert kw["cwd"] == land_tree
    assert Path(argv[1]).read_text(encoding="utf-8").startswith("#!/bin/sh")
    env = kw["env"]
    assert "SSH_AUTH_SOCK" not in env and "GH_CONFIG_DIR" not in env
    assert not [k for k in env if land_slice._LAND_ENV_SECRET_RE.search(k)]
    assert env["UV_PYTHON"] == "3.12" and env["UV_CACHE_DIR"]
    assert rig.fetches == ["https://github.com/retospect/catpath"]


def test_gate_env_drops_host_reach_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///x.sock")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/gh")
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "t")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", "/tmp/ops")
    profile = lanes.REPO_PROFILES["catpath"]
    env = repo_land.gate_env(profile)
    assert not [k for k in env if k.startswith("PRECIS_")]
    for name in ("SSH_AUTH_SOCK", "DOCKER_HOST", "GH_CONFIG_DIR"):
        assert name not in env
    assert not [k for k in env if land_slice._LAND_ENV_SECRET_RE.search(k)]
    assert env["UV_PYTHON"] == "3.12"
    monkeypatch.delenv("UV_PYTHON", raising=False)
    assert "UV_PYTHON" not in repo_land.gate_env(replace(profile, uv_python=None))


def test_merge_conflict_is_a_charged_failure_with_conflict_lines(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    rig.advance_origin({"src/a.py": "x = 3\n"})
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    lines = ctx.failure.splitlines()
    assert any(
        ln.startswith("CONFLICT (content): Merge conflict in src/a.py") for ln in lines
    )
    assert rig.launches == [] and not rig.land_tree().exists()
    assert {"STATUS:open", "attempt:1"} <= store.tag_strings(42)
    assert "CONFLICT (content)" in store.comments(42)[-1]
    # the handoff tells the builder to merge the local ref, not to rebase
    handoff = store.comments(42)[-1]
    assert handoff.startswith(run_slice._LANDER_HANDOFF_PREFIX)
    assert "`git merge origin/main`" in handoff and "rebase" not in handoff
    assert run_slice.LANDER_CONFLICT_MARK in handoff
    assert run_slice._lander_conflict(handoff)


def test_github_dir_change_is_refused_and_stamped_human_only(rig: Rig) -> None:
    rig.commit_on_branch(
        {".github/workflows/ci.yml": "on: push\n", "src/a.py": "x = 2\n"}
    )
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.failure is not None
    assert "deny path" in ctx.failure and ".github/workflows/ci.yml" in ctx.failure
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [".github/workflows/ci.yml"]
    assert rig.launches == [] and not rig.land_tree().exists()
    assert "attempt:1" in store.tag_strings(42)


@pytest.mark.parametrize(
    "name",
    [
        ".github/workflows/ci\tbot.yml",
        ".github/workflows/café.yml",
    ],
)
def test_deny_path_git_quotes_is_still_refused(rig: Rig, name: str) -> None:
    """A deny path carrying a tab or a non-ASCII byte is C-quoted by
    ``git diff --name-only``; ``-z`` must still refuse it (td677929)."""
    rig.commit_on_branch({name: "on: push\n"})
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.failure is not None
    assert "deny path" in ctx.failure and name in ctx.failure
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [name]
    assert rig.launches == [] and not rig.land_tree().exists()
    assert "attempt:1" in store.tag_strings(42)


def test_version_change_is_refused_but_other_pyproject_edits_land(rig: Rig) -> None:
    rig.commit_on_branch({"pyproject.toml": PYPROJECT.replace("1.0.0", "1.1.0")})
    ctx, handle, _ = _submit(rig)
    assert handle is None and ctx.failure is not None
    assert "[project].version" in ctx.failure
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == ["pyproject.toml"]
    assert rig.launches == [] and not rig.land_tree().exists()


def test_other_pyproject_edit_is_allowed(rig: Rig) -> None:
    rig.commit_on_branch(
        {
            "pyproject.toml": PYPROJECT.replace(
                "dependencies = []", 'dependencies = ["x"]'
            )
        }
    )
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and handle is not None
    assert len(rig.launches) == 1


def test_empty_diff_takes_the_no_land_path_and_launches_nothing(rig: Rig) -> None:
    # a build that changed nothing: the attempt ref sits on the base tip
    rig.head = _g(rig.clone, "rev-parse", "refs/heads/main")
    rig.ref = rig.push_attempt(rig.head)
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and ctx.status is None
    assert handle is not None and handle["no_land"] is True
    assert handle["repo"] == "catpath"
    assert rig.launches == [] and not rig.land_tree().exists()


def test_already_on_main_branch_is_no_land(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    _g(rig.builder, "push", "-q", "origin", "slice/42:main")
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and handle is not None and handle["no_land"] is True
    assert rig.launches == []


def test_fetch_failure_is_attempt_free_and_launches_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    monkeypatch.setattr(factory_git_mirror, "fetch_ref_into", lambda *a, **k: None)
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set["red_class"] == "origin-fetch"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    tags = store.tag_strings(42)
    assert not [t for t in tags if t.startswith("attempt:")]
    assert rig.launches == [] and not rig.land_tree().exists()


def test_a_stale_land_tree_is_replaced(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    stale = rig.land_tree()
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x", encoding="utf-8")
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and handle is not None
    assert not (stale / "junk").exists()


def _stub_profile(
    *,
    setup_cmds: tuple[str, ...],
    lint_cmds: tuple[str, ...],
    test_cmds: tuple[str, ...],
) -> lanes.RepoProfile:
    return replace(
        lanes.REPO_PROFILES["catpath"],
        setup_cmds=setup_cmds,
        lint_cmds=lint_cmds,
        test_cmds=test_cmds,
    )


def _run_gate(tmp_path: Path, profile: lanes.RepoProfile) -> tuple[int, str]:
    script = tmp_path / "gate.sh"
    script.write_text(repo_land.gate_script(profile), encoding="utf-8")
    proc = subprocess.run(
        ["sh", str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.returncode, proc.stdout


def test_gate_script_stops_at_the_first_failing_phase(tmp_path: Path) -> None:
    profile = _stub_profile(
        setup_cmds=("echo S",),
        lint_cmds=("echo L; exit 3",),
        test_cmds=("echo T",),
    )
    rc, out = _run_gate(tmp_path, profile)
    assert rc == 3
    assert out.splitlines() == ["S", "L", "✖ gate lint failed: echo L; exit 3"]


def test_gate_script_reports_setup_and_test_phases_and_green(tmp_path: Path) -> None:
    rc, out = _run_gate(
        tmp_path,
        _stub_profile(setup_cmds=("exit 5",), lint_cmds=("echo L",), test_cmds=()),
    )
    assert rc == 5 and out == "✖ gate setup failed: exit 5\n"
    rc, out = _run_gate(
        tmp_path,
        _stub_profile(
            setup_cmds=("true",), lint_cmds=(), test_cmds=("echo T", "exit 7")
        ),
    )
    assert rc == 7 and out.splitlines()[-1] == "✖ gate test failed: exit 7"
    rc, out = _run_gate(
        tmp_path, _stub_profile(setup_cmds=(), lint_cmds=("true",), test_cmds=("true",))
    )
    assert rc == 0 and out == ""


def _toml(version: str) -> str:
    return f'[project]\nname = "d"\nversion = "{version}"\n'


@pytest.mark.parametrize(
    ("tip_text", "sha_text", "expected"),
    [
        (_toml("1.0.0"), _toml("1.0.0"), False),
        (_toml("1.0.0"), _toml("1.0.1"), True),
        (_toml("1.0.0"), "not = [toml", True),
        ("not = [toml", _toml("1.0.0"), True),
        (None, _toml("1.0.0"), True),
        (_toml("1.0.0"), None, True),
        (None, None, True),
        ('[project]\nname = "d"\n', '[project]\nname = "d"\n', True),
        (_toml("1.0.0"), _toml("1.0.0") + 'description = "more"\n', False),
    ],
)
def test_version_changed_table(
    tip_text: str | None, sha_text: str | None, expected: bool
) -> None:
    assert repo_land.version_changed(tip_text, sha_text) is expected


# ── poll / kill ────────────────────────────────────────────────────

PROFILE = lanes.REPO_PROFILES["catpath"]
TEST_RED_LOG = (
    "collected 3 items\n"
    "=== short test summary info ===\n"
    "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
    "✖ gate test failed: uv run pytest -q\n"
)


@dataclass
class PushCall:
    url: str
    ref: str
    sha: str
    tip: str
    ops_parent: Path
    object_dirs: tuple[Path, ...]


def _fake_push(
    monkeypatch: pytest.MonkeyPatch, outcome: tuple[str, str] = ("ok", "main")
) -> list[PushCall]:
    calls: list[PushCall] = []

    def push_cas(
        url: str,
        ref: str,
        sha: str,
        tip: str,
        *,
        ops_parent: Path,
        env: Any,
        object_dirs: tuple[Path, ...],
    ) -> tuple[str, str]:
        calls.append(PushCall(url, ref, sha, tip, ops_parent, object_dirs))
        return outcome

    monkeypatch.setattr(factory_git_mirror, "push_cas", push_cas)
    return calls


def _gated(
    rig: Rig,
    *,
    exit_code: int | None,
    log_text: str = "",
    files: dict[str, str] | None = None,
) -> tuple[_Ctx, dict[str, Any], _Store]:
    """A submitted land whose gate has finished with ``exit_code``."""
    rig.commit_on_branch(files or {"src/a.py": "x = 2\n"})
    ctx, handle, store = _submit(rig)
    assert isinstance(handle, dict) and "sha" in handle
    Path(handle["log"]).write_text(log_text, encoding="utf-8")
    if exit_code is not None:
        Path(handle["exit_file"]).write_text(f"{exit_code}\n", encoding="utf-8")
    return ctx, handle, store


def _kept(rig: Rig, store: _Store) -> None:
    """A land that did not take leaves the work where the next attempt
    continues from it: the hub's attempt ref and the task's ``build_ref``
    are as they were, and no floor was stamped."""
    assert rig.refs() == {BUILD_JOB: rig.head}
    assert store.refs[42].meta[BUILD_REF_KEY]["sha"] == rig.head
    assert BUILD_FLOOR_KEY not in store.refs[42].meta


def _attempt_free(ctx: _Ctx, store: _Store, red_class: str) -> None:
    assert ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    assert ctx.meta_set["red_class"] == red_class
    assert not [t for t in store.tag_strings(42) if t.startswith("attempt:")]
    assert "STATUS:done" not in store.tag_strings(42)


def test_a_repo_closed_during_the_gate_is_not_pushed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission (``land_slice._repo_refusal``: the lander, the open set,
    the todo's tag) is re-read right before the push, so closing the repo or
    retagging the todo while the gate runs stops the land."""
    calls = _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    monkeypatch.setattr(
        land_slice, "_repo_refusal", lambda store, params: "land_slice: closed"
    )
    assert repo_land.poll(ctx, handle) is True
    assert calls == []
    _attempt_free(ctx, store, repo_land.CLOSED_CLASS)
    _kept(rig, store)
    assert lanes.LANDED_REPO_KEY not in store.refs[42].meta


def test_a_dead_gate_with_no_exit_file_is_attempt_free(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No exit file means the gate was killed from outside (a worker
    restart, a reboot), not that the branch failed."""
    calls = _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=None)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: False)
    assert repo_land.poll(ctx, handle) is True
    assert calls == []
    _attempt_free(ctx, store, repo_land.GATE_INFRA_CLASS)


def test_a_handle_for_another_repo_than_the_jobs_is_not_pushed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    ctx.meta["params"] = {**ctx.meta["params"], "repo": "precis"}
    assert repo_land.poll(ctx, handle) is True
    assert calls == []
    _attempt_free(ctx, store, repo_land.CLOSED_CLASS)


def test_the_factory_switch_off_during_the_gate_is_not_pushed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    assert repo_land.poll(ctx, handle) is True
    assert calls == []
    _attempt_free(ctx, store, repo_land.CLOSED_CLASS)


def test_green_pushes_the_handle_sha_and_records_the_land(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=0)
    pushes = _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    assert len(pushes) == 1
    call = pushes[0]
    assert (call.url, call.ref) == (PROFILE.remote, PROFILE.base)
    assert (call.sha, call.tip) == (handle["sha"], handle["tip"])
    assert call.object_dirs == (rig.clone / ".git" / "objects",)
    assert ctx.status == "succeeded" and ctx.failure is None
    assert rig.lease.releases == [("catpath", 901)]
    assert ctx.meta_set["landed_sha"] == handle["sha"]
    # the landed: sha is catpath's: the todo says so for the precis readers
    assert store.refs[42].meta[lanes.LANDED_REPO_KEY] == "catpath"
    assert (
        store.comments(42)[-1] == f"landed: {handle['sha'][:9]} — {handle['subject']}"
    )
    assert "STATUS:done" in store.tag_strings(42)
    assert not [t for t in store.tag_strings(42) if t.startswith("attempt:")]
    assert [k for k, _ in ctx.chunks if k != "job_summary"] == ["job_event"]
    # the tidy-up: the land tree and its worktree record are gone, the hub's
    # attempt refs are deleted, the task's build_ref and conflict are cleared
    # and the floor is this land job's id
    assert not rig.land_tree().exists()
    assert str(rig.land_tree()) not in _g(rig.clone, "worktree", "list")
    assert rig.refs() == {}
    meta = store.refs[42].meta
    assert meta[BUILD_REF_KEY] is None and meta[land_slice.CONFLICT_KEY] is None
    assert meta[BUILD_FLOOR_KEY] == JOB
    assert not [t for t in ctx.meta_set["cleanup"] if "FAILED" in t]


def test_push_uses_the_handle_sha_after_the_clone_refs_were_rewritten(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, _ = _gated(rig, exit_code=0)
    pushes = _fake_push(monkeypatch)
    seed = _g(rig.clone, "rev-parse", "refs/heads/main")
    _g(rig.clone, "update-ref", "refs/heads/decoy", seed)
    _g(rig.clone, "update-ref", "refs/remotes/origin/main", handle["sha"])
    assert repo_land.poll(ctx, handle) is True
    assert [(c.sha, c.tip) for c in pushes] == [(handle["sha"], handle["tip"])]
    assert ctx.status == "succeeded"


def test_real_push_cas_fast_forwards_the_local_origin(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lanes,
        "repo_profile",
        lambda name: replace(PROFILE, remote=str(rig.origin)),
    )
    ctx, handle, _ = _gated(rig, exit_code=0)
    assert repo_land.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failure
    assert _g(rig.origin, "rev-parse", "refs/heads/main") == handle["sha"]
    assert _g(rig.origin, "rev-parse", f"{handle['sha']}^") == handle["tip"]


def test_raced_is_attempt_free_and_keeps_the_slice(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=0)
    _fake_push(monkeypatch, ("raced", "main moved"))
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.RACED_CLASS)
    assert not rig.land_tree().exists()
    _kept(rig, store)


def test_push_error_is_attempt_free_and_keeps_the_slice(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=0)
    _fake_push(monkeypatch, ("error", "unreadable"))
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.PUSH_ERROR_CLASS)
    assert not rig.land_tree().exists()
    _kept(rig, store)


def test_a_sha_that_is_not_a_child_of_the_tip_is_never_pushed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=0)
    pushes = _fake_push(monkeypatch)
    handle["tip"] = handle["sha"]  # tampered: sha^ is not sha
    assert repo_land.poll(ctx, handle) is True
    assert pushes == []
    _attempt_free(ctx, store, repo_land.HANDLE_CLASS)
    assert not rig.land_tree().exists()


def test_a_deny_path_in_the_diff_is_refused_before_the_push(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=0)
    pushes = _fake_push(monkeypatch)
    monkeypatch.setattr(
        lanes, "repo_profile", lambda name: replace(PROFILE, deny_paths=("src/**",))
    )
    assert repo_land.poll(ctx, handle) is True
    assert pushes == [] and ctx.status == "failed"
    assert "deny path" in (ctx.failure or "")
    assert "attempt:1" in store.tag_strings(42)


def test_red_test_phase_is_charged_with_a_handoff_naming_phase_and_command(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=1, log_text=TEST_RED_LOG)
    pushes = _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    assert pushes == [] and ctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert run_slice.ATTEMPT_FREE_KEY not in ctx.meta_set
    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff:")
    assert "gate test failed: uv run pytest -q" in handoff
    assert "tests/test_x.py::test_a" in handoff
    assert not rig.land_tree().exists()
    _kept(rig, store)


def test_red_log_is_scrubbed_before_it_reaches_the_todo(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ghp_" + "".join(chr(97 + (i * 7) % 26) for i in range(36))
    log_text = f"token {secret}\n" + TEST_RED_LOG
    ctx, handle, store = _gated(rig, exit_code=1, log_text=log_text)
    _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    assert secret not in store.comments(42)[-1]
    assert secret not in (ctx.failure or "")


def test_red_setup_without_a_dependency_file_is_attempt_free(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(
        rig, exit_code=2, log_text="✖ gate setup failed: uv sync\n"
    )
    _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.SETUP_INFRA_CLASS)
    assert not rig.land_tree().exists()


def test_red_setup_with_pyproject_in_the_diff_is_charged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = PYPROJECT.replace("dependencies = []", 'dependencies = ["nope"]')
    ctx, handle, store = _gated(
        rig,
        exit_code=2,
        log_text="✖ gate setup failed: uv sync\n",
        files={"pyproject.toml": deps},
    )
    _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    assert ctx.status == "failed" and "attempt:1" in store.tag_strings(42)
    assert run_slice.ATTEMPT_FREE_KEY not in ctx.meta_set
    assert "gate setup failed: uv sync" in store.comments(42)[-1]


def test_exit_137_is_attempt_free(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, handle, store = _gated(rig, exit_code=137, log_text="Killed\n")
    _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.GATE_INFRA_CLASS)
    assert not rig.land_tree().exists()


def test_a_running_gate_returns_not_finished(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=None)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    pushes = _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is False
    assert pushes == [] and ctx.status is None
    assert ctx.meta_set["compute_handle_released"] is False
    assert ctx.meta_set["compute_handle"] is handle
    assert rig.land_tree().is_dir()


def test_past_the_deadline_the_gate_is_killed_and_the_land_fails_free(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=None)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: signals.append((pgid, int(sig)))
    )
    handle["deadline"] = 1.0
    assert repo_land.poll(ctx, handle) is True
    assert signals == [(4242, 15)]
    _attempt_free(ctx, store, repo_land.TIMED_OUT_CLASS)
    assert ctx.meta_set["land_again_cap"] is True
    assert not rig.land_tree().exists()


def test_no_land_handle_records_nothing_new_and_never_pushes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.head = _g(rig.clone, "rev-parse", "refs/heads/main")
    rig.ref = rig.push_attempt(rig.head)  # a build that changed nothing
    ctx, handle, store = _submit(rig)
    assert isinstance(handle, dict) and handle["no_land"] is True
    pushes = _fake_push(monkeypatch)
    assert repo_land.poll(ctx, handle) is True
    assert pushes == [] and ctx.status == "succeeded"
    assert store.comments(42)[-1] == land_slice.NO_DIFF_NOTE
    assert "STATUS:done" in store.tag_strings(42)
    assert ctx.meta_set["landed_sha"] is None
    assert lanes.LANDED_REPO_KEY not in store.refs[42].meta
    # the work is on the base already: the attempt is retired like a landed one
    assert rig.refs() == {} and store.refs[42].meta[BUILD_REF_KEY] is None
    assert store.refs[42].meta[BUILD_FLOOR_KEY] == JOB


def test_kill_signals_the_gate_group_and_drops_the_land_tree(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, handle, store = _gated(rig, exit_code=None)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: signals.append((pgid, int(sig)))
    )
    repo_land.kill(ctx, handle)
    assert signals == [(4242, 15)]
    assert not rig.land_tree().exists()
    _kept(rig, store)


def test_kill_permission_denied_still_drops_the_land_tree(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """gr702533: a reused foreign pgid must not prevent land cleanup."""
    import os

    ctx, handle, store = _gated(rig, exit_code=None)
    signals: list[tuple[int, int]] = []

    def denied(pgid: int, sig: int) -> None:
        signals.append((pgid, int(sig)))
        raise PermissionError("foreign process group")

    monkeypatch.setattr(os, "killpg", denied)
    land_slice.kill(ctx, handle)
    assert signals == [(4242, 15)]
    assert not rig.land_tree().exists()
    _kept(rig, store)
    assert "permission denied for pgid 4242" in caplog.text


def test_kill_never_signals_the_workers_own_pid_or_group(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    ctx, handle, _ = _gated(rig, exit_code=None)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: signals.append((pgid, int(sig)))
    )
    for own in (os.getpid(), os.getpgrp()):
        handle["pid"] = handle["pgid"] = own
        repo_land.kill(ctx, handle)
    assert signals == []
    assert not rig.land_tree().exists()


# ── The commit to land comes from the hub (gr741695) ───────────────


def _real_origin(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the profile's remote at the local origin so the land's push is
    the real ``push_cas``."""
    monkeypatch.setattr(
        lanes, "repo_profile", lambda name: replace(PROFILE, remote=str(rig.origin))
    )


def test_a_pushed_attempt_lands_through_the_green_path_with_a_wip_tip(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a second-repository land either reads something other
    than the hub (a builder folder, a local branch), takes the ``wip:``
    snapshot on top of the head, or leaves the attempt behind to be
    continued from after it landed."""
    _real_origin(rig, monkeypatch)
    rig.commit_on_branch({"src/a.py": "x = 2\n", "src/b.py": "y = 1\n"})
    snap = rig.snapshot_on_top()
    assert ws.hub_tip(rig.hub, rig.ref) == snap != rig.head  # the tip is the wip
    rig.advance_origin({"README.md": "hi\n"})
    ctx, handle, store = _submit(rig)
    assert ctx.failure is None and isinstance(handle, dict)
    assert handle["head"] == rig.head
    Path(handle["log"]).write_text("", encoding="utf-8")
    Path(handle["exit_file"]).write_text("0\n", encoding="utf-8")
    assert repo_land.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failure
    landed = _g(rig.origin, "rev-parse", "refs/heads/main")
    assert landed == handle["sha"] and ctx.meta_set["landed_sha"] == landed
    # one commit on the base tip with exactly head's changes: the snapshot's
    # leftover file and main's own move are not in it
    assert _g(rig.origin, "rev-parse", f"{landed}^") == handle["tip"]
    changed = _g(rig.origin, "diff", "--name-only", handle["tip"], landed).splitlines()
    assert changed == ["src/a.py", "src/b.py"]
    assert (
        _g(rig.origin, "log", "-1", "--format=%s", landed) == "feat(x): widget (td42)"
    )
    # the tidy-up
    assert rig.refs() == {} and not rig.land_tree().exists()
    meta = store.refs[42].meta
    assert meta[BUILD_REF_KEY] is None and meta[land_slice.CONFLICT_KEY] is None
    assert meta[BUILD_FLOOR_KEY] == JOB
    assert meta[lanes.LANDED_REPO_KEY] == "catpath"
    assert "STATUS:done" in store.tag_strings(42)


def test_head_below_a_wip_tip_is_the_commit_squashed(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    snap = rig.snapshot_on_top()
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and isinstance(handle, dict)
    tree = Path(handle["land_tree"])
    # the snapshot was never brought into the squash
    assert not (tree / "leftover.txt").exists()
    assert (tree / "src" / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert handle["head"] == rig.head and handle["head"] != snap


def test_no_head_and_a_wip_tip_is_refused_not_squashed(rig: Rig) -> None:
    """If this fails a land minted without a head squashes a ``wip:``
    snapshot, whose extra parents (stashes, other branches) carry content no
    reviewer saw."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    snap = rig.snapshot_on_top()
    ctx, handle, store = _submit(rig, head="")
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    note = store.comments(42)[-1]
    assert "wip: snapshot" in note and snap[:12] in note and "next:" in note
    assert rig.launches == [] and rig.fetches == []
    assert not rig.land_tree().exists()
    assert rig.refs() == {BUILD_JOB: snap}


def test_no_head_and_a_finished_tip_lands_the_tip(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    ctx, handle, _ = _submit(rig, head="")
    assert ctx.failure is None and isinstance(handle, dict)
    assert handle["head"] == rig.head
    a_py = Path(handle["land_tree"]) / "src" / "a.py"
    assert a_py.read_text(encoding="utf-8") == "x = 2\n"


def test_a_host_with_no_hub_refuses_without_creating_one(rig: Rig) -> None:
    """If this fails a land that reaches a host that never built the
    repository creates and seeds a hub there only to refuse it."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    shutil.rmtree(rig.hub)
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    assert "has no hub" in store.comments(42)[-1]
    assert not rig.hub.exists() and rig.launches == []
    assert not rig.land_tree().exists()


def test_a_moved_ref_is_refused_attempt_free_and_lands_nothing(rig: Rig) -> None:
    """If this fails a ref replaced by something else (a force push, a hand
    edit of the hub) lands the wrong commit, or falls back to another source."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    # the attempt ref is replaced by an unrelated commit
    _g(rig.builder, "checkout", "-q", "-B", "other", "main")
    (rig.builder / "other.txt").write_text("else\n", encoding="utf-8")
    _g(rig.builder, "add", "other.txt")
    _g(rig.builder, "commit", "-q", "-m", "feat: elsewhere")
    rig.push_attempt(_g(rig.builder, "rev-parse", "HEAD"))
    ctx, handle, store = _submit(rig)
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set["red_class"] == "infra-land"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    handoff = store.comments(42)[-1]
    assert "no longer holds" in handoff and rig.head[:12] in handoff
    assert not [t for t in store.tag_strings(42) if t.startswith("attempt:")]
    assert rig.launches == [] and rig.fetches == []
    assert not rig.land_tree().exists()
    assert list(rig.refs()) == [BUILD_JOB]  # the ref is left as it is


def test_a_ref_the_hub_does_not_hold_is_refused_attempt_free(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    ctx, handle, store = _submit(rig, ref=ws.build_ref(TODO, 99))
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    assert "holds no refs/factory/build/42/99" in store.comments(42)[-1]
    assert rig.launches == [] and rig.fetches == []


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"ref": ""}, "names no hub ref"),
        ({"ref": "refs/factory/build/43/7"}, "not an attempt ref of td42"),
        ({"ref": "refs/heads/main"}, "not an attempt ref of td42"),
        ({"head": "main"}, "not a full commit id"),
    ],
)
def test_a_land_without_a_usable_ref_or_head_is_refused_with_the_reason(
    rig: Rig, extra: dict[str, Any], why: str
) -> None:
    """If this fails a land with no hub ref falls back to a GitHub slice ref
    (a second repository has none) or to some other branch, or takes a head
    that names a ref rather than a commit."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    ctx, handle, store = _submit(rig, **extra)
    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-land"
    assert why in store.comments(42)[-1]
    assert not [t for t in store.tag_strings(42) if t.startswith("attempt:")]
    # nothing was fetched, taken, cut or launched; the lease was never asked for
    assert rig.fetches == [] and rig.launches == []
    assert not rig.land_tree().exists()
    if "ref" in extra:
        assert rig.lease.acquires == []
    if extra.get("ref") == "":
        # deterministic: the same params are refused again, so the plan tick
        # asks instead of re-minting the land
        assert ctx.meta_set["land_again_cap"] is True


def _floor_spy(rig: Rig, store: _Store, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record what the hub's attempt refs and the task meta looked like at
    each ``delete_builds`` call."""
    seen: list[Any] = []
    real = ws.delete_builds

    def spy(hub: Path, todo_id: int) -> str:
        seen.append((dict(store.refs[42].meta), rig.refs()))
        return real(hub, todo_id)

    monkeypatch.setattr(ws, "delete_builds", spy)
    return seen


def test_the_floor_is_stamped_before_the_refs_are_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a stamp that fails after the refs are gone leaves a
    ``build_ref`` naming a deleted ref and no floor, and every launch of a
    reopened task raises ``RefMoved``."""
    _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    seen = _floor_spy(rig, store, monkeypatch)
    assert repo_land.poll(ctx, handle) is True and ctx.status == "succeeded"
    ((meta, refs),) = seen
    assert meta[BUILD_FLOOR_KEY] == JOB and refs == {BUILD_JOB: rig.head}
    assert meta[BUILD_REF_KEY] is not None  # not cleared yet: the later stamp


def test_a_failed_clearing_stamp_leaves_the_floor_and_no_refmoved(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    recorded = dict(store.refs[42].meta[BUILD_REF_KEY])
    real = store.stamp_ref_meta

    def flaky(ref_id: int, updates: dict[str, Any]) -> None:
        if BUILD_REF_KEY in updates:
            raise RuntimeError("store down")
        real(ref_id, updates)

    monkeypatch.setattr(store, "stamp_ref_meta", flaky)
    assert repo_land.poll(ctx, handle) is True and ctx.status == "succeeded"
    meta = store.refs[42].meta
    assert meta[BUILD_FLOOR_KEY] == JOB
    assert meta[BUILD_REF_KEY] == recorded  # the clear failed
    assert rig.refs() == {}  # the refs are gone
    assert any("meta: stamp FAILED" in n for n in ctx.meta_set["cleanup"])
    # a later launch with that record neither starts from it nor raises
    recorded["ref"] = ws.build_ref(TODO, BUILD_JOB)
    got = ws.resolve_start(
        rig.hub, TODO, recorded=recorded, floor=meta[BUILD_FLOOR_KEY]
    )
    assert got is None


def test_without_the_floor_the_refs_are_not_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_push(monkeypatch)
    ctx, handle, store = _gated(rig, exit_code=0)
    real = store.stamp_ref_meta

    def flaky(ref_id: int, updates: dict[str, Any]) -> None:
        if BUILD_FLOOR_KEY in updates:
            raise RuntimeError("store down")
        real(ref_id, updates)

    monkeypatch.setattr(store, "stamp_ref_meta", flaky)
    assert repo_land.poll(ctx, handle) is True and ctx.status == "succeeded"
    assert rig.refs() == {BUILD_JOB: rig.head}
    assert BUILD_FLOOR_KEY not in store.refs[42].meta


def test_a_head_the_hub_does_not_have_is_refused(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    ctx, handle, store = _submit(rig, head="1" * 40)
    assert handle is None and ctx.status == "failed"
    assert "the hub does not have it" in store.comments(42)[-1]
    assert rig.launches == []


def test_a_parked_land_whose_ref_vanished_fails_attempt_free_on_waking(
    rig: Rig,
) -> None:
    ctx, handle, store = _parked(rig)
    assert handle["phase"] == "lease" and "worktree" not in handle
    ws.delete_builds(rig.hub, TODO)
    _due(handle)
    rig.lease.blocker = None
    assert repo_land.poll(ctx, handle) is True
    assert ctx.status == "failed" and ctx.meta_set["red_class"] == "infra-land"
    assert "holds no refs/factory/build/42/7" in store.comments(42)[-1]
    assert rig.launches == []


def test_a_land_folder_the_janitor_dropped_does_not_block_the_same_job(
    rig: Rig,
) -> None:
    """The janitor removes a terminal land's folder with ``_slice_ws.drop``,
    which leaves the host checkout's worktree record; a relaunch of the same
    job must still cut its folder."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    ctx, handle, _ = _submit(rig)
    assert isinstance(handle, dict)
    assert ws.drop(rig.land_tree())
    ctx2, handle2, _ = _submit(rig)
    assert ctx2.failure is None and isinstance(handle2, dict)
    assert rig.land_tree().is_dir()


# ── The per-repo lease ─────────────────────────────────────────────

HOLDER = {"job_id": 77, "kind": "land", "reason": "held"}


def _parked(rig: Rig, blocker: dict[str, Any] = HOLDER) -> tuple[_Ctx, Any, _Store]:
    """A land whose submit found the lease taken by ``blocker``."""
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    rig.lease.blocker = blocker
    return _submit(rig)


def _due(handle: dict[str, Any]) -> None:
    handle["not_before"] = "2000-01-01T00:00:00Z"


def test_a_blocked_submit_parks_and_touches_nothing(rig: Rig) -> None:
    """If this fails a land behind another job's lease fetches, squashes or
    launches a gate anyway (two lands of one repo overlap), or parks with a
    handle kill and rescue cannot read."""
    ctx, handle, _ = _parked(rig)
    assert ctx.failure is None and ctx.status is None
    assert rig.fetches == [] and rig.launches == []
    assert not rig.land_tree().exists()
    assert handle["lander"] == "repo_land" and handle["phase"] == "lease"
    assert handle["repo"] == "catpath" and handle["branch"] == "slice/42"
    assert "pid" not in handle and "sha" not in handle
    due = datetime.strptime(handle["not_before"], "%Y-%m-%dT%H:%M:%SZ")
    assert due.replace(tzinfo=UTC) > datetime.now(UTC)
    assert ctx.meta_set["compute_handle"] is handle
    assert ctx.meta_set["deadline"] > time.time() + repo_land.LEASE_PARK_S
    events = [text for kind, text in ctx.chunks if kind == "job_event"]
    assert len(events) == 1 and "held by land job #77" in events[0]
    assert rig.lease.acquires == [("catpath", 901, "land")]


def test_a_waiting_release_parks_a_land_the_same_way(rig: Rig) -> None:
    """If this fails a land overtakes a release that is waiting for the
    lease, and a steady stream of lands starves the release."""
    wanted = {"job_id": 88, "kind": "release", "reason": "wanted"}
    ctx, handle, _ = _parked(rig, wanted)
    assert handle["phase"] == "lease" and rig.launches == []
    events = [text for kind, text in ctx.chunks if kind == "job_event"]
    assert len(events) == 1 and "wanted by release job #88" in events[0]


def test_a_poll_before_not_before_waits_without_asking_for_the_lease(
    rig: Rig,
) -> None:
    """If this fails every poll tick hits the database for the lease and
    a parked land retries sooner than the park says."""
    ctx, handle, _ = _parked(rig)
    rig.lease.acquires.clear()
    rig.lease.blocker = None
    assert repo_land.poll(ctx, handle) is False
    assert rig.lease.acquires == [] and rig.launches == []
    assert handle["phase"] == "lease"


def test_a_due_poll_that_acquires_launches_the_gate_and_swaps_the_handle(
    rig: Rig,
) -> None:
    """If this fails a land that finally gets the lease never starts its
    gate, or starts it but the live handle (pid, process identity, sha) is
    not what the next poll reads."""
    ctx, handle, _ = _parked(rig)
    parked = handle
    _due(handle)
    rig.lease.blocker = None
    assert repo_land.poll(ctx, handle) is False
    assert ctx.failure is None
    assert len(rig.launches) == 1
    assert handle is parked and "phase" not in handle and "not_before" not in handle
    assert handle["pid"] == 4242 and handle["sha"] and handle["tip"]
    assert ctx.meta_set["compute_handle"] == handle
    assert ctx.meta_set["compute_handle_released"] is False
    assert ctx.meta_set["deadline"] == handle["deadline"]
    if sys.platform == "darwin":
        assert "ps_identity" in handle or "boot_id" not in handle
    else:
        assert ("boot_id" in handle) == ("start_time" in handle)
    assert rig.land_tree().is_dir()


def test_a_due_poll_still_blocked_parks_again_with_a_fresh_deadline(
    rig: Rig,
) -> None:
    """If this fails a land stuck behind a long holder is killed by its own
    wall clock while parked, or logs a job event on every retry."""
    ctx, handle, _ = _parked(rig)
    _due(handle)
    ctx.meta_set.clear()
    ctx.chunks.clear()
    assert repo_land.poll(ctx, handle) is False
    assert ctx.status is None and rig.launches == []
    due = datetime.strptime(handle["not_before"], "%Y-%m-%dT%H:%M:%SZ")
    assert due.replace(tzinfo=UTC) > datetime.now(UTC)
    assert ctx.meta_set["deadline"] > time.time() + repo_land.LEASE_PARK_S
    assert handle["deadline"] == ctx.meta_set["deadline"]
    assert ctx.chunks == []  # same blocker as the first park: no new event


def test_a_new_blocker_on_a_re_park_is_named_in_one_event(rig: Rig) -> None:
    """If this fails a land that moves from waiting on a land to waiting on
    a release does not say so."""
    ctx, handle, _ = _parked(rig)
    _due(handle)
    ctx.chunks.clear()
    rig.lease.blocker = {"job_id": 88, "kind": "release", "reason": "wanted"}
    assert repo_land.poll(ctx, handle) is False
    assert len(ctx.chunks) == 1 and "job #88" in ctx.chunks[0][1]


def test_a_land_blocked_past_the_wait_cap_fails_attempt_free(rig: Rig) -> None:
    """If this fails a land behind a wedged holder waits forever, or its
    failure charges the builder an attempt instead of re-minting the land."""
    ctx, handle, store = _parked(rig)
    _due(handle)
    handle["lease_since"] = time.time() - repo_land.LEASE_WAIT_CAP_S - 60
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.HELD_CLASS)
    assert rig.launches == []


@pytest.mark.parametrize("how", ["factory-off", "repo-closed"])
def test_a_parked_land_waking_to_a_closed_repo_starts_no_gate(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """If this fails a land that waited for the lease takes it and launches a
    gate after the operator switched the factory off or closed the repo."""
    ctx, handle, store = _parked(rig)
    _due(handle)
    rig.lease.blocker = None
    rig.lease.acquires.clear()
    if how == "factory-off":
        monkeypatch.setattr(run_slice, "factory_off", lambda: True)
    else:
        monkeypatch.setattr(
            land_slice, "_repo_refusal", lambda store, params: "closed by the operator"
        )
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.CLOSED_CLASS)
    assert rig.lease.acquires == [] and rig.launches == []


@pytest.mark.parametrize("holder_job", [77, None])
def test_green_without_the_lease_is_attempt_free_and_never_pushed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, holder_job: int | None
) -> None:
    """If this fails a land whose lease went stale and was taken by another
    job (or a release) still pushes over it."""
    ctx, handle, store = _gated(rig, exit_code=0)
    pushes = _fake_push(monkeypatch)
    rig.lease.holder_job = holder_job
    assert repo_land.poll(ctx, handle) is True
    _attempt_free(ctx, store, repo_land.HELD_CLASS)
    assert pushes == [] and rig.lease.releases == []
    assert lanes.LANDED_REPO_KEY not in store.refs[42].meta


def test_kill_on_a_parked_handle_does_not_raise(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the wall-clock or operator kill of a parked land raises
    (a handle without a pid) and the job is never reaped."""
    ctx, handle, _ = _parked(rig)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: signals.append((pgid, int(sig)))
    )
    repo_land.kill(ctx, handle)
    assert signals == []
