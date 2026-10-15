"""scripts/fleet-wave — the builder-wave coordinator kit, against a fake tmux.

Each test runs the real script in a throwaway git repo with a fake ``tmux``
(and, for launch, a fake ``claude``) first on PATH. The fake tmux logs its
argv as JSON lines and serves pane text from files, so the tests also pin
that tmux only ever gets an argument list.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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
    ext = ".esc" if "-e" in argv else ".txt"
    p = os.path.join(d, "pane-" + target() + ext)
    if not os.path.exists(p) and ext == ".esc":
        p = os.path.join(d, "pane-" + target() + ".txt")
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
    loader = importlib.machinery.SourceFileLoader("fleet_wave", str(SCRIPT))
    spec = importlib.util.spec_from_loader("fleet_wave", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        # close checks landedness against origin/main; there is no real
        # remote, so the ref is set by hand (fetch just fails silently).
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    shutil.copytree(
        REPO / ".claude" / "fleet" / "footers", repo / ".claude" / "fleet" / "footers"
    )
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
    assert prompt.startswith("Task: fix the thing.\nRules:")
    assert "coord-1" in prompt and "rel-2" in prompt
    assert "standing grant in .claude/fleet/authority.md" in prompt
    assert "{" not in prompt.replace("{0,", "")


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
