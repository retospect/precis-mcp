"""The MCP readiness gate for ``claude -p`` agent passes (gr463517).

No real ``claude``: ``FAKE_CLAUDE`` below speaks the stream-json control
protocol the gate uses (``mcp_status`` control requests answered with a
configurable status sequence; a ``user`` message answered with
init/assistant/result events; exit when stdin closes) and records whether it
ever received the prompt.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import precis.utils._claude_subprocess as cs
import precis.utils.claude_agent as ca
from precis.utils._claude_subprocess import ClaudeProcessError, run_claude_gated
from precis.utils.claude_agent import (
    ClaudeAgentError,
    _resolve_agent_args,
    call_claude_agent,
)

FAKE_CLAUDE = """\
import json, os, sys

seq = os.environ.get("FAKE_STATUS_SEQ", "connected").split(",")
name = os.environ.get("FAKE_SERVER", "precis")
mark = os.environ.get("FAKE_MARK")
answer = os.environ.get("FAKE_NO_ANSWER") != "1"
hang = os.environ.get("FAKE_HANG") == "1"
if os.environ.get("FAKE_DIE"):
    sys.stderr.write("boom\\n")
    sys.exit(int(os.environ["FAKE_DIE"]))
polls = 0
for line in sys.stdin:
    msg = json.loads(line)
    if msg["type"] == "control_request":
        if msg["request"]["subtype"] != "mcp_status" or not answer:
            continue
        st = seq[min(polls, len(seq) - 1)]
        polls += 1
        print(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": msg["request_id"],
            "response": {"mcpServers": [{"name": name, "status": st}]}}}),
            flush=True)
    elif msg["type"] == "user":
        if mark:
            with open(mark, "a", encoding="utf-8") as f:
                f.write(msg["message"]["content"])
        if hang:
            continue
        print(json.dumps({"type": "system", "subtype": "init",
                          "mcp_servers": [{"name": name, "status": "connected"}]}),
              flush=True)
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__precis__search", "id": "t1",
             "input": {}}]}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success",
                          "result": json.dumps(sys.argv[1:]),
                          "total_cost_usd": 0.01, "num_turns": 2}), flush=True)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class _Err(ClaudeProcessError):
    pass


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fake_claude.py"
    path.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(path))
    for k in (
        "FAKE_STATUS_SEQ",
        "FAKE_SERVER",
        "FAKE_MARK",
        "FAKE_NO_ANSWER",
        "FAKE_HANG",
        "FAKE_DIE",
        "FAKE_EXIT",
    ):
        monkeypatch.delenv(k, raising=False)
    return path


def _gated(binary: Path, **over: object):
    kw: dict[str, object] = {
        "prompt": "review the tree",
        "require_mcp": ("precis",),
        "gate_deadline_s": 5.0,
        "binary": str(binary),
        "label": "claude -p (test)",
        "timeout_s": 20.0,
        "error_cls": _Err,
        "bootstrap_oauth": False,
        "poll_s": 0.05,
    }
    kw.update(over)
    return run_claude_gated([str(binary), "-p"], **kw)  # type: ignore[arg-type]


# ── run_claude_gated ──────────────────────────────────────────────


def test_prompt_delivered_once_connected_after_polls(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_STATUS_SEQ", "pending,pending,connected")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    res = _gated(fake_claude)
    assert res.returncode == 0
    assert mark.read_text(encoding="utf-8") == "review the tree"  # exactly once
    events = [json.loads(x) for x in res.stdout.splitlines() if x.startswith("{")]
    types = [e["type"] for e in events]
    # Full stream is kept: >=3 control_responses (2 pending + connected), then
    # init/assistant/result.
    assert types.count("control_response") >= 3
    assert types[-3:] == ["system", "assistant", "result"]


@pytest.mark.parametrize("bad", ["failed", "needs-auth"])
def test_failed_server_raises_and_prompt_never_delivered(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_STATUS_SEQ", f"pending,{bad}")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    with pytest.raises(_Err) as ei:
        _gated(fake_claude)
    exc = ei.value
    assert exc.mcp_not_ready is True
    assert exc.timed_out is False
    assert exc.mcp_status == {"precis": bad}
    assert f"precis MCP not connected (status={bad})" in str(exc)
    assert "pass not started" in str(exc)
    assert not mark.exists()  # NO model turn: the prompt was never sent


def test_pending_past_deadline_fails_closed(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_STATUS_SEQ", "pending")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    t0 = time.monotonic()
    with pytest.raises(_Err) as ei:
        _gated(fake_claude, gate_deadline_s=0.6)
    assert time.monotonic() - t0 < 10
    assert ei.value.mcp_not_ready is True
    assert ei.value.mcp_status == {"precis": "pending"}
    assert "status=pending" in str(ei.value)
    assert not mark.exists()


def test_required_server_absent_from_list_is_not_ready(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_SERVER", "other")  # connected, but not `precis`
    monkeypatch.setenv("FAKE_MARK", str(mark))
    with pytest.raises(_Err) as ei:
        _gated(fake_claude, gate_deadline_s=0.5)
    assert ei.value.mcp_not_ready is True
    assert ei.value.mcp_status == {"precis": "absent"}
    assert not mark.exists()


def test_cli_never_answering_control_requests_fails_closed(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI protocol change (no control_response at all) must break loudly
    rather than silently skip the gate."""
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_NO_ANSWER", "1")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    with pytest.raises(_Err) as ei:
        _gated(fake_claude, gate_deadline_s=0.5)
    assert ei.value.mcp_not_ready is True
    assert ei.value.mcp_status is None
    assert "never answered" in str(ei.value)
    assert "status=unknown" in str(ei.value)
    assert not mark.exists()


def test_overall_timeout_covers_the_run(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate passes, the model then hangs: wall-clock timeout, killed, and it
    reads as ``timed_out`` (paused), not ``mcp_not_ready``."""
    monkeypatch.setenv("FAKE_HANG", "1")
    t0 = time.monotonic()
    with pytest.raises(_Err) as ei:
        _gated(fake_claude, timeout_s=1.5, gate_deadline_s=1.0)
    assert time.monotonic() - t0 < 15
    assert ei.value.timed_out is True
    assert ei.value.mcp_not_ready is False
    assert "timed out after 1.5s" in str(ei.value)


def test_timeout_shorter_than_gate_deadline_is_a_timeout(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STATUS_SEQ", "pending")
    with pytest.raises(_Err) as ei:
        _gated(fake_claude, timeout_s=0.5, gate_deadline_s=30.0)
    assert ei.value.timed_out is True
    assert ei.value.mcp_not_ready is False


def test_missing_binary_is_binary_missing(tmp_path: Path) -> None:
    ghost = tmp_path / "nope"
    with pytest.raises(_Err) as ei:
        _gated(ghost)
    assert ei.value.binary_missing is True
    assert ei.value.mcp_not_ready is False


def test_exit_before_readiness_is_a_plain_exit_error(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI dying outright (e.g. not logged in) keeps run_claude's
    ``exited N`` semantics — it is not an MCP verdict."""
    monkeypatch.setenv("FAKE_DIE", "7")
    with pytest.raises(_Err) as ei:
        _gated(fake_claude)
    assert ei.value.returncode == 7
    assert ei.value.mcp_not_ready is False
    assert "exited 7" in str(ei.value)
    assert "boom" in str(ei.value)


def test_nonzero_exit_after_run_carries_stream(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "3")
    with pytest.raises(_Err) as ei:
        _gated(fake_claude)
    assert ei.value.returncode == 3
    assert ei.value.mcp_not_ready is False
    assert '"type": "result"' in ei.value.stdout  # exhaustion recovery needs it


def test_env_is_copied_not_mutated(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = {"PATH": "/usr/bin:/bin"}
    _gated(fake_claude, env=mine)
    assert mine == {"PATH": "/usr/bin:/bin"}


def test_stream_parsers_ignore_control_responses(fake_claude: Path) -> None:
    """The gated stdout keeps ``control_response`` lines; every stream reader
    in claude_agent must be indifferent to them."""
    res = _gated(fake_claude)
    out = res.stdout
    assert '"control_response"' in out
    assert ca._last_result_event(out) is not None
    assert ca.count_tool_use_events(out) == 1
    assert ca.count_successful_tool_results(out) == 0
    assert ca._last_assistant_text(out) is None
    assert ca.stream_terminal_reason(out) is None
    assert ca.stream_mcp_server_status(out) == {"precis": "connected"}
    assert ca.stream_final_text(out) == json.dumps(["-p"])
    built = ca._build_agent_result(
        SimpleNamespace(stdout=out, stderr=""), duration_s=1.0
    )
    assert built.tool_calls == 1 and built.turns_used == 2 and built.cost_usd == 0.01


# ── call_claude_agent(require_mcp=…) ──────────────────────────────


def _mcp(tmp_path: Path) -> Path:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}", encoding="utf-8")
    return cfg


def _fast_gate(monkeypatch: pytest.MonkeyPatch, deadline: float = 3.0) -> None:
    monkeypatch.setattr(cs, "MCP_GATE_POLL_S", 0.05)
    monkeypatch.setattr(ca, "MCP_GATE_DEADLINE_S", deadline)


def _args(**over: object) -> list[str]:
    kw: dict[str, object] = {
        "prompt": "hi",
        "model": "claude-opus-4-8",
        "system_prompt": None,
        "mcp_config": "/tmp/mcp.json",
        "max_turns": 5,
        "timeout_s": 10.0,
        "max_usd": 1.0,
        "permission_mode": "bypassPermissions",
        "output_format": "stream-json",
        "bare": False,
        "disallowed_tools": (),
        "envelope": None,
        "extra_args": (),
    }
    kw.update(over)
    _, args, *_ = _resolve_agent_args(**kw)  # type: ignore[arg-type]
    return args


def test_gated_argv_has_input_format_and_no_positional_prompt() -> None:
    argv = _args(gated=True)
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert "hi" not in argv
    assert "--" not in argv
    assert "--strict-mcp-config" in argv


def test_ungated_argv_is_unchanged() -> None:
    argv = _args()
    assert "--input-format" not in argv
    assert argv[-2:] == ["--", "hi"]
    assert _args(gated=False) == argv


def test_gated_requires_stream_json_output() -> None:
    with pytest.raises(ValueError, match="stream-json"):
        _args(gated=True, output_format="text")


def test_call_claude_agent_gated_end_to_end(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_gate(monkeypatch)
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_STATUS_SEQ", "pending,connected")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    res = call_claude_agent(
        "-- a hostile --prompt",
        mcp_config=_mcp(tmp_path),
        output_format="stream-json",
        require_mcp=("precis",),
    )
    argv = json.loads(res.final_text)  # the fake echoes its own argv
    assert "--input-format" in argv and "--strict-mcp-config" in argv
    assert "-- a hostile --prompt" not in argv  # the prompt rode stdin
    assert mark.read_text(encoding="utf-8") == "-- a hostile --prompt"
    assert res.tool_calls == 1 and res.turns_used == 2


def test_call_claude_agent_gate_refusal_surfaces_marker_and_cost_nothing(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_gate(monkeypatch)
    mark = tmp_path / "mark"
    monkeypatch.setenv("FAKE_STATUS_SEQ", "failed")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    with pytest.raises(ClaudeAgentError) as ei:
        call_claude_agent(
            "p",
            mcp_config=_mcp(tmp_path),
            output_format="stream-json",
            require_mcp=("precis",),
        )
    assert ei.value.mcp_not_ready is True
    assert not mark.exists()


def test_require_mcp_without_mcp_config_is_not_gated(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config ⇒ nothing to wait for: the plain positional-prompt path."""
    captured: dict[str, object] = {}

    def _fake(argv, **k):
        captured["argv"] = argv
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude", _fake)
    monkeypatch.setattr(
        ca, "run_claude_gated", lambda *a, **k: pytest.fail("gated without config")
    )
    call_claude_agent("the prompt", require_mcp=("precis",))
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--input-format" not in argv and argv[-1] == "the prompt"


def test_gated_container_run_gets_dash_i_and_gate_runs_on_container_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)
    captured: dict[str, object] = {}

    def _fake_gated(argv, **k):
        captured["argv"] = argv
        captured["kw"] = k
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude_gated", _fake_gated)
    call_claude_agent(
        "the prompt",
        model="opus",
        mcp_config=_mcp(tmp_path),
        output_format="stream-json",
        require_mcp=("precis",),
    )
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "podman" and argv[1:3] == ["run", "-i"]
    assert "the prompt" not in argv and "--input-format" in argv
    kw = captured["kw"]
    assert isinstance(kw, dict)
    assert kw["prompt"] == "the prompt" and kw["require_mcp"] == ("precis",)
    assert kw["gate_deadline_s"] == ca.MCP_GATE_DEADLINE_S


def test_ungated_container_run_has_no_dash_i(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)
    captured: dict[str, object] = {}

    def _fake(argv, **k):
        captured["argv"] = argv
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude", _fake)
    call_claude_agent("the prompt", model="opus", mcp_config=_mcp(tmp_path))
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "-i" not in argv and argv[-1] == "the prompt"


def test_container_infra_failure_falls_back_in_proc_through_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gated containerized run whose ``docker run`` dies (exit 125) retries
    in-process — and that retry is ALSO gated (its argv has no positional
    prompt, so an ungated run would wait on stdin forever)."""
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)
    monkeypatch.setattr(ac, "trip_container_unhealthy", lambda *a, **k: None)
    calls: list[list[str]] = []

    def _fake_gated(argv, **k):
        calls.append(list(argv))
        if len(calls) == 1:
            raise ClaudeAgentError(
                "exited 125",
                stderr="Cannot connect to the Docker daemon",
                returncode=125,
            )
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude_gated", _fake_gated)
    monkeypatch.setattr(
        ca, "run_claude", lambda *a, **k: pytest.fail("fallback must stay gated")
    )
    call_claude_agent(
        "the prompt",
        model="opus",
        mcp_config=_mcp(tmp_path),
        output_format="stream-json",
        require_mcp=("precis",),
    )
    assert calls[0][0] == "podman" and calls[1][0] != "podman"
    assert "-i" not in calls[1] and "--input-format" in calls[1]


def test_router_folds_mcp_not_ready_into_result_flag() -> None:
    from precis.utils.llm.router import Tier, _error_result

    exc = ClaudeAgentError(
        "precis MCP not connected (status=failed) after 3s — pass not started",
        mcp_not_ready=True,
        mcp_status={"precis": "failed"},
    )
    res = _error_result(exc, model="m", tier=Tier.FRONTIER)
    assert res.mcp_not_ready is True
    assert res.error is not None and "status=failed" in res.error
    assert not res.paused and not res.timed_out and not res.interrupted
    plain = _error_result(ClaudeAgentError("x", returncode=1), model="m", tier=Tier.BIG)
    assert plain.mcp_not_ready is False


def test_router_provider_requires_precis_only_with_tools_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils.claude_agent import AgentResult
    from precis.utils.llm.router import ClaudeAgentProvider, LlmRequest, Tier

    seen: list[object] = []

    def _call(*a: object, **kw: object) -> AgentResult:
        seen.append(kw["require_mcp"])
        return AgentResult(
            final_text="ok", cost_usd=0.0, duration_s=0.1, turns_used=1, tool_calls=1
        )

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _call)
    p = ClaudeAgentProvider()
    p.run(LlmRequest(tier=Tier.FRONTIER, prompt="p", tools_needed=True), model="m")
    p.run(
        LlmRequest(
            tier=Tier.FRONTIER,
            prompt="p",
            tools_needed=True,
            mcp_config=Path("/tmp/x.json"),
        ),
        model="m",
    )
    p.run(
        LlmRequest(tier=Tier.FRONTIER, prompt="p", mcp_config=Path("/tmp/x.json")),
        model="m",
    )
    assert seen == [(), ("precis",), ()]
