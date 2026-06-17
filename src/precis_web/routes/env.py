"""Env tab — per-agent setup inspector.

Operator-facing surface for "is the dream / structural / deep-review /
job-claude-inproc agent's environment actually wired correctly?". Each
agent is a ``claude -p`` invocation with a system-prompt path + a
directive prompt path + an MCP config + a model + a deny list + env
vars. When any of those is missing or stale the agent dispatches but
produces nothing useful (see task #184 — dreams returning
``cost=$0.0000`` with zero memories written).

The page:

* Lists every introspectable agent (dropdown + Go).
* For the picked agent, shows the resolved system prompt, directive
  prompt, MCP server list (parsed from the MCP config JSON), model,
  deny rules, env vars consulted, and gating flags.

Pure read-only — never invokes anything. The view is what *would*
run if the agent fired right now.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import templates

router = APIRouter(prefix="/env", tags=["env"])


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static config snapshot for one agent.

    ``env_keys`` is the set of env vars the worker consults — the
    page reads the *current process's* env and reports each as
    present/absent + a redacted preview. ``file_paths`` carry the
    role of "render this file's contents inline" (system prompt /
    directive prompt) when present.
    """

    key: str
    label: str
    description: str
    model_default: str
    model_env: str
    system_prompt_env: str
    directive_prompt_env: str
    mcp_config_env: str
    disallowed_tools: tuple[str, ...]
    max_turns: int
    timeout_s: int
    env_keys: tuple[str, ...]
    gating: tuple[tuple[str, str], ...]  # (env_var, description)


#: Hard-coded registry of introspectable agents. New agents land here
#: when their worker module ships — adding one is a single dataclass
#: row, no template branching needed. Keep aligned with the actual
#: ``call_claude_agent`` call sites in workers/.
AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="dream_agent",
        label="Dream agent",
        description=(
            "15-min LaunchDaemon. Reads recent internal-thought memories "
            "and writes new tier:dream memories. Runs as hermes on "
            "melchior with Claude OAuth (no API key needed)."
        ),
        model_default="claude-sonnet-4-6",
        model_env="PRECIS_DREAM_AGENT_MODEL",
        system_prompt_env="PRECIS_DREAM_SOUL_PATH",
        directive_prompt_env="PRECIS_DREAM_PROMPT_PATH",
        mcp_config_env="PRECIS_MCP_CONFIG",
        disallowed_tools=("WebFetch", "WebSearch"),
        max_turns=20,
        timeout_s=600,
        env_keys=(
            "PRECIS_DREAM_AGENT",
            "PRECIS_DREAM_AGENT_MODEL",
            "PRECIS_DREAM_PROMPT_PATH",
            "PRECIS_DREAM_SOUL_PATH",
            "PRECIS_MCP_CONFIG",
            "PRECIS_DATABASE_URL",
            "PRECIS_PROCESS",
        ),
        gating=(
            ("PRECIS_DREAM_AGENT", "must be '1' / 'true' to run"),
            ("PRECIS_DATABASE_URL", "runtime can't load without it"),
        ),
    ),
    AgentSpec(
        key="structural",
        label="Structural reviewer",
        description=(
            "6h-dedup pass. Walks the todo tree, flags drift / sibling "
            "contradictions / depth-fanout warnings. Opus."
        ),
        model_default="claude-opus-4-7",
        model_env="PRECIS_STRUCTURAL_MODEL",
        system_prompt_env="",
        directive_prompt_env="",
        mcp_config_env="PRECIS_MCP_CONFIG",
        disallowed_tools=("WebFetch", "WebSearch"),
        max_turns=12,
        timeout_s=600,
        env_keys=(
            "PRECIS_STRUCTURAL_REVIEW",
            "PRECIS_STRUCTURAL_MODEL",
            "PRECIS_MCP_CONFIG",
            "PRECIS_DATABASE_URL",
            "PRECIS_DAILY_COST_CEILING",
        ),
        gating=(
            ("PRECIS_STRUCTURAL_REVIEW", "must be '1' to run"),
            ("PRECIS_DATABASE_URL", "runtime can't load without it"),
        ),
    ),
    AgentSpec(
        key="deep_review",
        label="Deep review",
        description=(
            "Weekly-dedup pass. Allen-style archive / prune / "
            "rebalance / long-wait review. Opus."
        ),
        model_default="claude-opus-4-7",
        model_env="PRECIS_DEEP_REVIEW_MODEL",
        system_prompt_env="",
        directive_prompt_env="",
        mcp_config_env="PRECIS_MCP_CONFIG",
        disallowed_tools=("WebFetch", "WebSearch"),
        max_turns=12,
        timeout_s=900,
        env_keys=(
            "PRECIS_DEEP_REVIEW",
            "PRECIS_DEEP_REVIEW_MODEL",
            "PRECIS_MCP_CONFIG",
            "PRECIS_DATABASE_URL",
            "PRECIS_DAILY_COST_CEILING",
        ),
        gating=(
            ("PRECIS_DEEP_REVIEW", "must be '1' to run"),
            ("PRECIS_DATABASE_URL", "runtime can't load without it"),
        ),
    ),
    AgentSpec(
        key="job_claude_inproc",
        label="Claude in-process executor",
        description=(
            "Planner-coroutine consumer. Claims minted kind='job' refs "
            "(plan_tick / fix_gripe), shells out to claude -p with the "
            "model tier from the parent's LLM:* tag, records summary."
        ),
        model_default="(per parent LLM:* tag)",
        model_env="PRECIS_JOB_CLAUDE_MODEL",
        system_prompt_env="",
        directive_prompt_env="",
        mcp_config_env="PRECIS_MCP_CONFIG",
        disallowed_tools=("WebFetch", "WebSearch"),
        max_turns=20,
        timeout_s=900,
        env_keys=(
            "PRECIS_MCP_CONFIG",
            "PRECIS_DATABASE_URL",
            "PRECIS_DAILY_COST_CEILING",
            "PRECIS_FIX_REPO_DIR",
            "PRECIS_FIX_WORK_DIR",
        ),
        gating=(
            ("PRECIS_MCP_CONFIG", "MCP config the in-proc claude reads"),
        ),
    ),
)

_BY_KEY: dict[str, AgentSpec] = {a.key: a for a in AGENTS}


def _read_file(path: str | None, *, max_chars: int = 50_000) -> dict[str, Any]:
    """Resolve a path and read its contents (capped)."""
    if not path:
        return {"path": None, "exists": False, "text": None, "size": 0}
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "text": None, "size": 0}
    try:
        raw = p.read_text(errors="replace")
    except OSError as exc:
        return {
            "path": str(p),
            "exists": True,
            "text": f"(read failed: {exc})",
            "size": 0,
        }
    size = len(raw)
    if size > max_chars:
        raw = raw[:max_chars] + f"\n\n… (truncated; full size {size:,} chars)"
    return {"path": str(p), "exists": True, "text": raw, "size": size}


def _parse_mcp_config(path: str | None) -> dict[str, Any]:
    """Parse the MCP config JSON and project (name, kind, transport)."""
    if not path or not Path(path).exists():
        return {"path": path, "exists": False, "servers": []}
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": path, "exists": True, "servers": [], "error": str(exc)}
    servers_raw = payload.get("mcpServers") or payload.get("servers") or {}
    servers: list[dict[str, Any]] = []
    for name, cfg in (servers_raw or {}).items():
        if not isinstance(cfg, dict):
            continue
        transport = "stdio" if "command" in cfg else "sse" if "url" in cfg else "?"
        servers.append(
            {
                "name": name,
                "transport": transport,
                "command": cfg.get("command"),
                "args": cfg.get("args") or [],
                "url": cfg.get("url"),
            }
        )
    return {"path": path, "exists": True, "servers": servers}


def _redact(value: str | None) -> str:
    """Quote-friendly redacted preview of an env var value."""
    if value is None:
        return "(unset)"
    if not value:
        return "(empty)"
    if any(k in value.lower() for k in ("password", "key", "secret", "token")):
        # Defensive: caller already filtered by var name, but redact if
        # the value shape looks credential-ish.
        return f"(redacted, {len(value)} chars)"
    if len(value) > 80:
        return value[:80] + "…"
    return value


def _env_snapshot(spec: AgentSpec) -> list[dict[str, Any]]:
    """One row per env var the agent consults."""
    sensitive = {"PASSWORD", "KEY", "SECRET", "TOKEN", "API_KEY", "URL", "DSN"}
    rows: list[dict[str, Any]] = []
    for key in spec.env_keys:
        raw = os.environ.get(key)
        present = raw is not None
        # ``PRECIS_DATABASE_URL`` carries the DB password embedded, so
        # we don't show it verbatim — present-or-absent is the signal.
        is_sensitive = any(tok in key.upper() for tok in sensitive)
        rows.append(
            {
                "key": key,
                "present": present,
                "value": (
                    f"(set, {len(raw)} chars)"
                    if is_sensitive and present
                    else _redact(raw)
                ),
            }
        )
    return rows


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    agent: str | None = None,
) -> HTMLResponse:
    """Render the env-inspector page; ``?agent=KEY`` selects the row.

    Without ``agent=``, just the dropdown + a short description per
    row. With it, the full detail block for that agent.
    """
    spec = _BY_KEY.get(agent) if agent else None
    detail: dict[str, Any] | None = None
    if spec is not None:
        system_prompt = _read_file(os.environ.get(spec.system_prompt_env))
        directive_prompt = _read_file(
            os.environ.get(spec.directive_prompt_env)
        )
        mcp = _parse_mcp_config(os.environ.get(spec.mcp_config_env))
        detail = {
            "spec": spec,
            "model": os.environ.get(spec.model_env) or spec.model_default,
            "system_prompt": system_prompt,
            "directive_prompt": directive_prompt,
            "mcp": mcp,
            "env_rows": _env_snapshot(spec),
        }
    return templates.TemplateResponse(
        request,
        "env/index.html.j2",
        {
            "active_tab": "env",
            "agents": AGENTS,
            "selected": agent or "",
            "detail": detail,
        },
    )
