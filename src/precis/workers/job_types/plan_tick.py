"""``plan_tick`` job_type — one LLM tick of the planner coroutine.

The dispatcher mints a ``plan_tick`` job under every ``LLM:*``-tagged
todo that has no live job and no live open children. The job runs
opus (or sonnet / haiku per the tag) with the planner prompts from
:mod:`precis.workers.planner_prompt` and exits.

What the planner does during the tick is its own call (mint
children, yield to user, halt, or finish). The runner doesn't
interpret the output — it just shells out, captures stdout as a
``job_summary`` chunk under the job ref, and lets the dispatcher's
next sweep notice whatever state the planner set.

Closed vocab: ``meta.params`` carries ``model`` (one of
``opus|sonnet|haiku``) plus an optional ``timeout_s``. The model is
synthesized from the parent's ``LLM:<value>`` tag at dispatch time;
callers normally don't write ``params`` directly.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.utils.claude_oauth import ensure_oauth_token
from precis.utils.llm.router import Tier, resolve_model

log = logging.getLogger(__name__)


#: Per-tick cost ceiling for the planner subprocess (``--max-budget-usd``).
#: Set higher than the ``claude_agent`` default ($2) because a plan_tick is
#: a multi-turn opus agentic run that legitimately spends more than a one-shot
#: agent call, so a lower cap would truncate normal ticks. It is a
#: runaway-spend backstop, not a normal-tick limit. A tick cut off by the
#: cap is treated as a *resumable exhaustion* — the same handling as the
#: ``--max-turns`` ceiling / wall-clock timeout (see
#: ``executors/claude_inproc._resume_reason``) — so a legitimately long task
#: continues on a fresh tick rather than hard-failing. Override via
#: ``PRECIS_PLAN_TICK_MAX_USD``.
_DEFAULT_MAX_USD: float = 5.00


#: Per-tick ``--max-turns`` ceiling for the planner subprocess. A research +
#: write section tick (read chunks, search the corpus, mint citations, write
#: paragraphs) legitimately needs many tool-call turns; 30 was too tight for
#: citation-dense sections (the agent front-loaded research and hit the wall
#: before writing — bubbling as a resume-streak failure), so the default is
#: 60. A tick that still hits the ceiling is a *resumable exhaustion*, not a
#: hard failure (see ``executors/claude_inproc._resume_reason``). Override via
#: ``PRECIS_PLAN_TICK_MAX_TURNS``.
_DEFAULT_MAX_TURNS: int = 60


DESCRIPTION: str = (
    "one LLM planner tick on an LLM:*-tagged todo — reads body + "
    "child summaries, mints children / yields / finishes"
)


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "enum": ["opus", "sonnet", "haiku"],
            "description": "Which Claude tier to run. Synthesized from the "
            "parent's LLM:<value> tag at dispatch time.",
        },
        "timeout_s": {
            "type": "integer",
            "minimum": 30,
            "maximum": 3600,
            "description": "Wall-clock cap on the tick. Default 1800s (30 min).",
        },
    },
    "required": ["model"],
    "additionalProperties": False,
}


COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})


#: ``plan_tick`` needs ``claude_bin`` (the CLI) and an
#: ``mcp_config`` (so the planner can call back via MCP). Everything
#: else is read-only.
REQUIRES: frozenset[str] = frozenset({"claude_bin", "mcp_config"})


@dataclass(frozen=True)
class PlanTickOutcome:
    """Result of one planner tick.

    ``stdout`` is captured for the ``job_summary`` chunk. ``exit_code``
    decides the job's STATUS (0 → succeeded, non-zero → failed).
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time check. Today: only validates the model value.

    ``gripe_id`` is ignored — plan_tick parents are todos, not gripes.
    Kept in the signature for the registry's uniform interface.
    """
    del gripe_id
    model = params.get("model")
    if model not in {"opus", "sonnet", "haiku"}:
        return (
            f"plan_tick: params.model must be one of "
            f"[opus, sonnet, haiku], got {model!r}"
        )
    return None


def run(
    *,
    store: Any,
    job_ref_id: int,
    parent_ref_id: int,
    params: dict[str, Any],
    log_event: Any = None,
    **_kw: Any,
) -> PlanTickOutcome:
    """Run one planner tick under ``parent_ref_id`` and return the outcome.

    The runner builds the prompts via
    :func:`precis.workers.planner_prompt.build_planner_prompts`, then
    shells out to ``claude -p`` with the appropriate ``--model`` /
    ``--append-system-prompt`` flags. The MCP config is forwarded so
    the planner can call back via the precis tools (put / tag / link /
    search / get).
    """
    from precis.workers.planner_prompt import build_planner_prompts

    model = params["model"]
    timeout_s = int(params.get("timeout_s", 1800))
    started = time.monotonic()

    # Patent tick: refresh the freedom-to-operate claims digest onto this
    # tick's ``meta.working_set`` before the prompt is built, so the planner
    # injects the prior-art claims the tick must design around. Best-effort.
    _refresh_patent_claims_digest(store, parent_ref_id)

    prompts = build_planner_prompts(store, ref_id=parent_ref_id, model=model)

    # Resolve the claude binary + MCP config from env. These are part
    # of the executor's REQUIRES set; the runner can assume they
    # exist or fail loudly.
    claude_bin = os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        log.warning(
            "plan_tick: PRECIS_MCP_CONFIG unset; planner won't be able to "
            "call back via MCP — children/yield/done won't land"
        )

    # Workspace context propagation: read meta.workspace from the
    # parent todo and pass its path through PRECIS_WORKSPACE in the
    # spawned claude -p's env. The MCP server inherits the env from
    # claude, file-kind handlers read PRECIS_WORKSPACE to route by
    # the layout convention. Without this, the LLM has to compute
    # paths manually and can get them wrong.
    workspace = _load_parent_workspace(store, parent_ref_id)
    subprocess_env = dict(os.environ)
    # Bootstrap the long-lived OAuth token from ~/.claude_oauth_token when the
    # env doesn't already carry it — launchd daemons don't source the shell
    # hook that would, so without this the spawned ``claude -p`` authenticates
    # off stale keychain creds and 401s (2026-07-12 plan_tick incident). Same
    # bootstrap ``claude_agent`` already does; shared helper keeps them in sync.
    ensure_oauth_token(subprocess_env)
    if workspace is not None:
        subprocess_env["PRECIS_WORKSPACE"] = workspace.path
    # Draft-bound tick: gate the colliding prose-file kind off the tool
    # surface so the agent cannot write the section to a freestanding
    # `kind='tex'`/`kind='markdown'` file the draft never renders (the
    # canonical store is the draft's chunks; the file is export-only).
    # The kind-gate reads PRECIS_KINDS_DISABLED at MCP-server boot — the
    # server inherits this env — and the inline `kind:reason` hint
    # surfaces verbatim in the Unsupported error if the agent still
    # reaches for it, pointing it back at put(kind='draft', …). Covers
    # both section-writing children and "around here…" anchored ticks.
    _disable_prose_file_kind(store, parent_ref_id, subprocess_env)
    # PRECIS_CURRENT_TODO: the runtime parent todo for this tick. The
    # MCP server reads this via utils/workspace.current_todo_from_env;
    # TodoHandler.put auto-defaults parent_id= to it when the caller
    # omits parent_id. So the LLM can mint subtasks via
    # put(kind='todo', tags=['LLM:sonnet'], text='...') without
    # remembering its own ref_id every call. Same back-door pattern
    # as PRECIS_WORKSPACE.
    subprocess_env["PRECIS_CURRENT_TODO"] = str(parent_ref_id)
    # PRECIS_CURRENT_MODEL: tells the LLM what tier it's running on.
    # Lets it make degradation/escalation decisions — too hard for
    # haiku? mint a child with LLM:opus. Sonnet on a topic needing
    # external state? call get(kind='perplexity-research', q='...') for a
    # perplexity research dive. Opus on something straightforward?
    # do it inline.
    subprocess_env["PRECIS_CURRENT_MODEL"] = model
    # PRECIS_CURRENT_AGENTLOG: open a run-attribution record (kind=
    # 'agentlog') carrying the full assembled prompt, and thread its id
    # to the subprocess so the MCP server inside it attributes every
    # draft chunk this tick writes/moves back to this run (a `touched`
    # link). Same env back-door as PRECIS_CURRENT_TODO. Best-effort: a
    # failure here must never abort the tick.
    from precis import agentlog

    agentlog_id: int | None = None
    try:
        agentlog_id = agentlog.open_log(
            store,
            source="plan_tick",
            title=f"plan_tick #{parent_ref_id} ({model})",
            model=model,
            prompt=f"{prompts.system}\n\n──── USER ────\n\n{prompts.user}",
            parent_ref_id=parent_ref_id,
            job_ref_id=job_ref_id,
        )
        subprocess_env[agentlog.ENV_VAR] = str(agentlog_id)
    except Exception:
        log.warning("plan_tick: failed to open agentlog", exc_info=True)

    def _finalize(status: str) -> None:
        if agentlog_id is None:
            return
        try:
            agentlog.finalize_log(store, log_id=agentlog_id, status=status)
        except Exception:
            log.warning("plan_tick: failed to finalize agentlog", exc_info=True)

    cmd: list[str] = [
        claude_bin,
        "-p",
        prompts.user,
        "--model",
        _model_alias(model),
        "--append-system-prompt",
        prompts.system,
        "--max-turns",
        str(_max_turns()),
        # Runaway-spend backstop. A tick that hits this cap is a *resumable*
        # exhaustion (like --max-turns / timeout), not a hard failure — the
        # executor's _resume_reason detects the budget result event and a
        # fresh tick continues. Set high so it never truncates a normal tick.
        "--max-budget-usd",
        str(_max_budget_usd()),
        "--permission-mode",
        "acceptEdits",
        # Emit the full message stream (every turn + tool call/result) so
        # the executor can store a debuggable transcript. The final text is
        # lifted from the trailing ``result`` event; ``--verbose`` is
        # required alongside ``stream-json`` in ``-p`` mode.
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if mcp_config:
        # Absolute so the neutral cwd below can't strand a relative path.
        cmd.extend(["--mcp-config", os.path.abspath(mcp_config), "--strict-mcp-config"])

    # ADR 0051 §12 — the turn-taker owns the entire system prompt. Run from a
    # neutral cwd so `claude -p` discovers no project CLAUDE.md, and surface
    # any ambient CLAUDE.md (user file or an unexpected one up the cwd tree)
    # that would still be prepended outside the assembler and bust the cache
    # prefix. Warn loudly + signal rather than hard-refuse, so a stray file
    # degrades observably instead of silently stalling the planner.
    cwd = _neutral_cwd()
    ambient = _ambient_claude_md_paths(cwd)
    if ambient:
        log.warning(
            "plan_tick: ambient CLAUDE.md would contaminate the persona floor "
            "outside the assembler (ADR 0051 §12) — remove it on agent hosts: %s",
            ambient,
        )

    try:
        if log_event:
            log_event(
                "plan_tick.spawn",
                {
                    "job_ref_id": job_ref_id,
                    "parent_ref_id": parent_ref_id,
                    "model": model,
                    "system_chars": len(prompts.system),
                    "user_chars": len(prompts.user),
                    "cwd": cwd,
                    "ambient_claude_md": ambient,
                },
            )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=subprocess_env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        log.warning(
            "plan_tick: parent #%d timed out after %ds",
            parent_ref_id,
            timeout_s,
        )
        _finalize("timeout")
        return PlanTickOutcome(
            exit_code=124,
            stdout=(exc.stdout or "").decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr=f"plan_tick: timeout after {timeout_s}s",
            duration_s=duration,
        )
    duration = time.monotonic() - started
    _finalize("ok" if proc.returncode == 0 else f"exit:{proc.returncode}")
    return PlanTickOutcome(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_s=duration,
    )


def _refresh_patent_claims_digest(store: Any, parent_ref_id: int) -> None:
    """For a **patent** tick with a bound draft, stamp the freedom-to-operate
    claims digest onto the tick's ``meta.working_set`` so the planner injects
    the prior-art claims (``docs/design/patent-authoring-loop.md``). Discovers
    the draft's linked prior-art patents and writes one eye per claim chunk.
    Best-effort — a digest failure must never sink a tick."""
    try:
        ws = _load_parent_workspace(store, parent_ref_id)
        if ws is None or ws.doc_type != "patent":
            return
        from precis.workers.planner_prompt import bound_draft

        resolved = bound_draft(store, parent_ref_id)
        if not resolved:
            return
        draft = store.get_ref(kind="draft", id=resolved[0])
        if draft is None:
            return
        from precis.workers.patent_digest import refresh_claims_digest

        refresh_claims_digest(store, parent_ref_id, draft.id)
    except Exception:  # pragma: no cover — enhancement, never fatal
        log.warning("plan_tick: patent claims-digest refresh failed", exc_info=True)


def _load_parent_workspace(store: Any, parent_ref_id: int):
    """Read meta.workspace from the parent todo, parse it.

    Returns a :class:`Workspace` or None when the todo carries no
    workspace block. Validation is lenient: malformed dicts log a
    warning and return None rather than raising. Cascade resilience.
    """
    from precis.utils.workspace import Workspace

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s",
            (parent_ref_id,),
        ).fetchone()
    if row is None:
        return None
    return Workspace.from_meta(row[0])


def _disable_prose_file_kind(
    store: Any, parent_ref_id: int, subprocess_env: dict[str, str]
) -> None:
    """When a draft is bound to this tick, add the colliding prose-file
    kind to ``PRECIS_KINDS_DISABLED`` in ``subprocess_env`` with an
    inline hint, so the spawned MCP server gates it off the tool surface.

    The colliding kind is the one whose files duplicate the draft body:
    ``tex`` for a tex-format draft, ``markdown`` for a md-format one.
    Figures / data (``pic`` / ``data``) are left enabled — they are
    draft-adjacent artefacts the draft references, not a second copy of
    its prose. Merges with any operator-set value (the gate reads a
    comma list); the hint carries no comma. Best-effort: any failure
    leaves the env untouched — the ``## Draft`` prompt block still steers
    the agent, the gate is just the belt to its suspenders.
    """
    from precis.workers.planner_prompt import bound_draft

    try:
        resolved = bound_draft(store, parent_ref_id)
    except Exception:
        log.warning("plan_tick: bound_draft lookup failed", exc_info=True)
        return
    if resolved is None:
        return
    ident, _title, fmt = resolved
    kind = "markdown" if fmt.lower() in ("md", "markdown") else "tex"
    hint = (
        f"this project's deliverable is draft '{ident}' — write prose with "
        f"put(kind='draft' ...) or edit(id='dc<id>') as the '## Draft' block "
        f"in your prompt describes; the {kind} file kind is export-only output "
        f"here"
    )
    existing = subprocess_env.get("PRECIS_KINDS_DISABLED", "").strip()
    entry = f"{kind}:{hint}"
    subprocess_env["PRECIS_KINDS_DISABLED"] = (
        f"{existing},{entry}" if existing else entry
    )


#: The ``LLM:<value>`` short-name → capability-tier map (ADR 0046). Each
#: tier resolves (via :func:`~precis.utils.llm.router.resolve_model`) to the
#: env-var + default in the router table, so a given ``LLM:opus`` tag binds
#: to the consolidated cloud reasoning generation (override via the env var):
#:   opus   → CLOUD_SUPER (``PRECIS_MODEL_OPUS``,  ``claude-opus-4-8``)
#:   sonnet → CLOUD_MID   (``PRECIS_MODEL_SONNET``, ``claude-sonnet-4-6``)
#:   haiku  → CLOUD_SMALL (``PRECIS_MODEL_HAIKU``,  ``claude-haiku-4-5-20251001``)
_TIER_BY_ALIAS: dict[str, Tier] = {
    "opus": Tier.CLOUD_SUPER,
    "sonnet": Tier.CLOUD_MID,
    "haiku": Tier.CLOUD_SMALL,
}


def _model_alias(model: str) -> str:
    """Translate the short LLM:<value> name to the real Claude model ID.

    Routes through the ADR 0046 resolver so model selection lives in one
    table. The tier map binds each short name to a router tier, so a
    ``LLM:opus`` tag resolves to the shared cloud-super default (opus-4.8),
    overridable via ``PRECIS_MODEL_OPUS=…`` etc.. An unrecognized
    name passes through unchanged — mirrors the old ``.get(model, model)``
    fallback (``validate_submit`` already constrains it to opus/sonnet/haiku).
    """
    tier = _TIER_BY_ALIAS.get(model)
    if tier is None:
        return model
    return resolve_model(tier)


def _max_turns() -> int:
    """The planner subprocess's ``--max-turns`` ceiling.

    Reads ``PRECIS_PLAN_TICK_MAX_TURNS`` (an int) or falls back to
    :data:`_DEFAULT_MAX_TURNS`. A malformed value logs and falls back rather
    than crashing the tick.
    """
    raw = os.environ.get("PRECIS_PLAN_TICK_MAX_TURNS")
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "plan_tick: PRECIS_PLAN_TICK_MAX_TURNS=%r is not an int; using %d",
            raw,
            _DEFAULT_MAX_TURNS,
        )
        return _DEFAULT_MAX_TURNS


#: Process-wide neutral cwd for the planner subprocess (ADR 0051 §12). Lazily
#: created, reused across ticks (an empty dir needs no per-tick churn).
_NEUTRAL_CWD: str | None = None


def _neutral_cwd() -> str:
    """A stable, empty working directory the planner subprocess runs in so
    ``claude -p``'s *project* ``CLAUDE.md`` auto-discovery finds nothing
    (ADR 0051 §12).

    The turn-taker must own the entire system prompt: a tick's rendered
    system prompt has to equal the assembler's bytes. Running from the
    daemon's cwd lets ``claude`` discover a project ``CLAUDE.md`` up the tree
    and prepend it *outside* the assembler — a competing uncontrolled persona
    that also silently busts the "stable" cache prefix (§2/§3). A fresh temp
    dir (ancestors ``/tmp`` → ``/``, none carrying a ``CLAUDE.md``) removes
    that discovery surface without ``--bare`` (which would force API-key auth
    and break OAuth). The *user* file ``~/.claude/CLAUDE.md`` is discovered
    regardless of cwd — :func:`_ambient_claude_md_paths` guards that."""
    global _NEUTRAL_CWD
    if _NEUTRAL_CWD is not None and os.path.isdir(_NEUTRAL_CWD):
        return _NEUTRAL_CWD
    _NEUTRAL_CWD = tempfile.mkdtemp(prefix="precis-plan-tick-cwd-")
    return _NEUTRAL_CWD


def _ambient_claude_md_paths(cwd: str) -> list[str]:
    """Every ``CLAUDE.md`` ``claude -p`` could auto-discover for a run in
    ``cwd`` and prepend outside the assembler (ADR 0051 §12): the user file
    ``~/.claude/CLAUDE.md`` plus any project ``CLAUDE.md`` from ``cwd`` up to
    the filesystem root. An empty list means a clean persona environment —
    the rendered system prompt is exactly the assembler's bytes."""
    found: list[str] = []
    try:
        home_md = Path.home() / ".claude" / "CLAUDE.md"
        if home_md.is_file():
            found.append(str(home_md))
        base = Path(cwd).resolve()
        for d in (base, *base.parents):
            md = d / "CLAUDE.md"
            if md.is_file():
                found.append(str(md))
    except OSError:  # a stat failure must not abort the tick
        log.warning("plan_tick: CLAUDE.md ambient-scan failed", exc_info=True)
    return found


def _max_budget_usd() -> float:
    """The planner subprocess's ``--max-budget-usd`` cap.

    Reads ``PRECIS_PLAN_TICK_MAX_USD`` (a float) or falls back to
    :data:`_DEFAULT_MAX_USD`. A malformed value logs and falls back rather
    than crashing the tick.
    """
    raw = os.environ.get("PRECIS_PLAN_TICK_MAX_USD")
    if not raw:
        return _DEFAULT_MAX_USD
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "plan_tick: PRECIS_PLAN_TICK_MAX_USD=%r is not a float; using $%.2f",
            raw,
            _DEFAULT_MAX_USD,
        )
        return _DEFAULT_MAX_USD
