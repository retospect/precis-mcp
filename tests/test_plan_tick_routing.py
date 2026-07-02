"""Unit 4b — plan_tick model selection routes through the ADR 0046 resolver
and the tick carries a ``--max-budget-usd`` runaway-spend cap.

DB-free: the model helpers are pure env reads, and the ``run()`` cmd test
monkeypatches ``build_planner_prompts`` / the workspace + agentlog helpers /
``subprocess.run`` so no ``claude`` binary spawns and no store is touched.
The byte-identity assertions are the behavior-preservation contract: each
``LLM:<tier>`` tag must resolve to the exact model string the legacy inline
``_model_alias`` table produced.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.workers.job_types import plan_tick as pt

# ── _model_alias: byte-identical to the legacy inline table ────────────

# The legacy defaults, kept literal here so a drift in either the resolver
# table or the plan_tick map is caught as a mismatch.
_LEGACY_DEFAULTS = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}
_ALIAS_ENV = {
    "opus": "PRECIS_MODEL_OPUS",
    "sonnet": "PRECIS_MODEL_SONNET",
    "haiku": "PRECIS_MODEL_HAIKU",
}


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_model_alias_default_byte_identical(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env unset → the resolver default matches the legacy pinned default."""
    for var in _ALIAS_ENV.values():
        monkeypatch.delenv(var, raising=False)
    assert pt._model_alias(alias) == _LEGACY_DEFAULTS[alias]


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_model_alias_env_override(alias: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env set → the override wins, same as the legacy per-alias env read."""
    monkeypatch.setenv(_ALIAS_ENV[alias], "pinned-model-z")
    assert pt._model_alias(alias) == "pinned-model-z"


def test_model_alias_unknown_passthrough() -> None:
    """An unrecognized name passes through unchanged (mirrors the old
    ``defaults.get(model, model)`` fallback)."""
    assert pt._model_alias("no-such-tier") == "no-such-tier"


# ── _max_budget_usd: env-overridable cap ───────────────────────────────


def test_max_budget_usd_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_PLAN_TICK_MAX_USD", raising=False)
    assert pt._max_budget_usd() == pt._DEFAULT_MAX_USD == 5.00


def test_max_budget_usd_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_USD", "12.5")
    assert pt._max_budget_usd() == 12.5


def test_max_budget_usd_malformed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_USD", "not-a-float")
    assert pt._max_budget_usd() == pt._DEFAULT_MAX_USD


# ── run(): the spawned cmd carries the resolved model + budget cap ─────


class _FakeCompleted:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


class _FakePrompts:
    system = "SYS"
    user = "USR"


def _capture_cmd(monkeypatch: pytest.MonkeyPatch, model: str) -> list[str]:
    """Run ``plan_tick.run`` with everything DB/subprocess stubbed and
    return the argv the runner would have spawned."""
    import precis.agentlog as agentlog
    import precis.workers.planner_prompt as planner_prompt

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        captured["cmd"] = cmd
        return _FakeCompleted()

    monkeypatch.setattr(
        planner_prompt, "build_planner_prompts", lambda *a, **k: _FakePrompts()
    )
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda *a, **k: None)
    monkeypatch.setattr(pt, "_disable_prose_file_kind", lambda *a, **k: None)
    monkeypatch.setattr(agentlog, "open_log", lambda *a, **k: 1)
    monkeypatch.setattr(agentlog, "finalize_log", lambda *a, **k: None)
    monkeypatch.setattr(pt.subprocess, "run", fake_run)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude-stub")
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)

    pt.run(
        store=object(),
        job_ref_id=1,
        parent_ref_id=2,
        params={"model": model},
    )
    return captured["cmd"]


def test_run_passes_resolved_model_and_budget_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _ALIAS_ENV.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PRECIS_PLAN_TICK_MAX_USD", raising=False)

    cmd = _capture_cmd(monkeypatch, "opus")

    # Resolved model is passed to --model (byte-identical to legacy default).
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"
    # The runaway-spend backstop is present with the default value.
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5.0"
    # Existing flags untouched.
    assert cmd[cmd.index("--max-turns") + 1] == "30"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_run_budget_cap_honours_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_USD", "9.0")
    cmd = _capture_cmd(monkeypatch, "sonnet")
    assert cmd[cmd.index("--max-budget-usd") + 1] == "9.0"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
