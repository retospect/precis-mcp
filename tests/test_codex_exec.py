"""Tests for the ``codex exec`` transport lane (gr543830).

No real codex: every test runs a fake ``codex`` script on PATH (or via
``PRECIS_CODEX_BIN``) that prints recorded outputs — the JSONL event
stream ``codex exec --json`` emits, bare text, or a ``login status``
answer. The lane ships dark, so every dispatch-path test lights it with
``PRECIS_CODEX_EXEC=1`` first.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

import precis.utils.codex_exec as cx
import precis.utils.llm.router as router
from precis.utils.codex_exec import (
    CodexExecError,
    CodexExecResult,
    CodexExecUnparseableError,
    call_codex_exec,
)
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    Transport,
    result_from_codex_exec,
    route,
)


def _write_codex_stub(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, body: str, *, name: str = "codex"
) -> Any:
    """An executable ``codex`` stub printing ``body``; also usable as the
    ``login status`` answer (the stub prints the same body regardless of
    argv — the probe only checks exit code + wording)."""
    bindir = tmp_path / "codex-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / name
    stub.write_text(body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(stub))
    return stub


def _jsonl_stub(events: list[dict[str, Any]]) -> str:
    lines = "\n".join(json.dumps(e) for e in events)
    return f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n"


def _agent_turn(
    text: str, *, usage: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    return [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage or {}},
    ]


def _lit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CODEX_EXEC", "1")
    cx._bust_login_probe()


@pytest.fixture(autouse=True)
def _fresh_probe() -> Any:
    cx._bust_login_probe()
    yield
    cx._bust_login_probe()


# ── call_codex_exec: the wire + parse contract ────────────────────────


def test_call_codex_exec_parses_jsonl_agent_message(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_codex_stub(
        tmp_path,
        monkeypatch,
        _jsonl_stub(
            _agent_turn(
                'Sure. {"ok": true, "n": 3}',
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 12,
                },
            )
        ),
    )
    res = call_codex_exec("judge this")
    assert res.data == {"ok": True, "n": 3}
    assert res.text == 'Sure. {"ok": true, "n": 3}'
    assert res.input_tokens == 100
    assert res.output_tokens == 12
    assert res.cache_read_tokens == 40
    assert res.cost_usd is None


def test_call_codex_exec_argv_shape_and_throwaway_cwd(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub records argv+cwd: read-only sandbox, a --cd throwaway dir
    that is cleaned up, --ignore-user-config (no MCP can attach),
    --json, --ephemeral, prompt last after --."""
    rec = tmp_path / "argv.txt"
    stub = tmp_path / "codex-bin" / "codex"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{rec}"\n'
        f'pwd > "{tmp_path}/cwd.txt"\n'
        + _jsonl_stub(_agent_turn('{"ok": true}')).split("\n", 1)[1],
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(stub))
    monkeypatch.delenv("PRECIS_CODEX_MODEL", raising=False)

    call_codex_exec("-not-a-flag prompt", model="gpt-5.1-codex")

    args = rec.read_text(encoding="utf-8").splitlines()
    assert args[0] == "exec"
    assert "--json" in args
    assert "--sandbox" in args and "read-only" in args
    assert "--skip-git-repo-check" in args
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--model" in args and "gpt-5.1-codex" in args
    assert args[-2:] == ["--", "-not-a-flag prompt"]
    cd = Path(args[args.index("--cd") + 1])
    cwd = Path((tmp_path / "cwd.txt").read_text(encoding="utf-8").strip())
    # resolve(): the stub's pwd is resolved while --cd carries the raw mkdtemp
    # path — on macOS /var → /private/var makes them differ textually.
    assert (
        cwd.resolve() == cd.resolve() and not cd.exists()
    )  # throwaway, removed after the run


def test_call_codex_exec_drops_claude_model_id(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tier-resolved claude id must never reach ``-m`` — the router
    hands a model-less codex rung the claude default, which would
    hard-fail every call."""
    rec = tmp_path / "argv.txt"
    stub = tmp_path / "codex-bin" / "codex"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{rec}"\n'
        + _jsonl_stub(_agent_turn('{"ok": true}')).split("\n", 1)[1],
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(stub))
    monkeypatch.delenv("PRECIS_CODEX_MODEL", raising=False)

    call_codex_exec("x", model="claude-sonnet-5-5")
    args = rec.read_text(encoding="utf-8").splitlines()
    assert "--model" not in args


def test_call_codex_exec_bare_text_fallback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub/older CLI that ignored --json prints the reply directly —
    the last JSON block is still the payload (claude_p's tolerance)."""
    _write_codex_stub(
        tmp_path, monkeypatch, "#!/bin/sh\nprintf '%s' 'Here you go: {\"v\": 7}'\n"
    )
    res = call_codex_exec("x")
    assert res.data == {"v": 7}
    assert res.text == 'Here you go: {"v": 7}'


def test_call_codex_exec_unparseable_reply(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_codex_stub(tmp_path, monkeypatch, _jsonl_stub(_agent_turn("prose, no json")))
    with pytest.raises(CodexExecUnparseableError) as exc:
        call_codex_exec("x")
    assert exc.value.reply == "prose, no json"


def test_call_codex_exec_turn_failed_event(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_codex_stub(
        tmp_path,
        monkeypatch,
        _jsonl_stub(
            [
                {"type": "turn.started"},
                {"type": "turn.failed", "error": {"message": "stream disconnected"}},
            ]
        ),
    )
    with pytest.raises(CodexExecError) as exc:
        call_codex_exec("x")
    assert "stream disconnected" in str(exc.value)
    assert exc.value.reply == "stream disconnected"


def test_call_codex_exec_missing_binary(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(tmp_path / "nope"))
    with pytest.raises(CodexExecError) as exc:
        call_codex_exec("x")
    assert exc.value.binary_missing is True


def test_call_codex_exec_scrubs_openai_api_key_when_logged_in(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ChatGPT login file under CODEX_HOME ⇒ OPENAI_API_KEY (the billed
    path) is scrubbed from the child env — never read, only existence-checked."""
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-billed")
    rec = tmp_path / "env.txt"
    stub = tmp_path / "codex-bin" / "codex"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/sh\n"
        f'printenv OPENAI_API_KEY > "{rec}" || true\n'
        + _jsonl_stub(_agent_turn('{"ok": true}')).split("\n", 1)[1],
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(stub))

    call_codex_exec("x")
    assert rec.read_text(encoding="utf-8").strip() == ""


# ── probes ───────────────────────────────────────────────────────────


def test_codex_bin_present(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CODEX_BIN", str(tmp_path / "missing"))
    assert cx.codex_bin_present() is False
    _write_codex_stub(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    assert cx.codex_bin_present() is True


def test_codex_login_present_requires_chatgpt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_codex_stub(
        tmp_path, monkeypatch, "#!/bin/sh\necho 'Logged in using ChatGPT'\n"
    )
    assert cx.codex_login_present() is True
    # An API-key-only login is the billed path, not the subscription lane.
    cx._bust_login_probe()
    _write_codex_stub(
        tmp_path, monkeypatch, "#!/bin/sh\necho 'Logged in using an API key'\n"
    )
    assert cx.codex_login_present() is False
    # Not logged in at all (non-zero exit).
    cx._bust_login_probe()
    _write_codex_stub(tmp_path, monkeypatch, "#!/bin/sh\nexit 1\n")
    assert cx.codex_login_present() is False


# ── result adapter + router ──────────────────────────────────────────


def test_result_from_codex_exec_quota_notice_pauses() -> None:
    res = CodexExecResult(
        data={},
        raw_stdout="",
        cost_usd=None,
        text="You've hit your usage limit. Try again later.",
    )
    out = result_from_codex_exec(res, model="gpt-5.1-codex", tier=Tier.BIG)
    assert out.paused is True
    assert out.quota_exhausted is True
    assert out.reason_class == "quota"


def test_codex_exec_tool_less() -> None:
    assert Transport.CODEX_EXEC.carries_tools is False


def _codex_chain(monkeypatch: pytest.MonkeyPatch, model: str = "gpt-5.1-codex") -> None:
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: [{"model": model, "transport": "codex_exec"}],
    )


def test_route_codex_rung_dropped_while_dark(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ships dark: with no llm.codex_exec/PRECIS_CODEX_EXEC a codex rung
    is skipped (paused degrade, cli_unavailable) — it never dispatches."""
    _codex_chain(monkeypatch)
    monkeypatch.delenv("PRECIS_CODEX_EXEC", raising=False)

    def _boom(*a: object, **kw: object) -> CodexExecResult:
        raise AssertionError("call_codex_exec must not run while the lane is dark")

    monkeypatch.setattr(router, "call_codex_exec", _boom)

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=False))
    assert out.paused is True
    assert out.cli_unavailable is True
    assert "dark" in (out.error or "")


def test_route_codex_rung_skips_without_binary(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lane lit but no codex binary on this host → clean skip."""
    _lit(monkeypatch)
    _codex_chain(monkeypatch)
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_bin_present", lambda: False
    )

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=False))
    assert out.paused is True
    assert "binary" in (out.error or "")


def test_route_codex_rung_skips_without_login(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lit(monkeypatch)
    _codex_chain(monkeypatch)
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_bin_present", lambda: True
    )
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_login_present", lambda: False
    )

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=False))
    assert out.paused is True
    assert "login" in (out.error or "")


def test_route_codex_rung_answers_when_lit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance leg 1: a lit, logged-in host answers via codex exec."""
    _lit(monkeypatch)
    _codex_chain(monkeypatch)
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_bin_present", lambda: True
    )
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_login_present", lambda: True
    )

    def fake_exec(prompt: str, **kw: object) -> CodexExecResult:
        return CodexExecResult(
            data={"ok": True},
            raw_stdout="",
            cost_usd=None,
            text='{"ok": true}',
            input_tokens=10,
            output_tokens=3,
        )

    monkeypatch.setattr(router, "call_codex_exec", fake_exec)

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=False))
    assert out.error is None
    assert out.data == {"ok": True}
    assert out.placement == "cloud"


def test_route_codex_quota_pauses_and_falls_through(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance leg 2: a quota notice on the codex rung pauses it and
    the chain falls through to the next rung instead of failing."""
    _lit(monkeypatch)
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_bin_present", lambda: True
    )
    monkeypatch.setattr(
        "precis.workers.capability_probe.codex_login_present", lambda: True
    )
    monkeypatch.setattr(
        "precis.utils.llm.live_config.chain_override",
        lambda tier: [
            {"model": "gpt-5.1-codex", "transport": "codex_exec"},
            {"model": "qwen-local", "transport": "local", "placement": "local"},
        ],
    )

    def quota_exec(prompt: str, **kw: object) -> CodexExecResult:
        raise CodexExecError(
            "codex exec reported an error: You've hit your usage limit",
            reply="You've hit your usage limit",
        )

    monkeypatch.setattr(router, "call_codex_exec", quota_exec)

    class _FakeClient:
        def __init__(self, config: object) -> None:
            pass

        def complete(self, messages: list[dict[str, str]]) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(text="local answered", total_tokens=3)

    monkeypatch.setattr("precis.workers.llm_summarize.LlmClient", _FakeClient)

    out = route(LlmRequest(tier=Tier.BIG, prompt="x", tools_needed=False))
    assert out.error is None
    assert out.text == "local answered"


def test_codex_error_result_quota_notice_pauses() -> None:
    """The error-path twin of the clean-reply quota check: a turn.failed
    usage-limit event pauses the rung (never an unparseable failure)."""
    exc = CodexExecError(
        "codex exec reported an error: You've hit your usage limit",
        reply="You've hit your usage limit",
    )
    out = router._codex_error_result(exc, model="gpt-5.1-codex", tier=Tier.BIG)
    assert out.paused is True
    assert out.quota_exhausted is True
    assert out.reason_class == "quota"


def test_codex_error_result_missing_binary_is_cli_unavailable() -> None:
    exc = CodexExecError("codex binary not found ('codex')", binary_missing=True)
    out = router._codex_error_result(exc, model="gpt-5.1-codex", tier=Tier.BIG)
    assert out.cli_unavailable is True
    assert out.paused is False


def test_codex_exec_filtered_from_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool-using call on a codex-only chain falls back to the default
    chain — the completion wire can't carry precis verbs."""
    _codex_chain(monkeypatch)
    rungs = router.resolve_chain(
        Tier.BIG, tools_needed=True, backend=router.Backend.ANTHROPIC
    )
    assert all(r.transport.carries_tools for r in rungs)
