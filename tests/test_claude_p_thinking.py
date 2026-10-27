"""``thinking=False`` must reach the ``claude -p`` child (gr506762 slice 4).

The ``claude_p`` rung silently dropped ``LlmRequest.thinking`` — a
``thinking=False`` ``survey_extract:table_recipe`` call still ran with
extended thinking on, billing 600-13,900 output tokens for a sub-500-token
reply (~$0.030/call where thinking-off is ~$0.008) and hitting the 120 s
timeout on the biggest thinking runs. The off switch is the documented
``MAX_THINKING_TOKENS=0`` in the child's env.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import precis.utils.claude_p as claude_p
from precis.utils.claude_oauth import ENV_VAR
from precis.utils.claude_p import ClaudePResult
from precis.utils.llm import router


def _spy(captured: dict[str, Any]) -> Any:
    def _fake(
        args: Any,
        *,
        binary: str,
        label: str,
        timeout_s: float,
        error_cls: Any,
        env: Any = None,
        **kw: Any,
    ) -> Any:
        captured["env"] = dict(env or {})
        return SimpleNamespace(stdout='{"ok": true}', stderr="")

    return _fake


def test_thinking_false_sets_max_thinking_tokens_zero(monkeypatch: Any) -> None:
    """The comment's failure: a ``thinking=False`` call must not let the
    child think."""
    monkeypatch.setenv(ENV_VAR, ("sk-a" + "nt-oat01-TEST"))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_p, "run_claude", _spy(captured))

    claude_p.call_claude_p("reply JSON {}", thinking=False)

    assert captured["env"]["MAX_THINKING_TOKENS"] == "0"


def test_thinking_unset_or_true_leaves_the_env_alone(monkeypatch: Any) -> None:
    """``None``/``True`` keep the ambient env — an operator-set budget
    still applies and the CLI's own default runs otherwise."""
    monkeypatch.setenv(ENV_VAR, ("sk-a" + "nt-oat01-TEST"))
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_p, "run_claude", _spy(captured))

    claude_p.call_claude_p("reply JSON {}")
    assert "MAX_THINKING_TOKENS" not in captured["env"]

    claude_p.call_claude_p("reply JSON {}", thinking=True)
    assert "MAX_THINKING_TOKENS" not in captured["env"]


def test_thinking_false_overrides_an_ambient_cap(monkeypatch: Any) -> None:
    """An ambient ``MAX_THINKING_TOKENS`` loses to an explicit off — the
    caller asked for no thinking, not the operator's budget."""
    monkeypatch.setenv(ENV_VAR, ("sk-a" + "nt-oat01-TEST"))
    monkeypatch.setenv("MAX_THINKING_TOKENS", "8000")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_p, "run_claude", _spy(captured))

    claude_p.call_claude_p("reply JSON {}", thinking=False)

    assert captured["env"]["MAX_THINKING_TOKENS"] == "0"


def test_claude_p_provider_threads_req_thinking(monkeypatch: Any) -> None:
    """The router was where the knob was dropped: a ``thinking=False``
    request must reach ``call_claude_p`` as ``thinking=False``."""
    seen: dict[str, Any] = {}

    def _fake(prompt: str, **kw: Any) -> ClaudePResult:
        seen.update(kw)
        return ClaudePResult(
            data={"ok": True},
            raw_stdout='{"ok": true}',
            cost_usd=0.0,
            text='{"ok": true}',
        )

    monkeypatch.setattr(router, "call_claude_p", _fake)

    out = router.ClaudePProvider().run(
        router.LlmRequest(tier=router.Tier.MEDIUM, prompt="x", thinking=False),
        model="claude-haiku-4-5",
    )

    assert seen["thinking"] is False
    assert out.error is None


def test_rung_knobs_reports_claude_p_thinking() -> None:
    """The structured-selection honesty table: claude_p now forwards
    ``thinking`` (on/off only — ``effort`` still has no wire), unlike
    ``claude_agent`` which ignores it."""
    assert (
        router.rung_knobs(router.Rung(router.Transport.CLAUDE_P, model="m", label="p"))[
            "thinking"
        ]
        is True
    )
    assert (
        router.rung_knobs(
            router.Rung(router.Transport.CLAUDE_AGENT, model="m", label="a")
        )["thinking"]
        is False
    )
