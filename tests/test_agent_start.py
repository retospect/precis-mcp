"""Agent launchers must establish a real, owned checkout before starting a CLI.

Real temporary Git repositories exercise checkout and lock identity. Fetch,
venv seeding and agent binaries are local stubs: no agents or network launch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")
REPO = Path(__file__).resolve().parents[1]


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@dataclass
class Launcher:
    root: Path
    real_git: str
    env: dict[str, str]
    calls: Path
    seeds: Path

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            [self.real_git, *args],
            cwd=cwd or self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=20,
        ).stdout.strip()

    def run(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts/agent-start"), *args],
            cwd=cwd or self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )

    def tree(self, name: str = "task") -> Path:
        return self.root / ".claude/worktrees" / name

    def add_tree(self, name: str = "task", *, checkout: bool = True) -> Path:
        tree = self.tree(name)
        self.git(
            "worktree",
            "add",
            *([] if checkout else ["--no-checkout"]),
            "-b",
            f"worktree-{name}",
            str(tree),
            "main",
        )
        return tree

    def rows(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
def launcher(tmp_path: Path) -> Launcher:
    # Use PATH's real Git (Homebrew on macOS), never hard-code /usr/bin/git.
    real_git = shutil.which("git")
    assert real_git, "these integration tests require Git"
    root = tmp_path.resolve() / "repository with spaces"
    root.mkdir()
    scripts = root / "scripts"
    for relative in (
        "agent-start",
        "claude-tree",
        "codex-tree",
        "kimi-tree",
        "devin-tree",
        "lib/agent-tree.sh",
        "lib/session-lock.sh",
        "lib/lock-holder.sh",
    ):
        target = scripts / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "scripts" / relative, target)
    (root / ".claude").mkdir()
    (root / ".claude/keep").write_text("tracked hook directory\n", encoding="utf-8")
    (root / "payload.txt").write_text("committed payload\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".claude/worktrees/\n.claude/purpose\n", encoding="utf-8"
    )
    home, bins = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bins.mkdir()
    calls, seeds = tmp_path / "calls.jsonl", tmp_path / "seeds.jsonl"
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_") or key in {"TMUX", "PRECIS_NO_AUTOREAP"}:
            env.pop(key)
    env.update(
        HOME=str(home),
        PATH=f"{bins}{os.pathsep}{env['PATH']}",
        REAL_GIT=real_git,
        AGENT_CALLS=str(calls),
        SEED_CALLS=str(seeds),
        FETCH_CALLS=str(tmp_path / "fetches"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    rig = Launcher(root, real_git, env, calls, seeds)
    executable(
        bins / "git",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "probe = args[:]\n"
        "while probe and probe[0] == '-C':\n"
        "    probe = probe[2:]\n"
        "if probe and probe[0] == 'fetch':\n"
        "    with open(os.environ['FETCH_CALLS'], 'a', encoding='utf-8') as out:\n"
        "        out.write('fetch\\n')\n"
        "    sys.exit(0)\n"
        "os.execv(os.environ['REAL_GIT'], [os.environ['REAL_GIT'], *args])\n",
    )
    recorder = (
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "row = {'agent': os.path.basename(sys.argv[0]), 'args': sys.argv[1:],\n"
        "       'cwd': os.getcwd(), 'pid': os.getpid(),\n"
        "       'handoff_present': 'AT_HANDOFF_PID' in os.environ}\n"
        "row['locks'] = subprocess.check_output(\n"
        "    [os.environ['REAL_GIT'], 'worktree', 'list', '--porcelain'],\n"
        "    text=True, encoding='utf-8')\n"
        "with open(os.environ['AGENT_CALLS'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(row) + '\\n')\n"
    )
    for agent in ("claude", "codex"):
        executable(bins / agent, recorder)
    executable(
        scripts / "seed-worktree-venv",
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "locks = subprocess.check_output(\n"
        "    [os.environ['REAL_GIT'], '-C', sys.argv[1], 'worktree', 'list', '--porcelain'],\n"
        "    text=True, encoding='utf-8')\n"
        "with open(os.environ['SEED_CALLS'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps({'tree': sys.argv[1], 'locks': locks}) + '\\n')\n",
    )
    rig.git("init", "-b", "main")
    rig.git("config", "user.name", "Launcher test")
    rig.git("config", "user.email", "launcher@example.invalid")
    rig.git("add", ".")
    message = tmp_path / "commit-message"
    message.write_text("test fixture\n", encoding="utf-8")
    rig.git("commit", "-F", str(message))
    rig.git("update-ref", "refs/remotes/origin/main", "HEAD")
    return rig


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_launch_creates_populated_checkout_and_locks_before_seed(
    launcher: Launcher, agent: str
) -> None:
    result = launcher.run(agent, "task", "--purpose", "review the parser")
    assert result.returncode == 0, result.stderr
    tree = launcher.tree()
    assert (tree / "payload.txt").read_text(encoding="utf-8") == "committed payload\n"
    assert launcher.git("branch", "--show-current", cwd=tree) == "worktree-task"
    assert launcher.git("branch", "--show-current") == "main"
    assert (tree / ".claude/purpose").read_text(
        encoding="utf-8"
    ).strip() == "review the parser"
    [call] = launcher.rows()
    assert Path(call["cwd"]).resolve() == tree.resolve()
    assert f"locked pid {call['pid']}\n" in call["locks"]
    [seed] = [
        json.loads(line)
        for line in launcher.seeds.read_text(encoding="utf-8").splitlines()
    ]
    assert f"worktree {tree}\n" in seed["locks"]
    assert "locked pid " in seed["locks"], "the tree must be held during slow seeding"
    assert "--model" not in call["args"] and "-m" not in call["args"]


def test_existing_dirty_checkout_is_reused_without_reset(launcher: Launcher) -> None:
    tree = launcher.add_tree()
    (tree / "payload.txt").write_text("unfinished change\n", encoding="utf-8")
    (tree / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    before = launcher.git("rev-parse", "HEAD", cwd=tree)
    result = launcher.run("codex", "task")
    assert result.returncode == 0, result.stderr
    assert launcher.git("rev-parse", "HEAD", cwd=tree) == before
    assert (tree / "payload.txt").read_text(encoding="utf-8") == "unfinished change\n"
    assert (tree / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.parametrize(
    "bad", ["plain-directory", "foreign", "wrong-branch", "empty", "sparse", "symlink"]
)
def test_invalid_existing_checkout_never_seeds_or_launches(
    launcher: Launcher, bad: str
) -> None:
    tree = launcher.tree()
    if bad == "plain-directory":
        tree.mkdir(parents=True)
    elif bad == "foreign":
        tree.mkdir(parents=True)
        launcher.git("init", "-b", "worktree-task", cwd=tree)
    elif bad == "symlink":
        tree.parent.mkdir(parents=True)
        tree.symlink_to(launcher.root, target_is_directory=True)
    else:
        launcher.add_tree(checkout=bad != "empty")
        if bad == "wrong-branch":
            launcher.git("branch", "-m", "someone-elses-branch", cwd=tree)
        if bad == "sparse":
            launcher.git("config", "core.sparseCheckout", "true", cwd=tree)
    result = launcher.run("claude", "task")
    assert result.returncode != 0, result.stdout
    assert not launcher.rows()
    assert not launcher.seeds.exists()
    assert tree.exists(), "a refusal must not clean up the rejected path"


def test_live_lock_is_not_stolen_or_seeded(launcher: Launcher) -> None:
    tree = launcher.add_tree()
    reason = f"pid {os.getpid()}"
    launcher.git("worktree", "lock", "--reason", reason, str(tree))
    before = launcher.git("worktree", "list", "--porcelain")
    result = launcher.run("codex", "task")
    assert result.returncode != 0, result.stdout
    assert launcher.git("worktree", "list", "--porcelain") == before
    assert not launcher.rows()
    assert not launcher.seeds.exists()


def test_script_checkout_wins_over_unrelated_caller_cwd(
    launcher: Launcher, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    launcher.git("init", "-b", "main", cwd=foreign)
    result = launcher.run("claude", "task", cwd=foreign)
    assert result.returncode == 0, result.stderr
    assert launcher.tree().exists()
    assert not (foreign / ".claude/worktrees/task").exists()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_arguments_keep_boundaries(launcher: Launcher, agent: str) -> None:
    arguments = ["--resume", "thread with spaces", "literal $(do-not-run) ; text"]
    result = launcher.run(agent, "task", "--", *arguments)
    assert result.returncode == 0, result.stderr
    assert launcher.rows()[0]["args"][-len(arguments) :] == arguments


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_explicit_model_is_forwarded_without_writing_configuration(
    launcher: Launcher, agent: str
) -> None:
    result = launcher.run(agent, "task", "--model", "user-selected-model")
    assert result.returncode == 0, result.stderr
    assert launcher.rows()[0]["args"] == ["--model", "user-selected-model"]
    assert not list(Path(launcher.env["HOME"]).iterdir())


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_tmux_child_takes_durable_lock_and_preserves_native_arguments(
    launcher: Launcher, agent: str
) -> None:
    bins = Path(launcher.env["PATH"].split(os.pathsep)[0])
    executable(
        bins / "tmux",
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == 'new-window', args\n"
        "cwd = args[args.index('-c') + 1]\n"
        "result = subprocess.run(['bash', '-c', args[-1]], cwd=cwd,\n"
        "                        env=os.environ.copy(), check=False, timeout=15)\n"
        "sys.exit(result.returncode)\n",
    )
    arguments = ["--resume", "thread with spaces", "literal $(do-not-run) ; text"]
    result = launcher.run(agent, "task", "--tmux", "session", "--", *arguments)
    assert result.returncode == 0, result.stderr
    [call] = launcher.rows()
    assert Path(call["cwd"]).resolve() == launcher.tree().resolve()
    assert call["args"] == arguments
    assert f"locked pid {call['pid']}\n" in call["locks"]
    assert not call["handoff_present"], "internal handoff must not reach the agent"
    assert (
        f"locked pid {call['pid']}"
        in launcher.git("worktree", "list", "--porcelain").splitlines()
    )


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("option", ["--worktree=elsewhere", "--cd=/tmp"])
def test_exec_reentry_cannot_bypass_checkout_binding(
    launcher: Launcher, agent: str, option: str
) -> None:
    tree = launcher.add_tree()
    result = launcher.run(agent, "--exec", str(tree), option)
    assert result.returncode != 0, result.stdout
    assert not launcher.rows()
    assert not launcher.seeds.exists()
    assert (tree / "payload.txt").read_text(encoding="utf-8") == "committed payload\n"


@pytest.mark.parametrize(
    "option",
    ["-w", "--worktree", "--worktree=other", "-C", "--cd", "--cd=/tmp", "--remote"],
)
def test_native_relocation_is_refused_before_mutation(
    launcher: Launcher, option: str
) -> None:
    result = launcher.run("claude", "task", "--", option, "elsewhere")
    assert result.returncode != 0, result.stdout
    assert not launcher.tree().exists()
    assert not launcher.rows()
    assert not launcher.seeds.exists()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_dry_run_has_no_launch_or_checkout_side_effects(
    launcher: Launcher, agent: str
) -> None:
    before = launcher.git("show-ref")
    result = launcher.run(agent, "task", "--purpose", "preview", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert str(launcher.tree()) in result.stdout
    assert launcher.git("show-ref") == before
    assert not launcher.tree().exists()
    assert not launcher.rows()
    assert not launcher.seeds.exists()
    assert not Path(launcher.env["FETCH_CALLS"]).exists()


@pytest.mark.parametrize(
    ("agent", "options"),
    [
        ("kimi", ["--model", "chosen-kimi", "--mode", "manual", "--dry-run"]),
        (
            "devin",
            [
                "--model",
                "chosen-devin",
                "--permission-mode",
                "auto",
                "--session",
                "my-session",
                "--dry-run",
            ],
        ),
    ],
)
def test_existing_wrappers_receive_options_unchanged(
    launcher: Launcher, agent: str, options: list[str]
) -> None:
    executable(
        launcher.root / "scripts" / f"{agent}-tree",
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['AGENT_CALLS'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps({'args': sys.argv[1:]}) + '\\n')\n",
    )
    result = launcher.run(agent, "task", *options)
    assert result.returncode == 0, result.stderr
    assert launcher.rows() == [{"args": ["task", *options]}]
    assert not launcher.tree().exists()
