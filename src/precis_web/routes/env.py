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

**Cross-daemon env**: the web process has its OWN environment; the
dream / worker-agent daemons have theirs. To answer "what env will
the dream see when it fires?" we read the target daemon's plist
directly (``/Library/LaunchDaemons/com.precis.*.plist``) and project
its ``EnvironmentVariables`` block. The web's process env is
irrelevant to that question.
"""

from __future__ import annotations

import json
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import templates

router = APIRouter(prefix="/env", tags=["env"])

#: Where macOS LaunchDaemon plists live. Read-only access is enough
#: for the env inspector — we never write or load anything.
_PLIST_DIR = Path("/Library/LaunchDaemons")


#: Patterns for parsing a bash wrapper. Captures ``export X=Y``,
#: ``X=Y`` (top-level assignment), with optional double-quotes around
#: the value. Identifiers can be upper or lower case — dream-pass.sh
#: uses lowercase locals like ``soul=…`` / ``prompt=…`` and then
#: exports them into uppercase env vars (``export
#: PRECIS_DREAM_PROMPT_PATH="$prompt"``). We resolve ``$other``
#: references against earlier assignments so the result reads as
#: ``PRECIS_DREAM_PROMPT_PATH=/opt/asa/files/dream-prompt.md``
#: instead of the literal ``$prompt``.
import re as _re

_BASH_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_BASH_ASSIGN_RE = _re.compile(
    r"^(?:export\s+)?(" + _BASH_IDENT + r')=(?:"([^"]*)"|(\S+))',
)


def _parse_wrapper_env(path: str) -> dict[str, str]:
    """Extract bash exports/assigns from a wrapper script.

    Returns ``{}`` when the path is empty, missing, or unreadable.
    Variable references inside ``"…"`` values (``$prompt`` etc.) are
    resolved against assignments earlier in the file (lowercase locals
    AND uppercase exports). This is a deliberately tiny shell-emulator
    — enough for our wrappers that only do ``x=literal`` and
    ``export Y=$x``, and we accept losing fidelity on anything fancier.
    """
    if not path:
        return {}
    p = Path(path)
    try:
        raw = p.read_text()
    except OSError:
        return {}
    locals_map: dict[str, str] = {}
    ref_re = _re.compile(r"\$\{?(" + _BASH_IDENT + r")\}?")
    for line in raw.splitlines():
        m = _BASH_ASSIGN_RE.match(line.strip())
        if m is None:
            continue
        name = m.group(1)
        value = m.group(2) if m.group(2) is not None else m.group(3)

        def _resolve(match: _re.Match[str]) -> str:
            return locals_map.get(match.group(1), match.group(0))

        value = ref_re.sub(_resolve, value)
        locals_map[name] = value
    # Only surface UPPERCASE keys — those are the real env exports.
    # Lowercase locals (``soul=…``) were only used internally during
    # resolution and aren't part of the runtime env.
    return {k: v for k, v in locals_map.items() if k.isupper() or k == k.upper()}


def _read_plist_env(label: str) -> dict[str, str]:
    """Project a LaunchDaemon's ``EnvironmentVariables`` to a flat dict.

    Returns an empty dict when the plist is missing or unreadable.
    macOS's plist tools tolerate XML comments that contain ``--``
    (e.g. a comment mentioning ``--only dream_agent``), but Python's expat
    parser rejects them as not-well-formed. Apple's ``plutil`` is the
    canonical way to round-trip a plist on macOS — fall back to
    shelling out and reading its JSON output when plistlib chokes.
    """
    path = _PLIST_DIR / f"{label}.plist"
    if not path.exists():
        return {}
    # First attempt: plistlib direct (fast path, no subprocess).
    try:
        with path.open("rb") as fh:
            payload = plistlib.load(fh)
    except Exception:
        # Lenient fallback via ``plutil -convert json -o - <path>``.
        try:
            import subprocess

            res = subprocess.run(
                ["/usr/bin/plutil", "-convert", "json", "-o", "-", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode != 0:
                return {}
            import json as _json

            payload = _json.loads(res.stdout)
        except Exception:
            return {}
    env = payload.get("EnvironmentVariables") or {}
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static config snapshot for one agent.

    ``env_keys`` is the set of env vars the worker consults — the
    page reads the *target daemon's* plist EnvironmentVariables (NOT
    the web process's env) and reports each as present/absent +
    a redacted preview. ``launchd_label`` names the plist:
    ``com.precis.dream`` → ``/Library/LaunchDaemons/com.precis.dream.plist``.

    Some daemons (dream) wrap their precis-cli invocation in a bash
    script that exports additional env vars at runtime. ``wrapper``
    points at that script; we parse its ``export X=Y`` lines and
    merge them on top of the plist env so the page reflects what
    the agent *actually* sees, not just the launchd layer.
    """

    key: str
    label: str
    description: str
    launchd_label: str
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
    wrapper: str = ""


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
        launchd_label="com.precis.dream",
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
        # The dream plist invokes ``bash dream-pass.sh`` which sets
        # PRECIS_DREAM_* before exec'ing precis. Parse those exports.
        wrapper="/opt/asa/bin/dream-pass.sh",
    ),
    AgentSpec(
        key="structural",
        label="Structural reviewer",
        description=(
            "6h-dedup pass. Walks the todo tree, flags drift / sibling "
            "contradictions / depth-fanout warnings. Opus."
        ),
        launchd_label="com.precis.worker-agent",
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
        launchd_label="com.precis.worker-agent",
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
        launchd_label="com.precis.worker-agent",
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
        gating=(("PRECIS_MCP_CONFIG", "MCP config the in-proc claude reads"),),
    ),
)

_BY_KEY: dict[str, AgentSpec] = {a.key: a for a in AGENTS}


def _read_file(path: str | None, *, max_chars: int = 50_000) -> dict[str, Any]:
    """Resolve a path and read its contents (capped).

    Distinguish "file is missing" from "file exists but the web user
    can't read it" — the latter is common when the agent runs as a
    different user (hermes) than the web (deploy) and the prompt
    lives under that user's home.
    """
    if not path:
        return {
            "path": None,
            "exists": False,
            "unreadable": False,
            "text": None,
            "size": 0,
        }
    p = Path(path)
    try:
        raw = p.read_text(errors="replace")
    except FileNotFoundError:
        return {
            "path": str(p),
            "exists": False,
            "unreadable": False,
            "text": None,
            "size": 0,
        }
    except OSError as exc:
        return {
            "path": str(p),
            "exists": True,
            "unreadable": True,
            "text": f"(web user can't read: {exc})",
            "size": 0,
        }
    size = len(raw)
    if size > max_chars:
        raw = raw[:max_chars] + f"\n\n… (truncated; full size {size:,} chars)"
    return {
        "path": str(p),
        "exists": True,
        "unreadable": False,
        "text": raw,
        "size": size,
    }


def _parse_mcp_config(path: str | None) -> dict[str, Any]:
    """Parse the MCP config JSON and project (name, kind, transport).

    The web service runs as ``deploy`` and can't always reach files
    under another user's home (e.g. ``/Users/hermes/.claude/``). When
    the path exists but isn't readable, surface "(unreadable by web —
    file is at the path but the deploy user can't read it)" rather
    than the misleading "not found".
    """
    if not path:
        return {"path": path, "exists": False, "servers": [], "unreadable": False}
    p = Path(path)
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return {"path": path, "exists": False, "servers": [], "unreadable": False}
    except OSError as exc:
        # Permission denied / not-a-file / etc. — the file *exists*,
        # we just can't get to it. Different signal than 'missing'.
        return {
            "path": path,
            "exists": True,
            "servers": [],
            "unreadable": True,
            "error": str(exc),
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "path": path,
            "exists": True,
            "servers": [],
            "unreadable": False,
            "error": str(exc),
        }
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


def _env_snapshot(spec: AgentSpec, plist_env: dict[str, str]) -> list[dict[str, Any]]:
    """One row per env var the agent consults, read from the plist."""
    sensitive = {"PASSWORD", "KEY", "SECRET", "TOKEN", "API_KEY", "URL", "DSN"}
    rows: list[dict[str, Any]] = []
    for key in spec.env_keys:
        raw = plist_env.get(key)
        present = raw is not None
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
    row. With it, the full detail block for that agent — env read
    from the agent's plist, not the web process's env.
    """
    spec = _BY_KEY.get(agent) if agent else None
    detail: dict[str, Any] | None = None
    if spec is not None:
        plist_env = _read_plist_env(spec.launchd_label)
        plist_path = _PLIST_DIR / f"{spec.launchd_label}.plist"
        # Merge the wrapper script's exports on top of the plist env
        # so the page reflects what the worker actually sees at
        # runtime. Wrapper wins on conflicts (it's set after the
        # plist's EnvironmentVariables apply).
        wrapper_env = _parse_wrapper_env(spec.wrapper) if spec.wrapper else {}
        effective_env: dict[str, str] = {**plist_env, **wrapper_env}
        system_prompt = _read_file(effective_env.get(spec.system_prompt_env))
        directive_prompt = _read_file(effective_env.get(spec.directive_prompt_env))
        mcp = _parse_mcp_config(effective_env.get(spec.mcp_config_env))
        detail = {
            "spec": spec,
            "model": effective_env.get(spec.model_env) or spec.model_default,
            "system_prompt": system_prompt,
            "directive_prompt": directive_prompt,
            "mcp": mcp,
            "env_rows": _env_snapshot(spec, effective_env),
            "plist_path": str(plist_path),
            "plist_found": plist_path.exists(),
            "wrapper_path": spec.wrapper or None,
            "wrapper_found": (Path(spec.wrapper).exists() if spec.wrapper else False),
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
