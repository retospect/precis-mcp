"""scripts/fleet-panes + scripts/fleet_panes.py — the shared pane parsers.

The per-agent fixtures under tests/fixtures/panes/ are scrubbed captures of
live panes (2026-10-19). CLI tests run the real script against a fake tmux
that serves pane text from files and logs argv (same shape as
tests/test_fleet_wave.py).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs scripts with fake bins"
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-panes"
LIB = REPO / "scripts" / "fleet_panes.py"
FIXTURES = REPO / "tests" / "fixtures" / "panes"

FAKE_TMUX = f"""#!{sys.executable}
import json, os, shutil, sys
argv = sys.argv[1:]
if argv[:1] == ["-L"]:
    argv = argv[2:]
d = os.environ["FAKE_TMUX_DIR"]
with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
cmd = argv[0]
if cmd in ("list-windows", "list-panes"):
    name = "windows" if cmd == "list-windows" else "panes"
    p = os.path.join(d, name)
    sys.stdout.write(open(p, encoding="utf-8").read() if os.path.exists(p) else "")
elif cmd == "capture-pane":
    t = argv[argv.index("-t") + 1]
    ext = ".esc" if "-e" in argv else ".txt"
    p = os.path.join(d, "pane-" + t + ext)
    if not os.path.exists(p) and ext == ".esc":
        p = os.path.join(d, "pane-" + t + ".txt")
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p, encoding="utf-8").read())
elif cmd == "send-keys":
    # An Enter can clear the box or answer the dialog: swap in the after-shot.
    t = argv[argv.index("-t") + 1]
    if "Enter" in argv:
        after = os.path.join(d, "pane-" + t + "-after.txt")
        pane = os.path.join(d, "pane-" + t + ".txt")
        if os.path.exists(after):
            shutil.copyfile(after, pane)
"""


def _lib() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("fleet_panes", str(LIB))
    spec = importlib.util.spec_from_loader("fleet_panes", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves cls.__module__ through sys.modules.
    sys.modules["fleet_panes"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    fake = tmp_path / "fake"
    fake.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "tmux"
    exe.write_text(FAKE_TMUX, encoding="utf-8")
    exe.chmod(0o755)
    out = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_TMUX_DIR": str(fake),
        "FLEET_PANES_STATE": str(tmp_path / "state"),
        "FLEET_PANES_FROZEN_MIN": "25",
        "CWD": str(tmp_path),
    }
    for k in ("FLEET_TMUX_SOCKET", "FLEET_TMUX_SESSION", "TMUX", "TMUX_PANE"):
        out.pop(k, None)
    return out


def _panes(env: dict[str, str], rows: dict[str, tuple[str, str, int]]) -> None:
    """rows: label -> (pane-file text, pane_current_command, pane_dead)."""
    d = Path(env["FAKE_TMUX_DIR"])
    lines = []
    for i, (label, (text, cmd, dead)) in enumerate(rows.items()):
        sess, name = label.split(":", 1)
        lines.append(f"{sess}\t{i}\t{name}\t%{i}\t{cmd}\t{dead}")
        (d / f"pane-%{i}.txt").write_text(text, encoding="utf-8")
        (d / f"pane-{name}.txt").write_text(text, encoding="utf-8")
    (d / "panes").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=env["CWD"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["FAKE_TMUX_DIR"]) / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]


def _fix(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- parser unit tests (fixtures are scrubbed live captures) ------------------


def test_claude_idle() -> None:
    m = _lib()
    rep = m.pane_report("claude", _fix("claude-idle.txt"))
    assert (rep.state, rep.permission, rep.ctx) == ("idle", "auto", 20)
    assert rep.last == "new task? /clear to save 196.5k tokens"


def test_claude_busy() -> None:
    m = _lib()
    rep = m.pane_report("claude", _fix("claude-busy.txt"))
    assert (rep.state, rep.ctx) == ("working", 43)


def test_claude_permission_dialog() -> None:
    m = _lib()
    pane = _fix("claude-permission.txt")
    rep = m.pane_report("claude", pane)
    assert (rep.state, rep.dialog) == ("prompt", "permission")
    assert rep.command == 'scripts/prod-psql --ro "\\! id"'
    assert rep.options == ["❯ 1. Yes", "2. No"]
    assert m.answerable_once(pane) is None  # option shape is a single Yes


def test_claude_trust_dialog() -> None:
    m = _lib()
    rep = m.pane_report("claude", _fix("claude-trust.txt"))
    assert (rep.state, rep.dialog) == ("prompt", "trust")


def test_devin_working() -> None:
    m = _lib()
    rep = m.pane_report("devin", _fix("devin-working.txt"))
    assert (rep.state, rep.permission, rep.ctx) == ("working", "bypass", 76)
    assert rep.last == "⢠⡀ Running tools · 184m 46s (esc twice to interrupt)"


def test_devin_queued() -> None:
    m = _lib()
    rep = m.pane_report("devin", _fix("devin-queued.txt"))
    assert rep.state == "working" and rep.queued == 2


def test_codex_states() -> None:
    m = _lib()
    idle = m.pane_report("codex", _fix("codex-idle.txt"))
    assert idle.state == "idle" and idle.ctx is None
    assert m.pane_report("codex", _fix("codex-working.txt")).state == "working"
    done = m.pane_report("codex", _fix("codex-done.txt"))
    assert done.state == "idle" and done.last == "Worked for 4s • 20:45"
    q = m.pane_report("codex", _fix("codex-question.txt"))
    assert (q.state, q.dialog) == ("prompt", "question")


def test_done_when_process_exited() -> None:
    m = _lib()
    rep = m.pane_report("devin", "user@host ~ % \n", command_running=False)
    assert rep.state == "done"
    rep = m.pane_report("claude", _fix("claude-busy.txt"), frozen=True)
    assert rep.state == "frozen"


def test_detect_agent() -> None:
    m = _lib()
    assert m.detect_agent("claude", "claude-x") == "claude"
    assert m.detect_agent("node", "w") == "claude"
    assert m.detect_agent("zsh", "devin-d-x") == "devin"
    assert m.detect_agent("zsh", "plain") == "other"


def test_read_only_reason() -> None:
    m = _lib()
    for ok in (
        "cat README.md",
        "head -5 f",
        "tail -n 20 f | grep err",
        "rg foo src/",
        "find . -name '*.py' -type f",
        "ls -la",
        "wc -l f",
        "git log --oneline -5",
        "git show HEAD",
        "git status --porcelain",
        "git diff main...HEAD",
        "sed -n '1,5p' f",
        "cat a | grep b | wc -l",
    ):
        assert m.read_only_reason(ok) is None, ok
    for bad in (
        "rm f",
        "git push",
        "git -C dir log",
        "find . -delete",
        "find . -exec rm {} +",
        "sed -i 's/a/b/' f",
        "sed 's/a/b/' f",
        "cat a > b",
        "cat $(secret)",
        "sudo cat /etc/shadow",
        "echo hi",
        "cat a | rm f",
    ):
        assert m.read_only_reason(bad), bad


def test_answerable_once_refusals() -> None:
    m = _lib()
    # A question dialog is not a permission dialog.
    assert m.answerable_once(_fix("codex-question.txt"))
    # Option 1 widening scope ("don't ask"/"always") refuses.
    widening = " Do you want to proceed?\n ❯ 1. Yes, and don't ask again\n   2. No\n"
    assert "widens scope" in m.answerable_once(widening)


def test_unsent_input_placeholders() -> None:
    m = _lib()
    assert m.unsent_input(_fix("codex-idle.txt")) == ""
    assert m.unsent_input(_fix("claude-idle.txt")) == ""
    assert m.unsent_input("out\n❯ \x1b[1mhalf typed\x1b[0m\n") == "half typed"


# --- CLI tests against the fake tmux ------------------------------------------


def test_status_rows(env: dict[str, str]) -> None:
    _panes(
        env,
        {
            "0:claude-a": (_fix("claude-idle.txt"), "claude", 0),
            "0:devin-b": (_fix("devin-working.txt"), "devin", 0),
            "0:codex-c": (_fix("codex-question.txt"), "codex", 0),
            "0:shell": ("x $ \n", "zsh", 0),
        },
    )
    out = _run(env, "status").stdout.splitlines()
    assert len(out) == 4
    fields = {ln.split()[0]: ln for ln in out}
    assert "idle" in fields["0:claude-a"] and "auto" in fields["0:claude-a"]
    assert "20%" in fields["0:claude-a"]
    assert "working" in fields["0:devin-b"] and "76%" in fields["0:devin-b"]
    assert "prompt" in fields["0:codex-c"]
    assert "shell" in fields["0:shell"]


def test_status_flagged_and_frozen(env: dict[str, str]) -> None:
    rows = {
        "0:claude-a": (_fix("claude-idle.txt"), "claude", 0),
        "0:codex-c": (_fix("codex-question.txt"), "codex", 0),
    }
    _panes(env, rows)
    out = _run(env, "status", "--flagged").stdout
    assert "0:codex-c" in out and "0:claude-a" not in out
    assert "2 windows, 1 flagged" in out
    # A pane unchanged for an hour is frozen: seed the hash state by running
    # once (fresh age 0), rewinding its stamp, then running again.
    st = Path(env["FLEET_PANES_STATE"])
    f = st / "0-0-claude-a"
    digest = f.read_text(encoding="utf-8").split()[0]
    f.write_text(f"{digest} {int(time.time()) - 3600}\n", encoding="utf-8")
    out = _run(env, "status", "--flagged").stdout
    assert "frozen" in out.split("0:claude-a")[1].splitlines()[0]


def test_read_strips_chrome(env: dict[str, str]) -> None:
    _panes(env, {"0:claude-a": (_fix("claude-idle.txt"), "claude", 0)})
    out = _run(env, "read", "claude-a").stdout
    assert "Churned for 10s" in out
    assert "ctx" not in out and "INSERT" not in out and "❯" not in out


def test_prompt_reports_dialog(env: dict[str, str]) -> None:
    _panes(env, {"0:claude-a": (_fix("claude-permission.txt"), "claude", 0)})
    proc = _run(env, "prompt", "a")
    assert proc.returncode == 0
    assert "permission" in proc.stdout and "prod-psql" in proc.stdout
    assert "1. Yes" in proc.stdout
    _panes(env, {"0:claude-a": (_fix("claude-idle.txt"), "claude", 0)})
    assert _run(env, "prompt", "a").returncode == 1


def test_say_types_and_verifies(env: dict[str, str]) -> None:
    _panes(env, {"0:devin-b": (_fix("devin-working.txt"), "devin", 0)})
    # The devin pane is busy but has no dialog: say proceeds.
    proc = _run(env, "say", "b", "hello there", "--pause", "0")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    sends = [c for c in _calls(env) if c[0] == "send-keys"]
    assert ["send-keys", "-t", "%0", "-l", "--", "hello there"] in sends
    assert ["send-keys", "-t", "%0", "Enter"] in sends


def test_say_refuses_dialog_and_unsent(env: dict[str, str]) -> None:
    _panes(env, {"0:claude-a": (_fix("claude-permission.txt"), "claude", 0)})
    proc = _run(env, "say", "a", "hi", "--pause", "0")
    assert proc.returncode == 3 and "REFUSED" in proc.stdout
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "pane-%0.esc").write_text(
        _fix("claude-idle.txt") + "❯ half typed\n", encoding="utf-8"
    )
    proc = _run(env, "say", "a", "hi", "--pause", "0")
    assert proc.returncode == 3 and "unsent" in proc.stdout


def test_answer_once_readonly(env: dict[str, str]) -> None:
    ask_cat = _fix("claude-permission.txt").replace(
        'scripts/prod-psql --ro "\\! id"', "cat README.md"
    )
    _panes(env, {"0:claude-a": (ask_cat, "claude", 0)})
    d = Path(env["FAKE_TMUX_DIR"])
    (d / "pane-%0-after.txt").write_text(_fix("claude-idle.txt"), encoding="utf-8")
    proc = _run(env, "answer", "a", "once", "--pause", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "answered once" in proc.stdout
    assert ["send-keys", "-t", "%0", "Enter"] in [
        c for c in _calls(env) if c[0] == "send-keys"
    ]


def test_answer_once_refuses_writes(env: dict[str, str]) -> None:
    # The fixture's prod-psql command is not on the read-only list.
    _panes(env, {"0:claude-a": (_fix("claude-permission.txt"), "claude", 0)})
    proc = _run(env, "answer", "a", "once", "--pause", "0")
    assert proc.returncode == 3 and "not clearly read-only" in proc.stdout
    assert not [c for c in _calls(env) if c[0] == "send-keys"]
    # No permission dialog at all.
    _panes(env, {"0:codex-c": (_fix("codex-idle.txt"), "codex", 0)})
    proc = _run(env, "answer", "c", "once")
    assert proc.returncode == 3 and "no permission dialog" in proc.stdout
