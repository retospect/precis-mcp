"""scripts/fleet verbs: say -m / --when-clear, queue, deliver, verdict, peek,
dialogs, refs.

Each test runs the real script (copied byte-for-byte into a temp git repo)
against a private tmux server (`PRECIS_FLEET_TMUX_SOCKET`) whose windows are
panes running `cat <canned>; sleep 600`, so the dialog and idle detection
sees real pane text. Nothing here touches the live fleet: the socket, the
state dir (`PRECIS_FLEET_STATE`) and the repo are all under tmp_path.

The dialog texts below are hand-written stand-ins; the classifier's patterns
await live samples (see the comment block above `dialog_type` in the script).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None,
    reason="needs POSIX bash and tmux",
)

REPO = Path(__file__).resolve().parents[1]

IDLE = "previous output\n\n❯ \n"
BUSY = "doing things\n✻ Working… (12s · esc to interrupt)\n"
PERMISSION = " Bash command\n\n   rm -rf /tmp/xyz\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
PERMISSION_BOXED = (
    "╭──────────────────────╮\n"
    "│ Bash command         │\n"
    "│   ls -la /tmp        │\n"
    "╰──────────────────────╯\n"
    " Do you want to proceed?\n ❯ 1. Yes\n"
)
QUESTION = (
    " Which approach?\n ❯ 1. Option A\n   2. Option B\n\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
HOOK = (
    " PreToolUse hook wants to run\n   ./check.sh --strict\n\n"
    " Do you want to allow this hook?\n ❯ 1. Yes\n"
)

# The live AskUserQuestion layout (review window, 2026-10-03): the question in
# `│` lines, numbered options below, a rule, then option 5 above the footer.
QUESTION_LIVE = (
    "←  ☒ Rewordings  ☐ Agent image  ✔ Submit  →\n"
    "│ Agent image rebuild: who runs it?\n"
    "│ The gate works without it.\n"
    "❯ 1. Organizer rebuilds (Recommended)\n"
    "     The organizer rebuilds it next round.\n"
    "  2. I'll rebuild it\n"
    "─────────────────────\n"
    "  5. Chat about this\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)
TRUST = (
    " Do you trust the files in this folder?\n\n /tmp/some/worktree\n\n"
    " ❯ 1. Yes, proceed\n   2. No, exit\n\n Enter to confirm · Esc to exit\n"
)
# A permission dialog whose command merely names scripts/hooks/: not a hook.
PERMISSION_HOOK_PATH = (
    " Bash command\n\n   bash scripts/hooks/session-end-reap.sh\n\n"
    " Do you want to proceed?\n ❯ 1. Yes\n"
)


@dataclass
class Fleet:
    repo: Path
    state: Path
    sock: str
    env: dict[str, str]

    def run(
        self, *args: str, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.repo / "scripts" / "fleet"), *args],
            cwd=self.repo,
            env=self.env,
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.sock, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def _command(self, name: str, text: str) -> str:
        canned = self.repo / f"canned-{name}.txt"
        canned.write_text(text, encoding="utf-8")
        return f"cat {shlex.quote(str(canned))}; sleep 600"

    def _settle(self, name: str, text: str) -> None:
        last = [ln for ln in text.splitlines() if ln.strip()][-1].strip()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if last in self.pane(name):
                return
            time.sleep(0.1)
        raise AssertionError(f"window {name} never showed {last!r}: {self.pane(name)}")

    def window(self, name: str, text: str) -> None:
        cp = self.tmux("new-window", "-d", "-n", name, self._command(name, text))
        assert cp.returncode == 0, cp.stderr
        self._settle(name, text)

    def respawn(self, name: str, text: str) -> None:
        cp = self.tmux("respawn-pane", "-k", "-t", name, self._command(name, text))
        assert cp.returncode == 0, cp.stderr
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "Do you want to" not in self.pane(name):
                break
            time.sleep(0.1)
        self._settle(name, text)

    def pane(self, name: str) -> str:
        return self.tmux("capture-pane", "-p", "-J", "-t", name).stdout

    def queued(self, name: str) -> list[Path]:
        return sorted((self.state / "fleet-queue" / name).glob("*.msg"))


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = os.environ.copy()
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    cp = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return cp.stdout.strip()


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[Fleet]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".claude" / "fleet").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "fleet", repo / "scripts" / "fleet")
    (repo / ".claude" / "fleet" / "threads.tsv").write_text(
        "# slug\teffort\tdesign-review\nalpha\thigh\tno\nbeta\tmedium\tno\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    sock = f"fleet-test-{uuid.uuid4().hex[:8]}"
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "TMUX": "/dummy/socket,1,0",  # need_tmux only checks it is set
        "PRECIS_FLEET_TMUX_SOCKET": sock,
        "PRECIS_FLEET_STATE": str(state),
    }
    env.pop("TMUX_PANE", None)
    f = Fleet(repo=repo, state=state, sock=sock, env=env)
    cp = f.tmux(
        "-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "200", "-y", "50"
    )
    assert cp.returncode == 0, cp.stderr
    try:
        yield f
    finally:
        f.tmux("kill-server")


# --- say -------------------------------------------------------------------


def test_say_inline_reaches_an_idle_pane(fleet: Fleet) -> None:
    fleet.window("alpha", IDLE)
    cp = fleet.run("say", "-m", "hello fleet world", "alpha")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("sent"), cp.stdout
    assert "hello fleet world" in fleet.pane("alpha")


def test_say_file_form_still_works_and_fills_the_slug(fleet: Fleet) -> None:
    fleet.window("alpha", IDLE)
    msg = fleet.repo / "msg.txt"
    msg.write_text("hi from {slug}\n", encoding="utf-8")
    cp = fleet.run("say", str(msg), "alpha")
    assert cp.returncode == 0, cp.stderr
    assert "hi from alpha" in fleet.pane("alpha")


@pytest.mark.parametrize("text", ["", "   "])
def test_say_inline_empty_text_is_refused(fleet: Fleet, text: str) -> None:
    fleet.window("alpha", IDLE)
    before = fleet.pane("alpha")
    cp = fleet.run("say", "-m", text, "alpha")
    assert cp.returncode == 2
    assert "empty" in cp.stderr
    assert fleet.pane("alpha") == before


def test_say_missing_file_is_refused(fleet: Fleet) -> None:
    cp = fleet.run("say", str(fleet.repo / "nope.txt"), "alpha")
    assert cp.returncode == 2


def test_say_to_a_missing_window_reports_it(fleet: Fleet) -> None:
    cp = fleet.run("say", "-m", "x", "ghost")
    assert cp.returncode == 0
    assert "no window: ghost" in cp.stdout


def test_say_when_clear_sends_now_to_an_idle_window(fleet: Fleet) -> None:
    fleet.window("alpha", IDLE)
    cp = fleet.run("say", "-m", "right away", "--when-clear", "alpha")
    assert cp.stdout.startswith("sent"), cp.stdout
    assert "right away" in fleet.pane("alpha")
    assert fleet.queued("alpha") == []


def test_say_without_when_clear_skips_a_dialog_window(fleet: Fleet) -> None:
    fleet.window("alpha", PERMISSION)
    cp = fleet.run("say", "-m", "nope", "alpha")
    assert "SKIPPED (dialog open): alpha" in cp.stdout
    assert fleet.queued("alpha") == []


# --- queue / deliver ---------------------------------------------------------


def test_when_clear_holds_for_a_dialog_then_deliver_sends(fleet: Fleet) -> None:
    fleet.window("alpha", PERMISSION)
    cp = fleet.run("say", "-m", "after the dialog", "--when-clear", "alpha")
    assert cp.returncode == 0, cp.stderr
    assert "HELD: alpha" in cp.stdout
    held = fleet.queued("alpha")
    assert len(held) == 1
    assert re.fullmatch(r"\d{8}T\d{6}Z-\d+\.msg", held[0].name)
    assert "after the dialog" not in fleet.pane("alpha")

    listing = fleet.run("queue").stdout.splitlines()
    assert len(listing) == 1
    assert re.fullmatch(r"alpha \d+s after the dialog", listing[0]), listing

    # Dialog still open: deliver leaves the message where it is.
    cp = fleet.run("deliver")
    assert cp.returncode == 0
    assert "delivered" not in cp.stdout
    assert len(fleet.queued("alpha")) == 1

    fleet.respawn("alpha", IDLE)
    cp = fleet.run("deliver")
    assert cp.returncode == 0, cp.stderr
    assert "delivered: alpha" in cp.stdout
    assert fleet.queued("alpha") == []
    assert fleet.run("queue").stdout.strip() == ""
    assert "after the dialog" in fleet.pane("alpha")


def test_deliver_sends_oldest_first(fleet: Fleet) -> None:
    fleet.window("alpha", PERMISSION)
    fleet.run("say", "-m", "first-msg", "--when-clear", "alpha")
    fleet.run("say", "-m", "second-msg", "--when-clear", "alpha")
    names = [p.name for p in fleet.queued("alpha")]
    assert names == sorted(names) and len(names) == 2
    fleet.respawn("alpha", IDLE)
    fleet.run("deliver")
    pane = fleet.pane("alpha")
    assert pane.index("first-msg") < pane.index("second-msg")
    assert fleet.queued("alpha") == []


def test_queue_keeps_a_message_whose_window_is_gone(fleet: Fleet) -> None:
    fleet.window("alpha", PERMISSION)
    fleet.run("say", "-m", "orphaned note", "--when-clear", "alpha")
    assert fleet.tmux("kill-window", "-t", "alpha").returncode == 0
    listing = fleet.run("queue").stdout
    assert (
        "alpha" in listing and "(no window)" in listing and "orphaned note" in listing
    )
    cp = fleet.run("deliver")
    assert cp.returncode == 0
    assert len(fleet.queued("alpha")) == 1


# --- verdict ---------------------------------------------------------------

HEADING = re.compile(r"^## .+ \(orchestrator, (\d{4}-\d\d-\d\d \d\d:\d\d)Z\)$")


def test_verdict_appends_a_stamped_section_and_notifies(fleet: Fleet) -> None:
    fleet.window("alpha", IDLE)
    cp = fleet.run(
        "verdict", "alpha", stdin="Claim 1 holds\n\nland it, then claim 2.\n"
    )
    assert cp.returncode == 0, cp.stderr
    review = fleet.state / "reviews" / "alpha.review.md"
    lines = review.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# alpha — review (orchestrator)"
    assert lines[1] == ""
    m = HEADING.match(lines[2])
    assert m, lines[2]
    assert lines[2].startswith("## Claim 1 holds (orchestrator, ")
    assert lines[3:] == ["", "", "land it, then claim 2."]
    stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 120
    assert "verdict written:" in fleet.pane("alpha")
    assert "newest section at the end" in fleet.pane("alpha")


def test_verdict_second_call_appends_after_the_first(fleet: Fleet) -> None:
    fleet.run("verdict", "ghost", stdin="One\nbody one\n")
    fleet.run("verdict", "ghost", stdin="Two\n")
    text = (fleet.state / "reviews" / "ghost.review.md").read_text(encoding="utf-8")
    heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert len(heads) == 2
    assert heads[0].startswith("## One (") and heads[1].startswith("## Two (")
    assert text.count("# ghost — review (orchestrator)") == 1


def test_verdict_with_no_window_still_writes_the_file(fleet: Fleet) -> None:
    cp = fleet.run("verdict", "ghost", stdin="Verdict\n")
    assert cp.returncode == 0, cp.stderr
    assert "no window: ghost" in cp.stdout
    assert (fleet.state / "reviews" / "ghost.review.md").exists()


def test_verdict_reads_a_file_and_holds_for_a_dialog(fleet: Fleet) -> None:
    fleet.window("alpha", PERMISSION)
    body = fleet.repo / "body.txt"
    body.write_text("From a file\nmore\n", encoding="utf-8")
    cp = fleet.run("verdict", "alpha", "--file", str(body))
    assert cp.returncode == 0, cp.stderr
    assert "HELD: alpha" in cp.stdout
    held = fleet.queued("alpha")
    assert len(held) == 1
    assert "verdict written:" in held[0].read_text(encoding="utf-8")
    review = fleet.state / "reviews" / "alpha.review.md"
    assert "## From a file (orchestrator, " in review.read_text(encoding="utf-8")


@pytest.mark.parametrize("body", ["", "\n\n  \n"])
def test_verdict_empty_body_is_refused_and_writes_nothing(
    fleet: Fleet, body: str
) -> None:
    cp = fleet.run("verdict", "alpha", stdin=body)
    assert cp.returncode == 2
    assert "empty body" in cp.stderr
    assert not (fleet.state / "reviews" / "alpha.review.md").exists()


def test_verdict_refuses_a_hand_stamped_heading(fleet: Fleet) -> None:
    """The stamp comes from the tool only (orchestrator verdict 2026-10-03):
    a first line already carrying `(orchestrator, …Z)` is refused, unwritten."""
    cp = fleet.run(
        "verdict",
        "alpha",
        stdin="Claim 1 LAND (orchestrator, 2026-10-03 12:00Z)\nbody\n",
    )
    assert cp.returncode == 2, cp.stdout
    assert "stamp" in cp.stderr
    assert not (fleet.state / "reviews" / "alpha.review.md").exists()


def test_verdict_rejects_a_path_slug(fleet: Fleet) -> None:
    cp = fleet.run("verdict", "../evil", stdin="x\n")
    assert cp.returncode == 2


# --- peek / dialogs ----------------------------------------------------------


def test_peek_returns_the_last_n_non_empty_lines(fleet: Fleet) -> None:
    text = "".join(f"line {i}\n\n" for i in range(1, 11))
    fleet.window("alpha", text)
    cp = fleet.run("peek", "alpha", "-n", "3")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.splitlines() == ["line 8", "line 9", "line 10"]
    default = fleet.run("peek", "alpha").stdout.splitlines()
    assert default == [f"line {i}" for i in range(1, 11)]


def test_peek_unknown_window_and_bad_count(fleet: Fleet) -> None:
    assert fleet.run("peek", "ghost").returncode == 1
    fleet.window("alpha", IDLE)
    assert fleet.run("peek", "alpha", "-n", "x").returncode == 2


def test_dialogs_classifies_each_window_and_omits_idle(fleet: Fleet) -> None:
    fleet.window("alpha", IDLE)
    fleet.window("beta", BUSY)
    fleet.window("perm", PERMISSION)
    fleet.window("boxed", PERMISSION_BOXED)
    fleet.window("quest", QUESTION)
    fleet.window("hooky", HOOK)
    cp = fleet.run("dialogs")
    assert cp.returncode == 0, cp.stderr
    rows = {ln.split(" ", 1)[0]: ln.split(" ", 2)[1:] for ln in cp.stdout.splitlines()}
    assert set(rows) == {"perm", "boxed", "quest", "hooky"}
    assert rows["perm"] == ["permission", "rm -rf /tmp/xyz"]
    assert rows["boxed"] == ["permission", "ls -la /tmp"]
    assert rows["quest"] == ["question", "Which approach?"]
    assert rows["hooky"][0] == "hook"


def test_dialogs_trust_type_and_a_hooks_path_is_not_a_hook(fleet: Fleet) -> None:
    fleet.window("fresh", TRUST)
    fleet.window("perm", PERMISSION_HOOK_PATH)
    cp = fleet.run("dialogs")
    assert cp.returncode == 0, cp.stderr
    rows = {ln.split(" ", 1)[0]: ln.split(" ", 2)[1:] for ln in cp.stdout.splitlines()}
    assert rows["fresh"][0] == "trust", rows
    fleet.window("live", QUESTION_LIVE)
    rows = {
        ln.split(" ", 1)[0]: ln.split(" ", 2)[1:]
        for ln in fleet.run("dialogs").stdout.splitlines()
    }
    assert rows["live"] == ["question", "Agent image rebuild: who runs it?"], rows
    assert rows["perm"] == ["permission", "bash scripts/hooks/session-end-reap.sh"], (
        rows
    )


def test_a_trust_dialog_holds_a_when_clear_message(fleet: Fleet) -> None:
    """The trust dialog is a dialog: a message typed into it would answer it."""
    fleet.window("alpha", TRUST)
    cp = fleet.run("say", "--when-clear", "-m", "resume", "alpha")
    assert cp.stdout.strip() == "HELD: alpha", cp.stdout


def test_dialogs_trims_the_command_to_100_chars(fleet: Fleet) -> None:
    long_cmd = "echo " + "x" * 200
    fleet.window("perm", f" Bash command\n   {long_cmd}\n\n Do you want to proceed?\n")
    row = fleet.run("dialogs").stdout.strip().split(" ", 2)
    assert row[:2] == ["perm", "permission"]
    assert row[2] == long_cmd[:100]


# --- usage -----------------------------------------------------------------


def test_usage_lists_the_new_verbs_through_the_last_header_line(fleet: Fleet) -> None:
    cp = fleet.run()
    assert cp.returncode == 2
    for verb in ("verdict", "peek", "dialogs", "refs", "deliver", "--when-clear"):
        assert verb in cp.stdout
    assert "PRECIS_FLEET_REVIEWS" in cp.stdout
    assert "set -euo" not in cp.stdout


# --- refs ------------------------------------------------------------------


def _ago(seconds: int) -> str:
    return f"{int(time.time()) - seconds} +0000"


def _remote_repo(fleet: Fleet, *, with_prod: bool = True) -> dict[str, str]:
    """origin with main (3 commits), gated at the 2nd, prod at the 1st."""
    bare = fleet.repo.parent / "origin.git"
    _git(fleet.repo.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(fleet.repo, "remote", "add", "origin", str(bare))
    shas: dict[str, str] = {}
    for tag, age in (
        ("c1", 3 * 86400 + 60),
        ("c2", 2 * 3600 + 60),
        ("c3", 12 * 60 + 5),
    ):
        (fleet.repo / f"{tag}.txt").write_text(tag, encoding="utf-8")
        _git(fleet.repo, "add", "-A")
        _git(fleet.repo, "commit", "-q", "-m", tag, date=_ago(age))
        shas[tag] = _git(fleet.repo, "rev-parse", "HEAD")
    _git(fleet.repo, "push", "-q", "origin", "main")
    _git(fleet.repo, "push", "-q", "origin", f"{shas['c2']}:refs/heads/gated")
    if with_prod:
        _git(fleet.repo, "push", "-q", "origin", f"{shas['c1']}:refs/heads/prod")
    return shas


def _fake_green(fleet: Fleet, body: str) -> None:
    helper = fleet.repo / "scripts" / "last-gated-main-sha"
    helper.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    helper.chmod(0o755)


def test_refs_prints_one_line_per_ref_with_age_and_distance(fleet: Fleet) -> None:
    shas = _remote_repo(fleet)
    _fake_green(fleet, f"echo {shas['c2']}")
    cp = fleet.run("refs")
    assert cp.returncode == 0, cp.stderr
    lines = cp.stdout.splitlines()
    assert len(lines) == 4, cp.stdout
    assert re.fullmatch(rf"main {shas['c3'][:9]} 1[0-9]m", lines[0]), lines[0]
    assert lines[1] == f"gated {shas['c2'][:9]} 2h 1 behind main"
    assert lines[2] == f"prod {shas['c1'][:9]} 3d 2 behind main"
    assert lines[3] == f"green {shas['c2'][:9]} 2h"


def test_refs_a_missing_ref_prints_missing(fleet: Fleet) -> None:
    shas = _remote_repo(fleet, with_prod=False)
    _fake_green(fleet, f"echo {shas['c3']}")
    lines = fleet.run("refs").stdout.splitlines()
    assert lines[2] == "prod missing"
    assert lines[1].startswith("gated ")


def test_refs_green_exit_contract(fleet: Fleet) -> None:
    _remote_repo(fleet)
    _fake_green(fleet, "exit 0")
    assert fleet.run("refs").stdout.splitlines()[3] == "green unknown"
    _fake_green(fleet, "exit 2")
    cp = fleet.run("refs")
    assert cp.returncode == 0
    assert cp.stdout.splitlines()[3] == "green none in 48h"
    _fake_green(fleet, "exit 1")
    assert fleet.run("refs").stdout.splitlines()[3] == "green unknown"
    (fleet.repo / "scripts" / "last-gated-main-sha").unlink()
    assert fleet.run("refs").stdout.splitlines()[3] == "green unknown"


def test_refs_fetch_failure_is_a_stderr_note_not_an_error(fleet: Fleet) -> None:
    _remote_repo(fleet)
    _git(fleet.repo, "remote", "set-url", "origin", str(fleet.repo.parent / "gone.git"))
    _fake_green(fleet, "exit 0")
    cp = fleet.run("refs")
    assert cp.returncode == 0
    assert "fetch failed" in cp.stderr
    assert cp.stdout.splitlines()[0].startswith("main ")
