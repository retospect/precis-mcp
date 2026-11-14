"""repo_land, the second-repo lander's submit half (td659522 step 2).

Real temp git repos (an "origin" bare repo and a clone carrying a
``slice/42`` branch); the credentialed fetch is replaced by a plain local
fetch and the detached launch by a recorder. The fake store and ctx are
``test_land_slice``'s.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.workers import factory_git, factory_git_mirror, repo_land
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


@dataclass
class Rig:
    origin: Path
    clone: Path
    launches: list[tuple[list[str], dict[str, Any]]]
    fetches: list[str]

    def land_tree(self) -> Path:
        return self.clone / ".claude" / "worktrees" / "land-td42"

    def commit_on_branch(
        self, files: dict[str, str], msg: str = "feat(x): widget"
    ) -> None:
        _g(self.clone, "checkout", "-q", "slice/42")
        for name, text in files.items():
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _g(self.clone, "add", "-A")
        _g(self.clone, "commit", "-q", "-m", msg)
        _g(self.clone, "checkout", "-q", "main")

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
    _g(clone, "branch", "-q", "slice/42")
    rig = Rig(origin, clone, [], [])

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

    monkeypatch.setattr(factory_git_mirror, "fetch_ref_into", fake_fetch)
    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    monkeypatch.setattr(run_slice, "slice_root_for", lambda repo: clone)
    monkeypatch.setattr(factory_git, "factory_git_token", lambda: None)
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    for name in IDENT:
        monkeypatch.delenv(name, raising=False)
    return rig


def _submit(rig: Rig) -> tuple[_Ctx, Any, _Store]:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "repo:catpath"))
    ctx = _Ctx(
        store,
        {
            "todo_id": 42,
            "repo": "catpath",
            "branch": "slice/42",
            "lane": "claude-sub",
            "worktree": str(rig.clone / ".claude" / "worktrees" / "slice-td42"),
        },
    )
    return ctx, repo_land.submit(ctx, None), store


def test_happy_path_squashes_one_commit_on_the_tip_and_launches_the_gate(
    rig: Rig,
) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n", "src/b.py": "y = 1\n"})
    rig.advance_origin({"README.md": "hi\n"})
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and handle is not None
    assert handle["repo"] == "catpath" and handle["pid"] == 4242
    tip = _g(rig.clone, "rev-parse", "refs/remotes/origin/main")
    assert handle["tip"] == tip
    sha = handle["sha"]
    assert _g(rig.clone, "rev-parse", f"{sha}^") == tip
    assert _g(rig.clone, "log", "-1", "--format=%s", sha) == "feat(x): widget (td42)"
    # the commit's change is the branch's change, nothing from main's move
    changed = _g(rig.clone, "diff", "--name-only", tip, sha).splitlines()
    assert changed == ["src/a.py", "src/b.py"]
    assert _g(rig.clone, "rev-parse", f"{sha}:src/a.py") == _g(
        rig.clone, "rev-parse", "refs/heads/slice/42:src/a.py"
    )
    # identity: no env set, so the branch tip's author (the setup identity)
    assert _g(rig.clone, "log", "-1", "--format=%an", sha) == "Setup"
    # the handle carries what the poll half needs
    land_tree = Path(handle["land_tree"])
    assert land_tree == rig.land_tree() and land_tree.is_dir()
    for key in ("branch", "log", "exit_file", "started_at", "deadline", "subject"):
        assert handle[key]
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
    profile = lanes.REPO_PROFILES["catpath"]
    env = repo_land.gate_env(profile)
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
    ctx, handle, _ = _submit(rig)
    assert ctx.failure is None and ctx.status is None
    assert handle is not None and handle["no_land"] is True
    assert handle["repo"] == "catpath"
    assert rig.launches == [] and not rig.land_tree().exists()


def test_already_on_main_branch_is_no_land(rig: Rig) -> None:
    rig.commit_on_branch({"src/a.py": "x = 2\n"})
    _g(rig.clone, "push", "-q", "origin", "slice/42:main")
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
