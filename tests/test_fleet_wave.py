"""scripts/fleet-wave — the builder-wave coordinator kit, against a fake tmux.

Each test runs the real script in a throwaway git repo with a fake ``tmux``
(and, for launch, a fake ``claude``) first on PATH. The fake tmux logs its
argv as JSON lines and serves pane text from files, so the tests also pin
that tmux only ever gets an argument list.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs scripts with fake bins"
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-wave"

FAKE_TMUX = f"""#!{sys.executable}
import json, os, subprocess, sys
argv = sys.argv[1:]
if argv[:1] == ["-L"]:
    argv = argv[2:]
d = os.environ["FAKE_TMUX_DIR"]
with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
cmd = argv[0]
def target():
    t = argv[argv.index("-t") + 1]
    return t.lstrip("=").split(":")[-1]
if cmd in ("list-windows", "list-panes"):
    name = "windows" if cmd == "list-windows" else "panes"
    p = os.path.join(d, name)
    sys.stdout.write(open(p, encoding="utf-8").read() if os.path.exists(p) else "")
elif cmd == "capture-pane":
    t = target()
    seq = os.path.join(d, "pane-" + t + ".seq")
    if os.path.isdir(seq):
        # Staged captures: serve the dir's files in call order; the last
        # one repeats once it is exhausted.
        cnt = os.path.join(d, "capcount-" + t)
        n = int(open(cnt).read()) if os.path.exists(cnt) else 0
        files = sorted(os.listdir(seq))
        sys.stdout.write(
            open(os.path.join(seq, files[min(n, len(files) - 1)])).read()
        )
        with open(cnt, "w") as fh:
            fh.write(str(n + 1))
        sys.exit(0)
    ext = ".esc" if "-e" in argv else ".txt"
    p = os.path.join(d, "pane-" + t + ext)
    if not os.path.exists(p) and ext == ".esc":
        p = os.path.join(d, "pane-" + t + ".txt")
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p, encoding="utf-8").read())
elif cmd == "new-window" and "--" in argv:
    sys.exit(subprocess.run(argv[argv.index("--") + 1:]).returncode)
"""

FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
"""


def _module() -> ModuleType:
    return load_script("fleet_wave", SCRIPT)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    # launch writes the tree's permission files; apply refuses unless ignored.
    (repo / ".gitignore").write_text(".claude/*\n.devin/*\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", ".gitignore"],
        ["git", "commit", "-q", "-m", "init"],
        # close checks landedness against origin/main; there is no real
        # remote, so the ref is set by hand (fetch just fails silently).
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    for sub in ("footers", "generated", "builder-workflow.txt"):
        src, dst = REPO / ".claude" / "fleet" / sub, repo / ".claude" / "fleet" / sub
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copyfile(src, dst)
    fake = tmp_path / "fake"
    fake.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("tmux", FAKE_TMUX), ("claude", FAKE_CLAUDE)):
        exe = bin_dir / name
        exe.write_text(body, encoding="utf-8")
        exe.chmod(0o755)
    out = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_TMUX_DIR": str(fake),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.json"),
        "FLEET_STATE": str(tmp_path / "state"),
        "FLEET_WORKTREES": str(repo / ".claude" / "worktrees"),
        "CWD": str(repo),
    }
    for k in ("FLEET_TMUX_SESSION", "FLEET_TMUX_SOCKET", "FLEET_MODEL",
              "FLEET_COORDINATOR", "FLEET_RELEASE_OWNER", "FLEET_APPROVAL",
              "FLEET_COORDINATOR_TREE", "CLAUDE_SESSION_ID"):  # fmt: skip
        out.pop(k, None)
    return out


def _windows(
    env: dict[str, str], panes: dict[str, str], esc: dict[str, str] | None = None
) -> None:
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "windows").write_text("".join(f"{w}\n" for w in panes), encoding="utf-8")
    (d / "panes").write_text(
        "".join(f"%{i}\tw:{w}\n" for i, w in enumerate(panes)), encoding="utf-8"
    )
    for i, (w, text) in enumerate(panes.items()):
        (d / f"pane-{w}.txt").write_text(text, encoding="utf-8")
        (d / f"pane-%{i}.txt").write_text(text, encoding="utf-8")
    for w, text in (esc or {}).items():
        (d / f"pane-{w}.esc").write_text(text, encoding="utf-8")


def _run(
    env: dict[str, str], *args: str, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=cwd or env["CWD"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _commit(env: dict[str, str], msg: str) -> str:
    repo = Path(env["CWD"])
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=repo, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _tmux_calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["FAKE_TMUX_DIR"]) / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]


def _worktree(env: dict[str, str], name: str) -> Path:
    wt = Path(env["FLEET_WORKTREES"]) / name
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", f"worktree-{name}", str(wt)],
        cwd=env["CWD"],
        check=True,
    )
    return wt


def _subagent_event(
    env: dict[str, str], event: str, agent_id: str, root: Path | None = None
) -> None:
    """Fire the real SubagentStart/SubagentStop hook; <root> is the tree the
    marker scopes to (the hook's project dir), defaulting to the repo root."""
    helper = REPO / "scripts" / "hooks" / "subagent-task-state.py"
    tree = root or Path(env["CWD"])
    proc = subprocess.run(
        [str(helper), event, str(tree)],
        cwd=str(tree),
        env={**env, "CLAUDE_PROJECT_DIR": str(tree)},
        input=json.dumps({"agent_id": agent_id}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def _branches(env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "branch", "--list", "worktree-*"],
        cwd=env["CWD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


IDLE = "some output\n\n────\n❯ \n────\n  12% ctx · opus\n"
# Layout sampled from a live pane, 2026-10-15.
PROMPT = (
    '● Bash(scripts/prod-psql --ro "\\! id")\n'
    " Bash command\n"
    " Run shell command\n"
    "╌╌╌╌╌╌╌╌\n"
    ' │ scripts/prod-psql --ro "\\! id"\n'
    "╌╌╌╌╌╌╌╌\n"
    " │ Hook PreToolUse:Bash requires confirmation for this command:\n"
    " │ `scripts/prod-psql` destructive or unclassifiable statement\n"
    " settings.json to update hooks\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    " Esc to cancel · Tab to amend\n"
)


def test_pane_parsers() -> None:
    m = _module()
    assert m.ctx_percent(IDLE) == 12
    assert m.ctx_percent("Context: 340k/1M (34%)") == 34
    assert m.ctx_percent("nothing") is None
    assert m.dialog_type(PROMPT) == "permission"
    assert m.dialog_type(IDLE) is None
    # A marker string printed in scrollback (a file, a diff) is not a dialog.
    assert m.dialog_type("re = 'Do you trust the files'\n1. item\n" + IDLE) is None
    question = "Pick one?\n❯ 1. Ship\n  2. Hold\nEnter to select · ↑/↓ to navigate\n"
    assert m.dialog_type(question) == "question"
    assert m.dialog_command(PROMPT) == 'scripts/prod-psql --ro "\\! id"'
    assert m.unsent_input("❯ \n") == ""
    assert m.unsent_input("│ ❯ half a mess │\n") == "half a mess"
    # A dim placeholder hint is not unsent text.
    assert m.unsent_input('❯ \x1b[2mTry "fix lint"\x1b[0m\n') == ""


def test_peek_ctx_prompts(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": IDLE, "claude-b": PROMPT, "other": "x\n"})
    out = _run(env, "peek", "a", "-n", "2").stdout
    assert out.splitlines() == ["--- claude-a", "────", "  12% ctx · opus"]
    # No windows named: every claude-/devin- window, nothing else.
    heads = [ln for ln in _run(env, "peek").stdout.splitlines() if ln.startswith("---")]
    assert heads == ["--- claude-a", "--- claude-b"]
    assert _run(env, "ctx").stdout == "w:claude-a\t12%\n"
    prompts = _run(env, "prompts").stdout.splitlines()
    assert prompts == ['claude-b\tpermission\tscripts/prod-psql --ro "\\! id"']
    for call in _tmux_calls(env):
        assert isinstance(call, list) and call[0] in (
            "list-windows",
            "list-panes",
            "capture-pane",
        )


def test_send_line_types_then_enters(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": IDLE})
    proc = _run(env, "send-line", "a", "rebase and reship", "--pause", "0")
    assert proc.returncode == 0, proc.stderr
    sends = [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    assert sends == [
        ["send-keys", "-t", "claude-a", "-l", "--", "rebase and reship"],
        ["send-keys", "-t", "claude-a", "Enter"],
    ]


def test_send_line_long_text_is_one_paste(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": IDLE})
    text = "line one\nline two; $(not expanded) `nor this`"
    assert _run(env, "send-line", "claude-a", text, "--pause", "0").returncode == 0
    calls = _tmux_calls(env)
    sb = next(c for c in calls if c[0] == "set-buffer")
    assert sb[-1] == text
    assert any(c[0] == "paste-buffer" and "-p" in c for c in calls)


def test_send_line_refuses_unsent_text_and_dialogs(env: dict[str, str]) -> None:
    _windows(
        env,
        {"claude-a": IDLE, "claude-b": PROMPT},
        esc={"claude-a": "out\n❯ \x1b[1mhalf typed\x1b[0m\n"},
    )
    for win in ("a", "b"):
        proc = _run(env, "send-line", win, "hello", "--pause", "0")
        assert proc.returncode == 3
        assert proc.stdout.startswith("REFUSED")
    assert not [c for c in _tmux_calls(env) if c[0] == "send-keys"]


# Devin's mid-compaction banner (verbatim shape from the binary's UI
# strings) — an Enter sent while it is up lands the text but swallows the
# submit (gr515698).
DEVIN_COMPACTING = (
    "⏺ earlier output\n"
    "Compacting conversation history with 142 messages and 198k tokens\n"
    "❭ \n"
)
#: The IDLE pane with our text still sitting in the input box.
UNSENT_HELD = IDLE.replace("❯ \n", "❯ rebase and reship\n")


def _seq(env: dict[str, str], win: str, *texts: str) -> None:
    """Staged captures for the fake tmux: served in call order, last repeats."""
    seq = Path(env["FAKE_TMUX_DIR"]) / f"pane-{win}.seq"
    seq.mkdir()
    for i, text in enumerate(texts):
        (seq / f"{i:02d}.txt").write_text(text, encoding="utf-8")


def test_send_line_waits_out_a_compacting_pane(env: dict[str, str]) -> None:
    _windows(env, {"devin-a": ""})
    _seq(env, "devin-a", DEVIN_COMPACTING, IDLE)
    proc = _run(env, "send-line", "a", "the brief", "--pause", "0", "--poll", "0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "sent: devin-a\n"
    sends = [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    assert sends == [
        ["send-keys", "-t", "devin-a", "-l", "--", "the brief"],
        ["send-keys", "-t", "devin-a", "Enter"],
    ]


def test_send_line_refuses_a_pane_still_compacting(env: dict[str, str]) -> None:
    _windows(env, {"devin-a": DEVIN_COMPACTING})
    proc = _run(env, "send-line", "a", "the brief", "--pause", "0", "--wait", "0")
    assert proc.returncode == 3
    assert "compacting" in proc.stdout
    assert not [c for c in _tmux_calls(env) if c[0] == "send-keys"]


def test_send_line_retries_a_swallowed_enter(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": ""})
    _seq(env, "claude-a", IDLE, UNSENT_HELD, IDLE)
    proc = _run(env, "send-line", "a", "rebase and reship", "--pause", "0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "sent (retried Enter): claude-a\n"
    enters = [
        c for c in _tmux_calls(env) if c[:1] == ["send-keys"] and c[-1] == "Enter"
    ]
    assert len(enters) == 2


def test_send_line_reports_unsubmitted_after_retries(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": ""})
    _seq(env, "claude-a", IDLE, UNSENT_HELD)
    proc = _run(env, "send-line", "a", "hi", "--pause", "0", "--retries", "1")
    assert proc.returncode == 4
    assert "UNVERIFIED" in proc.stderr
    enters = [
        c for c in _tmux_calls(env) if c[:1] == ["send-keys"] and c[-1] == "Enter"
    ]
    assert len(enters) == 2


def test_launch_fills_footer_and_runs_claude_by_argv(
    env: dict[str, str], tmp_path: Path
) -> None:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: fix the thing.\n", encoding="utf-8")
    proc = _run(env, "launch", "fix-x", "--brief", str(brief), "--tier", "qland")
    assert proc.returncode != 0 and "unfilled" in proc.stderr
    env = {**env, "FLEET_COORDINATOR": "coord-1", "FLEET_RELEASE_OWNER": "rel-2",
           "FLEET_APPROVAL": "approved 2026-01-01T00:00Z"}  # fmt: skip
    proc = _run(
        env,
        "--model",
        "sonnet",
        "launch",
        "fix-x",
        "--brief",
        str(brief),
        "--tier",
        "qland",
    )
    assert proc.returncode == 0, proc.stderr
    nw = next(c for c in _tmux_calls(env) if c[0] == "new-window")
    assert nw[nw.index("-n") + 1] == "claude-fix-x"
    argv = json.loads(Path(env["FAKE_CLAUDE_LOG"]).read_text(encoding="utf-8"))
    assert argv[:6] == ["-w", "fix-x", "--model", "sonnet", "--permission-mode", "auto"]
    prompt = argv[6]
    assert prompt.startswith("Task: fix the thing.\n") and "\nRules:" in prompt
    assert "coord-1" in prompt and "rel-2" in prompt
    assert "(version 2), tier W." in prompt and "\nFinish: " in prompt
    assert "GENERATED" not in prompt
    assert "{" not in prompt.replace("{0,", "")
    # qland is an alias for W: the tree exists with W's permissions applied.
    tree = Path(env["FLEET_WORKTREES"]) / "fix-x"
    local = json.loads(
        (tree / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert "Bash(scripts/ship --checked:*)" in local["permissions"]["allow"]
    assert (tree / ".devin" / "config.json").exists()
    assert "codex flags for tier W: --sandbox workspace-write" in proc.stdout


def _devin_dry_run(env: dict[str, str], tmp_path: Path, *global_args: str) -> list[str]:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = {**env, "FLEET_COORDINATOR": "c", "FLEET_RELEASE_OWNER": "r",
           "FLEET_APPROVAL": "approved"}  # fmt: skip
    proc = _run(env, *global_args, "launch", "d-x", "--brief", str(brief),
                "--tier", "qland", "--devin", "--dry-run")  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "devin-tree" in ln)
    return line.split()


def test_devin_launch_passes_model_when_set(
    env: dict[str, str], tmp_path: Path
) -> None:
    argv = _devin_dry_run(env, tmp_path, "--model", "swe-2")
    assert argv[argv.index("--model") + 1] == "swe-2"
    env2 = {**env, "FLEET_MODEL": "swe-3"}
    argv = _devin_dry_run(env2, tmp_path)
    assert argv[argv.index("--model") + 1] == "swe-3"


def test_devin_launch_omits_model_when_unset(
    env: dict[str, str], tmp_path: Path
) -> None:
    assert "--model" not in _devin_dry_run(env, tmp_path)


def test_launch_legacy_footers_behind_env_flag(
    env: dict[str, str], tmp_path: Path
) -> None:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = {**env, "FLEET_LEGACY_FOOTERS": "1", "FLEET_COORDINATOR": "c",
           "FLEET_RELEASE_OWNER": "r", "FLEET_APPROVAL": "a"}  # fmt: skip
    proc = _run(env, "launch", "old-x", "--brief", str(brief), "--tier", "W")
    assert proc.returncode == 0, proc.stderr
    argv = json.loads(Path(env["FAKE_CLAUDE_LOG"]).read_text(encoding="utf-8"))
    assert "standing grant in .claude/fleet/authority.md" in argv[6]
    assert not (Path(env["FLEET_WORKTREES"]) / "old-x").exists()


def test_watch_done_emits_once_per_finish_line(env: dict[str, str]) -> None:
    purpose = Path(env["FLEET_WORKTREES"]) / "t1" / ".claude" / "purpose"
    purpose.parent.mkdir(parents=True)
    purpose.write_text("working on it\n", encoding="utf-8")
    assert _run(env, "watch-done", "t1", "--ticks", "1").stdout == ""
    purpose.write_text("t1 at abc1234: did it\n", encoding="utf-8")
    out = _run(env, "watch-done", "t1", "--ticks", "1").stdout
    assert out.startswith("DONE ") and out.rstrip().endswith("t1 at abc1234: did it")
    assert _run(env, "watch-done", "t1", "--ticks", "1").stdout == ""


def test_stuck_watch_reports_prompt_and_gone(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": IDLE, "claude-b": PROMPT})
    out = _run(env, "stuck-watch", "a", "b", "c", "--ticks", "1").stdout
    lines = out.splitlines()
    assert any("claude-b prompt: scripts/prod-psql" in ln for ln in lines)
    assert any(ln.endswith(" c gone") for ln in lines)
    assert not any("claude-a" in ln for ln in lines)
    # Unchanged state is not re-emitted.
    assert _run(env, "stuck-watch", "a", "b", "c", "--ticks", "1").stdout == ""


def test_close_removes_clean_tree_keeps_dirty(env: dict[str, str]) -> None:
    wt = Path(env["FLEET_WORKTREES"])
    for name in ("done1", "dirty1"):
        _worktree(env, name)
    (wt / "dirty1" / "x.txt").write_text("unsaved\n", encoding="utf-8")
    _windows(env, {"claude-done1": IDLE, "claude-dirty1": IDLE})
    proc = _run(env, "close", "done1", "dirty1")
    assert proc.returncode == 1
    assert "branch deleted: worktree-done1" in proc.stdout
    assert "KEPT (dirty or busy): dirty1" in proc.stdout
    assert not (wt / "done1").exists() and (wt / "dirty1").exists()
    branches = _branches(env)
    assert "worktree-done1" not in branches and "worktree-dirty1" in branches
    log = (Path(env["FLEET_STATE"]) / "dropped-branches.log").read_text(
        encoding="utf-8"
    )
    assert " done1 worktree-done1 " in log and "dirty1" not in log
    kills = [c for c in _tmux_calls(env) if c[0] == "kill-window"]
    assert [c[-1] for c in kills] == ["claude-done1", "claude-dirty1"]


def test_close_refuses_while_a_subagent_task_is_live(env: dict[str, str]) -> None:
    wt = _worktree(env, "subagent-parent")
    _windows(env, {"claude-subagent-parent": IDLE})
    _subagent_event(env, "start", "task-live-close", root=wt)
    try:
        proc = _run(env, "close", "subagent-parent")
        assert proc.returncode == 1
        assert "active subagent task task-live-close" in proc.stdout
        assert wt.exists()
        assert not [c for c in _tmux_calls(env) if c[0] == "kill-window"]
    finally:
        _subagent_event(env, "stop", "task-live-close", root=wt)

    # gr513174: a task scoped to ANOTHER tree must not hold this one — the
    # close goes through and the tree is removed.
    _subagent_event(env, "start", "task-elsewhere")
    try:
        proc = _run(env, "close", "subagent-parent")
        assert "active subagent task" not in proc.stdout
        assert "removed: subagent-parent" in proc.stdout
        assert not wt.exists()
    finally:
        _subagent_event(env, "stop", "task-elsewhere")


def test_close_refuses_unlanded_commit_and_carries_on(env: dict[str, str]) -> None:
    wt = Path(env["FLEET_WORKTREES"])
    wip = _worktree(env, "wip1")
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "unlanded work"],
        cwd=wip,
        check=True,
    )
    _worktree(env, "ok1")  # HEAD == main: a clean close
    _windows(env, {"claude-wip1": IDLE, "claude-ok1": IDLE})
    proc = _run(env, "close", "wip1", "ok1")
    assert proc.returncode == 1
    assert "REFUSED wip1" in proc.stdout and "unlanded work" in proc.stdout
    assert "removed: ok1" in proc.stdout
    assert wip.exists() and not (wt / "ok1").exists()
    branches = _branches(env)
    assert "worktree-wip1" in branches and "worktree-ok1" not in branches
    kills = [c for c in _tmux_calls(env) if c[0] == "kill-window"]
    assert [c[-1] for c in kills] == ["claude-ok1"]


def test_close_squash_landed_tree(env: dict[str, str]) -> None:
    repo = Path(env["CWD"])
    sq = _worktree(env, "sq1")
    (sq / "f.txt").write_text("payload\n", encoding="utf-8")
    for cmd in (["git", "add", "f.txt"], ["git", "commit", "-q", "-m", "squash-me"]):
        subprocess.run(cmd, cwd=sq, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sq,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    # Same patch lands on main under a different sha (a squash-merge;
    # --no-commit because a plain cherry-pick would fast-forward here).
    subprocess.run(["git", "cherry-pick", "-n", sha], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "squashed"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    _windows(env, {"claude-sq1": IDLE})
    proc = _run(env, "close", "sq1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "branch deleted: worktree-sq1" in proc.stdout
    assert not sq.exists() and "worktree-sq1" not in _branches(env)
    kills = [c for c in _tmux_calls(env) if c[0] == "kill-window"]
    assert [c[-1] for c in kills] == ["claude-sq1"]


def test_close_tree_equal_squash_land(env: dict[str, str]) -> None:
    """The gr497896 failure shape: two branch commits whose combined state
    landed on main under different shas — no per-commit patch match and no
    subject match would prove it, but the tree diff against origin/main is
    empty, so close must not refuse."""
    repo = Path(env["CWD"])
    sq = _worktree(env, "sq2")
    (sq / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=sq, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "part one"], cwd=sq, check=True)
    (sq / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-q", "-am", "part two"], cwd=sq, check=True)
    # One squashed commit on main holds the same tree — the way ship lands
    # a multi-commit branch.
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix: squashed"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    _windows(env, {"claude-sq2": IDLE})
    proc = _run(env, "close", "sq2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "branch deleted: worktree-sq2" in proc.stdout
    assert not sq.exists() and "worktree-sq2" not in _branches(env)


def test_close_refuses_subject_only_match(env: dict[str, str]) -> None:
    """gr497896 review: a branch whose commit subject already appears on
    main but whose content is unlanded must still be refused — a subject is
    a hint, not a landed proof."""
    repo = Path(env["CWD"])
    sub = _worktree(env, "subj1")
    (sub / "new-work.txt").write_text("real unlanded work\n", encoding="utf-8")
    subprocess.run(["git", "add", "new-work.txt"], cwd=sub, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix(x): done"], cwd=sub, check=True)
    # A same-subject commit on main carrying DIFFERENT content.
    (repo / "other.txt").write_text("different\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix(x): done"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    _windows(env, {"claude-subj1": IDLE})
    proc = _run(env, "close", "subj1")
    assert proc.returncode == 1
    assert "REFUSED subj1" in proc.stdout
    assert sub.exists() and "worktree-subj1" in _branches(env)


def _coordinator(env: dict[str, str]) -> dict[str, str]:
    """env with the approve guard satisfied: coordinator tree + a user."""
    return {**env, "FLEET_COORDINATOR_TREE": env["CWD"], "USER": "tester"}


def _ledger(env: dict[str, str], tree: str = "t1") -> Path:
    return Path(env["FLEET_STATE"]) / "approvals" / f"{tree}.tsv"


def test_ledger_entries_parse_and_roundtrip(tmp_path: Path) -> None:
    m = _module()
    path = tmp_path / "t.tsv"
    path.write_text(
        "2026-10-15T19:30:00Z\tapprove\tabc,def\tnote here\treto+tty\n"
        "2026-10-15T19:31:00Z\trevoke\tdef\t\treto+s1\n"
        "garbage line\n",
        encoding="utf-8",
    )
    entries = m._ledger_entries(path)
    assert entries == [
        ("2026-10-15T19:30:00Z", "approve", ["abc", "def"], "note here", "reto+tty"),
        ("2026-10-15T19:31:00Z", "revoke", ["def"], "", "reto+s1"),
    ]
    assert m._ledger_entries(tmp_path / "missing.tsv") == []
    assert m._tsv("a\tb\nc") == "a b c"


def test_approve_writes_full_shas_then_approved_passes(env: dict[str, str]) -> None:
    env = _coordinator(env)
    sha1, sha2 = _commit(env, "one"), _commit(env, "two")
    proc = _run(env, "approve", "t1", f"{sha1[:8]},{sha2}", "--note", "tab\tnote")
    assert proc.returncode == 0, proc.stderr
    lines = _ledger(env).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stamp, action, shas, note, approver = lines[0].split("\t")
    assert stamp.endswith("Z") and action == "approve"
    assert shas == f"{sha1},{sha2}"  # short shas resolved to full
    assert note == "tab note"  # tabs cannot break the TSV
    assert approver == "tester+tty"
    ok = _run(env, "approved", "t1", f"{sha1},{sha2[:9]}")
    assert ok.returncode == 0, ok.stdout
    listed = _run(env, "approve", "--list", "t1")
    assert listed.returncode == 0 and lines[0] in listed.stdout


def test_approved_fails_on_partial_list(env: dict[str, str]) -> None:
    env = _coordinator(env)
    sha1, sha2, sha3 = (_commit(env, m) for m in ("one", "two", "three"))
    assert _run(env, "approve", "t1", f"{sha1},{sha2}").returncode == 0
    proc = _run(env, "approved", "t1", f"{sha1},{sha3}")
    assert proc.returncode == 1
    assert proc.stdout == f"missing: {sha3}\n"
    assert _run(env, "approved", "t1", sha1).returncode == 0


def test_revoke_beats_an_earlier_approve(env: dict[str, str]) -> None:
    env = _coordinator(env)
    sha = _commit(env, "one")
    assert _run(env, "approve", "t1", sha).returncode == 0
    assert _run(env, "approve", "--revoke", "t1", sha).returncode == 0
    proc = _run(env, "approved", "t1", sha)
    assert proc.returncode == 1 and proc.stdout == f"revoked: {sha}\n"
    # A later approve wins again; the revoke line was never edited.
    assert _run(env, "approve", "t1", sha).returncode == 0
    assert _run(env, "approved", "t1", sha).returncode == 0
    actions = [
        ln.split("\t")[1]
        for ln in _ledger(env).read_text(encoding="utf-8").splitlines()
    ]
    assert actions == ["approve", "revoke", "approve"]


def test_approve_refuses_without_coordinator_tree_or_known_sha(
    env: dict[str, str], tmp_path: Path
) -> None:
    sha = _commit(env, "one")
    # FLEET_COORDINATOR_TREE unset.
    proc = _run(env, "approve", "t1", sha)
    assert proc.returncode == 3 and "FLEET_COORDINATOR_TREE" in proc.stderr
    # cwd is not the coordinator tree.
    env = _coordinator(env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run(env, "approve", "t1", sha, cwd=str(elsewhere))
    assert proc.returncode == 3 and "coordinator tree" in proc.stderr
    assert (
        _run(env, "approve", "--revoke", "t1", sha, cwd=str(elsewhere)).returncode == 3
    )
    # Unknown shas are refused; nothing was ever written.
    proc = _run(env, "approve", "t1", "0" * 40)
    assert proc.returncode == 2 and "unknown sha" in proc.stderr
    assert not _ledger(env).exists()


# ── spec claims (gr509461) ──────────────────────────────────────────


def _fills(env: dict[str, str]) -> dict[str, str]:
    return {
        **env,
        "FLEET_COORDINATOR": "coord",
        "FLEET_RELEASE_OWNER": "rel",
        "FLEET_APPROVAL": "approved",
    }


def _bare_origin(env: dict[str, str]) -> None:
    """A real (local, bare) origin remote so claim commits exercise push."""
    repo = Path(env["CWD"])
    bare = repo.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)


def _spec(env: dict[str, str], slug: str, status: str = "ready") -> Path:
    """Commit a docs/backlog/<slug>.md spec on main; returns its path."""
    repo = Path(env["CWD"])
    path = repo / "docs" / "backlog" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\ntitle: {slug}\npillar: platform\n---\n\n# {slug}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", str(path.relative_to(repo))], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"spec {slug}"], cwd=repo, check=True)
    pushed = subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=repo, check=False
    )
    if pushed.returncode:
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=repo,
            check=True,
        )
    return path


def _claims(env: dict[str, str]) -> str:
    path = Path(env["FLEET_STATE"]) / "claims.tsv"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _launch(env: dict[str, str], tmp_path: Path, name: str, brief_text: str) -> None:
    _windows(env, {})
    brief = tmp_path / f"{name}.brief"
    brief.write_text(brief_text, encoding="utf-8")
    proc = _run(env, "launch", name, "--brief", str(brief), "--tier", "qland")
    assert proc.returncode == 0, proc.stderr


def test_launch_claims_ready_backlog_spec(env: dict[str, str], tmp_path: Path) -> None:
    env = _fills(env)
    _bare_origin(env)
    spec = _spec(env, "my-spec")
    _launch(env, tmp_path, "b1", "Build docs/backlog/my-spec.md please.\n")
    text = spec.read_text(encoding="utf-8")
    assert "status: in-progress" in text
    assert "claimed-by: b1" in text
    repo = Path(env["CWD"])
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert subject == "chore(backlog): claim my-spec for b1"
    # The claim reached origin/main — an unpushed claim would diverge local
    # main and break scripts/ship's ff.
    remote_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert (
        remote_tip
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
    )
    claims = _claims(env)
    assert "\tclaim\tb1\tdocs/backlog/my-spec.md" in claims
    # Purpose seed: inflight's PURPOSE column shows the claim immediately.
    purpose = (Path(env["FLEET_WORKTREES"]) / "b1" / ".claude" / "purpose").read_text(
        encoding="utf-8"
    )
    assert "my-spec" in purpose


def test_launch_leaves_non_ready_and_dirty_specs_alone(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _fills(env)
    draft = _spec(env, "draft-spec", status="draft")
    dirty = _spec(env, "dirty-spec")
    dirty.write_text(dirty.read_text(encoding="utf-8") + "\nwip\n", encoding="utf-8")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=env["CWD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    _launch(
        env,
        tmp_path,
        "b2",
        "Do docs/backlog/draft-spec.md and docs/backlog/dirty-spec.md.\n",
    )
    assert "status: draft" in draft.read_text(encoding="utf-8")
    assert "status: ready" in dirty.read_text(encoding="utf-8")
    assert _claims(env) == ""
    # No claim commit landed either.
    assert (
        head
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=env["CWD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )


def test_launch_dry_run_claims_nothing(env: dict[str, str], tmp_path: Path) -> None:
    env = _fills(env)
    spec = _spec(env, "dry-spec")
    _windows(env, {})
    brief = tmp_path / "d.brief"
    brief.write_text("Build docs/backlog/dry-spec.md.\n", encoding="utf-8")
    proc = _run(env, "launch", "d1", "--brief", str(brief), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "status: ready" in spec.read_text(encoding="utf-8")
    assert _claims(env) == ""


def test_launch_warns_on_brief_sha_off_main(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _fills(env)
    _windows(env, {})
    repo = Path(env["CWD"])
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=repo, check=True)
    side = _commit(env, "side work")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    brief = tmp_path / "s.brief"
    brief.write_text(f"Inspect {side[:9]} on the side branch.\n", encoding="utf-8")
    proc = _run(env, "launch", "s1", "--brief", str(brief), "--tier", "qland")
    assert proc.returncode == 0, proc.stderr
    assert side[:9] in proc.stderr and "not on origin/main" in proc.stderr


def test_launch_quiet_for_landed_or_unknown_sha(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _fills(env)
    _windows(env, {})
    repo = Path(env["CWD"])
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    brief = tmp_path / "m.brief"
    brief.write_text(
        f"See {head[:9]} and the unresolvable deadbeef0 token.\n",
        encoding="utf-8",
    )
    proc = _run(env, "launch", "m1", "--brief", str(brief), "--tier", "qland")
    assert proc.returncode == 0, proc.stderr
    assert "origin/main" not in proc.stderr


def test_close_releases_claimed_spec(env: dict[str, str], tmp_path: Path) -> None:
    env = _fills(env)
    _bare_origin(env)
    spec = _spec(env, "rel-spec")
    _launch(env, tmp_path, "w1", "Build docs/backlog/rel-spec.md.\n")
    assert "in-progress" in spec.read_text(encoding="utf-8")
    _windows(env, {"claude-w1": IDLE})
    proc = _run(env, "close", "w1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = spec.read_text(encoding="utf-8")
    assert "status: ready" in text
    assert "claimed-by" not in text
    assert "\trelease\tw1\tdocs/backlog/rel-spec.md" in _claims(env)


def test_close_on_gone_tree_releases_its_claim(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The abandoned/reaped-unmerged path: the tree is already gone when
    close runs — the spec must still be freed."""
    env = _fills(env)
    _bare_origin(env)
    spec = _spec(env, "gone-spec")
    _launch(env, tmp_path, "w2", "Build docs/backlog/gone-spec.md.\n")
    tree = Path(env["FLEET_WORKTREES"]) / "w2"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(tree)],
        cwd=env["CWD"],
        check=True,
    )
    subprocess.run(["git", "branch", "-D", "worktree-w2"], cwd=env["CWD"], check=True)
    _windows(env, {"claude-w2": IDLE})
    proc = _run(env, "close", "w2")
    assert "no tree: w2" in proc.stdout
    text = spec.read_text(encoding="utf-8")
    assert "status: ready" in text
    assert "claimed-by" not in text
    assert "\trelease\tw2\tdocs/backlog/gone-spec.md" in _claims(env)
