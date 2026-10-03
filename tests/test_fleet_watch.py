"""scripts/fleet compact, mcp-check and watch.

Same harness as test_fleet_verbs.py: the real script against a private tmux
server whose windows are panes running `cat <canned>; sleep 600`. On top of
that: a fake `gh` on PATH (canned JSON in a file the test switches), and a fake
`~/.claude/sessions` + client-log cache for the MCP helper. Nothing here
touches the live fleet.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.test_fleet_verbs import (  # noqa: F401  (fleet is the shared fixture)
    BUSY,
    IDLE,
    PERMISSION,
    QUESTION,
    REPO,
    Fleet,
    fleet,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None,
    reason="needs POSIX bash and tmux",
)

CTX = "previous output\n\n❯ \n  {pct}% ctx\n"
CTX_BUSY = "doing things\n✻ Working… (12s · esc to interrupt)\n  {pct}% ctx\n"

GAVE_UP = "Max reconnection attempts (5) reached, giving up"
SESSION_GONE = 'Error POSTing to endpoint: {"jsonrpc":"2.0","error":{"message":"Session not found"}}'
STDIO_GONE = "Connection failed after 5ms: MCP error -32000: Connection closed"
CONNECTED = 'Connection established with capabilities: {"hasTools":true}'


@pytest.fixture
def wf(fleet: Fleet, tmp_path: Path) -> Iterator[Fleet]:  # noqa: F811
    """The shared fleet plus the MCP helper, a fake gh and short waits."""
    lib = fleet.repo / "scripts" / "lib"
    lib.mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "lib" / "fleet_mcp_state.py", lib)
    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    gh = gh_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo call >> "$FAKE_GH_DIR/calls"\n'
        '[ -e "$FAKE_GH_DIR/fail" ] && exit 1\n'
        # exec, so the alarm's SIGALRM lands on the process holding stdout.
        '[ -e "$FAKE_GH_DIR/hang" ] && exec sleep 30\n'
        'cat "$FAKE_GH_DIR/runs.json"\n',
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    (gh_dir / "runs.json").write_text("[]", encoding="utf-8")
    sessions, cache = tmp_path / "sessions", tmp_path / "cache"
    sessions.mkdir()
    cache.mkdir()
    (fleet.state / "reviews").mkdir(parents=True)
    fleet.env.update(
        {
            "PATH": f"{gh_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_DIR": str(gh_dir),
            "PRECIS_FLEET_SESSIONS_DIR": str(sessions),
            "PRECIS_FLEET_CLIENT_CACHE": str(cache),
            "PRECIS_FLEET_COMPACT_GRACE": "1",
            "PRECIS_FLEET_COMPACT_POLL": "1",
            "PRECIS_FLEET_WATCH_TICK": "1",
            "PRECIS_FLEET_WATCH_TICKS": "1",
        }
    )
    yield fleet


def wait_for(pred: Callable[[], bool], what: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def bg(f: Fleet, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(f.repo / "scripts" / "fleet"), *args],
        cwd=f.repo,
        env=f.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )


def stop(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill the script and its `sleep` child, which would hold the pipes open."""
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    return proc.communicate()


def pane_id(f: Fleet, name: str) -> str:
    return f.tmux("display-message", "-p", "-t", name, "#{pane_id}").stdout.strip()


# --- compact -----------------------------------------------------------------


def test_compact_refuses_the_callers_own_window(wf: Fleet) -> None:
    wf.window("alpha", IDLE)
    wf.env["TMUX_PANE"] = pane_id(wf, "alpha")
    cp = wf.run("compact", "alpha")
    assert cp.returncode == 3, cp.stdout
    assert "refusing alpha" in cp.stderr
    assert "/next" not in wf.pane("alpha")
    # Another window is still fine for the same caller.
    wf.window("beta", PERMISSION)
    assert wf.run("compact", "beta").returncode == 3  # refused as a dialog, not as own


def test_compact_refuses_window_zero_claude_and_organizer(wf: Fleet) -> None:
    wf.tmux("rename-window", "-t", "t:0", "seat")
    wf.window("claude", IDLE)
    wf.window("organizer", IDLE)
    for name in ("seat", "claude", "organizer"):
        cp = wf.run("compact", name)
        assert cp.returncode == 3, (name, cp.stdout)
        assert "refusing" in cp.stderr and "orchestrator" in cp.stderr
        assert "/next" not in wf.pane(name)
    cp = wf.run("compact", "--at-idle", "claude")
    assert cp.returncode == 3
    assert "/next" not in wf.pane("claude")


@pytest.mark.parametrize(("text", "why"), [(BUSY, "busy"), (PERMISSION, "dialog")])
def test_compact_without_at_idle_refuses_a_busy_or_dialog_window(
    wf: Fleet, text: str, why: str
) -> None:
    wf.window("alpha", text)
    cp = wf.run("compact", "alpha")
    assert cp.returncode == 3
    assert f"refusing alpha: {why}" in cp.stderr
    assert "/next" not in wf.pane("alpha")


def test_compact_unknown_window_exits_1(wf: Fleet) -> None:
    cp = wf.run("compact", "ghost")
    assert cp.returncode == 1
    assert "no window: ghost" in cp.stderr


def test_compact_usage_errors_exit_2(wf: Fleet) -> None:
    assert wf.run("compact").returncode == 2
    assert wf.run("compact", "--bogus", "alpha").returncode == 2


def test_compact_sends_next_then_compact_on_an_idle_window(wf: Fleet) -> None:
    wf.window("alpha", IDLE)
    cp = wf.run("compact", "alpha")
    assert cp.returncode == 0, cp.stderr
    pane = wf.pane("alpha")
    assert "/next" in pane and "/compact" in pane
    assert pane.index("/next") < pane.index("/compact")
    assert cp.stdout.count("sent") == 2


def test_compact_waits_while_the_window_is_busy_after_next(wf: Fleet) -> None:
    wf.window("alpha", IDLE)
    proc = bg(wf, "compact", "alpha")
    try:
        wait_for(lambda: "/next" in wf.pane("alpha"), "/next to reach the pane")
        wf.respawn("alpha", BUSY)  # the window turns busy once /next registers
        time.sleep(5)
        assert proc.poll() is None, "compact finished while the window was busy"
        assert "/compact" not in wf.pane("alpha")
        wf.respawn("alpha", IDLE)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        assert "/compact" in wf.pane("alpha")
    finally:
        stop(proc)


def test_compact_at_idle_waits_through_busy_before_next(wf: Fleet) -> None:
    wf.window("alpha", BUSY)
    proc = bg(wf, "compact", "alpha", "--at-idle")
    try:
        time.sleep(3)
        assert proc.poll() is None
        assert "/next" not in wf.pane("alpha")
        wf.respawn("alpha", IDLE)
        out, err = proc.communicate(timeout=40)
        assert proc.returncode == 0, err
        pane = wf.pane("alpha")
        assert pane.index("/next") < pane.index("/compact")
    finally:
        stop(proc)


@pytest.mark.parametrize("text", [BUSY, QUESTION])
def test_compact_at_idle_times_out_and_sends_nothing(wf: Fleet, text: str) -> None:
    wf.window("alpha", text)
    wf.env["PRECIS_FLEET_COMPACT_WAIT"] = "3"
    cp = wf.run("compact", "alpha", "--at-idle")
    assert cp.returncode == 3
    assert "not idle within 3s" in cp.stderr
    assert "nothing sent" in cp.stderr
    assert "/next" not in wf.pane("alpha")


def test_compact_wait_cap_after_next_sends_no_compact(wf: Fleet) -> None:
    wf.window("alpha", IDLE)
    wf.env["PRECIS_FLEET_COMPACT_WAIT"] = "5"
    proc = bg(wf, "compact", "alpha")
    try:
        wait_for(lambda: "/next" in wf.pane("alpha"), "/next to reach the pane")
        wf.respawn("alpha", BUSY)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 3
        assert "/compact not sent" in err
        assert "/compact" not in wf.pane("alpha")
    finally:
        stop(proc)


def test_compact_several_windows_exit_code_is_the_worst(wf: Fleet) -> None:
    wf.window("claude", IDLE)
    wf.window("alpha", IDLE)
    cp = wf.run("compact", "claude", "alpha")
    assert cp.returncode == 3
    assert "/compact" in wf.pane("alpha")
    assert "/next" not in wf.pane("claude")


# --- mcp-check ----------------------------------------------------------------


def _session(f: Fleet, n: int, pane: str) -> str:
    sid = f"sid-{n}"
    sessions = Path(f.env["PRECIS_FLEET_SESSIONS_DIR"])
    (sessions / f"{n}.json").write_text(
        json.dumps(
            {"pid": n, "sessionId": sid, "startedAt": 1000, "tmux": f"0:@{n}.{pane}"}
        ),
        encoding="utf-8",
    )
    return sid


def _log(f: Fleet, server: str, sid: str, text: str, stamp: str) -> None:
    d = Path(f.env["PRECIS_FLEET_CLIENT_CACHE"]) / "proj" / f"mcp-logs-{server}"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"debug": text, "timestamp": stamp, "sessionId": sid}) + "\n"
        )


def _mcp(f: Fleet, n: int, win: str, precis: str | None, cc: str | None) -> None:
    sid = _session(f, n, pane_id(f, win))
    if precis:
        _log(f, "precis", sid, precis, "2026-10-03T12:00:00Z")
    if cc:
        _log(f, "claude-context", sid, cc, "2026-10-03T12:00:00Z")


def _mcp_fleet(wf: Fleet) -> None:
    wf.tmux("rename-window", "-t", "t:0", "seat")
    for name, text in (
        ("alpha", IDLE),
        ("beta", BUSY),
        ("gamma", PERMISSION),
        ("delta", IDLE),
        ("epsilon", IDLE),
    ):
        wf.window(name, text)
    _mcp(wf, 1, "seat", GAVE_UP, CONNECTED)
    _mcp(wf, 2, "alpha", GAVE_UP, CONNECTED)  # precis dead
    _mcp(wf, 3, "beta", CONNECTED, STDIO_GONE)  # claude-context dead
    _mcp(wf, 4, "gamma", SESSION_GONE, CONNECTED)  # precis session expired
    _mcp(wf, 5, "delta", CONNECTED, CONNECTED)
    # epsilon: no session record at all


def test_mcp_check_reports_each_window(wf: Fleet) -> None:
    _mcp_fleet(wf)
    cp = wf.run("mcp-check")
    assert cp.returncode == 0, cp.stderr
    assert sorted(cp.stdout.splitlines()) == [
        "alpha precis=dead claude-context=connected",
        "beta precis=connected claude-context=dead",
        "delta precis=connected claude-context=connected",
        "epsilon precis=unknown claude-context=unknown",
        "gamma precis=dead claude-context=connected",
        "seat precis=dead claude-context=connected",
    ]
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        assert "/mcp" not in wf.pane(name)  # plain check never sends


def test_mcp_check_fix_sends_only_to_idle_dead_windows(wf: Fleet) -> None:
    _mcp_fleet(wf)
    cp = wf.run("mcp-check", "--fix")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout.splitlines()
    assert "pending: beta (busy)" in out
    assert "pending: gamma (dialog)" in out
    assert "skipped: seat (orchestrator)" in out
    assert any(ln.startswith("sent") and "alpha" in ln for ln in out)
    assert "/mcp reconnect all" in wf.pane("alpha")
    for name in ("beta", "gamma", "delta", "epsilon", "seat"):
        assert "/mcp reconnect all" not in wf.pane(name), name


def test_mcp_check_without_the_helper_exits_1(wf: Fleet) -> None:
    (wf.repo / "scripts" / "lib" / "fleet_mcp_state.py").unlink()
    cp = wf.run("mcp-check")
    assert cp.returncode == 1
    assert "unknown" in cp.stderr


def test_mcp_check_rejects_a_bad_flag(wf: Fleet) -> None:
    assert wf.run("mcp-check", "--bogus").returncode == 2


# --- watch ---------------------------------------------------------------------


def _runs(f: Fleet, *runs: tuple[int, str, str]) -> None:
    """Newest first, the way gh prints them."""
    Path(f.env["FAKE_GH_DIR"], "runs.json").write_text(
        json.dumps(
            [{"databaseId": i, "headSha": sha, "conclusion": c} for i, sha, c in runs]
        ),
        encoding="utf-8",
    )


def _watch(f: Fleet) -> list[str]:
    cp = f.run("watch")
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.splitlines()


def test_watch_seeds_silently_then_emits_one_line_per_transition(wf: Fleet) -> None:
    reviews = wf.state / "reviews"
    note = reviews / "alpha.md"
    note.write_text("design", encoding="utf-8")
    (reviews / "alpha.review.md").write_text("verdict", encoding="utf-8")
    os.utime(note, (1_700_000_000, 1_700_000_000))
    _runs(wf, (11, "a" * 40, "success"))
    wf.window("alpha", PERMISSION)  # a dialog already open at start
    wf.window("beta", IDLE)
    wf.window("gamma", PERMISSION)  # holds a message below

    # First start: nothing existing is announced, and the state file appears.
    assert _watch(wf) == []
    state = wf.state / "fleet-watch.state"
    assert state.exists()
    assert not list(wf.state.glob("fleet-watch.state.tmp*"))
    # A second run over unchanged state replays nothing.
    snapshot = state.read_text(encoding="utf-8")
    assert _watch(wf) == []
    assert state.read_text(encoding="utf-8") == snapshot

    os.utime(note, (1_700_000_100, 1_700_000_100))
    assert _watch(wf) == ["note alpha changed"]
    assert _watch(wf) == []

    (reviews / "beta.md").write_text("new note", encoding="utf-8")
    (reviews / "beta.review.md").write_text("review", encoding="utf-8")  # excluded
    assert _watch(wf) == ["note beta new"]

    _runs(wf, (12, "b" * 40, "failure"), (11, "a" * 40, "success"))
    assert _watch(wf) == [f"ci main {'b' * 9} failure"]
    assert _watch(wf) == []

    wf.respawn("beta", PERMISSION)
    assert _watch(wf) == ["dialog beta permission"]
    assert _watch(wf) == []  # still open: nothing
    wf.respawn("beta", IDLE)
    assert _watch(wf) == []  # cleared: nothing
    wf.respawn("beta", QUESTION)
    assert _watch(wf) == ["dialog beta question"]

    # A held message goes out once its dialog clears; one `delivered` line, no
    # `sent:` echo, and the clearing itself is silent.
    cp = wf.run("say", "-m", "after the dialog", "--when-clear", "gamma")
    assert "HELD: gamma" in cp.stdout
    assert _watch(wf) == []  # dialog still open: stays held
    assert len(wf.queued("gamma")) == 1
    wf.respawn("gamma", IDLE)
    assert _watch(wf) == ["delivered gamma"]
    assert wf.queued("gamma") == []
    assert "after the dialog" in wf.pane("gamma")


def test_watch_ctx_emits_on_a_new_bucket_at_or_above_the_floor(wf: Fleet) -> None:
    wf.window("alpha", CTX.format(pct=25))
    assert _watch(wf) == []  # seed
    wf.respawn("alpha", CTX.format(pct=27))
    assert _watch(wf) == []  # below the 30 floor
    wf.respawn("alpha", CTX.format(pct=34))
    assert _watch(wf) == ["ctx alpha 34% idle"]
    wf.respawn("alpha", CTX.format(pct=39))
    assert _watch(wf) == []  # same bucket
    wf.respawn("alpha", CTX_BUSY.format(pct=41))
    assert _watch(wf) == ["ctx alpha 41% busy"]
    wf.respawn("alpha", CTX.format(pct=12))  # compacted
    assert _watch(wf) == []
    wf.respawn("alpha", CTX.format(pct=33))  # grows again: a fresh crossing
    assert _watch(wf) == ["ctx alpha 33% idle"]


def test_watch_ctx_floor_is_configurable(wf: Fleet) -> None:
    wf.env["PRECIS_FLEET_CTX_FLOOR"] = "60"
    wf.window("alpha", CTX.format(pct=10))
    _watch(wf)
    wf.respawn("alpha", CTX.format(pct=45))
    assert _watch(wf) == []
    wf.respawn("alpha", CTX.format(pct=61))
    assert _watch(wf) == ["ctx alpha 61% idle"]


def test_watch_ci_failure_is_silent_and_the_first_success_only_seeds(
    wf: Fleet,
) -> None:
    gh_dir = Path(wf.env["FAKE_GH_DIR"])
    _runs(wf, (5, "c" * 40, "success"), (4, "d" * 40, "failure"))
    (gh_dir / "fail").write_text("", encoding="utf-8")
    assert _watch(wf) == []  # gh down at first start
    (gh_dir / "fail").unlink()
    assert _watch(wf) == []  # first successful listing seeds, no replay
    _runs(wf, (6, "e" * 40, "cancelled"), (5, "c" * 40, "success"))
    assert _watch(wf) == [f"ci main {'e' * 9} cancelled"]


def test_watch_ci_runs_every_eighth_tick(wf: Fleet) -> None:
    wf.env["PRECIS_FLEET_WATCH_TICKS"] = "9"
    wf.env["PRECIS_FLEET_WATCH_TICK"] = "0"
    assert _watch(wf) == []
    calls = (Path(wf.env["FAKE_GH_DIR"]) / "calls").read_text(encoding="utf-8")
    assert len(calls.splitlines()) == 2  # ticks 0 and 8 of 0..8


def test_watch_new_ci_runs_come_out_oldest_first(wf: Fleet) -> None:
    _runs(wf, (1, "a" * 40, "success"))
    _watch(wf)
    _runs(
        wf,
        (3, "c" * 40, "failure"),
        (2, "b" * 40, "success"),
        (1, "a" * 40, "success"),
    )
    assert _watch(wf) == [f"ci main {'b' * 9} success", f"ci main {'c' * 9} failure"]


def test_watch_emits_mcp_dead_on_transition_only(wf: Fleet) -> None:
    wf.window("alpha", IDLE)
    wf.window("beta", IDLE)
    _mcp(wf, 1, "alpha", GAVE_UP, CONNECTED)  # dead before the watch starts
    _mcp(wf, 2, "beta", CONNECTED, CONNECTED)
    assert _watch(wf) == []  # seeded, not announced
    assert _watch(wf) == []
    _log(wf, "claude-context", "sid-2", STDIO_GONE, "2026-10-03T13:00:00Z")
    assert _watch(wf) == ["mcp beta dead"]
    assert _watch(wf) == []
    _log(wf, "claude-context", "sid-2", CONNECTED, "2026-10-03T14:00:00Z")
    assert _watch(wf) == []  # recovery is not announced
    _log(wf, "precis", "sid-2", SESSION_GONE, "2026-10-03T15:00:00Z")
    assert _watch(wf) == ["mcp beta dead"]  # a second death is a new transition


def test_watch_state_survives_a_restart_with_a_transition_in_between(
    wf: Fleet,
) -> None:
    """A re-arm replays nothing but still reports what changed while it was down."""
    wf.window("alpha", IDLE)
    assert _watch(wf) == []
    wf.respawn("alpha", QUESTION)
    assert _watch(wf) == ["dialog alpha question"]


def test_watch_ctx_alias_still_runs(wf: Fleet) -> None:
    """`watch-ctx` is the old loop: it emits at startup, then every 60 s."""
    wf.window("alpha", CTX.format(pct=44))
    proc = bg(wf, "watch-ctx")
    try:
        wait_for(lambda: proc.poll() is not None or _readable(proc), "a ctx line")
    finally:
        out, _ = stop(proc)
    assert "ctx alpha 44% idle" in out


def _readable(proc: subprocess.Popen[str]) -> bool:
    import select

    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 0)
    return bool(ready)


def test_usage_lists_compact_mcp_check_and_watch(wf: Fleet) -> None:
    cp = wf.run()
    assert cp.returncode == 2
    for word in (
        "compact",
        "--at-idle",
        "mcp-check",
        "--fix",
        "scripts/fleet watch ",
        "PRECIS_FLEET_WATCH_TICKS",
        "fleet-watch.state",
    ):
        assert word in cp.stdout, word
    assert "set -euo" not in cp.stdout


def test_watch_a_hung_gh_is_cut_off_and_the_tick_finishes(wf: Fleet) -> None:
    """A hung gh would stall every tick and the queue drain with it."""
    gh_dir = Path(wf.env["FAKE_GH_DIR"])
    (gh_dir / "hang").write_text("", encoding="utf-8")
    wf.env["PRECIS_FLEET_GH_TIMEOUT"] = "1"
    started = time.monotonic()
    assert _watch(wf) == []
    assert time.monotonic() - started < 15
