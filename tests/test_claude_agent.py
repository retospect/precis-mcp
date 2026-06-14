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


def _write_stub(path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
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


def test_system_prompt_path_is_read(
    stub_bin: Path, tmp_path: Path
) -> None:
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


def test_mcp_config_adds_flag_and_strict(
    stub_bin: Path, tmp_path: Path
) -> None:
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
    assert "--disallowed-tools" in res.final_text
    assert "WebFetch,WebSearch" in res.final_text


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
        def append_event(self, *a, **kw):  # noqa: D401
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


# Skip the whole module when there's no bash available — the stub
# pattern assumes a POSIX shell. CI without bash falls back to a
# graceful skip.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash required for the stub-binary pattern",
)


# Silence unused-import warning on the os import — kept for future
# env-related tests.
_ = os
