"""scripts/fleet-wave — the builder-wave coordinator kit, against a fake tmux.

Each test runs the real script in a throwaway git repo with a fake ``tmux``
(and, for launch, a fake ``claude``) first on PATH. The fake tmux logs its
argv as JSON lines and serves pane text from files, so the tests also pin
that tmux only ever gets an argument list.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
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
elif cmd == "display-message":
    t = target()
    p = os.path.join(d, "facts-" + t)
    sys.stdout.write(
        open(p, encoding="utf-8").read() if os.path.exists(p) else "unknown\\t0\\n"
    )
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


# Devin's footer (layout from tests/fixtures/panes/devin-working.txt).
DEVIN_CTX76 = (
    "⏺ earlier output\n"
    "❭ \n"
    "SWE-2 High                                     Context: 200k / 262k tokens (76%)\n"
)


def test_ctx_under_a_session_targets_pane_ids(env: dict[str, str]) -> None:
    """gr515267: `fleet-wave ctx` printed nothing under FLEET_TMUX_SESSION —
    the pane id was wrapped in `=session:` and tmux looked for a window
    literally named %N. Pane ids are global targets and must go bare."""
    _windows(env, {"devin-d": DEVIN_CTX76, "claude-a": IDLE})
    env = {**env, "FLEET_TMUX_SESSION": "w"}
    out = _run(env, "ctx").stdout
    assert "w:devin-d\t76%\n" in out
    assert "w:claude-a\t12%\n" in out
    targets = [c[c.index("-t") + 1] for c in _tmux_calls(env) if c[0] == "capture-pane"]
    assert targets and all(t.startswith("%") for t in targets)


def _ctx_guard_marker(env: dict[str, str], name: str) -> Path:
    return Path(env["FLEET_STATE"]) / "ctx-guard" / name


def test_ctx_guard_nudges_once_then_rearms(env: dict[str, str]) -> None:
    _windows(env, {"devin-g": DEVIN_CTX76})
    args = ("ctx-guard", "g", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    proc = _run(env, *args)
    assert proc.returncode == 0
    assert "ctx-guard: devin-g at 76% — sent WIP+handoff" in proc.stdout
    sent = [c for c in _tmux_calls(env) if c[0] == "set-buffer"]
    assert sent and "context ceiling" in sent[0][-1]
    marker = _ctx_guard_marker(env, "g")
    assert marker.exists()
    # A second poll at the same level does not resend.
    proc = _run(env, *args)
    assert proc.stdout == ""
    assert len([c for c in _tmux_calls(env) if c[0] == "send-keys"]) == 1
    # Below --rearm the marker clears (the post-/clear state).
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "pane-devin-g.txt").write_text(
        DEVIN_CTX76.replace("(76%)", "(12%)"), encoding="utf-8"
    )
    assert _run(env, *args).stdout == ""
    assert not marker.exists()
    # Back at the ceiling it is armed again and nudges a second time.
    (d / "pane-devin-g.txt").write_text(DEVIN_CTX76, encoding="utf-8")
    proc = _run(env, *args)
    assert "sent WIP+handoff" in proc.stdout and marker.exists()


def test_ctx_guard_refusal_never_marks(env: dict[str, str]) -> None:
    _windows(env, {"devin-g": PROMPT + "  81% ctx · opus\n"})
    proc = _run(
        env,
        "ctx-guard",
        "g",
        "--ticks",
        "1",
        "--wait",
        "0",
        "--poll",
        "0",
        "--pause",
        "0",
    )
    assert "send refused (a dialog is open" in proc.stdout
    assert not _ctx_guard_marker(env, "g").exists()
    assert not [c for c in _tmux_calls(env) if c[0] == "send-keys"]


def test_ctx_guard_repeats_a_refusal_only_when_the_reason_changes(
    env: dict[str, str],
) -> None:
    """gr515152: a refused send retried (and printed) every poll."""
    _windows(env, {"devin-g": PROMPT + "  81% ctx · opus\n"})
    proc = _run(env, "ctx-guard", "g", "--ticks", "3", "--interval", "0",
                "--wait", "0", "--poll", "0", "--pause", "0")  # fmt: skip
    assert proc.stdout.count("send refused") == 1


def test_ctx_guard_compact_sends_slash_compact(env: dict[str, str]) -> None:
    """gr515153: the orchestrator main loops are compacted early; --compact
    re-arms at half the ceiling, not at the builders' 40%."""
    _windows(env, {"coordinator": DEVIN_CTX76})
    args = ("ctx-guard", "coordinator", "--compact", "--ceiling", "60",
            "--ticks", "1", "--wait", "0", "--poll", "0", "--pause", "0")  # fmt: skip
    proc = _run(env, *args)
    assert "at 76% — sent /compact" in proc.stdout
    sends = [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    assert sends[0][-1] == "/compact"
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "pane-coordinator.txt").write_text(
        DEVIN_CTX76.replace("(76%)", "(35%)"), encoding="utf-8"
    )
    assert _run(env, *args).stdout == ""
    assert _ctx_guard_marker(env, "coordinator").exists()  # 35 >= 30: not re-armed
    (d / "pane-coordinator.txt").write_text(
        DEVIN_CTX76.replace("(76%)", "(12%)"), encoding="utf-8"
    )
    assert _run(env, *args).stdout == ""
    assert not _ctx_guard_marker(env, "coordinator").exists()


#: A Claude idle pane prints the absolute context size in the /clear hint;
#: a busy one carries it in the spinner (layouts: tests/fixtures/panes/).
CLAUDE_TOK196 = (
    "⏺ done 6:38 PM\n"
    "                new task? /clear to save 196.5k tokens\n"
    "────\n❯ \n────\n  Sonnet 5.5  20% ctx\n"
)
CLAUDE_TOK80 = CLAUDE_TOK196.replace("196.5k", "80k")
CLAUDE_BUSY_TOK240 = (
    "⏺ working\n✻ Churning… (47s · esc to interrupt · 240.1k tokens)\n"
    "────\n❯ \n────\n  Opus 5.5  43% ctx\n"
)


def test_ctx_tokens_reads_the_absolute_size() -> None:
    m = _module()
    assert m.ctx_tokens(DEVIN_CTX76) == 200_000
    assert m.ctx_tokens("Context: 340k/1M (34%)") == 340_000
    assert m.ctx_tokens(CLAUDE_TOK196) == 196_500
    assert m.ctx_tokens(CLAUDE_BUSY_TOK240) == 240_100
    # Bottom-most figure wins, same rule as ctx_percent.
    pane = DEVIN_CTX76 + "new task? /clear to save 150k tokens\n"
    assert m.ctx_tokens(pane) == 150_000
    # A bare % footer is unjudgeable by a token threshold.
    assert m.ctx_tokens("  20% ctx · opus\n") is None
    assert m.ctx_tokens("nothing") is None


def test_ctx_guard_tokens_compacts_an_idle_peer(env: dict[str, str]) -> None:
    """gr515153: a peer main loop past ~120k tokens is compacted at idle."""
    _windows(env, {"chat": CLAUDE_TOK196})
    args = ("ctx-guard", "chat", "--tokens", "120k", "--ticks", "1",
            "--wait", "0", "--poll", "0", "--pause", "0")  # fmt: skip
    proc = _run(env, *args)
    assert "at 196.5k tokens — sent /compact" in proc.stdout
    sends = [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    assert sends[0][-1] == "/compact"
    assert _ctx_guard_marker(env, "chat").exists()


def test_ctx_guard_tokens_skips_a_busy_pane(env: dict[str, str]) -> None:
    """gr515153: never mid-turn — a busy pane is skipped to the next poll,
    not queued a /compact."""
    _windows(env, {"chat": CLAUDE_BUSY_TOK240})
    args = ("ctx-guard", "chat", "--tokens", "--ticks", "1",
            "--wait", "0", "--poll", "0", "--pause", "0")  # fmt: skip
    proc = _run(env, *args)
    assert proc.stdout == ""
    assert not _ctx_guard_marker(env, "chat").exists()
    assert not [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    # Bare --tokens defaults to 120k; once idle the same pane compacts.
    _windows(env, {"chat": CLAUDE_TOK196})
    assert "sent /compact" in _run(env, *args).stdout


def test_ctx_guard_tokens_leaves_small_and_pct_only_panes(
    env: dict[str, str],
) -> None:
    _windows(env, {"chat": CLAUDE_TOK80, "pct-only": IDLE})
    proc = _run(env, "ctx-guard", "chat", "pct-only", "--tokens",
                "--ticks", "1", "--wait", "0", "--poll", "0",
                "--pause", "0")  # fmt: skip
    assert proc.stdout == ""
    assert not _ctx_guard_marker(env, "chat").exists()
    assert not [c for c in _tmux_calls(env) if c[0] == "send-keys"]


def test_ctx_guard_tokens_env_sets_the_threshold(env: dict[str, str]) -> None:
    """FLEET_COMPACT_TOKENS tunes the ~120k default without a flag."""
    env = {**env, "FLEET_COMPACT_TOKENS": "150k"}
    _windows(env, {"chat": CLAUDE_TOK196})
    args = ("ctx-guard", "chat", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    assert "sent /compact" in _run(env, *args).stdout
    _windows(env, {"chat": CLAUDE_TOK80.replace("80k", "140k")})
    # 140k < 150k: under the env threshold it stays armed and silent.
    assert _run(env, *args).stdout == ""


def test_token_amount_parses_suffixes() -> None:
    m = _module()
    assert m._token_amount("120k") == 120_000
    assert m._token_amount("1.5M") == 1_500_000
    assert m._token_amount("96000") == 96_000
    with pytest.raises(SystemExit):
        m._token_amount("lots")


def test_ctx_guard_message_fills_name(env: dict[str, str]) -> None:
    _windows(env, {"devin-g": DEVIN_CTX76})
    proc = _run(
        env,
        "ctx-guard",
        "g",
        "--ticks",
        "1",
        "--wait",
        "0",
        "--poll",
        "0",
        "--pause",
        "0",
        "--message",
        "stop {name} now",
    )
    assert "sent WIP+handoff" in proc.stdout
    sends = [c for c in _tmux_calls(env) if c[0] == "send-keys"]
    assert sends[0][-1] == "stop g now"


# ── the hard ctx ceiling (gr515267) ─────────────────────────────────
#: A Claude footer past the refuse line; Devin's own footer shapes.
HOT = "some output\n\n────\n❯ \n────\n  92% ctx · opus\n"
DEVIN_HOT = "⏺ still going\nContext: 240k / 262k tokens (92%)\n❭ \n"
DEVIN_COOL = "⏺ still going\nContext: 20k / 262k tokens (8%)\n❭ \n"
DEVIN_WARM_UNSENT = "⏺ still going\nContext: 196k / 262k tokens (75%)\n❭ half typed\n"


def _typed(env: dict[str, str]) -> list[list[str]]:
    """tmux calls that put text into a box (set-buffer paste or -l type)."""
    return [
        c
        for c in _tmux_calls(env)
        if c[0] == "set-buffer" or (c[0] == "send-keys" and "-l" in c)
    ]


def test_send_line_refuses_fresh_work_past_the_hard_ceiling(
    env: dict[str, str],
) -> None:
    _windows(env, {"claude-a": HOT})
    proc = _run(env, "send-line", "a", "keep building", "--pause", "0")
    assert proc.returncode == 3
    assert proc.stdout.startswith("REFUSED claude-a: 92% ctx")
    # The refusal names the way through — the handoff goes with --force.
    assert "--force" in proc.stdout
    assert not _typed(env)
    # Slash commands are the remediation — /compact and /clear must pass.
    assert _run(env, "send-line", "a", "/compact", "--pause", "0").returncode == 0
    proc = _run(env, "send-line", "a", "the handoff", "--force", "--pause", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    texts = [c[-1] for c in _typed(env)]
    assert "/compact" in texts and "the handoff" in texts


def test_ctx_guard_forced_handoff_at_the_hard_ceiling(
    env: dict[str, str],
) -> None:
    _windows(env, {"devin-a": DEVIN_HOT})
    args = ("ctx-guard", "a", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    out = _run(env, *args).stdout
    # A straight-to-hard crossing still delivers the instruction, then
    # flags the episode to the coordinator.
    assert "at 92% — sent WIP+handoff" in out
    assert "at 92% — FORCED HANDOFF" in out
    assert _typed(env)
    assert _ctx_guard_marker(env, "a").exists()
    assert _ctx_guard_marker(env, "a.hard").exists()
    # Both markers dedupe: a second poll at the same level prints nothing.
    assert _run(env, *args).stdout == ""
    assert len([c for c in _tmux_calls(env) if c[0] == "send-keys"]) == 1


def test_ctx_guard_rearms_the_hard_band_below_rearm(
    env: dict[str, str],
) -> None:
    _windows(env, {"devin-a": DEVIN_HOT})
    args = ("ctx-guard", "a", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    _run(env, *args)
    # A /clear drops the footer below --rearm: both markers clear.
    _windows(env, {"devin-a": DEVIN_COOL})
    assert _run(env, *args).stdout == ""
    assert not _ctx_guard_marker(env, "a").exists()
    assert not _ctx_guard_marker(env, "a.hard").exists()
    # The next episode nudges and forces the handoff again.
    _windows(env, {"devin-a": DEVIN_HOT})
    out = _run(env, *args).stdout
    assert "sent WIP+handoff" in out and "FORCED HANDOFF" in out


def test_ctx_guard_retries_a_busy_input_box(env: dict[str, str]) -> None:
    _windows(env, {"devin-a": DEVIN_WARM_UNSENT})
    args = ("ctx-guard", "a", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    proc = _run(env, *args)
    assert "send refused (input box holds unsent text" in proc.stdout
    assert not _ctx_guard_marker(env, "a").exists()
    assert not _typed(env)
    # Unsent text cleared: the next tick delivers the instruction.
    _windows(env, {"devin-a": DEVIN_CTX76})
    assert "sent WIP+handoff" in _run(env, *args).stdout


def test_ctx_guard_counts_unverified_enter_as_sent(env: dict[str, str]) -> None:
    """rc 4 (text typed, Enter unconfirmed) must mark the pane handled."""
    _windows(env, {"devin-a": ""})
    # Guard read, send pre-check, then the box keeps holding the text.
    _seq(env, "devin-a", DEVIN_CTX76, DEVIN_CTX76, DEVIN_WARM_UNSENT)
    args = ("ctx-guard", "a", "--ticks", "1", "--wait", "0",
            "--poll", "0", "--pause", "0")  # fmt: skip
    out = _run(env, *args).stdout
    assert "sent WIP+handoff, UNVERIFIED" in out
    assert _ctx_guard_marker(env, "a").exists()
    # Handled: the next poll neither resends nor nags.
    n_typed = len(_typed(env))
    assert _run(env, *args).stdout == ""
    assert len(_typed(env)) == n_typed


def test_ctx_guard_refuses_rearm_at_or_above_ceiling(env: dict[str, str]) -> None:
    _windows(env, {"devin-a": DEVIN_CTX76})
    proc = _run(env, "ctx-guard", "a", "--ticks", "1", "--rearm", "80",
                "--ceiling", "70")  # fmt: skip
    assert proc.returncode == 2
    assert "--rearm (80) must be below the trigger (70)" in proc.stderr
    assert not _typed(env)


def test_send_line_namespace_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-code callers (reassign, handoff-wait) build a Namespace with no
    ``force`` attribute; cmd_send_line must default it to False."""
    import argparse
    from types import SimpleNamespace

    m = _module()
    seen: dict[str, object] = {}

    def fake_send_text(*_a: object, **kw: object) -> tuple[int, str]:
        seen.update(kw)
        return 0, "sent"

    monkeypatch.setattr(m, "_send_text", fake_send_text)
    fleet = SimpleNamespace(resolve=lambda n: f"claude-{n}")
    ns = argparse.Namespace(
        win="a", text="hi", wait=0.0, poll=0.0, retries=0, pause=0.0
    )
    assert m.cmd_send_line(fleet, ns) == 0
    assert seen["refuse_ctx"] == m.CTX_HARD


def test_ctx_percent_prefers_the_bottom_most_footer() -> None:
    m = _module()
    pane = (
        "SWE-2 High      Context: 200k / 262k tokens (76%)\n"
        "● editing the fixture above\n"
        "────\n❯ \n────\n  12% ctx · opus\n"
    )
    assert m.ctx_percent(pane) == 12
    # And the reverse: a Devin footer at the bottom beats Claude text above.
    assert m.ctx_percent("  12% ctx · opus\n" + DEVIN_CTX76) == 76


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


def _devin_dry_run(
    env: dict[str, str],
    tmp_path: Path,
    *global_args: str,
    launch_args: tuple[str, ...] = (),
) -> list[str]:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = {**env, "FLEET_COORDINATOR": "c", "FLEET_RELEASE_OWNER": "r",
           "FLEET_APPROVAL": "approved"}  # fmt: skip
    proc = _run(env, *global_args, "launch", "d-x", "--brief", str(brief),
                "--tier", "qland", "--devin", *launch_args, "--dry-run")  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "devin-tree" in ln)
    return line.split()


def _fake_devin_tree(env: dict[str, str], tmp_path: Path) -> Path:
    """A stub ``<repo>/scripts/devin-tree`` that logs its argv and exits 0;
    returns the file the argv lands in."""
    log = tmp_path / "devin-tree.json"
    exe = Path(env["CWD"]) / "scripts" / "devin-tree"
    exe.parent.mkdir()
    exe.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'w', encoding='utf-8').write(\n"
        "    json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return log


def _devin_launch_fills(env: dict[str, str]) -> dict[str, str]:
    return {**env, "FLEET_COORDINATOR": "c", "FLEET_RELEASE_OWNER": "r",
            "FLEET_APPROVAL": "approved"}  # fmt: skip


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


def test_devin_model_wins_over_model_and_fleet_model(
    env: dict[str, str], tmp_path: Path
) -> None:
    argv = _devin_dry_run(
        env,
        tmp_path,
        "--model",
        "swe-2",
        launch_args=("--devin-model", "gemini-3-8-flash-high"),
    )
    assert argv[argv.index("--model") + 1] == "gemini-3-8-flash-high"
    env2 = {**env, "FLEET_MODEL": "swe-3"}
    argv = _devin_dry_run(env2, tmp_path, launch_args=("--devin-model", "swe-2-high"))
    assert argv[argv.index("--model") + 1] == "swe-2-high"


def test_devin_model_requires_devin(env: dict[str, str], tmp_path: Path) -> None:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = _devin_launch_fills(env)
    for extra in ((), ("--codex",)):
        proc = _run(env, "launch", "x", "--brief", str(brief), "--tier",
                    "qland", "--devin-model", "swe-2", *extra)  # fmt: skip
        assert proc.returncode == 2 and "--devin" in proc.stderr


def test_devin_model_rejects_a_bad_id(env: dict[str, str], tmp_path: Path) -> None:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = _devin_launch_fills(env)
    proc = _run(env, "launch", "x", "--brief", str(brief), "--tier", "qland",
                "--devin", "--devin-model", "bad id!")  # fmt: skip
    assert proc.returncode == 2 and "bad --devin-model" in proc.stderr


def test_devin_model_non_swe_warns_it_bills_per_token(
    env: dict[str, str], tmp_path: Path
) -> None:
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = _devin_launch_fills(env)
    for model, warns in (("gemini-3-8-flash-high", True), ("swe-2-high", False)):
        proc = _run(env, "launch", "d-x", "--brief", str(brief), "--tier",
                    "qland", "--devin", "--devin-model", model, "--dry-run")  # fmt: skip
        assert proc.returncode == 0, proc.stderr
        line = f"devin model {model} is billed per token"
        assert (line in proc.stdout) is warns, proc.stdout


def test_devin_launch_records_the_model_and_prints_the_lane(
    env: dict[str, str], tmp_path: Path
) -> None:
    log = _fake_devin_tree(env, tmp_path)
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = _devin_launch_fills(env)
    proc = _run(env, "launch", "d-x", "--brief", str(brief), "--tier",
                "qland", "--devin", "--devin-model", "gemini-3-8-flash-high")  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "gemini-3-8-flash-high"
    assert "launched devin-d-x (model gemini-3-8-flash-high, tier W" in proc.stdout
    lane = next(ln for ln in proc.stdout.splitlines() if ln.startswith("lane:"))
    assert lane.startswith("lane: devin:devin-d-x@")
    assert lane.endswith(", tree d-x, model gemini-3-8-flash-high")
    rec = Path(env["FLEET_STATE"]) / "devin-d-x.model"
    assert rec.read_text(encoding="utf-8").split() == ["gemini-3-8-flash-high"]


def test_devin_launch_without_a_model_drops_a_stale_record(
    env: dict[str, str], tmp_path: Path
) -> None:
    log = _fake_devin_tree(env, tmp_path)
    st = Path(env["FLEET_STATE"])
    st.mkdir(parents=True, exist_ok=True)
    (st / "devin-d-x.model").write_text("swe-2\n", encoding="utf-8")
    _windows(env, {})
    brief = tmp_path / "brief.txt"
    brief.write_text("Task: x.\n", encoding="utf-8")
    env = _devin_launch_fills(env)
    proc = _run(env, "launch", "d-x", "--brief", str(brief), "--tier",
                "qland", "--devin")  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert "--model" not in json.loads(log.read_text(encoding="utf-8"))
    assert "model default" in proc.stdout
    assert not (st / "devin-d-x.model").exists()


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


_STUCK = ("stuck-watch", "--interval", "0")


def test_stuck_watch_reports_prompt_and_gone(env: dict[str, str]) -> None:
    _windows(env, {"claude-a": IDLE, "claude-b": PROMPT})
    out = _run(env, *_STUCK, "a", "b", "c", "--ticks", "2").stdout
    lines = out.splitlines()
    assert any("claude-b prompt: scripts/prod-psql" in ln for ln in lines)
    assert any(ln.endswith(" c gone") for ln in lines)
    assert not any("claude-a" in ln for ln in lines)
    # Unchanged state is not re-emitted.
    assert _run(env, *_STUCK, "a", "b", "c", "--ticks", "2").stdout == ""


def test_stuck_watch_ignores_a_blip_and_coalesces_the_rest(
    env: dict[str, str],
) -> None:
    """gr515152: a state must hold --settle polls, and the non-urgent lines
    of one poll leave in a single write."""
    _windows(env, {"claude-a": PROMPT})
    # One poll of a prompt is a blip (an auto-approved dialog): silent.
    assert _run(env, *_STUCK, "a", "--ticks", "1").stdout == ""
    # Two windows gone at once: one write carrying both lines.
    proc = _run(env, *_STUCK, "x", "y", "--ticks", "2")
    assert [ln.rsplit(" ", 1)[-1] for ln in proc.stdout.splitlines()] == [
        "gone",
        "gone",
    ]


def test_stuck_watch_holds_non_urgent_until_a_prompt_flushes_it(
    env: dict[str, str],
) -> None:
    m = _module()
    d = m._Digest(3600.0)
    seen: list[str] = []
    d.add("a", "quiet line", False, lambda: seen.append("a"))
    d.flush()
    assert seen == []  # held: not due, not urgent
    d.add("b", "prompt line", True, lambda: seen.append("b"))
    d.add("a", "quiet line again", False, lambda: seen.append("a2"))
    d.flush()
    assert sorted(seen) == ["a2", "b"]  # urgent flushed both; a was replaced
    d.add("c", "late", False, lambda: seen.append("c"))
    d.drop("c")
    d.flush(final=True)
    assert "c" not in seen


def test_stuck_watch_reports_a_quiet_pane_once(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr515152: quiet fired every 10 min (quiet20m, quiet30m, ...); it now
    keys on --requiet-min buckets."""
    m = _module()
    _windows(env, {"claude-q": IDLE})
    for k in ("PATH", "FLEET_STATE", "FAKE_TMUX_DIR", "FLEET_WORKTREES"):
        monkeypatch.setenv(k, env[k])
    monkeypatch.chdir(env["CWD"])
    fleet = m.Fleet(m.parser().parse_args(["peek"]))
    st = fleet.state / "stuck-seen"
    st.mkdir(parents=True)
    st.joinpath("q.hash").write_text(
        m.hashlib.sha256(IDLE.encode()).hexdigest()[:12], encoding="utf-8"
    )
    now = int(m.time.time())
    keys = []
    for quiet_for in (25 * 60, 35 * 60, 59 * 60, 61 * 60):
        st.joinpath("q.t").write_text(str(now - quiet_for), encoding="utf-8")
        keys.append(m._stuck_state(fleet, "q", "claude-q", 20 * 60, 60 * 60)[0])
    assert keys == ["quiet#0", "quiet#0", "quiet#0", "quiet#1"]


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


def test_close_ignores_a_subagent_of_the_session_that_locks_the_tree(
    env: dict[str, str],
) -> None:
    """gr573220: a finished builder tree handed to the coordinator keeps a
    plain ``pid <P>`` lock naming the coordinator's session pid — and every
    subagent marker that session writes carries the same pid. The pid
    clause exists for the harness's agent locks (``claude agent <id> (pid
    N)``); a plain ``pid N`` lock must never scope a marker to the tree,
    or the coordinator's own live subagents freeze every handed-off tree.
    """
    wt = _worktree(env, "handed-off")
    _windows(env, {"claude-handed-off": IDLE})
    subprocess.run(
        ["git", "worktree", "lock", str(wt), "--reason", f"pid {os.getpid()}"],
        cwd=env["CWD"],
        check=True,
    )
    # A coordinator-session marker written directly: its cwd is the
    # coordinator's tree (the repo root here), never the builder tree.
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=env["CWD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    state = Path(common) / "precis-agent-tasks"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = state / "coord-task.json"
    marker.write_text(
        json.dumps(
            {
                "agent_id": "coord-task",
                "cwd": env["CWD"],
                "pid": os.getpid(),
                "started_at": datetime.now(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    try:
        proc = _run(env, "close", "handed-off")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "removed: handed-off" in proc.stdout
        assert "active subagent task" not in proc.stdout
        assert not wt.exists()
    finally:
        marker.unlink(missing_ok=True)
        subprocess.run(
            ["git", "worktree", "unlock", str(wt)], cwd=env["CWD"], check=False
        )


def _ship_lock(env: dict[str, str], wt: Path, pid: int) -> Path:
    """Stand scripts/ship's per-worktree lock inside <wt> held by <pid>:
    <absolute-git-dir>/precis-ship-worktree.lock.d/holder, the
    scripts/lib/ship-lock.sh + lock-holder.sh format."""
    gitdir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=wt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    lockdir = Path(gitdir) / "precis-ship-worktree.lock.d"
    lockdir.mkdir(parents=True)
    (lockdir / "holder").write_text(f"{wt} pid={pid} host=test\n", encoding="utf-8")
    return lockdir


def test_close_refuses_a_tree_with_a_running_ship(env: dict[str, str]) -> None:
    """A ship running inside a builder tree rewrites its branch, index and
    origin/main mid-close — the tree (and its window) stays until the
    ship exits."""
    wt = _worktree(env, "shipping")
    _windows(env, {"claude-shipping": IDLE})
    _ship_lock(env, wt, os.getpid())
    proc = _run(env, "close", "shipping")
    assert proc.returncode == 1
    assert (
        f"REFUSED shipping: ship running (pid {os.getpid()}); tree kept" in proc.stdout
    )
    assert wt.exists()
    assert not [c for c in _tmux_calls(env) if c[0] == "kill-window"]


def test_close_ignores_a_stale_or_missing_ship_lock(env: dict[str, str]) -> None:
    """A dead holder pid (a crashed ship) and no lock at all are no guard."""
    wt = _worktree(env, "stale-ship")
    free = _worktree(env, "no-ship")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _ship_lock(env, wt, dead.pid)
    _windows(env, {"claude-stale-ship": IDLE, "claude-no-ship": IDLE})
    proc = _run(env, "close", "stale-ship", "no-ship")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "removed: stale-ship" in proc.stdout
    assert "removed: no-ship" in proc.stdout
    assert "ship running" not in proc.stdout
    assert not wt.exists() and not free.exists()


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


# ── launch --batch: the brief comes from the gripes (gr519361) ─────────

FAKE_GRIPE_FIX = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GRIPE_FIX_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[0] == "brief":
    out = args[args.index("--out") + 1]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("# batch " + args[1] + " — 2 gripes\\n\\n## brief (all)\\nTask text.\\n")
print("ok")
"""


def _with_gripe_fix(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    exe = tmp_path / "bin" / "fake-gripe-fix"
    exe.write_text(FAKE_GRIPE_FIX, encoding="utf-8")
    exe.chmod(0o755)
    return {
        **env,
        "FLEET_GRIPE_FIX_CMD": str(exe),
        "FAKE_GRIPE_FIX_LOG": str(tmp_path / "gripe-fix.jsonl"),
        "FLEET_COORDINATOR": "coord-1",
        "FLEET_RELEASE_OWNER": "rel-2",
        "FLEET_APPROVAL": "approved 2026-01-01T00:00Z",
    }


def test_launch_batch_renders_brief_and_assigns(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {})
    proc = _run(env, "launch", "draft-reads", "--batch", "draft-reads", "--tier", "R")
    assert proc.returncode == 0, proc.stderr
    log = Path(env["FAKE_GRIPE_FIX_LOG"]).read_text(encoding="utf-8")
    calls = [json.loads(ln) for ln in log.splitlines()]
    assert calls[0][:2] == ["brief", "draft-reads"]
    assert calls[0][-1].endswith("claude-draft-reads.brief.md")
    assign = calls[1]
    assert assign[:2] == ["assign", "draft-reads"]
    assert assign[assign.index("--session") + 1] == "claude:claude-draft-reads"
    assert assign[assign.index("--tree") + 1] == "draft-reads"
    assert "--host" in assign
    argv = json.loads(Path(env["FAKE_CLAUDE_LOG"]).read_text(encoding="utf-8"))
    assert argv[6].startswith("# batch draft-reads")
    assert "assigned batch draft-reads" in proc.stdout


def test_launch_needs_exactly_one_of_brief_and_batch(
    env: dict[str, str], tmp_path: Path
) -> None:
    _windows(env, {})
    proc = _run(env, "launch", "x")
    assert proc.returncode == 2 and "exactly one" in proc.stderr
    brief = tmp_path / "b.txt"
    brief.write_text("t\n", encoding="utf-8")
    proc = _run(env, "launch", "x", "--brief", str(brief), "--batch", "x")
    assert proc.returncode == 2 and "exactly one" in proc.stderr


def test_launch_batch_dry_run_prints_the_commands(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {})
    proc = _run(env, "launch", "d-y", "--batch", "wave-1", "--devin", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "brief wave-1 --out" in proc.stdout
    assert "assign wave-1 --host <host> --session devin:devin-d-y --tree d-y" in (
        proc.stdout
    )
    assert not Path(env["FAKE_GRIPE_FIX_LOG"]).exists()


def _pane_alive(env: dict[str, str], win: str, text: str = IDLE) -> None:
    """A pane the fake tmux can capture, without listing the window (so a
    launch does not see it as already existing)."""
    (Path(env["FAKE_TMUX_DIR"]) / f"pane-{win}.txt").write_text(text, encoding="utf-8")


def test_launch_codex_tier_w_uses_the_tier_flags_verbatim(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {})
    _pane_alive(env, "codex-castor-s2")
    proc = _run(
        env, "launch", "castor-s2", "--batch", "castor-s2", "--codex",
        "--tier", "W", "--settle", "0",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    nw = next(c for c in _tmux_calls(env) if c[0] == "new-window")
    assert nw[nw.index("-n") + 1] == "codex-castor-s2"
    # exactly scripts/fleet-authority's tier-W codex flags, nothing appended
    assert nw[-1].startswith(
        "codex --sandbox workspace-write --ask-for-approval never 'Read "
    )
    assert nw[-1].count("--ask-for-approval") == 1
    assert "scratch/codex-brief.md" in nw[-1]
    tree = Path(env["FLEET_WORKTREES"]) / "castor-s2"
    brief = (tree / "scratch" / "codex-brief.md").read_text(encoding="utf-8")
    assert brief.startswith("# batch castor-s2") and "NO precis MCP access" in brief
    # gr567689: a tier-W codex sandbox cannot write the board log — the note
    # routes status through .claude/purpose + scratch/handoff.txt instead.
    # (The appended builder footer still names scripts/board post — that
    # text is authority-gated and unchanged.)
    assert "Post to the board with" not in brief
    assert "sandbox writes only inside this tree" in brief
    assert "scratch/handoff.txt" in brief
    locked = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=env["CWD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert "locked" in locked
    log = Path(env["FAKE_GRIPE_FIX_LOG"]).read_text(encoding="utf-8")
    assign = json.loads(log.splitlines()[1])
    assert assign[assign.index("--session") + 1] == "codex:codex-castor-s2"
    assert "launched codex-castor-s2" in proc.stdout


def test_launch_codex_refuses_tier_r(env: dict[str, str], tmp_path: Path) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {})
    proc = _run(env, "launch", "castor-s2", "--batch", "castor-s2", "--codex")
    assert proc.returncode == 2
    assert "REFUSED --codex at tier R" in proc.stderr and "--tier W" in proc.stderr
    assert not any(c[0] == "new-window" for c in _tmux_calls(env))
    log = Path(env["FAKE_GRIPE_FIX_LOG"]).read_text(encoding="utf-8")
    assert "assign" not in log  # the brief render happened, nothing was recorded


def test_launch_codex_refuses_a_window_that_died(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {})  # no pane for codex-castor-s2: the window is gone
    proc = _run(
        env, "launch", "castor-s2", "--batch", "castor-s2", "--codex",
        "--tier", "W", "--settle", "0",
    )  # fmt: skip
    assert proc.returncode == 3, proc.stdout
    assert "REFUSED codex-castor-s2: window gone" in proc.stderr
    assert "launched" not in proc.stdout
    log = Path(env["FAKE_GRIPE_FIX_LOG"]).read_text(encoding="utf-8")
    assert "assign" not in log


HANDOFF_PANE = "committed abc1234\n⏺ DONE-HANDOFF\n\n────\n❯ \n────\n  71% ctx\n"


def test_handoff_wait_holds_when_clear_is_refused(env: dict[str, str]) -> None:
    # DONE-HANDOFF printed, but a permission dialog is open: send-line
    # refuses /clear, so the resume line must not go out either.
    _windows(env, {"devin-x": "⏺ DONE-HANDOFF\n" + PROMPT})
    proc = _run(env, "handoff-wait", "x", "--ticks", "1", "--pause", "0")
    assert proc.returncode == 0, proc.stderr
    assert "HELD x" in proc.stdout and "resumed" not in proc.stdout
    assert not any(c[0] == "send-keys" for c in _tmux_calls(env))


def test_handoff_wait_clears_and_resumes(env: dict[str, str]) -> None:
    _windows(env, {"devin-x": HANDOFF_PANE, "devin-y": IDLE})
    proc = _run(
        env,
        "handoff-wait",
        "x",
        "y",
        "--ticks",
        "1",
        "--pause",
        "0",
        "--resume",
        "go on",
    )
    assert proc.returncode == 0, proc.stderr
    assert "resumed x from handoff" in proc.stdout
    assert "y" not in proc.stdout.replace("resumed x from handoff", "")
    sends = [c[-1] for c in _tmux_calls(env) if c[0] == "send-keys" and "-l" in c]
    assert sends == ["/clear", "go on"]


def test_reassign_batch_clears_points_and_assigns(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _with_gripe_fix(env, tmp_path)
    _windows(env, {"devin-x": IDLE})
    proc = _run(env, "reassign", "x", "--batch", "wave-2", "--pause", "0")
    assert proc.returncode == 0, proc.stderr
    sends = [c[-1] for c in _tmux_calls(env) if c[0] == "send-keys" and "-l" in c]
    assert sends[0] == "/clear"
    assert sends[1].startswith("Read ") and sends[1].endswith(
        "devin-x.brief.md and do it."
    )
    calls = [
        json.loads(ln)
        for ln in Path(env["FAKE_GRIPE_FIX_LOG"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert calls[0][:2] == ["brief", "wave-2"]
    assert calls[1][:2] == ["assign", "wave-2"]
    assert calls[1][calls[1].index("--session") + 1] == "devin:devin-x"
    assert "reassigned devin-x" in proc.stdout
    assert _run(env, "reassign", "x").returncode == 2


def test_reassign_brief_stages_the_file_in_the_builder_tree(
    env: dict[str, str], tmp_path: Path
) -> None:
    # gr570390: the pane resolves the path inside its own tree, so a
    # coordinator-relative --brief must be staged under <tree>/scratch/.
    _windows(env, {"devin-x": IDLE})
    tree = _worktree(env, "x")
    brief = Path(env["CWD"]) / "scratch" / "brief-r2.md"
    brief.parent.mkdir()
    brief.write_text("Round 2: redo it.\n", encoding="utf-8")
    proc = _run(env, "reassign", "x", "--brief", "scratch/brief-r2.md", "--pause", "0")
    assert proc.returncode == 0, proc.stderr
    sends = [c[-1] for c in _tmux_calls(env) if c[0] == "send-keys" and "-l" in c]
    assert sends == ["/clear", "Read scratch/brief-r2.md and do it."]
    staged = tree / "scratch" / "brief-r2.md"
    assert staged.read_text(encoding="utf-8") == "Round 2: redo it.\n"


def test_reassign_brief_refuses_an_unreadable_file(env: dict[str, str]) -> None:
    _windows(env, {"devin-x": IDLE})
    proc = _run(env, "reassign", "x", "--brief", "scratch/missing.md", "--pause", "0")
    assert proc.returncode == 2 and "missing.md" in proc.stderr
    # Refuse before /clear: a dead brief must not wipe the builder's context.
    assert not any(c[0] == "send-keys" for c in _tmux_calls(env))


def test_cites_lists_unlanded_commits_naming_a_gripe(env: dict[str, str]) -> None:
    repo = Path(env["CWD"])
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    for cmd in (
        ["git", "checkout", "-q", "-b", "worktree-z"],
        ["git", "add", "f.txt"],
        ["git", "commit", "-q", "-m", "fix(z): the thing (gr514001)"],
        ["git", "checkout", "-q", "main"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    proc = _run(env, "cites", "514001", "gr999999")
    assert proc.returncode == 0, proc.stderr
    assert "== gr514001\n" in proc.stdout and "the thing (gr514001)" in proc.stdout
    assert "== gr999999\nnone" in proc.stdout


# ── land: ship run in the builder's own tree ────────────────────────

FAKE_SHIP = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_SHIP_LOG"], "w", encoding="utf-8") as fh:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, fh)
print("fake ship ran")
sys.exit(int(os.environ.get("FAKE_SHIP_RC", "0")))
"""


def _ship_tree(env: dict[str, str], name: str, body: str = FAKE_SHIP) -> Path:
    """A builder worktree whose scripts/ship is a logging fake."""
    wt = _worktree(env, name)
    (wt / "scripts").mkdir()
    ship = wt / "scripts" / "ship"
    ship.write_text(body, encoding="utf-8")
    ship.chmod(0o755)
    return wt


def _land_env(env: dict[str, str], tmp_path: Path, rc: int = 0) -> dict[str, str]:
    return {
        **_coordinator(env),
        "FAKE_SHIP_LOG": str(tmp_path / "ship.json"),
        "FAKE_SHIP_RC": str(rc),
    }


def _ship_call(tmp_path: Path) -> dict[str, object] | None:
    p = tmp_path / "ship.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _all_branches(env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short) %(objectname)"],
        cwd=env["CWD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def test_land_runs_the_trees_own_ship_quick_by_default(
    env: dict[str, str], tmp_path: Path
) -> None:
    wt = _ship_tree(env, "b1")
    env = _land_env(env, tmp_path)
    before = _all_branches(env)
    proc = _run(env, "land", "b1")
    assert proc.returncode == 0, proc.stderr
    call = _ship_call(tmp_path)
    assert call is not None
    assert call["argv"] == ["--quick"]
    assert Path(str(call["cwd"])).resolve() == wt.resolve()
    log = Path(proc.stdout.split("log: ", 1)[1].splitlines()[0])
    assert log.parent == Path(env["FLEET_STATE"]) / "land"
    assert log.name.startswith("b1-") and log.suffix == ".log"
    assert log.read_text(encoding="utf-8") == "fake ship ran\n"
    assert proc.stdout.splitlines()[-1] == "exit: 0"
    # No cherry-pick, no pin branch: the ref set is exactly what it was.
    assert _all_branches(env) == before


def test_land_remote_message_and_exit_code_pass_through(
    env: dict[str, str], tmp_path: Path
) -> None:
    wt = _ship_tree(env, "b2")
    env = _land_env(env, tmp_path, rc=7)
    # A path resolves like a name.
    proc = _run(env, "land", str(wt), "--remote", "-m", "fix(x): the thing")
    assert proc.returncode == 7
    call = _ship_call(tmp_path)
    assert call is not None
    assert call["argv"] == ["--remote", "-m", "fix(x): the thing"]
    assert proc.stdout.splitlines()[-1] == "exit: 7"


def test_land_quick_runs_ship_quick(env: dict[str, str], tmp_path: Path) -> None:
    _ship_tree(env, "b7")
    proc = _run(_land_env(env, tmp_path), "land", "b7", "--quick")
    assert proc.returncode == 0, proc.stderr
    call = _ship_call(tmp_path)
    assert call is not None
    assert call["argv"] == ["--quick"]


def test_land_checked_runs_ship_checked(env: dict[str, str], tmp_path: Path) -> None:
    _ship_tree(env, "b9")
    proc = _run(_land_env(env, tmp_path), "land", "b9", "--checked")
    assert proc.returncode == 0, proc.stderr
    call = _ship_call(tmp_path)
    assert call is not None
    assert call["argv"] == ["--checked"]


def test_land_modes_are_mutually_exclusive(env: dict[str, str], tmp_path: Path) -> None:
    _ship_tree(env, "b8")
    lenv = _land_env(env, tmp_path)
    for pair in (("--checked", "--quick"), ("--quick", "--remote")):
        proc = _run(lenv, "land", "b8", *pair)
        assert proc.returncode == 2 and "not allowed with" in proc.stderr, pair


def test_land_refuses_outside_the_coordinator_tree(
    env: dict[str, str], tmp_path: Path
) -> None:
    _ship_tree(env, "b3")
    lenv = _land_env(env, tmp_path)
    unset = {k: v for k, v in lenv.items() if k != "FLEET_COORDINATOR_TREE"}
    proc = _run(unset, "land", "b3")
    assert proc.returncode == 3 and "FLEET_COORDINATOR_TREE" in proc.stderr
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run(lenv, "land", "b3", cwd=str(elsewhere))
    assert proc.returncode == 3 and "coordinator tree" in proc.stderr
    assert _ship_call(tmp_path) is None


def test_land_refuses_a_live_session_but_not_a_dead_lock(
    env: dict[str, str], tmp_path: Path
) -> None:
    wt = _ship_tree(env, "b4")
    env = _land_env(env, tmp_path)
    lock = ["git", "worktree", "lock", "--reason"]
    # The live pid must NOT be in the land's own ancestor chain — one of
    # those is the calling session itself and passes now (gr574979), so a
    # foreign session is a spawned sleeper, not os.getpid().
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        subprocess.run(
            [*lock, f"claude agent b4 (pid {other.pid})", str(wt)],
            cwd=env["CWD"],
            check=True,
        )
        proc = _run(env, "land", "b4")
        assert proc.returncode == 3
        assert f"live session live#{other.pid}" in proc.stderr
        assert _ship_call(tmp_path) is None
    finally:
        other.kill()
        other.wait()
    # A pid that has exited is a dead lock, not a session.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    subprocess.run(["git", "worktree", "unlock", str(wt)], cwd=env["CWD"], check=True)
    subprocess.run(
        [*lock, f"claude agent b4 (pid {dead.pid})", str(wt)],
        cwd=env["CWD"],
        check=True,
    )
    proc = _run(env, "land", "b4")
    assert proc.returncode == 0, proc.stderr
    assert _ship_call(tmp_path) is not None


def test_land_retries_past_a_lock_naming_its_own_session(
    env: dict[str, str], tmp_path: Path
) -> None:
    """gr574979: a failed land leaves the tree locked with the CALLING
    session's pid — ship's reassert_session_lock resolves its own caller's
    session chain and re-locks a dead-pid builder tree with it. The retry
    must not read its own ancestor as a live builder holding the tree."""
    wt = _ship_tree(env, "b11")
    lenv = _land_env(env, tmp_path, rc=1)
    proc = _run(lenv, "land", "b11")
    assert proc.returncode == 1
    assert _ship_call(tmp_path) is not None
    # The state the failed ship leaves: a live lock naming a pid in the
    # fleet-wave process's own ancestor chain — os.getpid() (pytest)
    # stands in for the coordinator session the first land ran under.
    subprocess.run(
        ["git", "worktree", "lock", "--reason", f"pid {os.getpid()}", str(wt)],
        cwd=env["CWD"],
        check=True,
    )
    proc = _run({**lenv, "FAKE_SHIP_RC": "0"}, "land", "b11")
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED" not in proc.stderr
    assert "not a builder" in proc.stdout


FAKE_SHIP_FAIL_ONCE = f"""#!{sys.executable}
import os, sys
from pathlib import Path
runs = Path(os.environ["FAKE_SHIP_RUNS"])
n = int(runs.read_text(encoding="utf-8")) if runs.exists() else 0
runs.write_text(str(n + 1), encoding="utf-8")
if n == 0:
    sys.stdout.write(os.environ["FAKE_SHIP_FAIL_OUT"])
    sys.exit(1)
print("fake ship ran")
"""

#: git/ship's words for the transient class (gr574979): a bare remote
#: rejection whose reason is ``(failed)`` — no hook name to decline on.
REMOTE_REJECTED_FAILED = (
    "To github.invalid:t/precis.git\n"
    " ! [remote rejected]        HEAD -> main (failed)\n"
    "error: failed to push some refs to 'github.invalid:t/precis.git'\n"
    "✖ git push failed inside the ship lock for a reason other than a lost "
    "race (exit 1) — lock released, nothing landed.\n"
)

REMOTE_REJECTED_HOOK = (
    " ! [remote rejected]        HEAD -> main (pre-receive hook declined)\n"
    "error: failed to push some refs to 'github.invalid:t/precis.git'\n"
)


def test_land_retries_once_on_a_bare_remote_rejected_push(
    env: dict[str, str], tmp_path: Path
) -> None:
    """gr574979: `(failed)` is the host's transient receive error — ship's
    lost-race classifier declines every remote-rejected, so the land
    retries once before dying."""
    _ship_tree(env, "b12", FAKE_SHIP_FAIL_ONCE)
    runs = tmp_path / "ship-runs"
    lenv = {
        **_land_env(env, tmp_path),
        "FAKE_SHIP_RUNS": str(runs),
        "FAKE_SHIP_FAIL_OUT": REMOTE_REJECTED_FAILED,
    }
    proc = _run(lenv, "land", "b12")
    assert proc.returncode == 0, proc.stderr
    assert "retrying" in proc.stdout
    assert runs.read_text(encoding="utf-8") == "2"
    log = Path(proc.stdout.split("log: ", 1)[1].splitlines()[0])
    text = log.read_text(encoding="utf-8")
    assert "remote rejected" in text and "fake ship ran" in text


def test_land_does_not_retry_a_rejection_with_a_reason(
    env: dict[str, str], tmp_path: Path
) -> None:
    """A remote rejection carrying its cause (hook, protected branch) is
    terminal — the same input that makes ship die must not retry here."""
    _ship_tree(env, "b13", FAKE_SHIP_FAIL_ONCE)
    runs = tmp_path / "ship-runs"
    lenv = {
        **_land_env(env, tmp_path),
        "FAKE_SHIP_RUNS": str(runs),
        "FAKE_SHIP_FAIL_OUT": REMOTE_REJECTED_HOOK,
    }
    proc = _run(lenv, "land", "b13")
    assert proc.returncode == 1
    assert "retrying" not in proc.stdout
    assert runs.read_text(encoding="utf-8") == "1"


def test_land_does_not_retry_an_unrelated_failure(
    env: dict[str, str], tmp_path: Path
) -> None:
    _ship_tree(env, "b14", FAKE_SHIP_FAIL_ONCE)
    runs = tmp_path / "ship-runs"
    lenv = {
        **_land_env(env, tmp_path),
        "FAKE_SHIP_RUNS": str(runs),
        "FAKE_SHIP_FAIL_OUT": "✖ ruff check failed\n",
    }
    proc = _run(lenv, "land", "b14")
    assert proc.returncode == 1
    assert runs.read_text(encoding="utf-8") == "1"


def test_land_refuses_a_live_subagent_task(env: dict[str, str], tmp_path: Path) -> None:
    wt = _ship_tree(env, "b5")
    env = _land_env(env, tmp_path)
    _subagent_event(env, "start", "task-land", root=wt)
    try:
        proc = _run(env, "land", "b5")
        assert proc.returncode == 3 and "active subagent task" in proc.stderr
        assert _ship_call(tmp_path) is None
    finally:
        _subagent_event(env, "stop", "task-land", root=wt)


def test_land_refuses_unknown_primary_and_unregistered_trees(
    env: dict[str, str], tmp_path: Path
) -> None:
    env = _land_env(env, tmp_path)
    proc = _run(env, "land", "nope")
    assert proc.returncode == 2 and "no such tree" in proc.stderr
    proc = _run(env, "land", env["CWD"])
    assert proc.returncode == 3 and "not a builder tree" in proc.stderr
    stray = tmp_path / "stray"
    stray.mkdir()
    proc = _run(env, "land", str(stray))
    assert proc.returncode == 2 and "not a registered worktree" in proc.stderr
    assert _ship_call(tmp_path) is None


def test_land_checked_and_remote_are_exclusive(
    env: dict[str, str], tmp_path: Path
) -> None:
    _ship_tree(env, "b6")
    proc = _run(_land_env(env, tmp_path), "land", "b6", "--checked", "--remote")
    assert proc.returncode == 2 and "not allowed with" in proc.stderr


def test_live_session_marker_matches_inflight() -> None:
    m = _module()
    assert m._live_session("") == ""
    assert m._live_session("locked") == ""  # a Codex lock: no pid
    assert m._live_session(f"claude agent x (pid {os.getpid()})") == (
        f"live#{os.getpid()}"
    )


# ── event-watch (td570414) ─────────────────────────────────────────
#: A Devin pane dead on its quota banner, still on the default model
#: (shape: tests/fixtures/panes/devin-quota.txt).
DEVIN_QUOTA = (
    "⏺ earlier output\n"
    "▲ Quota exhausted\n"
    "Your weekly usage quota has been exhausted.\n"
    "──────────────── (bypass permissions on) ─\n"
    "SWE-2                                     Context: 143k / 262k tokens (55%)\n"
    "❭ \n"
)
#: The same pane after the model switch — no reflex left to run.
DEVIN_QUOTA_HIGH = DEVIN_QUOTA.replace("SWE-2", "SWE-2 High")
#: Quota banner with text parked in the input box: the send is refused.
DEVIN_QUOTA_UNSENT = DEVIN_QUOTA.replace("❭ \n", "❭ half typed\n")
#: A builder that hit its handoff ceiling and wrote DONE-HANDOFF.
EW_HANDOFF = (
    "⏺ wrote scratch/handoff.md\n⏺ DONE-HANDOFF\n────\n❯ \n────\n  92% ctx · opus\n"
)
#: An unhandled error — no reflex applies to a Traceback on a claude pane.
EW_ERR = (
    "⏺ blew up\nTraceback (most recent call last):\n────\n❯ \n────\n  40% ctx · opus\n"
)
_EW = (
    "event-watch",
    "--interval",
    "0",
    "--wait",
    "0",
    "--poll",
    "0",
    "--pause",
    "0",
    "--digest-sec",
    "0",
)


def _ew_log(env: dict[str, str]) -> str:
    p = Path(env["FLEET_STATE"]) / "event-watch.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _sent_texts(env: dict[str, str]) -> list[str]:
    """Texts typed into boxes, in order (paste buffers and -l sends)."""
    out = []
    for c in _tmux_calls(env):
        if c[0] == "set-buffer" or (c[0] == "send-keys" and "-l" in c):
            out.append(c[-1])
    return out


def test_event_watch_quota_switches_model_and_resumes(env: dict[str, str]) -> None:
    """The quota reflex fires once, logs, and prints nothing."""
    _windows(env, {"devin-q": DEVIN_QUOTA})
    proc = _run(env, *_EW, "q", "--ticks", "1")
    assert proc.stdout == "", proc.stdout + proc.stderr
    texts = _sent_texts(env)
    assert texts[0] == "/model swe-2-high"
    assert "Resume your batch" in texts[1]
    log = _ew_log(env)
    assert "quota" in log and "swe-2-high" in log
    # Marker written: a second poll neither resends nor prints.
    n = len(_sent_texts(env))
    assert _run(env, *_EW, "q", "--ticks", "1").stdout == ""
    assert len(_sent_texts(env)) == n


def test_event_watch_quota_on_high_is_printed_not_sent(env: dict[str, str]) -> None:
    """Already on swe-2-high: the reflex has nothing to do — the pane's
    error becomes a coordinator event instead of a send."""
    _windows(env, {"devin-q": DEVIN_QUOTA_HIGH})
    out = _run(env, *_EW, "q", "--ticks", "2").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1 and "devin-q" in lines[0] and "swe-2-high" in lines[0]
    assert "/model" not in _sent_texts(env)
    assert _run(env, *_EW, "q", "--ticks", "1").stdout == ""


def test_event_watch_quota_reflex_failure_prints(env: dict[str, str]) -> None:
    """A reflex that cannot deliver is itself a judgment event."""
    _windows(env, {"devin-q": DEVIN_QUOTA_UNSENT})
    out = _run(env, *_EW, "q", "--ticks", "1").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1 and "reflex failed" in lines[0]
    assert "quota" in lines[0]
    assert "refused" in _ew_log(env)


def test_event_watch_quota_skips_the_reflex_for_a_non_swe_model(
    env: dict[str, str],
) -> None:
    """td573330: launch's .model record says this pane runs a non-SWE id —
    /model swe-2-high would silently switch it to SWE-2, so the reflex
    stands down and the quota becomes a coordinator event."""
    st = Path(env["FLEET_STATE"])
    st.mkdir(parents=True, exist_ok=True)
    (st / "devin-q.model").write_text("gemini-3-8-flash-high\n", encoding="utf-8")
    _windows(env, {"devin-q": DEVIN_QUOTA})
    out = _run(env, *_EW, "q", "--ticks", "2").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1 and "no reflex" in lines[0]
    assert "gemini-3-8-flash-high" in lines[0]
    assert _sent_texts(env) == []


def test_event_watch_quota_swe_record_keeps_the_reflex(
    env: dict[str, str],
) -> None:
    st = Path(env["FLEET_STATE"])
    st.mkdir(parents=True, exist_ok=True)
    (st / "devin-q.model").write_text("swe-2\n", encoding="utf-8")
    _windows(env, {"devin-q": DEVIN_QUOTA})
    proc = _run(env, *_EW, "q", "--ticks", "1")
    assert proc.stdout == "", proc.stdout + proc.stderr
    assert _sent_texts(env)[0] == "/model swe-2-high"


def test_event_watch_done_handoff_resumes_once(env: dict[str, str]) -> None:
    """Bare DONE-HANDOFF → handoff-wait's /clear + resume, silent."""
    _windows(env, {"claude-h": EW_HANDOFF})
    proc = _run(env, *_EW, "h", "--ticks", "1")
    assert proc.stdout == "", proc.stdout + proc.stderr
    texts = _sent_texts(env)
    assert texts[0] == "/clear"
    assert "Resume your batch" in texts[1]
    assert "handoff" in _ew_log(env)
    # The marker suppresses a re-fire while the scrollback line persists.
    n = len(_sent_texts(env))
    assert _run(env, *_EW, "h", "--ticks", "1").stdout == ""
    assert len(_sent_texts(env)) == n


def test_event_watch_compacts_an_idle_hot_pane(env: dict[str, str]) -> None:
    """ctx-guard's /compact as a reflex: once per episode, re-armed below
    half the trigger, never printed."""
    _windows(env, {"devin-c": DEVIN_CTX76})
    proc = _run(env, *_EW, "c", "--ticks", "1")
    assert proc.stdout == "", proc.stdout + proc.stderr
    assert "/compact" in _sent_texts(env)
    marker = Path(env["FLEET_STATE"]) / "event-watch" / "compact-c"
    assert marker.exists()
    n = len(_sent_texts(env))
    assert _run(env, *_EW, "c", "--ticks", "1").stdout == ""
    assert len(_sent_texts(env)) == n
    # Below re-arm the marker clears; the next episode fires again.
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "pane-devin-c.txt").write_text(DEVIN_COOL, encoding="utf-8")
    assert _run(env, *_EW, "c", "--ticks", "1").stdout == ""
    assert not marker.exists()
    (d / "pane-devin-c.txt").write_text(DEVIN_CTX76, encoding="utf-8")
    assert _run(env, *_EW, "c", "--ticks", "1").stdout == ""
    assert marker.exists()


def test_event_watch_never_compacts_a_busy_pane(env: dict[str, str]) -> None:
    """gr515153: never /compact mid-turn — a busy pane is left alone."""
    _windows(env, {"claude-c": CLAUDE_BUSY_TOK240})
    proc = _run(env, *_EW, "c", "--ticks", "1")
    assert proc.stdout == ""
    assert "/compact" not in _sent_texts(env)


def test_event_watch_prompt_gone_done_each_print_once(env: dict[str, str]) -> None:
    _windows(env, {"claude-p": PROMPT, "devin-d": IDLE})
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "facts-devin-d").write_text("zsh\t1\n", encoding="utf-8")  # dead
    out = _run(env, *_EW, "p", "d", "ghost", "--ticks", "1").stdout
    lines = out.splitlines()
    assert any("claude-p" in ln and "prompt" in ln for ln in lines)
    assert any(ln.endswith("ghost gone") for ln in lines)
    assert any("devin-d" in ln and "done" in ln for ln in lines)
    # No repeats on the next run — each terminal event prints once.
    assert _run(env, *_EW, "p", "d", "ghost", "--ticks", "1").stdout == ""


def test_event_watch_frozen_prints_once(env: dict[str, str]) -> None:
    _windows(env, {"claude-z": IDLE})
    assert _run(env, *_EW, "z", "--ticks", "1").stdout == ""
    # Backdate the tail-age marker past the frozen threshold.
    tfile = Path(env["FLEET_STATE"]) / "event-watch" / "z.t"
    tfile.write_text("0", encoding="utf-8")
    out = _run(env, *_EW, "z", "--ticks", "1").stdout
    assert "claude-z" in out and "frozen" in out
    assert _run(env, *_EW, "z", "--ticks", "1").stdout == ""


def test_event_watch_error_prints_after_settle_once(env: dict[str, str]) -> None:
    """An unhandled error must hold --settle polls, then prints once."""
    _windows(env, {"claude-e": EW_ERR})
    assert _run(env, *_EW, "e", "--ticks", "1").stdout == ""
    out = _run(env, *_EW, "e", "--ticks", "2").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1 and "claude-e" in lines[0] and "error" in lines[0]
    assert _run(env, *_EW, "e", "--ticks", "1").stdout == ""


def test_event_watch_board_alerts_for_watched_trees_only(
    env: dict[str, str], tmp_path: Path
) -> None:
    board = tmp_path / "board.log"
    board.write_text(
        "2026-11-05T01:00:00Z\twatch1\tdone watch1 at abc1234: shipped\n"
        "2026-11-05T01:01:00Z\tother\tdone other at def5678: shipped\n"
        "2026-11-05T01:02:00Z\twatch1\twaiting on the gate\n"
        "2026-11-05T01:03:00Z\twatch2\tred gate red: flake\n"
        "2026-11-05T01:04:00Z\twatch2\tblocked parked until ruling\n",
        encoding="utf-8",
    )
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-watch1": IDLE, "devin-watch2": IDLE})
    # First run only seeds the byte cursor at EOF — the lines above are
    # history and print nothing.
    assert _run(env, *_EW, "watch1", "watch2", "--ticks", "1").stdout == ""
    with board.open("a", encoding="utf-8") as fh:
        fh.write(
            "2026-11-05T01:00:00Z\twatch1\tdone watch1 at abc1234: shipped\n"
            "2026-11-05T01:01:00Z\tother\tdone other at def5678: shipped\n"
            "2026-11-05T01:02:00Z\twatch1\twaiting on the gate\n"
            "2026-11-05T01:03:00Z\twatch2\tred gate red: flake\n"
            "2026-11-05T01:04:00Z\twatch2\tblocked parked until ruling\n"
        )
    out = _run(env, *_EW, "watch1", "watch2", "--ticks", "1").stdout
    assert "board: done watch1 at abc1234" in out
    assert "board: red gate red" in out and "board: blocked parked" in out
    assert "other" not in out and "waiting" not in out
    # The reader cursor advanced: nothing is re-printed.
    assert _run(env, *_EW, "watch1", "watch2", "--ticks", "1").stdout == ""


def test_event_watch_board_held_lines_survive_a_restart(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r1: a board line held when the watcher died is re-queued from
    board.pending on the next run — printed once, then dropped."""
    board = tmp_path / "board.log"
    board.write_text("", encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    pend = Path(env["FLEET_STATE"]) / "event-watch" / "board.pending"
    pend.parent.mkdir(parents=True, exist_ok=True)
    # A line that was queued but never printed when the last run died.
    pend.write_text(
        "2026-11-05T01:00:00Z\tw1\tdone w1 at abc1234: shipped\n",
        encoding="utf-8",
    )
    out = _run(env, *_EW, "w1", "--ticks", "1").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert lines == [ln for ln in lines if "board: done w1 at abc1234" in ln]
    assert "done w1 at abc1234" not in pend.read_text(encoding="utf-8")
    # And it does not print a second time.
    assert _run(env, *_EW, "w1", "--ticks", "1").stdout == ""


def test_event_watch_board_first_run_does_not_replay_history(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r1: the board cursor seeds at EOF — the first run sees no history."""
    board = tmp_path / "board.log"
    board.write_text(
        "2026-11-05T01:00:00Z\tw1\tdone w1 at old0000: ancient\n",
        encoding="utf-8",
    )
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    assert _run(env, *_EW, "w1", "--ticks", "1").stdout == ""
    with board.open("a", encoding="utf-8") as fh:
        fh.write("2026-11-05T02:00:00Z\tw1\tred gate red now\n")
    out = _run(env, *_EW, "w1", "--ticks", "1").stdout
    assert "board: red gate red now" in out and "ancient" not in out


def test_event_watch_board_holds_a_line_until_its_window_exists(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r1: an alert line for a tree with no window is held pending and
    prints once the window appears — never dropped."""
    board = tmp_path / "board.log"
    board.write_text("", encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-other": IDLE})
    # Seed, then a done line arrives for a tree with no window yet. The
    # stamp is fresh — a stale one is a never-match and ages out.
    assert _run(env, *_EW, "--ticks", "1").stdout == ""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    board.write_text(
        f"{now}\tnewt\tdone newt at abc1234: shipped\n",
        encoding="utf-8",
    )
    assert _run(env, *_EW, "--ticks", "1").stdout == ""
    _windows(env, {"claude-other": IDLE, "devin-newt": IDLE})
    out = _run(env, *_EW, "--ticks", "1").stdout
    assert "board: done newt at abc1234" in out


def test_event_watch_idle_resets_the_error_dedup(env: dict[str, str]) -> None:
    """r1: a recovered pane can report a new episode — the idle poll
    between two error runs resets the marker, so each prints."""
    _windows(env, {"claude-e": ""})
    _seq(env, "claude-e", IDLE, EW_ERR, EW_ERR, IDLE, EW_ERR, EW_ERR)
    out = _run(env, *_EW, "e", "--ticks", "6").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 2 and all("error" in ln for ln in lines)


def test_event_watch_quota_still_errors_after_a_switch(
    env: dict[str, str],
) -> None:
    """r1: a fresh quota marker only caps resends — an error that has not
    cleared still counts toward --settle and prints."""
    _windows(env, {"devin-q": DEVIN_QUOTA})
    assert _run(env, *_EW, "q", "--ticks", "1").stdout == ""  # reflex sent
    n = len(_sent_texts(env))
    out = _run(env, *_EW, "q", "--ticks", "2").stdout
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1 and "error" in lines[0] and "quota" in lines[0]
    assert len(_sent_texts(env)) == n  # nothing resent either


def test_event_watch_quota_retries_the_resume_line(env: dict[str, str]) -> None:
    """r1: /model sent, resume refused — the marker records the stage and
    the next poll retries the resume, not the /model."""
    clean = "⏺ ok\nSWE-2    Context: 20k / 262k tokens (8%)\n❭ \n"
    _windows(env, {"devin-q": ""})
    _seq(
        env,
        "devin-q",
        DEVIN_QUOTA,
        DEVIN_QUOTA,
        clean,
        DEVIN_QUOTA_UNSENT,
        DEVIN_QUOTA,
        clean,
        clean,
    )
    out = _run(env, *_EW, "q", "--ticks", "1").stdout
    assert "reflex failed" in out
    assert _sent_texts(env) == ["/model swe-2-high"]
    out = _run(env, *_EW, "q", "--ticks", "1").stdout
    texts = _sent_texts(env)
    assert texts.count("/model swe-2-high") == 1
    assert any("Resume your batch" in t for t in texts)


def test_event_watch_refusal_logs_once_per_reason(env: dict[str, str]) -> None:
    """r1: a refused reflex retried every poll logs once per distinct
    reason, not once per poll."""
    hot_unsent = DEVIN_CTX76.replace("❭ \n", "❭ half typed\n")
    _windows(env, {"devin-c": ""})
    _seq(env, "devin-c", hot_unsent, hot_unsent, hot_unsent)
    out = _run(env, *_EW, "c", "--ticks", "3").stdout
    assert "reflex failed" in out
    assert _ew_log(env).count("compact") == 1


def test_event_watch_log_rotates_at_the_cap(env: dict[str, str]) -> None:
    st = Path(env["FLEET_STATE"])
    st.mkdir(parents=True, exist_ok=True)
    (st / "event-watch.log").write_text("x" * 600_000, encoding="utf-8")
    hot_unsent = DEVIN_CTX76.replace("❭ \n", "❭ half typed\n")
    _windows(env, {"devin-c": ""})
    _seq(env, "devin-c", hot_unsent, hot_unsent)
    _run(env, *_EW, "c", "--ticks", "1")
    assert (st / "event-watch.log.1").exists()
    assert len(_ew_log(env)) < 600_000


def test_event_watch_calm_drops_a_held_error_line(env: dict[str, str]) -> None:
    """r1: an error that flapped back before the digest window let it
    print is dropped, like stuck-watch's held lines."""
    _windows(env, {"claude-e": ""})
    _seq(env, "claude-e", EW_ERR, EW_ERR, IDLE)
    out = _run(env, *_EW, "e", "--ticks", "3", "--digest-sec", "300").stdout
    assert out == ""


def test_event_watch_window_gone_between_list_and_capture(
    env: dict[str, str],
) -> None:
    """r1: a window listed this poll but gone at capture reports gone."""
    _windows(env, {"claude-v": IDLE})
    (Path(env["FAKE_TMUX_DIR"]) / "pane-claude-v.txt").unlink()
    out = _run(env, *_EW, "v", "--ticks", "1").stdout
    assert "claude-v" in out and "gone" in out


def test_event_watch_compact_uses_the_token_trigger_when_set(
    env: dict[str, str],
) -> None:
    """r1: with --tokens the token figure alone decides; the % trigger
    applies only when --tokens is absent (ctx-guard's rule)."""
    hot_tok = "⏺ ok\nSWE-2    Context: 200k / 262k tokens (8%)\n❭ \n"
    _windows(env, {"devin-c": hot_tok})
    # 8% under the ceiling: no compact without --tokens.
    assert _run(env, *_EW, "c", "--ticks", "1").stdout == ""
    assert "/compact" not in _sent_texts(env)
    # 200k over the token threshold: compact even at 8%.
    out = _run(env, *_EW, "c", "--ticks", "1", "--tokens", "120k")
    assert out.stdout == "" and "/compact" in _sent_texts(env)


def test_event_watch_board_clear_rereads_from_the_top(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r2: a truncated board (scripts/board clear) resets the byte cursor
    to 0 — every new line after it is seen."""
    board = tmp_path / "board.log"
    board.write_text(
        "2026-11-05T01:00:00Z\tw1\tdone w1 at aaa0000: old\n" * 10,
        encoding="utf-8",
    )
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    _run(env, *_EW, "w1", "--ticks", "1")  # seeds the cursor at EOF
    board.write_text(  # smaller than before: a truncation
        "2026-11-05T02:00:00Z\tw1\tdone w1 at b1: one\n"
        "2026-11-05T02:01:00Z\tw1\tred r2 two\n"
        "2026-11-05T02:02:00Z\tw1\tblocked b3 three\n",
        encoding="utf-8",
    )
    out = _run(env, *_EW, "w1", "--ticks", "1").stdout
    assert "one" in out and "two" in out and "three" in out


def test_event_watch_explicit_names_ignore_other_seen_trees(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r2: with NAMES only those posters match — a done line from a
    window the watcher saw but did not name is not ours."""
    board = tmp_path / "board.log"
    board.write_text("", encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-foo": IDLE, "claude-bar": IDLE})
    assert _run(env, *_EW, "foo", "--ticks", "1").stdout == ""
    board.write_text(
        "2026-11-05T02:00:00Z\tbar\tdone bar at abc1234: shipped\n",
        encoding="utf-8",
    )
    out = _run(env, *_EW, "foo", "--ticks", "1").stdout
    assert "board:" not in out


def test_event_watch_board_queued_twice_prints_once(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r2: a kill between pending-save and pos-write re-reads the same
    bytes — the line is already pending and still prints exactly once."""
    line = "2026-11-05T01:00:00Z\tw1\tdone w1 at abc1234: shipped\n"
    board = tmp_path / "board.log"
    board.write_text(line, encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    st = Path(env["FLEET_STATE"]) / "event-watch"
    st.mkdir(parents=True, exist_ok=True)
    # The between state: pending holds the line, pos is still behind it.
    (st / "board.pos").write_text("0", encoding="utf-8")
    (st / "board.pending").write_text(line, encoding="utf-8")
    out = _run(env, *_EW, "w1", "--ticks", "1").stdout
    assert len([ln for ln in out.splitlines() if "abc1234" in ln]) == 1
    assert _run(env, *_EW, "w1", "--ticks", "1").stdout == ""


def test_event_watch_pending_ages_out_and_logs(
    env: dict[str, str], tmp_path: Path
) -> None:
    """r2: a held line whose poster never surfaces is dropped at 30 min,
    with a note on the reflex log."""
    board = tmp_path / "board.log"
    board.write_text("", encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    pend = Path(env["FLEET_STATE"]) / "event-watch" / "board.pending"
    pend.parent.mkdir(parents=True, exist_ok=True)
    pend.write_text(
        "2020-01-01T00:00:00Z\tghost\tdone ghost at abc1234: shipped\n",
        encoding="utf-8",
    )
    assert _run(env, *_EW, "--ticks", "1").stdout == ""
    assert "ghost" not in pend.read_text(encoding="utf-8")
    assert "aged out" in _ew_log(env)


def test_event_watch_pending_caps_at_200(env: dict[str, str], tmp_path: Path) -> None:
    """r2: held lines are capped at 200 — the oldest drop first, logged."""
    board = tmp_path / "board.log"
    board.write_text("", encoding="utf-8")
    env = {**env, "PRECIS_BOARD": str(board)}
    _windows(env, {"claude-w1": IDLE})
    pend = Path(env["FLEET_STATE"]) / "event-watch" / "board.pending"
    pend.parent.mkdir(parents=True, exist_ok=True)
    pend.write_text(
        "".join(
            f"2099-01-01T00:00:00Z\tghost-{i}\tdone x at {i}\n" for i in range(210)
        ),
        encoding="utf-8",
    )
    _run(env, *_EW, "--ticks", "1")
    kept = pend.read_text(encoding="utf-8").splitlines()
    assert len(kept) == 200
    assert kept[0].split("\t")[1] == "ghost-10"
    assert "over cap" in _ew_log(env)


def test_event_watch_second_watcher_refuses(env: dict[str, str]) -> None:
    """r2: one watcher per state dir — a second exits with a message."""
    _windows(env, {"claude-w1": IDLE})
    lock = Path(env["FLEET_STATE"]) / "event-watch.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = _run(env, *_EW, "w1", "--ticks", "1")
        assert proc.returncode != 0
        assert "another watcher" in proc.stderr + proc.stdout
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert _run(env, *_EW, "w1", "--ticks", "1").returncode == 0


def test_event_watch_quota_send_poll_does_not_count(env: dict[str, str]) -> None:
    """r2: the poll that sent the reflex is not counted — 'uncleared
    after 2 polls' means two polls after the send."""
    _windows(env, {"devin-q": DEVIN_QUOTA})
    # Poll 1 sends the reflex (count 0); poll 2 is the first counted one.
    assert _run(env, *_EW, "q", "--ticks", "2").stdout == ""
    assert "/model swe-2-high" in _sent_texts(env)
    # Two counted polls in a run — the uncleared error prints on the
    # second (the count is in-memory per watcher).
    out = _run(env, *_EW, "q", "--ticks", "2").stdout
    assert "error" in out and "quota" in out
