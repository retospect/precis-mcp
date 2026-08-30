"""fix_gripe — clone the repo, run claude on a gripe_<id> branch, push.

The first job_type. Invoked by the `claude_inproc` executor's
runner. Reads the linked gripe's body + comment timeline as the
brief, clones ``$PRECIS_FIX_REPO_DIR`` into
``$PRECIS_FIX_WORK_DIR/clones/gripe_<id>``, then routes through the
:func:`~precis.utils.claude_agent.call_claude_agent` chokepoint (§H
cycle a — the last direct ``subprocess.run`` of claude in the codebase
is gone) with an isolated env (``env_base=_restricted_env(...)``, no
DB creds, ``--bare`` API-key auth), an explicit fix_gripe envelope
(egress ``api-only`` — the LLM call needs the network, everything else
is local), and a bind mount for the clone ONLY. The agent commits
inside the clone; it never has the source repo (origin) mounted and
has no network route to it either, so it CANNOT push. Once
``call_claude_agent`` returns, ``run()`` — trusted, host-side —
performs the push itself: write-back is a commit inside the sandbox,
pushed on the trusted side, never with creds inside the sandbox. On
success the resulting ``gripe_<id>`` branch lands on origin (the
source repo) for human review.

Trust model: a **containerized** run (the default whenever
``PRECIS_AGENT_CONTAINER`` is on and the host can run it) is isolated
by network namespace (``egress:api-only`` — reaches only the Anthropic
API + its own bind-mounted clone, no DB, no source repo) + the
env-base scrub + the trusted-side-only push, so it needs no operator
ack. A run that can't containerize (feature off, probe-failed, or an
infra failure mid-run) is fail-closed: it refuses to fall back to
running full-privilege and unsandboxed unless an operator has
explicitly set ``PRECIS_FIX_GRIPE_UNSANDBOXED_ACK`` (gr179498;
``require_container=not _unsandboxed_ack()`` on the chokepoint call —
see :class:`~precis.utils.claude_agent.ContainerRequiredError`). Both
a pre-push hook in every clone AND a host-side branch-name guard on
the trusted push reject anything not matching ``gripe_*`` (belt and
braces — the guard doesn't rely on the hook alone). See the safety
section in ``precis-fix-gripe-help`` for the full picture.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.utils import claude_oauth as _oauth
from precis.utils.claude_agent import ClaudeAgentError, ContainerRequiredError
from precis.utils.llm.router import Backend, Tier, resolve_backend, resolve_model

log = logging.getLogger(__name__)

# Tag namespace gripes carry to declare which repo they're about.
# Open tag (not closed-prefix) so a gripe could in theory list
# multiple repos for a cross-cutting bug — the runner picks the
# first one and clones it. Keep it lower-case to follow the
# existing precedent for free-form axes like ``due:`` and
# ``project:`` used on todos.
_REPO_TAG_NAMESPACE = "repo"


# ── Declared metadata (read by the dispatcher and the runner) ──────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})

REQUIRES: frozenset[str] = frozenset(
    {"claude_bin", "git", "clones_dir", "claude_config_mount"}
)

DESCRIPTION: str = (
    "Clone the repo, run the fix agent through call_claude_agent "
    "(containerized when available, isolated env otherwise), push the "
    "resulting branch gripe_<id> to origin for human review."
)


# ── Configuration helpers ──────────────────────────────────────────


@dataclass(frozen=True)
class FixGripeConfig:
    #: Fallback repo when a gripe carries no ``repo:`` tag. Preserves
    #: the v0 single-repo workflow ("everything is about precis-mcp").
    #: Can be None on a deployment that requires every gripe to be
    #: explicitly tagged.
    default_repo_dir: Path | None
    work_dir: Path
    claude_bin: str
    claude_model: str
    timeout_seconds: int
    #: Allowlist of ``repo:<name>`` tag values → host paths. Read
    #: from ``PRECIS_FIX_REPOS`` JSON; gripes carrying a ``repo:``
    #: tag must match a key here or the job is rejected.
    repos: dict[str, Path] = field(default_factory=dict)


def load_config_from_env() -> FixGripeConfig:
    """Read the fix_gripe env vars.

    Required: ``PRECIS_FIX_WORK_DIR`` (scratch root for clones).
    Optional: ``PRECIS_FIX_REPO_DIR`` (single-repo fallback) AND/OR
    ``PRECIS_FIX_REPOS`` (multi-repo allowlist as JSON map). At
    least one of the two must be set or the runner has no repo
    to clone from.
    """
    work_dir_raw = os.environ.get("PRECIS_FIX_WORK_DIR")
    if not work_dir_raw:
        raise RuntimeError(
            "fix_gripe: PRECIS_FIX_WORK_DIR is not set (clone scratch root)"
        )
    repo_dir_raw = os.environ.get("PRECIS_FIX_REPO_DIR")
    default_repo = Path(repo_dir_raw).resolve() if repo_dir_raw else None
    repos = _parse_repos_env(os.environ.get("PRECIS_FIX_REPOS"))
    if default_repo is None and not repos:
        raise RuntimeError(
            "fix_gripe: neither PRECIS_FIX_REPO_DIR (single-repo "
            "fallback) nor PRECIS_FIX_REPOS (multi-repo JSON map) "
            "is set — the runner has no repo to clone"
        )
    return FixGripeConfig(
        default_repo_dir=default_repo,
        work_dir=Path(work_dir_raw).resolve(),
        claude_bin=os.environ.get("PRECIS_FIX_CLAUDE_BIN", "claude"),
        # Model selection via the LLM routing seam resolver's FRONTIER tier
        # (``PRECIS_MODEL_OPUS`` / ``claude-opus-4-8`` — the consolidated cloud
        # reasoning tier the planner + reviewers + dream share). The bespoke
        # ``PRECIS_FIX_CLAUDE_MODEL`` override still wins so a deployment can pin
        # fix-gripe to a different model; unset, it falls through to the shared
        # tier default (opus-4.8).
        claude_model=os.environ.get("PRECIS_FIX_CLAUDE_MODEL")
        or resolve_model(Tier.FRONTIER),
        timeout_seconds=int(os.environ.get("PRECIS_FIX_TIMEOUT_SECONDS", "1800")),
        repos=repos,
    )


def _parse_repos_env(raw: str | None) -> dict[str, Path]:
    """Parse ``PRECIS_FIX_REPOS`` JSON into ``{name: Path}``.

    Empty / missing → empty dict. Anything else must parse as a
    JSON object of string → string; a malformed value raises so the
    operator notices.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"fix_gripe: PRECIS_FIX_REPOS is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            "fix_gripe: PRECIS_FIX_REPOS must be a JSON object (name → host path)"
        )
    out: dict[str, Path] = {}
    for name, path in parsed.items():
        if not isinstance(name, str) or not isinstance(path, str):
            raise RuntimeError(
                "fix_gripe: PRECIS_FIX_REPOS entries must be "
                "string → string (name → host path)"
            )
        out[name] = Path(path).resolve()
    return out


def validate_submit(
    # tests pass narrow local stubs (tags_for-only / get_ref-only),
    # diverging from Store
    store: Any,
    *,
    gripe_id: int,
    params: dict[str, Any],
) -> str | None:
    """Pre-submit check: can we actually run this fix on this gripe?

    Returns an error message string if not, ``None`` if OK. The
    JobHandler surfaces non-None as a ``BadInput`` at the
    ``put(kind='job', ...)`` boundary so the caller gets an
    immediate, actionable rejection rather than a queued job that
    silently fails at claim time.

    We check three things:

    1. The fix_gripe env is wired (``PRECIS_FIX_WORK_DIR``,
       repo resolution available).
    2. The linked gripe's ``repo:`` tag resolves to an allowed
       repo (or there's a fallback for un-tagged gripes).
    3. ``ANTHROPIC_API_KEY`` is set — the in-container
       ``claude -p --bare`` invocation can't see the host's
       OAuth / Keychain state, so an API key is the only
       workable auth path.
    """
    try:
        cfg = load_config_from_env()
    except RuntimeError as exc:
        return str(exc)
    try:
        resolve_repo_for_gripe(store, gripe_id, cfg)
    except ValueError as exc:
        return str(exc)
    from precis import secrets as _secrets

    if not _secrets.get_secret("ANTHROPIC_API_KEY"):
        return (
            "fix_gripe: ANTHROPIC_API_KEY is not set in the precis "
            "container env. The in-container `claude -p --bare` "
            "invocation can't reach the host's OAuth / Keychain "
            "state, so an API key is required. Add it to the "
            "precis-dev service in your compose file."
        )
    return None


def resolve_repo_for_gripe(
    store: Any,  # see validate_submit -- tests pass a tags_for-only stub
    gripe_id: int,
    cfg: FixGripeConfig,
) -> Path:
    """Look up the repo path for a gripe at submit / claim time.

    Reads the gripe's tags; if a ``repo:<name>`` tag is present, the
    name must be in ``cfg.repos`` and the resolved path is returned.
    If no ``repo:`` tag, falls back to ``cfg.default_repo_dir`` (the
    single-repo deployment path).

    Raises ``ValueError`` with a message naming the missing piece —
    the dispatcher surfaces this as a ``BadInput`` at submit time so
    the LLM gets a clear recovery hint rather than queueing an
    unrunnable job.
    """
    tags = store.tags_for(gripe_id)
    repo_tags = [
        str(t).split(":", 1)[1]
        for t in tags
        if str(t).startswith(f"{_REPO_TAG_NAMESPACE}:")
    ]
    if repo_tags:
        name = repo_tags[0]
        path = cfg.repos.get(name)
        if path is None:
            known = sorted(cfg.repos.keys()) or "<none>"
            raise ValueError(
                f"gripe:{gripe_id} is tagged repo:{name!r} but that "
                f"repo is not in PRECIS_FIX_REPOS (known: {known})"
            )
        return path
    if cfg.default_repo_dir is not None:
        return cfg.default_repo_dir
    raise ValueError(
        f"gripe:{gripe_id} has no repo: tag and no "
        "PRECIS_FIX_REPO_DIR fallback is configured"
    )


# ── Runner entry point ─────────────────────────────────────────────


@dataclass
class RunOutcome:
    """Result of one fix_gripe attempt — what the executor needs to
    transition status and write the summary."""

    status: str  # "succeeded" | "failed" | "skipped"
    summary_text: str
    gripe_comment_text: str
    branch: str | None
    sha: str | None
    wall_seconds: float


#: Operator ack for running fix_gripe's full-privilege agent WITHOUT the
#: §13 container isolation (gr179498). Fail-closed by default.
_UNSANDBOXED_ACK_ENV = "PRECIS_FIX_GRIPE_UNSANDBOXED_ACK"


def _unsandboxed_ack() -> bool:
    """Whether an operator has explicitly accepted running fix_gripe's
    full-privilege agent WITHOUT the §13 container isolation (gr179498).

    fix_gripe's agent gets full Bash/Edit/Write on VERBATIM gripe text —
    filable by any MCP client with ``put``. §H cycle a routes the spawn
    through :func:`~precis.utils.claude_agent.call_claude_agent` with
    ``require_container=not _unsandboxed_ack()``: a **containerized** run
    (network-isolated, no DB creds, mounts only the clone — never the
    source repo, and never pushes; the trusted host side does that after
    the agent returns) is safe enough to need no ack — but the moment the
    container path is unavailable (feature off, capability probe failed, or
    an infra failure mid-run), the chokepoint refuses to silently fall back
    to running full-privilege and unsandboxed
    (:class:`~precis.utils.claude_agent.ContainerRequiredError`) UNLESS
    ``PRECIS_FIX_GRIPE_UNSANDBOXED_ACK`` is truthy — so enabling
    ``backlog_groom`` alone (which auto-promotes every open gripe into a
    fix_gripe job) can't unleash an unsandboxed run on attacker-shaped text
    without a second, conscious operator opt-in.
    """
    return os.environ.get(_UNSANDBOXED_ACK_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def run(
    *,
    # tests pass narrow local stubs (get_ref-only, some raising past the
    # gate), diverging from Store -- see validate_submit
    store: Any,
    job_id: int,
    gripe_id: int,
    config: FixGripeConfig | None = None,
) -> RunOutcome:
    """Execute one fix_gripe attempt.

    Reads the gripe at run-time (no snapshot), clones the repo
    into a fresh ``gripe_<id>`` dir under ``work_dir/clones``,
    runs claude, pushes on success. Returns the structured
    outcome — the caller (executor runner) is responsible for
    writing chunks / tags / events back to the DB.
    """
    import time

    t0 = time.perf_counter()
    cfg = config or load_config_from_env()

    # GLM/OpenRouter fleet-flip safety gate: fix_gripe's agent runs claude -p (via the
    # call_claude_agent chokepoint), which assumes Claude model semantics —
    # under backend=openai, resolve_model(FRONTIER) returns an OSS slug that
    # `claude -p` can't run (HTTP 400). Skip cleanly rather than spawn a
    # doomed call; the gripe stays open for a re-attempt once the backend
    # reverts to anthropic (recommended: skip-clean, per the proposal's
    # Part 3).
    if resolve_backend() is Backend.OPENAI:
        wall = time.perf_counter() - t0
        log.warning(
            "fix_gripe: llm.backend=openai — skipping gripe:%d fix attempt "
            "(claude -p assumes Claude model semantics, unsupported under "
            "the OSS/OpenRouter backend)",
            gripe_id,
        )
        return RunOutcome(
            status="skipped",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} skipped: "
                "llm.backend=openai — fix_gripe's agent runs `claude -p`, "
                "which assumes Claude model semantics, so it does not run "
                "under the OSS/OpenRouter backend. Re-attempt once the "
                "backend reverts to anthropic."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt skipped: "
                "llm.backend=openai is not supported by fix_gripe "
                "(`claude -p`). Will need a re-attempt once the "
                "backend is anthropic again."
            ),
            branch=None,
            sha=None,
            wall_seconds=wall,
        )

    # Fail-closed safety catch (gr179498): fix_gripe's agent gets full
    # Bash/Edit/Write on VERBATIM (agent-filable) gripe text. §H cycle a
    # routes it through the containerized executor whenever the host can run
    # one — network-isolated, no DB creds — which needs no ack. But when the
    # container path is unavailable (feature off, capability probe failed),
    # refuse to fall back to running full-privilege and unsandboxed unless an
    # operator has explicitly acked the risk — so turning on `backlog_groom`
    # alone can't feed attacker-shaped gripe text into an unsandboxed run.
    # Skip clean here (before spending any clone effort); the actual
    # enforcement is `require_container=not _unsandboxed_ack()` on the
    # chokepoint call below, which also catches the race where a container
    # that looked available here dies at the infra level mid-run.
    from precis.workers.executors import agent_container as _agent_container

    container_ready = (
        _agent_container.container_agent_enabled()
        and _agent_container.container_capability_ok()
    )
    if not container_ready and not _unsandboxed_ack():
        wall = time.perf_counter() - t0
        log.warning(
            "fix_gripe: refusing gripe:%d — no containerized agent path "
            "available and unsandboxed run not acked (gr179498); "
            "fail-closed. Set %s=1 to run unsandboxed anyway, or make the "
            "§13 container available.",
            gripe_id,
            _UNSANDBOXED_ACK_ENV,
        )
        return RunOutcome(
            status="skipped",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} skipped: no "
                "containerized agent path is available on this host, and "
                "running the agent full-privilege and unsandboxed on "
                "verbatim (agent-filable) gripe text is fail-closed "
                f"(gr179498) — set {_UNSANDBOXED_ACK_ENV}=1 to run it "
                "unsandboxed anyway (accepting the risk on a "
                "trusted-operator deployment), or make the §13 container "
                "available on this host."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt skipped: fix_gripe is "
                f"fail-closed ({_UNSANDBOXED_ACK_ENV} unset, no container "
                "available) — it would run an unsandboxed agent on this "
                "gripe's verbatim text (gr179498). No action taken."
            ),
            branch=None,
            sha=None,
            wall_seconds=wall,
        )

    # Resolve the gripe so we can fail fast if it was deleted between
    # claim and run.
    ref = store.get_ref(kind="gripe", id=gripe_id)
    if ref is None:
        raise RuntimeError(f"fix_gripe: gripe id={gripe_id} not found")

    # Pick the repo per the gripe's ``repo:<name>`` tag (multi-repo
    # deployments) or the single-repo fallback.
    repo_dir = resolve_repo_for_gripe(store, gripe_id, cfg)

    blocks = store.chunks.list_chunks_for_ref(gripe_id)
    if not blocks:
        raise RuntimeError(f"fix_gripe: gripe id={gripe_id} has no body chunk")
    prompt = _compose_prompt(ref_title=ref.title, blocks=blocks)

    clone_dir = cfg.work_dir / "clones" / f"gripe_{gripe_id}"
    branch = f"gripe_{gripe_id}"
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    _git_clone_and_branch(repo_dir, clone_dir, branch)
    _install_prepush_hook(clone_dir)

    base_sha = _git_rev_parse(clone_dir, "origin/main")

    try:
        _spawn_claude(cfg, clone_dir, prompt)
    except ValueError as exc:
        # Mount/workdir validation (containerize_claude_argv, agent_container)
        # raises ValueError on a bad Mount/workdir shape — a config bug, not a
        # claude/model failure, but it must not escape run() as a raw
        # exception (the executor runner expects a RunOutcome, not an
        # unhandled traceback).
        wall = time.perf_counter() - t0
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                f"invalid container mount/workdir configuration: {exc}. "
                f"Took {wall:.1f}s."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt failed: invalid "
                f"container mount/workdir configuration ({exc})."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )
    except ContainerRequiredError as exc:
        # The chokepoint's own fail-closed refusal (require_container=True,
        # container unavailable) — a race against the container_ready
        # pre-check above (a probe result that flipped, or an infra failure
        # mid-run), not a claude/model failure. Same skip disposition as the
        # pre-check: the gripe stays open for a re-attempt.
        wall = time.perf_counter() - t0
        log.warning(
            "fix_gripe: gripe:%d — container became unavailable mid-run and "
            "the unsandboxed fallback is not acked: %s",
            gripe_id,
            exc,
        )
        return RunOutcome(
            status="skipped",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} skipped: the "
                f"containerized agent path became unavailable mid-run and "
                f"the unsandboxed fallback is fail-closed (gr179498): {exc}"
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt skipped: the "
                "containerized agent path became unavailable mid-run "
                "(gr179498 fail-closed). No action taken."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )
    except ClaudeAgentError as exc:
        wall = time.perf_counter() - t0
        tail = (exc.stderr or exc.stdout or "").splitlines()[-20:]
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                f"claude failed ({exc}). Took {wall:.1f}s. "
                "stderr tail:\n" + "\n".join(tail)
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt failed: claude failed "
                f"({exc}). stderr tail:\n" + "\n".join(tail[-5:])
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )
    wall = time.perf_counter() - t0

    # Verify the agent actually committed, then push on its behalf. The agent
    # never has origin mounted or reachable — write-back is a commit inside
    # the sandbox, pushed on the TRUSTED (host) side, never with creds inside
    # the sandbox (§H cycle a design decision). call_claude_agent raises on a
    # genuine failure (above) and silently recovers a resumable exhaustion
    # (--max-turns / --max-budget-usd) into a clean return — either way, the
    # real judge of success is whether the agent produced any commit, so
    # there's no exit-code branch to check here (unlike the old bare
    # subprocess.run(check=False) contract).
    branch_sha = _git_rev_parse(clone_dir, branch)
    if branch_sha is None or branch_sha == base_sha:
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                "no commits pushed to origin under branch "
                f"{branch}. Took {wall:.1f}s."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] claude exited cleanly but made "
                f"no commits on branch {branch}. No fix to review."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )

    try:
        _push_branch_trusted(clone_dir, branch)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        wall = time.perf_counter() - t0
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                f"trusted-side push of branch {branch} failed: {exc}. "
                f"Took {wall:.1f}s."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] claude committed a fix on branch "
                f"{branch}, but the trusted-side push failed ({exc})."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )

    pushed_sha = _git_rev_parse(clone_dir, f"origin/{branch}")
    main_sha_after = _git_rev_parse(clone_dir, "origin/main")
    if pushed_sha is None or branch_sha != pushed_sha:
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                "no commits pushed to origin under branch "
                f"{branch}. Took {wall:.1f}s."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] the trusted-side push of branch "
                f"{branch} did not land as expected. No fix to review."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )
    if main_sha_after != base_sha:
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                "origin/main moved during the run (the prepush hook "
                "should have prevented this — bug?)."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] aborted: origin/main was "
                "modified during the run."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )

    diffstat = _git_diff_stat(clone_dir, base_sha, branch_sha)
    return RunOutcome(
        status="succeeded",
        summary_text=(
            f"Fix attempt pushed to origin as branch {branch} @ "
            f"{branch_sha}. {diffstat}. Took {wall:.1f}s."
        ),
        gripe_comment_text=(
            f"[worker:job:{job_id}] branch {branch} @ {branch_sha} "
            "pushed to origin. Review with: "
            f"`git fetch && git checkout {branch} && git diff main..{branch}`."
        ),
        branch=branch,
        sha=branch_sha,
        wall_seconds=wall,
    )


# ── Prompt composition ────────────────────────────────────────────


def _compose_prompt(*, ref_title: str, blocks: list[Any]) -> str:
    """Build the prompt fed to ``claude -p`` from the gripe timeline."""
    lines: list[str] = []
    lines.append(
        "You are an autonomous engineer assigned a bug fix in the "
        "precis-mcp repository."
    )
    lines.append("")
    lines.append("BUG REPORT (gripe body + comments, in timeline order):")
    lines.append("")
    for i, block in enumerate(blocks):
        if i == 0:
            lines.append(f"BODY: {block.text}")
        else:
            lines.append(f"COMMENT {i}: {block.text}")
    lines.append("")
    lines.append("CONSTRAINTS:")
    lines.append("- You are on a fresh branch named gripe_<id>.")
    lines.append("- Make the smallest commits that fix the reported bug.")
    lines.append("- Run any relevant tests before committing.")
    lines.append(
        "- Commit your fix locally. Do NOT push — you have no network route "
        "to origin and no push credentials; a trusted process pushes your "
        "branch (only gripe_* branches are eligible) after you finish."
    )
    lines.append("- Do NOT touch main. Do NOT switch branches.")
    return "\n".join(lines)


# ── Subprocess + git plumbing ─────────────────────────────────────


def _spawn_claude(cfg: FixGripeConfig, clone_dir: Path, prompt: str) -> Any:
    """Run the fix agent through the :func:`call_claude_agent` chokepoint
    (§H cycle a — no more bare ``subprocess.run`` of claude).

    Uses ``bare=True`` so auth is strictly ``ANTHROPIC_API_KEY`` (no OAuth,
    no keychain reads, no plugin sync, no CLAUDE.md auto-discovery) — the
    claude_inproc executor runs where Claude Code's OAuth state from an
    interactive host is unreachable, so an API key is the only workable auth
    path. ``env_base=_restricted_env(...)`` replaces the ambient worker env
    entirely (no PG*/PRECIS_DATABASE_URL — the DB-isolation boundary), and
    ``envelope=Envelope(egress="api-only", ...)`` gives the container
    tier-3 network access to the Anthropic API only (the local git remote
    needs no network at all — ``git clone --local``). ``mounts`` bind the
    clone ONLY (rw) — the source repo (origin) is never mounted, so the
    agent has no filesystem path to it and no network route either
    (egress is ``api-only``, Anthropic only). It can commit inside the
    clone; it cannot push. Write-back is a commit, pushed on the TRUSTED
    side: once this call returns, ``run()`` (host-side, has the real repo
    path + no sandbox) performs the actual ``git push`` — never inside the
    sandbox, never with push creds handed to the agent.

    ``require_container=not _unsandboxed_ack()`` is the gr179498 fail-closed
    gate, now enforced by the chokepoint itself rather than a local check
    here: a containerized run needs no ack; the moment the container path is
    unavailable — at call time OR mid-run (an infra failure) —
    :func:`call_claude_agent` raises
    :class:`~precis.utils.claude_agent.ContainerRequiredError` instead of
    silently falling back to running full-privilege and unsandboxed, unless
    the ack is set.

    The agent still has full tool access (Bash/Read/Write/Edit) and skill
    resolution via ``/skill-name`` inside its box — ``--bare`` only strips
    auto-discovery, and the fix_gripe envelope denies neither FS tools nor
    fetch tools (``write="full"``, ``egress="api-only"``); the *real*
    boundaries are the container's network namespace, the absent DB creds,
    and the absent origin mount — not a cooperative tool deny.
    """
    from precis.utils.claude_agent import call_claude_agent
    from precis.workers.envelope import Envelope
    from precis.workers.executors.agent_container import Mount

    env_base = _restricted_env(clone_dir)
    if "ANTHROPIC_API_KEY" not in env_base:
        raise RuntimeError(
            "fix_gripe: ANTHROPIC_API_KEY is required to run claude -p "
            "in the precis container (OAuth / keychain auth aren't "
            "reachable from inside the container). Set it in the "
            "precis-dev compose service."
        )
    # Explicit, minimal envelope for this call rather than inheriting the
    # ambient one — fix_gripe runs under claude_inproc's envelope_scope, but
    # a job's meta.envelope (if any) is about DB write scope, not about
    # "FS tools yes, DB no" (fix_gripe's actual shape: it never touches the
    # DB at all — no --mcp-config, no DSN in env_base — so the write axis is
    # moot; egress must stay reachable for the LLM call itself, hence
    # api-only rather than none).
    envelope = Envelope(egress="api-only", write="full", return_="full")
    mounts = (
        Mount(host_path=str(clone_dir), container_path=str(clone_dir), mode="rw"),
    )
    return call_claude_agent(
        prompt,
        model=cfg.claude_model,
        bare=True,
        env_base=env_base,
        cwd=clone_dir,
        mounts=mounts,
        workdir=str(clone_dir),
        envelope=envelope,
        require_container=not _unsandboxed_ack(),
        timeout_s=float(cfg.timeout_seconds),
        # No MCP server for fix_gripe — it never reaches the precis DB.
        mcp_config=None,
    )


def _restricted_env(cwd: Path, *, prefer_oauth: bool = False) -> dict[str, str]:
    """Build the subprocess env: minimal vars, no DB creds.

    Strips every ``PG*`` and ``PRECIS_DATABASE_URL`` so claude can't
    reach the postgres backing the precis runtime even if it tries.
    Keeps ``HOME`` (so claude can read ``~/.claude``), ``PATH``,
    ``TERM``, and a small allowlist of safe vars.

    Both auth credentials are carried through and back-filled from the
    secrets vault: ``ANTHROPIC_API_KEY`` (what ``claude -p --bare`` reads)
    and ``CLAUDE_CODE_OAUTH_TOKEN`` (the subscription token the container's
    oauth mode passes by key). ``prefer_oauth=True`` additionally drops the
    API key whenever a token is present, so the caller can key ``bare`` off
    "is there a token in here" and the CLI cannot fall back to the billed
    path. It defaults to False because ``--bare`` auth is *strictly*
    ``ANTHROPIC_API_KEY`` (``claude --help``): scrubbing the key out from
    under a ``bare=True`` caller would leave it with no credential at all.
    """
    src = os.environ
    allowed_prefixes = ("ANTHROPIC_",)
    allowed_keys = {
        "HOME",
        "PATH",
        "TERM",
        "LANG",
        "LC_ALL",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        _oauth.ENV_VAR,
    }
    out: dict[str, str] = {}
    for k, v in src.items():
        if k.startswith("PG") or k.startswith("PRECIS_DATABASE"):
            continue
        if k.startswith("PRECIS_"):
            # Strip every other PRECIS_* var — claude doesn't need
            # to know about precis internals (and a stray DSN that
            # leaks via PRECIS_FOO_DATABASE would otherwise survive
            # the prefix filter above).
            continue
        if k in allowed_keys or any(k.startswith(p) for p in allowed_prefixes):
            out[k] = v
    out["PWD"] = str(cwd)
    # Inject the auth credentials from the secrets vault when the worker env
    # doesn't already carry them, so the in-container ``claude -p`` still
    # authenticates once the ambient env has been stripped. Best-effort: a
    # vault miss just leaves the var absent and the caller's own guard fires.
    # Only an ``prefer_oauth`` caller gets the token back-filled — a
    # ``bare=True`` run can't read it (auth is strictly the API key), so
    # handing it one would widen the credential's blast radius for nothing.
    wanted = (
        (_oauth.ENV_VAR, _oauth.API_KEY_VAR) if prefer_oauth else (_oauth.API_KEY_VAR,)
    )
    for var in wanted:
        if var in out:
            continue
        try:
            from precis import secrets as _secrets

            tok = _secrets.get_secret(var)
            if tok:
                out[var] = tok
        except Exception:
            pass
    if prefer_oauth:
        _oauth.prefer_oauth_over_api_key(out)
    return out


def _git_clone_and_branch(repo_dir: Path, dest: Path, branch: str) -> None:
    """Clone ``repo_dir`` into ``dest`` and check out ``branch``.

    Uses ``--local --no-hardlinks`` so a single file in the source
    repo working tree being modified mid-clone can't corrupt the
    clone's object store.
    """
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(repo_dir), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=str(dest),
        check=True,
        capture_output=True,
        text=True,
    )


def _install_prepush_hook(clone_dir: Path) -> None:
    """Drop a pre-push hook that rejects pushes outside ``gripe_*``."""
    hook_dir = clone_dir / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "pre-push"
    hook_path.write_text(
        "#!/usr/bin/env bash\n"
        "# precis fix_gripe pre-push guard: only branches matching\n"
        "# gripe_* may be pushed. Protects origin/main from an agent\n"
        "# pushing the wrong thing.\n"
        "while read local_ref local_sha remote_ref remote_sha; do\n"
        '  case "$remote_ref" in\n'
        "    refs/heads/gripe_*) ;;\n"
        "    *)\n"
        '      echo "[fix_gripe] refusing push to $remote_ref '
        '(only gripe_* branches may be pushed)" >&2\n'
        "      exit 1\n"
        "      ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)


#: Branch names the trusted-side push will accept — mirrors the pre-push
#: hook's ``gripe_*`` glob but as an exact ``gripe_<digits>`` match (the
#: shape ``run()`` always constructs; anything else is refused).
_GRIPE_BRANCH_PATTERN = re.compile(r"^gripe_\d+$")


def _push_branch_trusted(clone_dir: Path, branch: str) -> None:
    """Push ``branch`` to origin from the TRUSTED (host) side.

    §H cycle a design decision: write-back is a commit, pushed on the
    trusted side — never inside the sandbox, never with push creds handed
    to the agent. The agent has no origin mount and no network route to it
    (see :func:`_spawn_claude`), so it physically cannot push; this is the
    only path a fix branch reaches origin.

    Guards the branch name against ``gripe_<id>`` HOST-side, before
    shelling out — belt and braces alongside the clone's pre-push hook
    (:func:`_install_prepush_hook`): don't rely on the hook alone, since
    this function is the one actually authorized to push, and a defense
    that only lived inside the clone's ``.git`` would be one config bug
    away from silently trusting whatever ``branch`` this function was
    called with.
    """
    if not _GRIPE_BRANCH_PATTERN.match(branch):
        raise RuntimeError(
            f"fix_gripe: refusing to push branch {branch!r} — must match "
            "gripe_<id> (never main or anything else)"
        )
    subprocess.run(
        ["git", "push", "origin", f"{branch}:refs/heads/{branch}"],
        cwd=str(clone_dir),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_rev_parse(clone_dir: Path, refname: str) -> str | None:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", refname],
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def _git_diff_stat(clone_dir: Path, base: str | None, head: str) -> str:
    if base is None:
        return "diff stats unavailable (no base)"
    res = subprocess.run(
        ["git", "diff", "--shortstat", f"{base}..{head}"],
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    text = (res.stdout or "").strip()
    return text or "no detectable diff"


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "FixGripeConfig",
    "RunOutcome",
    "load_config_from_env",
    "resolve_repo_for_gripe",
    "run",
    "validate_submit",
]
