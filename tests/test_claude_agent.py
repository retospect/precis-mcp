"""Tests for :mod:`precis.utils.claude_agent`.

Same stub-binary pattern :mod:`precis.utils.claude_p` already uses
for testing: set ``PRECIS_CLAUDE_BIN`` to a tiny shell script that
emits a deterministic stdout / stderr / exit code. No real claude
binary required.
"""

from __future__ import annotations

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
