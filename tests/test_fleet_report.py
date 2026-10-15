"""scripts/fleet-report against fixture transcripts, rollouts and a fake tmux."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

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


def assistant(
    content: list[dict],
    stop: str | None,
    age_s: float = 0,
    model: str | None = None,
) -> dict:
    message: dict[str, Any] = {
        "role": "assistant",
        "stop_reason": stop,
        "content": content,
        "usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 20_000,
        },
    }
    if model:
        message["model"] = model
    return {"type": "assistant", "timestamp": stamp(age_s), "message": message}


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


def test_codex_async_question_beats_ready_footer(env):
    # request_user_input_async completes the turn; the footer says Ready while
    # the question still waits for Reto. Shape seen on melchior 2026-10-09.
    env.panes.write_text(pane_line(env, command="codex"), encoding="utf-8")
    env.capture.write_text(
        "  proj · ~/proj · main · Context 98% left · Ready\n", encoding="utf-8"
    )
    call = {"type": "function_call", "name": "request_user_input_async"}
    write_jsonl(
        env.tmp_path / "codex" / "sessions" / "rollout-2026-10-09T10-48-00-q.jsonl",
        [
            {"timestamp": stamp(60), "type": "session_meta", "payload": {}},
            {
                "timestamp": stamp(59),
                "type": "turn_context",
                "payload": {"cwd": str(env.repo.resolve())},
            },
            {
                "timestamp": stamp(58),
                "type": "event_msg",
                "payload": {"type": "task_started"},
            },
            {"timestamp": stamp(50), "type": "response_item", "payload": call},
            {
                "timestamp": stamp(45),
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
        ],
        age_s=45,
    )
    rep = report(env)
    assert next(r for r in rep["rows"] if r["vendor"] == "codex")["state"] == "asking"
    assert any(x["reason"] == "question for Reto" for x in rep["exceptions"])


def test_codex_question_modal_on_screen_is_asking(env):
    # Plan-mode modal, captured on melchior 2026-10-09; no rollout needed.
    env.panes.write_text(pane_line(env, command="codex"), encoding="utf-8")
    env.capture.write_text(
        "  Question 1/1 (1 unanswered)\n"
        "  Which would you like to discuss: A, B, or C?\n"
        "  › 1. A                  Discuss A.\n"
        "    2. B                  Discuss B.\n"
        "  tab to add notes | enter to submit answer | esc to interrupt\n",
        encoding="utf-8",
    )
    row = next(r for r in report(env)["rows"] if r["vendor"] == "codex")
    assert row["state"] == "asking"


def test_claude_window_follows_the_transcript_model(env):
    # 120_002 tokens: 12.0% of a 1M window, 60.0% of 200k.
    for model, pct in (
        ("claude-opus-5-5", 12.0),
        ("claude-sonnet-5-5", 12.0),
        ("claude-fable-5-1", 12.0),
        ("claude-haiku-4-5-20251001", 60.0),
        ("claude-haiku-4-5[1m]", 12.0),
        ("unlisted-1", 60.0),
    ):
        write_jsonl(
            env.transcript_dir / "a.jsonl",
            [assistant([{"type": "text"}], "end_turn", 5, model=model)],
        )
        row = report(env)["rows"][0]
        assert row["ctx_pct"] == pct, model
        assert row["model"] == model


def test_claude_window_flag_overrides_the_model_table(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "text"}], "end_turn", 5, model="claude-opus-5-5")],
    )
    row = report(env, "--claude-window", "150000")["rows"][0]
    assert row["ctx_pct"] == 80.0


def test_claude_pane_footer_ctx_wins_over_the_transcript(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl",
        [assistant([{"type": "text"}], "end_turn", 5, model="claude-opus-5-5")],
    )
    env.panes.write_text(pane_line(env, command="claude"), encoding="utf-8")
    env.capture.write_text(
        "stale scrollback 55% ctx\n"
        "❯\n"
        "────────────────────────────────────────\n"
        "  Opus 5.5  19% ctx\n"
        "  -- INSERT -- ⏵⏵ auto mode on\n",
        encoding="utf-8",
    )
    row = report(env)["rows"][0]
    assert row["ctx_pct"] == 19.0  # the last %ctx line is the live footer


def test_devin_pane_footer_gives_ctx_for_a_devin_row(env):
    env.panes.write_text(pane_line(env, command="devin"), encoding="utf-8")
    env.capture.write_text(
        "❭ Ask Devin to build features\n"
        "SWE-2 High            Context: 212k / 262k tokens (81%)\n",
        encoding="utf-8",
    )
    row = next(r for r in report(env)["rows"] if r["vendor"] == "devin")
    assert row["ctx_pct"] == 81.0


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
    assert set(rep) == {
        "host",
        "version",
        "generated",
        "rows",
        "exceptions",
        "quota",
    }
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
    assert keys | {"quiet_min", "last_active", "ctx_pct", "attach"} <= set(
        rep["rows"][0]
    )
    assert rep["rows"][0]["tmux"]["pane_id"] == "%7"


def test_text_output_blocks(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    out = run_script(env)
    assert out.splitlines()[0] == "EXCEPTIONS"
    assert re.search(r"claude@\S+/proj/proj · idle · \d+m · build the thing · 60%", out)


def test_text_output_ready_line_and_tmux_target(env):
    (env.repo / ".claude" / "purpose").write_text(
        "proj at 93f5ef461: landed the slice\n", encoding="utf-8"
    )
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    env.panes.write_text(pane_line(env), encoding="utf-8")
    out = run_script(env)
    lines = out.splitlines()
    ready = lines[lines.index("READY") + 1 : lines.index("AGENTS")]
    assert len(ready) == 1
    assert re.match(
        r"  claude@\S+/proj/proj main:3 · done@93f5ef461"
        r"   attach: tmux attach -t main:3",
        ready[0],
    )
    assert re.search(
        r"claude@\S+/proj/proj main:3 · idle · \d+m · proj at 93f5ef461: landed the slice · 60% · done@93f5ef461",
        out,
    )


def test_last_active_is_iso_utc_and_matches_quiet(env):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 600)]
    )
    row = report(env)["rows"][0]
    la = datetime.fromisoformat(row["last_active"].replace("Z", "+00:00"))
    assert row["last_active"].endswith("Z")
    assert abs((datetime.now(UTC) - la).total_seconds() / 60 - row["quiet_min"]) < 1


class FakeMcp:
    """Minimal streamable-HTTP MCP: records every POST."""

    def __init__(self, sse: bool) -> None:
        self.sse = sse
        self.posts: list[tuple[dict, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((dict(self.headers), body))
                if "id" not in body:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                result: dict[str, Any]
                if body["method"] == "initialize":
                    result = {"protocolVersion": "2025-03-26", "capabilities": {}}
                else:
                    result = {"content": [{"type": "text", "text": "reported ok"}]}
                msg = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
                if outer.sse:
                    payload = f"event: message\ndata: {msg}\n\n".encode()
                    ctype = "text/event-stream"
                else:
                    payload, ctype = msg.encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Mcp-Session-Id", "sess-1")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("sse", [False, True])
def test_push_runs_the_mcp_handshake_and_puts_the_report(env, monkeypatch, sse):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    with FakeMcp(sse) as mcp:
        monkeypatch.setenv("PRECIS_FLEET_MCP_URL", mcp.url)
        monkeypatch.setenv("PRECIS_MCP_TOKEN", "tok-secret")
        out = run_script(env, "--push")
    assert out.strip() == "local: reported ok"
    assert "tok-secret" not in out
    methods = [b["method"] for _, b in mcp.posts]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    for headers, _ in mcp.posts:
        assert headers["Authorization"] == "Bearer tok-secret"
    assert "Mcp-Session-Id" not in mcp.posts[0][0]
    assert mcp.posts[1][0]["Mcp-Session-Id"] == "sess-1"
    assert mcp.posts[2][0]["Mcp-Session-Id"] == "sess-1"
    call = mcp.posts[2][1]["params"]
    assert call["name"] == "put"
    assert call["arguments"]["kind"] == "fleet"
    assert call["arguments"]["mode"] == "report"
    sent = call["arguments"]["args"]
    assert sent["host"] == sent["report"]["host"]
    assert sent["report"]["rows"][0]["last_active"]


def test_push_reads_the_token_from_a_file(env, monkeypatch, tmp_path):
    write_jsonl(
        env.transcript_dir / "a.jsonl", [assistant([{"type": "text"}], "end_turn", 5)]
    )
    token_file = tmp_path / "token"
    token_file.write_text("tok-from-file\n", encoding="utf-8")
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_MCP_TOKEN_FILE", str(token_file))
    with FakeMcp(False) as mcp:
        monkeypatch.setenv("PRECIS_FLEET_MCP_URL", mcp.url)
        out = run_script(env, "--push")
    assert out.strip() == "local: reported ok"
    assert mcp.posts[0][0]["Authorization"] == "Bearer tok-from-file"


def test_push_failure_exits_nonzero_without_leaking_token(env, monkeypatch):
    monkeypatch.setenv("PRECIS_FLEET_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "tok-secret")
    r = subprocess.run(
        [str(SCRIPT), "--repo", str(env.repo), "--push"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 1
    assert "tok-secret" not in r.stdout + r.stderr


def test_remote_runs_script_over_ssh_and_pushes_that_report(env, monkeypatch):
    fake_ssh = env.bindir / "ssh"
    # Stands in for the remote: ignores host/command, runs the script from stdin.
    fake_ssh.write_text(
        '#!/bin/sh\nexec python3 - --json --repo "$FAKE_REPO"\n', encoding="utf-8"
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("FAKE_REPO", str(env.repo))
    with FakeMcp(False) as mcp:
        monkeypatch.setenv("PRECIS_FLEET_MCP_URL", mcp.url)
        out = run_script(env, "--remote", "someone@remote")
    assert out.strip() == "someone@remote: reported ok"
    sent = mcp.posts[2][1]["params"]["arguments"]["args"]
    assert sent["report"]["rows"] == [] and sent["host"]


def run_hook(e, payload: dict, *args: str):
    return subprocess.run(
        [str(SCRIPT), "--hook", *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def hook_json(e, payload: dict, *args: str) -> dict:
    r = run_hook(e, payload, "--json", *args)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_hook_reports_only_the_calling_session(env, monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    rep = hook_json(
        env,
        {"cwd": str(env.repo), "hook_event_name": "SessionStart"},
        "--vendor",
        "devin",
    )
    assert rep["partial"] is True
    (row,) = rep["rows"]
    assert (row["vendor"], row["project"], row["tree"]) == ("devin", "proj", "proj")
    assert (row["state"], row["via"], row["purpose"]) == (
        "working",
        "hook",
        "build the thing",
    )
    assert row["tmux"] is None and row["attach"] is None


def test_hook_state_flag_beats_event_and_dead_drops_the_pane(env, monkeypatch):
    env.panes.write_text(pane_line(env, command="devin"), encoding="utf-8")
    monkeypatch.setenv("TMUX_PANE", "%7")
    rep = hook_json(
        env,
        {"cwd": str(env.repo), "hook_event_name": "SessionStart"},
        "--vendor",
        "devin",
        "--state",
        "dead",
    )
    row = rep["rows"][0]
    assert row["state"] == "dead"
    assert row["tmux"] is None and row["attach"] is None


def test_hook_ctx_and_vendor_from_the_own_pane(env, monkeypatch):
    env.panes.write_text(pane_line(env, command="devin"), encoding="utf-8")
    env.capture.write_text(
        "SWE-2 High            Context: 212k / 262k tokens (81%)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TMUX_PANE", "%7")
    rep = hook_json(env, {"cwd": str(env.repo)})
    row = rep["rows"][0]
    assert row["ctx_pct"] == 81.0
    assert row["vendor"] == "devin"  # detected from the pane command
    assert row["tmux"]["session"] == "main" and row["attach"] == "tmux attach -t main:3"
    assert row["state"] == "unknown"  # no --state and no hook_event_name


def test_hook_ctx_from_the_claude_transcript(env, monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    t = env.tmp_path / "t.jsonl"
    write_jsonl(t, [assistant([{"type": "text"}], "end_turn", 5)])
    rep = hook_json(
        env,
        {
            "cwd": str(env.repo),
            "transcript_path": str(t),
            "hook_event_name": "Stop",
        },
        "--vendor",
        "claude",
    )
    row = rep["rows"][0]
    assert row["ctx_pct"] == 60.0 and row["state"] == "idle"


def test_hook_push_sends_the_partial_report(env, monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    with FakeMcp(False) as mcp:
        monkeypatch.setenv("PRECIS_FLEET_MCP_URL", mcp.url)
        monkeypatch.setenv("PRECIS_MCP_TOKEN", "tok-secret")
        r = run_hook(
            env,
            {"cwd": str(env.repo)},
            "--vendor",
            "devin",
            "--state",
            "idle",
            "--push",
        )
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""  # hooks stay silent on stdout
    sent = mcp.posts[2][1]["params"]["arguments"]["args"]["report"]
    assert sent["partial"] is True
    (row,) = sent["rows"]
    assert (row["via"], row["state"], row["vendor"]) == ("hook", "idle", "devin")


def test_hook_push_failure_never_breaks_the_session(env, monkeypatch):
    monkeypatch.setenv("PRECIS_FLEET_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "tok-secret")
    r = run_hook(env, {"cwd": str(env.repo)}, "--vendor", "devin", "--push")
    assert r.returncode == 0
    assert "cannot reach" in r.stderr
    assert "tok-secret" not in r.stdout + r.stderr


def test_hook_outside_a_worktree_is_a_noop(env):
    r = run_hook(env, {"cwd": str(env.tmp_path)}, "--vendor", "devin", "--json")
    assert r.returncode == 0
    assert r.stdout == "" and "worktree" in r.stderr
