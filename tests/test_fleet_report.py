"""scripts/fleet-report against fixture transcripts, rollouts and a fake tmux."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-report"

FAKE_TMUX = """#!/bin/sh
case "$1" in
  list-panes) cat "$FAKE_PANES" ;;
  capture-pane) cat "$FAKE_CAPTURE" ;;
  *) exit 1 ;;
esac
"""


def stamp(age_s: float = 0) -> str:
    return datetime.fromtimestamp(time.time() - age_s, UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def write_jsonl(path: Path, rows: list[dict], age_s: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    t = time.time() - age_s
    os.utime(path, (t, t))


def assistant(content: list[dict], stop: str | None, age_s: float = 0) -> dict:
    return {
        "type": "assistant",
        "timestamp": stamp(age_s),
        "message": {
            "role": "assistant",
            "stop_reason": stop,
            "content": content,
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 100_000,
                "cache_creation_input_tokens": 20_000,
            },
        },
    }


def user_result(age_s: float = 0) -> dict:
    return {
        "type": "user",
        "timestamp": stamp(age_s),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    }


@dataclass
class Env:
    tmp_path: Path
    repo: Path
    panes: Path
    capture: Path
    bindir: Path
    transcript_dir: Path


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "proj"
    repo.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), *ident, "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    (repo / ".claude").mkdir()
    (repo / ".claude" / "purpose").write_text(
        "\n  build the thing\nsecond\n", encoding="utf-8"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text(FAKE_TMUX, encoding="utf-8")
    tmux.chmod(0o755)
    panes = tmp_path / "panes.txt"
    capture = tmp_path / "capture.txt"
    panes.write_text("", encoding="utf-8")
    capture.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PANES", str(panes))
    monkeypatch.setenv("FAKE_CAPTURE", str(capture))
    return Env(
        tmp_path=tmp_path,
        repo=repo,
        panes=panes,
        capture=capture,
        bindir=bindir,
        transcript_dir=home
        / ".claude"
        / "projects"
        / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve())),
    )


def run_script(e, *args: str) -> str:
    r = subprocess.run(
        [str(SCRIPT), "--repo", str(e.repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def report(e, *args: str) -> dict:
    return json.loads(run_script(e, "--json", *args))


def pane_line(e, command="claude", activity=None) -> str:
    activity = activity or int(time.time())
    return f"main\t3\tproj-tree\t%7\t123\t{command}\t{e.repo.resolve()}\t{activity}\n"


def test_claude_idle_and_context(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "text", "text": "done"}], "end_turn", 5)],
    )
    row = report(env)["rows"][0]
    assert (row["vendor"], row["state"], row["project"], row["purpose"]) == (
        "claude",
        "idle",
        "proj",
        "build the thing",
    )
    assert row["ctx_tokens"] == 120_002
    assert row["ctx_pct"] == 60.0
    assert row["attach"] is None  # no pane


def test_claude_working_pending_tool_and_tool_result(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "tool_use", "id": "t1"}], "tool_use", 5)],
    )
    assert report(env)["rows"][0]["state"] == "working"
    rows = [
        assistant([{"type": "tool_use", "id": "t1"}], "tool_use", 9),
        user_result(2),
    ]
    write_jsonl(env.transcript_dir / "a.jsonl", rows)
    assert report(env)["rows"][0]["state"] == "working"


def test_newest_transcript_wins_and_tail_only(env):
    write_jsonl(
        env.transcript_dir / "old.jsonl",
        [assistant([{"type": "tool_use"}], "tool_use")],
        age_s=500,
    )
    pad = [
        {"type": "attachment", "x": "y" * 1000}
    ] * 600  # > TAIL_BYTES before the real end
    write_jsonl(
        env.transcript_dir / "new.jsonl",
        [*pad, assistant([{"type": "text"}], "end_turn", 1)],
        age_s=1,
    )
    assert report(env)["rows"][0]["state"] == "idle"


def test_codex_rate_limits_and_context(env):
    sessions = env.tmp_path / "codex" / "sessions" / "2026" / "10" / "09"
    token_count = {
        "type": "token_count",
        "info": {
            "last_token_usage": {"input_tokens": 100_000},
            "model_context_window": 200_000,
        },
        "rate_limits": {
            "primary": {"used_percent": 18.0, "window_minutes": 300},
            "secondary": {"used_percent": 61.0, "window_minutes": 10080},
        },
    }
    rows = [
        {
            "timestamp": stamp(60),
            "type": "session_meta",
            # Daemon mode: session_meta holds the daemon's cwd, not the tree.
            "payload": {"cwd": str(env.tmp_path / "home")},
        },
        {
            "timestamp": stamp(55),
            "type": "turn_context",
            "payload": {"cwd": str(env.repo.resolve())},
        },
        {
            "timestamp": stamp(50),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {"timestamp": stamp(40), "type": "event_msg", "payload": token_count},
        {
            "timestamp": stamp(30),
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]
    write_jsonl(sessions / "rollout-2026-10-09T00-00-00-abc.jsonl", rows, age_s=30)
    row = next(r for r in report(env)["rows"] if r["vendor"] == "codex")
    assert row["state"] == "idle"
    assert row["ctx_pct"] == 50.0
    assert row["rate_limits"]["secondary"]["used_percent"] == 61.0
    assert "QUOTA   codex 5h 18% · codex week 61%" in run_script(env)


def test_codex_footer_without_rollout_and_account_quota(env):
    # A fresh Codex pane has no rollout yet; its footer gives state and context.
    env.panes.write_text(pane_line(env, command="codex"), encoding="utf-8")
    env.capture.write_text(
        "› Ask Codex to do anything\n"
        "  proj · ~/proj · main · No changes · Context 87% left · Ready\n",
        encoding="utf-8",
    )
    # An editor session in $HOME still carries the account's quota.
    sessions = env.tmp_path / "codex" / "sessions"
    write_jsonl(
        sessions / "rollout-2026-10-09T00-00-00-home.jsonl",
        [
            {
                "timestamp": stamp(60),
                "type": "session_meta",
                "payload": {"cwd": str(env.tmp_path / "home")},
            },
            {
                "timestamp": stamp(40),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 50.0, "window_minutes": 10080},
                        "secondary": None,
                    },
                },
            },
        ],
        age_s=40,
    )
    rep = report(env)
    row = next(r for r in rep["rows"] if r["vendor"] == "codex")
    assert (row["state"], row["ctx_pct"]) == ("idle", 13.0)
    assert "QUOTA   codex week 50%" in run_script(env)

    env.capture.write_text(
        "  proj · ~/proj · main · Context 60% left · Working (12s)\n", encoding="utf-8"
    )
    assert (
        next(r for r in report(env)["rows"] if r["vendor"] == "codex")["state"]
        == "working"
    )


def test_approval_pattern_marks_waiting_with_attach(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "tool_use"}], "tool_use", 5)],
    )
    env.panes.write_text(pane_line(env), encoding="utf-8")
    env.capture.write_text(
        "Bash command\n Do you want to proceed?\n 1. Yes\n 2. No\n", encoding="utf-8"
    )
    rep = report(env)
    row = rep["rows"][0]
    assert row["state"] == "waiting"
    assert row["attach"] == "tmux attach -t main:3"
    assert any(
        x["reason"] == "waiting on approval" and x["attach"] for x in rep["exceptions"]
    )


def test_no_approval_text_stays_working(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "tool_use"}], "tool_use", 5)],
    )
    env.panes.write_text(pane_line(env), encoding="utf-8")
    env.capture.write_text("running tests...\n", encoding="utf-8")
    assert report(env)["rows"][0]["state"] == "working"


def test_exception_rules(env):
    # working but silent for an hour with the pane present -> quiet; ctx 120k/150k -> 80%
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "tool_use"}], "tool_use", 3600)],
        age_s=3600,
    )
    env.panes.write_text(
        pane_line(env, activity=int(time.time()) - 3600), encoding="utf-8"
    )
    reasons = [
        x["reason"] for x in report(env, "--claude-window", "150000")["exceptions"]
    ]
    assert any(r.startswith("quiet 60m") for r in reasons)
    assert any(r.startswith("context 80%") for r in reasons)
    assert "dead" not in reasons
    relaxed = [x["reason"] for x in report(env, "--quiet-min", "90")["exceptions"]]
    assert not any(r.startswith("quiet") for r in relaxed)


def test_dead_when_pane_gone_and_working_silent(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "tool_use"}], "tool_use", 1200)],
        age_s=1200,
    )
    rep = report(env)
    assert rep["rows"][0]["state"] == "dead"
    assert rep["exceptions"][0]["reason"] == "dead"


def test_no_tmux_server_is_not_an_error(env):
    (env.bindir / "tmux").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    rep = report(env)
    assert rep["rows"] == [] and rep["exceptions"] == []


def test_json_shape(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    env.panes.write_text(pane_line(env), encoding="utf-8")
    rep = report(env)
    assert set(rep) == {"host", "generated", "rows", "exceptions", "quota"}
    assert rep["generated"].endswith("Z") and "." not in rep["host"]
    keys = {
        "vendor",
        "host",
        "project",
        "tree",
        "branch",
        "dirty",
        "ahead",
        "behind",
        "purpose",
        "state",
    }
    assert keys | {"quiet_min", "ctx_pct", "attach"} <= set(rep["rows"][0])
    assert rep["rows"][0]["tmux"]["pane_id"] == "%7"


def test_text_output_blocks(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    out = run_script(env)
    assert out.splitlines()[0] == "EXCEPTIONS"
    assert re.search(r"claude@\S+/proj/proj · idle · \d+m · build the thing · 60%", out)
