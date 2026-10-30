"""Ratchet: every agent launch site binds an agentlog (gr540852).

A *launch site* is an ``LlmRequest(...)`` / ``call_claude_agent(...)`` /
``call_claude_agent_async(...)`` call under ``src/`` that hands the agent a
precis MCP server (an ``mcp_config=`` keyword that is not the literal
``None``) — the runs whose ``tool_calls`` rows need ``agentlog_id`` and
``source``. ``source`` rides ``LlmRequest.source`` (the router adds
``PRECIS_CURRENT_SOURCE``); the agentlog id must be put in the request's
env by the runner, via ``agentlog.run_env(...)`` or ``agentlog.ENV_VAR``.

A launch site in a file that does neither fails here unless it is listed in
:data:`PENDING`. ``PENDING`` only shrinks: an entry that now binds an
agentlog, or no longer launches an agent, fails too, so the list stays true.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

_LAUNCH_CALLS = frozenset(
    {"LlmRequest", "call_claude_agent", "call_claude_agent_async"}
)

#: Files that define the transport itself, not a runner.
_TRANSPORT_FILES = frozenset(
    {
        "src/precis/utils/llm/router.py",
        "src/precis/utils/claude_agent.py",
        "src/precis/utils/llm/requirement.py",
    }
)

#: The env binding a runner uses to hand its agentlog id to the child.
_BINDS_AGENTLOG = re.compile(r"agentlog\.(run_env\(|ENV_VAR\b)")

#: Agent launch sites that do not mint an agentlog yet. Shrink-only: fix one
#: (``agentlog.open_run`` + ``env_overlay=agentlog.run_env(log_id)``) and
#: delete its line.
PENDING: dict[str, str] = {
    "src/asa_slack/bot.py": "Slack bridge turn; mirror asa_bot.claude_invoke",
    "src/precis/diagram/agent.py": "diagram agent turn",
    "src/precis_web/ask.py": "web follow-up chat",
}

#: The runners gr540852 fixed plus the two that already bound one; each must
#: stay a compliant launch site (guards against the scan going blind).
REQUIRED: tuple[str, ...] = (
    "src/precis/workers/job_types/plan_tick.py",
    "src/precis/workers/dream_agent.py",
    "src/precis/workers/job_types/doctor_tick.py",
    "src/precis/workers/review.py",
    "src/asa_bot/claude_invoke.py",
)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _launches_agent(node: ast.Call) -> bool:
    if _call_name(node) not in _LAUNCH_CALLS:
        return False
    for kw in node.keywords:
        if kw.arg == "mcp_config":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def _launch_sites() -> dict[str, list[int]]:
    sites: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _TRANSPORT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(name + "(" in text for name in _LAUNCH_CALLS):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and _launches_agent(node):
                sites.setdefault(rel, []).append(node.lineno)
    return sites


def _binds(rel: str) -> bool:
    return bool(_BINDS_AGENTLOG.search((ROOT / rel).read_text(encoding="utf-8")))


SITES = _launch_sites()


def test_scan_finds_the_known_runners() -> None:
    missing = [rel for rel in REQUIRED if rel not in SITES]
    assert not missing, f"launch-site scan no longer sees {missing}"


def test_every_launch_site_binds_an_agentlog() -> None:
    offenders = {
        rel: lines
        for rel, lines in SITES.items()
        if rel not in PENDING and not _binds(rel)
    }
    assert not offenders, (
        "agent launch sites with no agentlog (their tool_calls rows get no "
        "agentlog_id): "
        + ", ".join(f"{rel}:{lines}" for rel, lines in offenders.items())
        + ". Mint one with agentlog.open_run(...) and pass "
        "env_overlay=agentlog.run_env(log_id) (gr540852)."
    )


def test_pending_only_shrinks() -> None:
    gone = [rel for rel in PENDING if rel not in SITES]
    fixed = [rel for rel in PENDING if rel in SITES and _binds(rel)]
    assert not gone, f"PENDING entries no longer launch an agent; delete: {gone}"
    assert not fixed, f"PENDING entries now bind an agentlog; delete: {fixed}"


# ── behaviour: the fixed runners put the id in the request env ──────────


class _Res:
    error = None
    text = ""
    raw_text = ""
    duration_s = 0.0
    cost_usd = None
    model = None
    paused = False
    timed_out = False
    terminal_reason = None


def _stub_agentlog(monkeypatch: pytest.MonkeyPatch, log_id: int) -> list[Any]:
    from precis import agentlog

    closed: list[Any] = []
    monkeypatch.setattr(agentlog, "open_run", lambda store, **kw: log_id)
    monkeypatch.setattr(
        agentlog, "close_run", lambda store, lid, **kw: closed.append((lid, kw))
    )
    return closed


def test_doctor_tick_passes_its_agentlog(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.agentlog import ENV_VAR
    from precis.utils.llm import router
    from precis.workers.job_types import doctor_tick

    closed = _stub_agentlog(monkeypatch, 4242)
    seen: list[Any] = []

    def fake_route(req: Any) -> _Res:
        seen.append(req)
        return _Res()

    monkeypatch.setattr(router, "route", fake_route)
    doctor_tick.run(store=object(), job_ref_id=7, params={})
    assert seen, "doctor_tick did not dispatch"
    assert seen[0].env_overlay == {ENV_VAR: "4242"}
    assert seen[0].source == "doctor_tick"
    assert closed and closed[0][0] == 4242


def test_review_passes_its_agentlog(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.agentlog import ENV_VAR
    from precis.workers import review

    closed = _stub_agentlog(monkeypatch, 99)
    seen: list[Any] = []

    def fake_route(req: Any) -> _Res:
        seen.append(req)
        raise RuntimeError("stop after dispatch")

    from precis.workers.structural import STRUCTURAL as reviewer

    monkeypatch.setattr(review, "_gate_enabled", lambda env: True)
    monkeypatch.setattr(review, "skip_if_high_load", lambda name: False)
    monkeypatch.setattr(review, "_recent_digest_exists", lambda *a: False)
    monkeypatch.setattr(review, "_recent_failure", lambda *a: False)
    monkeypatch.setattr(review, "_assemble_reviewer_blocks", lambda *a: [])
    monkeypatch.setattr(
        review.ClaudeAgentAdapter, "render", staticmethod(lambda blocks: ("", "p"))
    )
    monkeypatch.setattr(review, "route", fake_route)
    store: Any = object()
    with pytest.raises(RuntimeError, match="stop after dispatch"):
        review.run_review_pass(reviewer, store)
    assert seen[0].env_overlay == {ENV_VAR: "99"}
    assert seen[0].source == f"review:{reviewer.name}"
    assert not closed  # the stub raised before close; real route never raises


def test_asa_turn_overlay_carries_the_agentlog() -> None:
    from types import SimpleNamespace

    from asa_bot.claude_invoke import _turn_overlay
    from precis.agentlog import ENV_VAR

    cfg: Any = SimpleNamespace(env={"CLAUDE_CODE_OAUTH_TOKEN": "x"})
    overlay = _turn_overlay(cfg, "conv-1", None, 77)
    assert overlay[ENV_VAR] == "77"
    assert ENV_VAR not in _turn_overlay(cfg, "conv-1", None, None)


def test_asa_invoke_mints_one_log_per_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    from asa_bot import claude_invoke
    from asa_bot.config import LLMConfig
    from precis.agentlog import ENV_VAR
    from precis.utils.llm.router import LlmResult, Tier

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)
    monkeypatch.setattr(claude_invoke, "warm_runtime", lambda: None)
    opened: list[tuple[str, ...]] = []
    closed: list[int] = []

    def fake_open(model: str, slug: str, msg: str) -> int:
        opened.append((model, slug, msg))
        return 55

    monkeypatch.setattr(claude_invoke, "_open_turn_log", fake_open)
    monkeypatch.setattr(
        claude_invoke, "_close_turn_log", lambda lid, res: closed.append(lid)
    )
    seen: list[Any] = []

    def fake_route(req: Any) -> LlmResult:
        seen.append(req)
        return LlmResult(
            text="ok", cost_usd=0.0, turns_used=1, model="m", tier=Tier.BIG
        )

    monkeypatch.setattr(claude_invoke, "route", fake_route)
    asyncio.run(claude_invoke.invoke(LLMConfig(), "sys", "hi", conv_slug="c-1"))
    assert opened == [("local", "c-1", "hi")]
    assert seen[0].env_overlay[ENV_VAR] == "55"
    assert seen[0].source == "asa_bot"
    assert closed == [55]


def test_container_allowlist_carries_agentlog_and_source() -> None:
    """The containerised path forwards both keys and never the token."""
    from precis.agentlog import ENV_VAR, SOURCE_ENV_VAR
    from precis.workers.executors.agent_container import run_context_env

    env = run_context_env(
        {ENV_VAR: "5", SOURCE_ENV_VAR: "asa_bot", "CLAUDE_CODE_OAUTH_TOKEN": "s"}
    )
    assert env == {ENV_VAR: "5", SOURCE_ENV_VAR: "asa_bot"}
