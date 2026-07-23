"""Tests for :mod:`precis.utils.claude_agent`.

Same stub-binary pattern :mod:`precis.utils.claude_p` already uses
for testing: set ``PRECIS_CLAUDE_BIN`` to a tiny shell script that
emits a deterministic stdout / stderr / exit code. No real claude
binary required.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from precis.utils.claude_agent import (
    AgentResult,
    ClaudeAgentError,
    call_claude_agent,
    call_claude_agent_async,
)


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Yield a writable path. Tests fill it with a tiny stub script."""
    path = tmp_path / "claude_stub.sh"
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(path))
    return path


def _write_stream_stub(path: Path, *, stdout: str, exit_code: int = 0) -> None:
    """Stub that emits a verbatim (possibly multi-line) stdout payload.

    ``_write_stub`` inlines ``stdout`` into a ``textwrap.dedent``'d heredoc,
    which mangles a multi-line ``stream-json`` body (continuation lines have
    no indent, so dedent strips nothing and the shebang keeps its leading
    spaces). Stash the payload in a sidecar file and ``cat`` it instead.
    """
    import shlex

    payload = path.parent / (path.name + ".out")
    payload.write_text(stdout)
    path.write_text(
        f"#!/usr/bin/env bash\ncat {shlex.quote(str(payload))}\nexit {exit_code}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _write_stub(
    path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0
) -> None:
    """Write a bash stub that echoes the given streams and exits."""
    body = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        cat <<'STDOUT_EOF'
        {stdout}
        STDOUT_EOF
        cat <<'STDERR_EOF' >&2
        {stderr}
        STDERR_EOF
        exit {exit_code}
        """
    )
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


# ── happy path ────────────────────────────────────────────────────


def test_returns_stdout_in_final_text(stub_bin: Path) -> None:
    _write_stub(stub_bin, stdout="agent did the thing.")
    res = call_claude_agent("do the thing")
    assert isinstance(res, AgentResult)
    assert "agent did the thing" in res.final_text


def test_extracts_cost_from_stderr(stub_bin: Path) -> None:
    _write_stub(
        stub_bin,
        stdout="done",
        stderr="Cost: $0.0123 (model claude-sonnet-4-6)",
    )
    res = call_claude_agent("do something")
    assert res.cost_usd == 0.0123


def test_extracts_turns_from_stderr(stub_bin: Path) -> None:
    _write_stub(
        stub_bin,
        stdout="done",
        stderr="agent finished. turns: 7. Cost: $0.05",
    )
    res = call_claude_agent("do work")
    assert res.turns_used == 7
    assert res.cost_usd == 0.05


def test_cost_is_none_when_stderr_silent(stub_bin: Path) -> None:
    _write_stub(stub_bin, stdout="done", stderr="")
    res = call_claude_agent("hi")
    assert res.cost_usd is None
    assert res.turns_used is None


def test_duration_is_positive(stub_bin: Path) -> None:
    _write_stub(stub_bin, stdout="done")
    res = call_claude_agent("hi")
    assert res.duration_s >= 0


# ── failure surfaces ──────────────────────────────────────────────


def test_nonzero_exit_raises_with_context(stub_bin: Path) -> None:
    _write_stub(
        stub_bin,
        stdout="partial",
        stderr="boom: model unavailable",
        exit_code=2,
    )
    with pytest.raises(ClaudeAgentError) as exc_info:
        call_claude_agent("do")
    err = exc_info.value
    assert err.returncode == 2
    assert "boom" in err.stderr
    assert err.stdout.strip() == "partial"


def _stream(events: list[dict]) -> str:
    """Render a list of stream-json events to a newline-delimited body."""
    import json

    return "\n".join(json.dumps(e) for e in events)


def test_max_turns_exit1_recovers_partial(stub_bin: Path) -> None:
    """A ``--max-turns`` cutoff exits 1 with a stream-json result event.

    That is a *resumable exhaustion*, not a crash: the agent ran and
    produced a partial answer + telemetry. ``call_claude_agent`` must
    return it, not raise (which surfaced "⚠️ thinking failed: …exited 1:"
    to the follow-up reader)."""
    stdout = _stream(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial answer"}]},
            },
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "total_cost_usd": 0.19,
                "num_turns": 20,
                "result": "partial answer",
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout, exit_code=1)
    res = call_claude_agent("do")  # must not raise
    assert res.final_text == "partial answer"
    assert res.cost_usd == 0.19
    assert res.turns_used == 20


def test_budget_cap_exit1_recovers(stub_bin: Path) -> None:
    """The ``--max-budget-usd`` cap is likewise a resumable exhaustion."""
    stdout = _stream(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "capped reply"}]},
            },
            {
                "type": "result",
                "subtype": "error_max_budget",
                "is_error": True,
                "total_cost_usd": 2.0,
                "num_turns": 7,
                "result": "capped reply",
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout, exit_code=1)
    res = call_claude_agent("do")
    assert res.final_text == "capped reply"
    assert res.cost_usd == 2.0


def test_max_turns_falls_back_to_assistant_text(stub_bin: Path) -> None:
    """When the result event has no usable ``result`` string, the final
    text comes from the last assistant message — not the raw JSON stream."""
    stdout = _stream(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "the real answer"}]},
            },
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "total_cost_usd": 0.1,
                "num_turns": 20,
                "result": None,
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout, exit_code=1)
    res = call_claude_agent("do")
    assert res.final_text == "the real answer"
    assert "{" not in res.final_text  # not the raw JSON stream


def test_completed_turn_with_nonzero_exit_recovers(stub_bin: Path) -> None:
    """A run whose result event says it ``completed`` its turn but the CLI
    still exits 1 (a process/teardown artifact seen on the web "ask &
    think" path) is recovered — surface the answer, not a bare
    "⚠️ thinking failed: …exited 1: (terminal_reason=completed)"."""
    stdout = _stream(
        [
            {
                "type": "result",
                "terminal_reason": "completed",
                "total_cost_usd": 0.3,
                "num_turns": 4,
                "result": "the completed answer",
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout, exit_code=1)
    res = call_claude_agent("do")
    assert res.final_text == "the completed answer"


def test_error_during_execution_still_raises_with_reason(stub_bin: Path) -> None:
    """A genuine runtime error is NOT recovered — it re-raises, and the
    terminal reason is folded into the message (the CLI's bare "exited 1:"
    has empty stderr for stream-json errors)."""
    stdout = _stream(
        [
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 3,
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout, exit_code=1)
    with pytest.raises(ClaudeAgentError, match="error_during_execution"):
        call_claude_agent("do")


def test_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "/nonexistent/claude/binary")
    with pytest.raises(ClaudeAgentError, match="not found"):
        call_claude_agent("do")


def test_timeout_raises(stub_bin: Path) -> None:
    # Stub that sleeps longer than the timeout.
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            sleep 5
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(ClaudeAgentError, match="timed out"):
        call_claude_agent("do", timeout_s=0.5)


# ── flag plumbing ─────────────────────────────────────────────────


def test_system_prompt_path_is_read(stub_bin: Path, tmp_path: Path) -> None:
    """Pass a Path; the wrapper reads it and forwards as text."""
    soul = tmp_path / "soul.md"
    soul.write_text("you are asa")

    # Stub echoes its argv to stdout so we can check the flag landed.
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)

    res = call_claude_agent("do", system_prompt=soul)
    assert "--append-system-prompt" in res.final_text
    assert "you are asa" in res.final_text


def test_mcp_config_adds_flag_and_strict(stub_bin: Path, tmp_path: Path) -> None:
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)

    res = call_claude_agent("do", mcp_config=mcp)
    assert "--mcp-config" in res.final_text
    assert "--strict-mcp-config" in res.final_text


def test_bare_flag_emitted_when_requested(stub_bin: Path) -> None:
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do", bare=True)
    assert "--bare" in res.final_text


def test_disallowed_tools_joined(stub_bin: Path) -> None:
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do", disallowed_tools=("WebFetch", "WebSearch"))
    # Deny rules ride via ``--settings`` JSON, not the variadic
    # ``--disallowed-tools`` flag — see the long comment in
    # claude_agent.py for the Commander.js variadic story.
    assert "--settings" in res.final_text
    assert '"deny"' in res.final_text
    assert "WebFetch" in res.final_text
    assert "WebSearch" in res.final_text


def _write_argv_env_stub(path: Path) -> None:
    """Stub echoing argv then ``PRECIS_MCP_DB_ROLE`` from the env."""
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            printf 'DB_ROLE=%s\\n' "${PRECIS_MCP_DB_ROLE:-unset}"
            """
        )
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_envelope_param_merges_tier1_deny(stub_bin: Path) -> None:
    """An explicit envelope drops the mutate verbs + fetch tools (slice 8)."""
    from precis.workers.envelope import Envelope

    _write_argv_env_stub(stub_bin)
    res = call_claude_agent("do", envelope=Envelope(write="none", egress="none"))
    assert "--settings" in res.final_text
    assert "mcp__precis__put" in res.final_text
    assert "WebFetch" in res.final_text
    # tier 2: the read-only role is advertised to the spawned MCP server.
    assert "DB_ROLE=agent_ro" in res.final_text


def test_envelope_merges_with_explicit_disallowed(stub_bin: Path) -> None:
    """Envelope deny list unions with a caller's ``disallowed_tools``."""
    from precis.workers.envelope import Envelope

    _write_argv_env_stub(stub_bin)
    res = call_claude_agent(
        "do",
        disallowed_tools=("Bash",),
        envelope=Envelope(write="none"),
    )
    assert "Bash" in res.final_text
    assert "mcp__precis__delete" in res.final_text


def test_active_scope_envelope_applies_without_param(stub_bin: Path) -> None:
    """The executor-scoped envelope is picked up when no param is passed."""
    from precis.workers.envelope import Envelope, envelope_scope

    _write_argv_env_stub(stub_bin)
    with envelope_scope(Envelope(egress="none")):
        res = call_claude_agent("do")
    assert "WebFetch" in res.final_text
    assert "WebSearch" in res.final_text


def test_default_envelope_denies_nothing_and_no_role(stub_bin: Path) -> None:
    """No envelope, no scope → today's behavior: no deny, role unset."""
    _write_argv_env_stub(stub_bin)
    res = call_claude_agent("do")
    assert "--settings" not in res.final_text
    assert "DB_ROLE=unset" in res.final_text


# ── §13 container executor selection (dark) ────────────────────────


def test_container_executor_off_uses_host_binary(monkeypatch, stub_bin: Path) -> None:
    """Default (PRECIS_AGENT_CONTAINER unset) → the host claude runs directly."""
    from types import SimpleNamespace

    import precis.utils.claude_agent as ca

    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
    captured: dict[str, object] = {}

    def _fake(argv, **k):
        captured["argv"] = argv
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude", _fake)
    call_claude_agent("do", model="opus")
    assert captured["argv"][0] == str(stub_bin)  # host binary, not a container


def test_container_executor_on_wraps_in_podman(monkeypatch, stub_bin: Path) -> None:
    """PRECIS_AGENT_CONTAINER=1 → the SAME claude -p runs inside a container; the
    prompt + flags are preserved and the run is synchronous (no -d)."""
    from types import SimpleNamespace

    import precis.utils.claude_agent as ca
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")  # deterministic bin
    # The selection seam now also gates on the verified-capability probe (§15d);
    # the test host has no real podman/image, so force it capable.
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)
    captured: dict[str, object] = {}

    def _fake(argv, **k):
        captured["argv"] = argv
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude", _fake)
    call_claude_agent("the prompt", model="opus")
    argv = captured["argv"]
    assert argv[0] == "podman" and "run" in argv
    assert str(stub_bin) not in argv  # host binary dropped
    assert "claude" in argv and argv[-1] == "the prompt"  # command preserved
    assert "-d" not in argv  # synchronous (stdout captured)


def test_container_reinjects_scrubbed_dsn(monkeypatch, stub_bin: Path) -> None:
    """Regression (spark review retry-storm, 2026-07-19). The worker scrubs
    ``PRECIS_DATABASE_URL`` from ``os.environ`` at boot (``adopt_process_store``,
    ADR 0059), so the container's by-key ``--env PRECIS_DATABASE_URL`` would
    inherit nothing → the entrypoint aborts "PRECIS_DATABASE_URL not set" and
    every agentic pass fails 1. The container path must re-inject the captured
    (adopted) DSN into the subprocess env docker inherits from — by KEY, so the
    secret never enters the argv."""
    from types import SimpleNamespace

    import precis.utils.claude_agent as ca
    from precis import secrets as _secrets
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)  # §15d
    # Boot-time state: DSN gone from the environ, captured in the secrets module.
    monkeypatch.delenv("PRECIS_DATABASE_URL", raising=False)
    monkeypatch.setattr(_secrets, "_ADOPTED_DSN", "postgresql://ro@h:6432/db")

    captured: dict[str, object] = {}

    def _fake(argv, **k):
        captured["argv"] = argv
        captured["env"] = k.get("env")
        return SimpleNamespace(stdout="done", stderr="")

    monkeypatch.setattr(ca, "run_claude", _fake)
    call_claude_agent("the prompt", model="opus")

    argv = captured["argv"]
    assert "--env" in argv and "PRECIS_DATABASE_URL" in argv  # by-key passthrough
    assert "postgresql://ro@h:6432/db" not in argv  # value NEVER in argv
    env = captured["env"]
    assert isinstance(env, dict)
    # …the value IS in the env docker inherits the by-key var from.
    assert env["PRECIS_DATABASE_URL"] == "postgresql://ro@h:6432/db"


def test_model_override(stub_bin: Path) -> None:
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do", model="claude-opus-4-7")
    assert "claude-opus-4-7" in res.final_text


def test_extra_args_passthrough(stub_bin: Path) -> None:
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do", extra_args=("--custom-flag", "v"))
    assert "--custom-flag" in res.final_text
    assert "v" in res.final_text.split()


# ── env overrides ─────────────────────────────────────────────────


def test_env_model_default_honoured(
    stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CLAUDE_AGENT_MODEL", "claude-opus-4-7")
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do")
    assert "claude-opus-4-7" in res.final_text


def test_env_max_usd_default_honoured(
    stub_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CLAUDE_AGENT_MAX_USD", "5.0")
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$@"
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)
    res = call_claude_agent("do")
    assert "5.0" in res.final_text


# ── log_event hook ────────────────────────────────────────────────


class _FakeStore:
    """Minimal store stub recording ``append_event`` calls."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_event(
        self,
        ref_id: int,
        *,
        source: str,
        event: str,
        payload: dict | None = None,
        conn=None,
    ) -> None:
        self.events.append(
            {
                "ref_id": ref_id,
                "source": source,
                "event": event,
                "payload": payload,
            }
        )


def test_log_event_writes_on_success(stub_bin: Path) -> None:
    _write_stub(stub_bin, stdout="done", stderr="Cost: $0.05")
    store = _FakeStore()
    call_claude_agent(
        "do",
        log_event=(store, 42, "structural-reviewer"),  # type: ignore[arg-type]
    )
    assert len(store.events) == 1
    evt = store.events[0]
    assert evt["ref_id"] == 42
    assert evt["source"] == "structural-reviewer"
    assert evt["event"] == "agent:done"
    assert evt["payload"]["cost_usd"] == 0.05
    assert evt["payload"]["model"]


def test_log_event_swallows_store_errors(stub_bin: Path) -> None:
    """A buggy store mustn't lose the agent's work."""
    _write_stub(stub_bin, stdout="done")

    class _BrokenStore:
        def append_event(self, *a, **kw):
            raise RuntimeError("nope")

    # Should not raise.
    res = call_claude_agent(
        "do",
        log_event=(_BrokenStore(), 42, "src"),  # type: ignore[arg-type]
    )
    assert res.final_text.strip() == "done"


# ── pyright-friendly _to_str / _extract_* helpers ────────────────


def test_helpers_handle_bytes_and_none() -> None:
    from precis.utils.claude_agent import _extract_cost_usd, _to_str

    assert _to_str(None) == ""
    assert _to_str(b"hello") == "hello"
    assert _to_str("hello") == "hello"
    assert _extract_cost_usd("") is None
    assert _extract_cost_usd("Cost: $1.23") == 1.23
    assert _extract_cost_usd("nothing relevant here") is None


def test_cost_and_turns_from_stream_json_result_event() -> None:
    """Claude Code 2.1.x emits totals in the trailing ``result`` event
    on stdout (stream-json mode), not stderr."""
    import json

    from precis.utils.claude_agent import (
        _cost_from_stdout_result,
        _turns_from_stdout_result,
    )

    # A realistic two-event tail: an assistant message then the result.
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "content": "hi"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "total_cost_usd": 0.258,
                    "num_turns": 15,
                    "result": "done",
                }
            ),
        ]
    )
    assert _cost_from_stdout_result(stdout) == 0.258
    assert _turns_from_stdout_result(stdout) == 15


def test_cost_and_turns_none_when_no_result_event() -> None:
    from precis.utils.claude_agent import (
        _cost_from_stdout_result,
        _turns_from_stdout_result,
    )

    assert _cost_from_stdout_result("") is None
    assert _cost_from_stdout_result("not json at all") is None
    assert _turns_from_stdout_result("") is None
    assert _turns_from_stdout_result('{"type":"system"}') is None


def test_cost_walks_to_latest_result_event() -> None:
    """If the stream contains multiple ``result`` events (some interim),
    we want the LAST one — the final totals."""
    import json

    from precis.utils.claude_agent import _cost_from_stdout_result

    stdout = "\n".join(
        [
            json.dumps({"type": "result", "total_cost_usd": 0.05, "num_turns": 1}),
            json.dumps({"type": "assistant", "content": "more"}),
            json.dumps({"type": "result", "total_cost_usd": 0.42, "num_turns": 12}),
        ]
    )
    assert _cost_from_stdout_result(stdout) == 0.42


def test_count_tool_use_events_counts_blocks_across_assistants() -> None:
    """Every ``tool_use`` content block across all assistant events counts —
    the review seam's positive evidence that a pass actually acted."""
    import json

    from precis.utils.claude_agent import _count_tool_use_events

    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "let me search"},
                            {"type": "tool_use", "name": "search", "input": {}},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "get", "input": {}},
                            {"type": "tool_use", "name": "get", "input": {}},
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "total_cost_usd": 0.1, "num_turns": 3}),
        ]
    )
    assert _count_tool_use_events(stdout) == 3


def test_count_tool_use_events_zero_and_robust() -> None:
    """A text-only stream counts zero; junk/empty lines never raise."""
    import json

    from precis.utils.claude_agent import _count_tool_use_events

    text_only = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                }
            ),
            json.dumps({"type": "result", "total_cost_usd": 0.0, "num_turns": 0}),
        ]
    )
    assert _count_tool_use_events(text_only) == 0
    assert _count_tool_use_events("") == 0
    assert _count_tool_use_events("not json\n{broken") == 0


def test_stream_usage_reads_trailing_result_event_only() -> None:
    """The trailing ``result`` event's ``usage`` is already a cumulative
    total for the whole run (empirically confirmed), so an earlier
    ``assistant`` event's usage must NOT be added on top — only the
    trailing result event's numbers should surface."""
    import json

    from precis.utils.claude_agent import _stream_usage

    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "partial"}],
                        "usage": {
                            "input_tokens": 999,
                            "output_tokens": 999,
                            "cache_read_input_tokens": 999,
                            "cache_creation_input_tokens": 999,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": 0.29,
                    "num_turns": 1,
                    "result": "hello",
                    "usage": {
                        "input_tokens": 3555,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 46653,
                    },
                }
            ),
        ]
    )
    assert _stream_usage(stdout) == {
        "input_tokens": 3555,
        "output_tokens": 4,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 46653,
    }


def test_stream_usage_none_when_no_result_event() -> None:
    """No stream-json result event (text/stub path) → all four ``None``,
    matching ``tool_calls``'s never-a-false-zero discipline."""
    from precis.utils.claude_agent import _stream_usage

    assert _stream_usage("plain text stdout, not json") == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }
    assert _stream_usage("") == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }


def test_stream_usage_none_when_result_event_has_no_usage() -> None:
    """A result event present but with no ``usage`` key at all → ``None``,
    not ``0`` — an older CLI without token telemetry shouldn't look like a
    definitively-zero-token run."""
    import json

    from precis.utils.claude_agent import _stream_usage

    stdout = json.dumps({"type": "result", "total_cost_usd": 0.1, "num_turns": 1})
    assert _stream_usage(stdout) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }


def test_call_claude_agent_populates_usage_fields(stub_bin: Path) -> None:
    """End-to-end: ``call_claude_agent`` threads the trailing result event's
    ``usage`` dict onto ``AgentResult``'s four token fields."""
    stdout = _stream(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
            {
                "type": "result",
                "total_cost_usd": 0.29,
                "num_turns": 1,
                "result": "hello",
                "usage": {
                    "input_tokens": 3555,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 46653,
                },
            },
        ]
    )
    _write_stream_stub(stub_bin, stdout=stdout)
    res = call_claude_agent("do")
    assert res.input_tokens == 3555
    assert res.output_tokens == 4
    assert res.cache_read_tokens == 0
    assert res.cache_creation_tokens == 46653


def test_call_claude_agent_usage_none_on_text_path(stub_bin: Path) -> None:
    """The text/stub path (no stream-json to parse) leaves all four token
    fields ``None`` — same gate as ``tool_calls``."""
    _write_stub(stub_bin, stdout="plain text reply", stderr="")
    res = call_claude_agent("hi")
    assert res.input_tokens is None
    assert res.output_tokens is None
    assert res.cache_read_tokens is None
    assert res.cache_creation_tokens is None


# Skip the whole module on Windows, where ``shutil.which("bash")``
# may find ``bash.exe`` (Git Bash) but ``#!/usr/bin/env bash``
# shebangs can't be invoked directly via ``subprocess.run`` —
# Windows can't execute POSIX shell scripts as if they were native
# binaries. Same family of failures shows up on CI runners with
# no ``bash`` at all (an Ubuntu image that didn't install it),
# hence the second branch of the OR.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="POSIX bash + execute-shebang support required for the stub-binary pattern",
)


# Silence unused-import warning on the os import — kept for future
# env-related tests.
_ = os


def test_stream_final_text_lifts_result_then_falls_back() -> None:
    from precis.utils.claude_agent import stream_final_text

    stream = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
        '{"type":"result","result":"final answer","total_cost_usd":0.01,"num_turns":3}'
    )
    assert stream_final_text(stream) == "final answer"
    # text-format / stub output (no result event) → raw stdout
    assert stream_final_text("plain stub output") == "plain stub output"
    assert stream_final_text("") == ""


# ── §15d/§15h: container selection gate + infra-fallback breaker ───
#
# These patch ``run_claude`` directly (no stub binary), so they exercise the
# selection logic in ``call_claude_agent`` — which run argv is chosen, and how a
# containerized failure is classified — without a real container or claude.


def _ok(stdout: str = "agent ran", stderr: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(stdout=stdout, stderr=stderr)


class _RunClaudeSeq:
    """A ``run_claude`` stub: applies ``behaviors`` (a result object to return or
    an exception to raise) per successive call, recording each call's argv."""

    def __init__(self, *behaviors) -> None:
        self.calls: list[list[str]] = []
        self._it = iter(behaviors)

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        b = next(self._it)
        if isinstance(b, BaseException):
            raise b
        return b


@pytest.fixture
def _container_selected(monkeypatch):
    """Opt in + force-capable, with hermetic DSN/secrets and an observable latch.

    Returns the list the patched ``trip_container_unhealthy`` appends to, so a
    test can assert whether the infra-health latch was tripped."""
    import precis.utils.claude_agent as ca
    from precis import secrets as _secrets
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", "podman")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: True)
    monkeypatch.setattr(_secrets, "get_adopted_dsn", lambda: None, raising=False)
    ac.reset_capability_cache()
    trips: list[int] = []
    monkeypatch.setattr(ac, "trip_container_unhealthy", lambda *a, **k: trips.append(1))
    ca._warned_container_incapable = False
    return trips


def test_enabled_and_capable_runs_containerized(monkeypatch, _container_selected):
    import precis.utils.claude_agent as ca

    fake = _RunClaudeSeq(_ok("done"))
    monkeypatch.setattr(ca, "run_claude", fake)
    res = ca.call_claude_agent("do it", model="opus")
    assert isinstance(res, AgentResult)
    argv = fake.calls[0]
    assert argv[0] == "podman" and "run" in argv  # containerized
    assert any("precis-agent" in a for a in argv)  # the agent image
    assert _container_selected == []  # healthy run → no latch trip


def test_enabled_but_incapable_runs_in_proc(monkeypatch):
    import precis.utils.claude_agent as ca
    from precis.workers.executors import agent_container as ac

    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setattr(ac, "container_capability_ok", lambda *a, **k: False)
    ca._warned_container_incapable = False
    fake = _RunClaudeSeq(_ok("in proc"))
    monkeypatch.setattr(ca, "run_claude", fake)
    res = ca.call_claude_agent("do it", model="opus")
    assert isinstance(res, AgentResult)
    argv = fake.calls[0]
    assert argv[0] == "claude"  # the host claude argv, not `podman run`
    assert not any("precis-agent" in a for a in argv)
    assert "podman" not in argv


def test_container_infra_failure_falls_back_in_proc_and_latches(
    monkeypatch, _container_selected
):
    import precis.utils.claude_agent as ca

    infra = ClaudeAgentError(
        "cannot connect to the Docker daemon",
        stdout="",
        stderr="cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        returncode=125,
    )
    fake = _RunClaudeSeq(infra, _ok("recovered in proc"))
    monkeypatch.setattr(ca, "run_claude", fake)
    res = ca.call_claude_agent("do it", model="opus")
    assert isinstance(res, AgentResult)
    assert "recovered in proc" in res.final_text
    assert fake.calls[0][0] == "podman"  # first: containerized
    assert fake.calls[1][0] == "claude"  # then: in-proc fallback
    assert _container_selected == [1]  # infra failure latched the host unhealthy


def test_container_oom_137_falls_back_not_skipped(monkeypatch, _container_selected):
    import precis.utils.claude_agent as ca

    # Exit 137 (OOM/SIGKILL) is ≥128, which the router's LlmResult.interrupted
    # would read as a signal 'interrupt' and *skip*. The breaker must catch it
    # first as a container-infra failure and fall back in-proc — not skip.
    oom = ClaudeAgentError("exited 137", stdout="", stderr="", returncode=137)
    fake = _RunClaudeSeq(oom, _ok("in proc after OOM"))
    monkeypatch.setattr(ca, "run_claude", fake)
    res = ca.call_claude_agent("do it", model="opus")
    assert "in proc after OOM" in res.final_text
    assert fake.calls[1][0] == "claude"
    assert _container_selected == [1]


def test_container_model_error_still_raises(monkeypatch, _container_selected):
    import precis.utils.claude_agent as ca

    # A claude/model failure INSIDE the container (non-infra: no runtime marker,
    # rc=1, no recoverable-exhaustion stream) must NOT be swallowed by the
    # fallback — it's a real failure the caller needs to see.
    model_err = ClaudeAgentError(
        "exited 1", stdout="", stderr="model overloaded", returncode=1
    )
    fake = _RunClaudeSeq(model_err)
    monkeypatch.setattr(ca, "run_claude", fake)
    with pytest.raises(ClaudeAgentError):
        ca.call_claude_agent("do it", model="opus")
    assert len(fake.calls) == 1  # no in-proc retry
    assert _container_selected == []  # not an infra failure → no latch trip


# ── call_claude_agent_async: streaming twin (router-migration Phase 2) ─
#
# No ``pytest-asyncio`` in this repo yet; ``asyncio.run()`` inside a plain
# sync test is the lightest way to drive the coroutine without adding a new
# test-time dependency.


def test_call_claude_agent_async_streams_events_in_order(stub_bin: Path) -> None:
    """``on_event`` fires once per stream-json JSON line, in arrival order."""
    events: list[dict] = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.05,
            "num_turns": 2,
            "result": "hi",
        },
    ]
    _write_stream_stub(stub_bin, stdout=_stream(events))
    seen: list[dict] = []

    async def on_event(evt: dict) -> None:
        seen.append(evt)

    res = asyncio.run(call_claude_agent_async("do", on_event=on_event))

    assert seen == events
    assert isinstance(res, AgentResult)
    assert res.final_text == "hi"
    assert res.cost_usd == 0.05
    assert res.turns_used == 2


def test_call_claude_agent_async_matches_sync_result(stub_bin: Path) -> None:
    """Same stdout ⇒ the async path's ``AgentResult`` matches the sync path's
    (every field except ``duration_s``, which is wall-clock and non-
    deterministic between two separate invocations)."""
    events: list[dict] = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "sync me"}]},
        },
        {
            "type": "result",
            "total_cost_usd": 0.12,
            "num_turns": 4,
            "result": "sync me",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 2,
            },
        },
    ]
    _write_stream_stub(stub_bin, stdout=_stream(events))

    sync_res = call_claude_agent("do")
    async_res = asyncio.run(call_claude_agent_async("do"))

    assert async_res.final_text == sync_res.final_text
    assert async_res.cost_usd == sync_res.cost_usd
    assert async_res.turns_used == sync_res.turns_used
    assert async_res.tool_calls == sync_res.tool_calls
    assert async_res.raw_stdout == sync_res.raw_stdout
    assert async_res.terminal_reason == sync_res.terminal_reason
    assert async_res.input_tokens == sync_res.input_tokens
    assert async_res.output_tokens == sync_res.output_tokens
    assert async_res.cache_read_tokens == sync_res.cache_read_tokens
    assert async_res.cache_creation_tokens == sync_res.cache_creation_tokens


def test_call_claude_agent_async_without_on_event(stub_bin: Path) -> None:
    """Omitting ``on_event`` still returns a valid, complete result."""
    _write_stub(stub_bin, stdout="plain reply", stderr="Cost: $0.02")

    res = asyncio.run(call_claude_agent_async("hi"))

    assert isinstance(res, AgentResult)
    assert res.final_text.strip() == "plain reply"
    assert res.cost_usd == 0.02


def test_call_claude_agent_async_nonzero_exit_raises(stub_bin: Path) -> None:
    """Same exception contract as the sync path: exit code + stderr surface
    on the raised ``ClaudeAgentError``."""
    _write_stub(stub_bin, stdout="partial", stderr="boom: async failure", exit_code=2)

    with pytest.raises(ClaudeAgentError) as exc_info:
        asyncio.run(call_claude_agent_async("do"))

    err = exc_info.value
    assert err.returncode == 2
    assert "boom" in err.stderr
    assert err.stdout.strip() == "partial"


def test_call_claude_agent_async_timeout_raises(stub_bin: Path) -> None:
    """Same timeout exception contract as the sync path."""
    stub_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            sleep 5
            """
        )
    )
    stub_bin.chmod(stub_bin.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(ClaudeAgentError, match="timed out"):
        asyncio.run(call_claude_agent_async("do", timeout_s=0.5))
