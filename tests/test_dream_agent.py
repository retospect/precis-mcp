"""Tests for the dream_agent worker (Slice-3 dispatch unification)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.store import Store
from precis.utils.claude_agent import AgentResult, ClaudeAgentError
from precis.workers.dream_agent import (
    _apply_fisheye,
    _gate_enabled,
    _load_prompt,
    run_dream_pass,
)


def test_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_AGENT", raising=False)
    assert _gate_enabled() is False


def test_apply_fisheye_appends_kind_diverse_draw(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dream eye-draw (default-ON) appends fresh memories/papers/patents as a
    fisheye working set; PRECIS_DREAM_FISHEYE=0 leaves the prompt untouched."""
    monkeypatch.delenv("PRECIS_DREAM_FISHEYE", raising=False)
    store.insert_ref(kind="memory", slug=None, title="a recent dreamable note")
    store.insert_ref(kind="paper", slug="freshpaper", title="A Fresh Paper")

    out = _apply_fisheye("DREAM DIRECTIVE", store)
    assert "DREAM DIRECTIVE" in out
    assert "Fresh material to dream over" in out
    assert "a recent dreamable note" in out
    assert "A Fresh Paper" in out  # rendered by handle+title, not the slug

    monkeypatch.setenv("PRECIS_DREAM_FISHEYE", "0")
    assert _apply_fisheye("DREAM DIRECTIVE", store) == "DREAM DIRECTIVE"


def test_apply_fisheye_noop_on_empty_corpus(store: Store) -> None:
    # no memories/papers/patents → prompt unchanged (never a spurious block)
    assert _apply_fisheye("D", store) == "D"


def test_pass_skips_when_gate_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_DREAM_AGENT", raising=False)
    result = run_dream_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_packaged_prompt_is_the_persona_neutral_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No override → the packaged dreaming workflow loads, and it carries
    # the precis closed DREAM: axis but none of the operator persona
    # (asa applies persona via the system prompt, not this file).
    monkeypatch.delenv("PRECIS_DREAM_PROMPT_PATH", raising=False)
    prompt = _load_prompt()
    assert prompt is not None
    assert prompt.startswith("DREAM CYCLE")
    assert "DREAM:speculative" in prompt
    assert "user:asa" not in prompt


def test_packaged_prompt_carries_thread_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # opus-4.8 on the dream pass gains a "pursue threads worth returning
    # to" directive — captured as ``thread:`` memories, capped + dedup'd
    # + explicitly speculative so it never clogs the doable rotation.
    monkeypatch.delenv("PRECIS_DREAM_PROMPT_PATH", raising=False)
    prompt = _load_prompt()
    assert prompt is not None
    # The directive is present and lands as a memory, not a todo.
    assert "thread:" in prompt
    assert "Thread worth returning to" in prompt
    assert "WHY IT MIGHT MATTER LATER" in prompt
    # Anti-noise constraints: capped to a handful, dedup'd, speculative,
    # and never a todo (so the doable rotation stays clean).
    assert "at most THREE" in prompt
    assert 'search(kind="memory", tags=["thread:"]' in prompt
    assert "DREAM:speculative" in prompt
    assert "Never mint a ``kind='todo'``" in prompt


def test_dream_default_model_is_cloud_super(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The dream pass consolidated onto the router's cloud-super tier
    # (opus-4.8); an explicit PRECIS_DREAM_AGENT_MODEL pin still wins.
    from precis.utils.llm.router import Tier, resolve_model
    from precis.workers.dream_agent import _default_model

    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    assert _default_model() == resolve_model(Tier.CLOUD_SUPER)
    assert _default_model() == "claude-opus-4-8"


def test_override_prompt_wins_over_packaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "site-dream.md"
    override.write_text("SITE-SPECIFIC DREAM PROMPT")
    monkeypatch.setenv("PRECIS_DREAM_PROMPT_PATH", str(override))
    assert _load_prompt() == "SITE-SPECIFIC DREAM PROMPT"


def test_falls_back_to_packaged_prompt_when_override_missing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unset override no longer skips the pass — it dispatches with the
    # packaged prompt.
    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.delenv("PRECIS_DREAM_PROMPT_PATH", raising=False)

    captured: dict = {}

    def _fake(*args, **kw) -> AgentResult:
        captured["prompt"] = args[0] if args else kw.get("prompt")
        return AgentResult(
            final_text="dreamed.", cost_usd=0.0, duration_s=1, turns_used=1
        )

    monkeypatch.setattr("precis.workers.dream_agent.call_claude_agent", _fake)
    result = run_dream_pass(store)
    assert result.claimed == 1 and result.ok == 1
    assert "DREAM CYCLE" in captured["prompt"]


def test_happy_path_dispatches_with_files(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "dream-prompt.md"
    prompt.write_text("DREAM CYCLE — do dream things.")
    soul = tmp_path / "soul.md"
    soul.write_text("you are asa.")
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")

    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.setenv("PRECIS_DREAM_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("PRECIS_DREAM_SOUL_PATH", str(soul))
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(mcp))

    captured: dict = {}

    def _fake(*args, **kw) -> AgentResult:
        captured["prompt"] = args[0] if args else kw.get("prompt")
        captured["system_prompt"] = kw.get("system_prompt")
        captured["mcp_config"] = kw.get("mcp_config")
        captured["disallowed"] = kw.get("disallowed_tools")
        return AgentResult(
            final_text="dreamed.", cost_usd=0.02, duration_s=10, turns_used=5
        )

    monkeypatch.setattr("precis.workers.dream_agent.call_claude_agent", _fake)
    result = run_dream_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0
    # Prompt body landed.
    assert "DREAM CYCLE" in captured["prompt"]
    # system_prompt / mcp_config came through as Path objects.
    assert isinstance(captured["system_prompt"], Path)
    assert isinstance(captured["mcp_config"], Path)
    # WebFetch / WebSearch disabled.
    assert "WebFetch" in captured["disallowed"]
    assert "WebSearch" in captured["disallowed"]


def test_pass_counts_failure_on_dispatch_error(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "dream-prompt.md"
    prompt.write_text("dream.")
    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.setenv("PRECIS_DREAM_PROMPT_PATH", str(prompt))

    def _err(*a, **kw):
        raise ClaudeAgentError("bad", stdout="", stderr="model died")

    monkeypatch.setattr("precis.workers.dream_agent.call_claude_agent", _err)
    result = run_dream_pass(store)
    assert result.claimed == 1
    assert result.failed == 1


# ── lens selection (persona-from-oracle + process fallback) ─────────


def test_process_lens_prob_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.workers.dream_agent import _process_lens_prob

    monkeypatch.delenv("PRECIS_DREAM_PROCESS_PROB", raising=False)
    assert _process_lens_prob() == 0.15
    monkeypatch.setenv("PRECIS_DREAM_PROCESS_PROB", "0.5")
    assert _process_lens_prob() == 0.5
    monkeypatch.setenv("PRECIS_DREAM_PROCESS_PROB", "nonsense")
    assert _process_lens_prob() == 0.15


def test_dream_lens_names_default_and_commalist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.workers.dream_agent import _dream_lens_names

    monkeypatch.delenv("PRECIS_DREAM_LENS", raising=False)
    assert _dream_lens_names() == ["sci"]
    monkeypatch.setenv("PRECIS_DREAM_LENS", "sci, art")
    assert _dream_lens_names() == ["sci", "art"]


def test_select_lens_block_process_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the process branch: it must return the Disney lens block and
    # never touch the oracle.
    from precis.workers import dream_agent as da

    monkeypatch.setenv("PRECIS_DREAM_PROCESS_PROB", "1")

    def _boom(*a, **kw):  # pragma: no cover — must not be called
        raise AssertionError("oracle draw should be skipped in process branch")

    monkeypatch.setattr(da, "draw_lens_entry", _boom)
    block = da._select_lens_block(store)
    assert block is not None
    assert "Disney creativity strategy" in block


def test_select_lens_block_oracle_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from precis.store import Block, Ref
    from precis.utils.oracle_lens import LensDraw
    from precis.workers import dream_agent as da

    monkeypatch.setenv("PRECIS_DREAM_PROCESS_PROB", "0")
    fake = LensDraw(
        ref=cast(Ref, SimpleNamespace(id=1, slug="scientists", title="Scientists")),
        block=cast(
            Block,
            SimpleNamespace(
                id=11,
                pos=6,
                text="Take Shannon's stance.",
                meta={"section_path": ["Shannon"]},
            ),
        ),
        from_favoured=True,
    )
    monkeypatch.setattr(da, "draw_lens_entry", lambda *a, **kw: fake)
    block = da._select_lens_block(store)
    assert block is not None
    assert block.startswith("## This cycle's lens: Shannon")
    assert "Take Shannon's stance." in block


def test_apply_lens_unlensed_when_no_oracle(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers import dream_agent as da

    monkeypatch.setenv("PRECIS_DREAM_PROCESS_PROB", "0")
    monkeypatch.setattr(da, "draw_lens_entry", lambda *a, **kw: None)
    out = da._apply_lens("DIRECTIVE", store)
    assert out == "DIRECTIVE"
