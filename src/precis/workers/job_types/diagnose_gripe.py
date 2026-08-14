"""``diagnose_gripe`` — read-only root-cause diagnosis appended to a gripe.

Every runtime failure surfaces as a gripe, but a raw gripe says "something
in the retrieval layer broke," not "the embedder lib added in the latest
manifest bump isn't installed." The expensive rails — ``fix_gripe``
(FRONTIER, clone+build+push) and the human gripe-sweep — both start from
that raw state. This pass upgrades the gripe *in place* with a pinned root
cause + evidence anchors + a proposed-fix sketch, so whichever rail picks
it up next starts from a one-line cause instead of a blank slate. It is
the per-gripe sibling of the self-healing spine's Layer-3 doctor
(``docs/backlog/self-healing-spine.md`` §doctor), at its report rung. Arming
the whole dark-factory gripe loop this feeds is ``docs/backlog/dark-factory-arming.md``.

**Read-only by construction, unlike ``fix_gripe``.** The repo clone is a
throwaway scratch copy (reusing ``fix_gripe.load_config_from_env`` /
``resolve_repo_for_gripe`` for repo resolution, but with none of
``fix_gripe``'s branch/prepush-hook/push machinery — see
:func:`_git_clone_readonly`), bind-mounted **read-only** into the
container (``Mount(mode="ro")``, tighter than ``fix_gripe``'s ``rw``), and
deleted the moment the agent call returns. The model gets full
Bash/Read/Grep tool access to *browse* the clone for evidence, but can't
write to it, commit, branch, or push — the only write-back is one
``gripe_comment`` chunk on the gripe (never a fix, never a status flip).

The agent here still runs on VERBATIM, agent-filable gripe text with a
full-tool box (same threat shape as ``fix_gripe``'s gr179498 concern), so
this module reuses ``fix_gripe``'s fail-closed container-required gate
(``PRECIS_FIX_GRIPE_UNSANDBOXED_ACK``) and its GLM/OpenRouter backend-flip
skip verbatim rather than re-deriving either — same risk register, same
mitigation.

**Auto-promotion bridge, shipped dark.** When ``PRECIS_DIAGNOSE_AUTOPROMOTE=1``
and the model's self-reported confidence is >= 0.8, the gripe gets tagged
``OPEN:auto-fix`` — the key the (separate, dark) ``fixer-gripe-intake``
lane reads. Unset (the default), diagnosis never tags anything.

Registered as a dispatch-protocol plugin (``spec.dispatch``) under
``claude_inproc`` — see ``precis.workers.job_types`` for the protocol.
The minting half (select undiagnosed open gripes, cap the fan-out, dedup
by idem_key) is :mod:`precis.workers.diagnose_scan`, dark behind
``PRECIS_DIAGNOSE_SCAN_ENABLED``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from precis.utils.claude_agent import (
    AgentResult,
    ClaudeAgentError,
    ContainerRequiredError,
)
from precis.utils.llm.router import Backend, Tier, resolve_backend, resolve_model
from precis.workers.executors._common import append_chunk as _append_chunk
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.fix_gripe import (
    _UNSANDBOXED_ACK_ENV,
    _restricted_env,
    _unsandboxed_ack,
    load_config_from_env,
    resolve_repo_for_gripe,
)

log = logging.getLogger(__name__)

# ── Declared metadata (read by the dispatcher and the runner) ──────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"gripe_id": {"type": "integer"}},
    "required": ["gripe_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})

#: Same host capabilities fix_gripe needs — a clone dir, git, the claude
#: binary, and the mounted claude config (§13 container path).
REQUIRES: frozenset[str] = frozenset(
    {"claude_bin", "git", "clones_dir", "claude_config_mount"}
)

DESCRIPTION: str = (
    "Clone the repo read-only, ask claude for a structured root-cause "
    "diagnosis of a gripe, and append it as a gripe_comment. Never "
    "fixes, branches, pushes, or flips the gripe's STATUS."
)

_GRIPE_COMMENT_KIND = "gripe_comment"
_DIAGNOSIS_PREFIX = "DIAGNOSIS (auto, job {job_id}):"
_CONFIDENCE_RE = re.compile(r"Confidence:\s*([01](?:\.\d+)?)", re.IGNORECASE)

#: Iff PRECIS_DIAGNOSE_AUTOPROMOTE=1 AND confidence >= this threshold, the
#: gripe gets tagged OPEN:auto-fix (fixer-gripe-intake's read key).
_AUTOPROMOTE_ENV = "PRECIS_DIAGNOSE_AUTOPROMOTE"
_AUTOPROMOTE_THRESHOLD = 0.8
_AUTOFIX_TAG = "auto-fix"


def _autopromote_enabled() -> bool:
    """Whether an operator opted into confidence-gated auto-fix tagging.

    Default off — diagnosis is a report-only pass until this is set;
    mirrors fix_gripe's ``_unsandboxed_ack``-style boolean env parse.
    """
    return os.environ.get(_AUTOPROMOTE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _parse_confidence(text: str) -> float | None:
    """Pull ``Confidence: 0.NN`` out of the model's reply, or ``None``.

    Accepts the first match in range ``[0, 1]``; anything else (missing,
    malformed, out of range) degrades to ``None`` rather than raising —
    a diagnosis that forgot the confidence line is still worth keeping.
    """
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if 0.0 <= val <= 1.0 else None


# ── Prompt composition ────────────────────────────────────────────


def _compose_prompt(*, ref_title: str, blocks: list[Any]) -> str:
    """Build the prompt fed to ``claude -p`` from the gripe timeline.

    Shape mirrors ``fix_gripe._compose_prompt`` (BODY + numbered COMMENTs,
    timeline order) but asks for a structured ``DIAGNOSIS:`` block instead
    of a fix, and is explicit that the clone is read-only scratch space,
    not a workspace to edit.
    """
    lines: list[str] = []
    lines.append(
        "You are diagnosing a bug report in the precis-mcp repository. "
        "You have a disposable, READ-ONLY clone to investigate — do NOT "
        "edit, commit, branch, or push anything. Your job is to pin down "
        "the root cause, not to fix it."
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
    lines.append("TASK:")
    lines.append(
        "- Investigate the cloned repo (Read/Grep/Bash for inspection only) "
        "to find the actual cause — don't guess from the bug report alone."
    )
    lines.append("- Reply with EXACTLY one block in this shape, nothing else:")
    lines.append("")
    lines.append("DIAGNOSIS:")
    lines.append("Root cause: <one or two sentences>")
    lines.append(
        "Evidence: <durable file anchors (path + symbol/anchor, not just a "
        "line number) that support the cause>"
    )
    lines.append("Proposed fix: <a short sketch — no diff, no patch>")
    lines.append("Confidence: 0.NN")
    lines.append("")
    lines.append(
        "Confidence is your calibrated probability (0.00-1.00) that the "
        "root cause above is correct."
    )
    return "\n".join(lines)


# ── Subprocess + git plumbing (read-only subset of fix_gripe's) ────


def _git_clone_readonly(repo_dir: Path, dest: Path) -> None:
    """Clone ``repo_dir`` into ``dest`` — no branch, no hooks, no push.

    Same ``--local --no-hardlinks`` shape as
    ``fix_gripe._git_clone_and_branch`` (protects the source repo's
    working tree from a concurrent-modification race), minus the
    ``checkout -b`` — diagnose_gripe never commits, so there is nothing
    to branch for. The clone is discarded by the caller once the agent
    call returns (see :func:`_dispatch`).
    """
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(repo_dir), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )


def _spawn_claude(
    *, model: str, clone_dir: Path, prompt: str, timeout_s: float
) -> AgentResult:
    """Run the diagnosis agent through the ``call_claude_agent`` chokepoint.

    Mirrors ``fix_gripe._spawn_claude``'s isolation shape — ``bare=True``
    (API-key auth only), ``env_base=_restricted_env(...)`` (no DB creds),
    ``egress='api-only'`` — and its ``require_container=not
    _unsandboxed_ack()`` fail-closed gate (gr179498): this agent also gets
    full Bash/Read tool access on VERBATIM, agent-filable gripe text, so it
    reuses the same operator ack rather than a bespoke one. The one
    difference: the clone mount is ``mode="ro"`` (fix_gripe's is ``"rw"``,
    since it needs to commit) — diagnose_gripe never writes to its clone,
    so the container structurally can't either.
    """
    from precis.utils.claude_agent import call_claude_agent
    from precis.workers.envelope import Envelope
    from precis.workers.executors.agent_container import Mount

    env_base = _restricted_env(clone_dir)
    if "ANTHROPIC_API_KEY" not in env_base:
        raise RuntimeError(
            "diagnose_gripe: ANTHROPIC_API_KEY is required to run claude -p "
            "in the precis container (OAuth / keychain auth aren't reachable "
            "from inside the container)."
        )
    # write axis is moot (mcp_config=None below — no MCP server, so no DB
    # reachable regardless); egress must stay reachable for the LLM call
    # itself. Mirrors fix_gripe._spawn_claude's envelope rationale.
    envelope = Envelope(egress="api-only", write="full", return_="full")
    mounts = (
        Mount(host_path=str(clone_dir), container_path=str(clone_dir), mode="ro"),
    )
    return call_claude_agent(
        prompt,
        model=model,
        bare=True,
        env_base=env_base,
        cwd=clone_dir,
        mounts=mounts,
        workdir=str(clone_dir),
        envelope=envelope,
        require_container=not _unsandboxed_ack(),
        timeout_s=timeout_s,
        # No MCP server — diagnose_gripe never reaches the precis DB; its
        # only output channel is the returned text.
        mcp_config=None,
    )


# ── Dispatch (plugin protocol, claude_inproc) ───────────────────────


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher: clone read-only, diagnose, write back, done.

    Terminal outcomes:

    * malformed params / gripe not found / no repo config / empty body /
      clone failure / agent failure / empty reply → ``ctx.record_failure``
      (``STATUS:failed``).
    * llm.backend=openai (claude -p can't run an OSS slug) or the
      gr179498 fail-closed container gate → ``STATUS:cancelled`` (a clean
      skip, not a failure — same disposition ``fix_gripe.run`` uses for
      both).
    * a written diagnosis → ``ctx.set_status("succeeded")`` (the gripe
      comment always lands before this).
    """
    params = (ctx.meta or {}).get("params") or {}
    try:
        gripe_id = int(params["gripe_id"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"diagnose_gripe: malformed params ({exc})")
        return

    # Same fleet-flip safety gate as fix_gripe.run(): claude -p assumes
    # Claude model semantics; under backend=openai, resolve_model(BIG)
    # would hand it an OSS slug it can't run (HTTP 400).
    if resolve_backend() is Backend.OPENAI:
        log.warning(
            "diagnose_gripe: llm.backend=openai — skipping gripe:%d diagnosis "
            "(claude -p assumes Claude model semantics, unsupported under "
            "the OSS/OpenRouter backend)",
            gripe_id,
        )
        ctx.append_chunk(
            "job_event",
            "diagnose_gripe skipped: llm.backend=openai — claude -p does not "
            "run under the OSS/OpenRouter backend. Re-attempt once the "
            "backend reverts to anthropic.",
        )
        ctx.set_status("cancelled")
        return

    ref = ctx.store.get_ref(kind="gripe", id=gripe_id)
    if ref is None:
        ctx.record_failure(f"diagnose_gripe: gripe id={gripe_id} not found")
        return

    try:
        cfg = load_config_from_env()
    except RuntimeError as exc:
        ctx.record_failure(f"diagnose_gripe: {exc}")
        return
    try:
        repo_dir = resolve_repo_for_gripe(ctx.store, gripe_id, cfg)
    except ValueError as exc:
        ctx.record_failure(f"diagnose_gripe: {exc}")
        return

    # gr179498 fail-closed gate, reused verbatim from fix_gripe: this agent
    # also gets full Bash/Read tool access on VERBATIM, agent-filable
    # gripe text, so a host with no containerized agent path refuses
    # unless an operator has explicitly acked running unsandboxed.
    from precis.workers.executors import agent_container as _agent_container

    container_ready = (
        _agent_container.container_agent_enabled()
        and _agent_container.container_capability_ok()
    )
    if not container_ready and not _unsandboxed_ack():
        log.warning(
            "diagnose_gripe: refusing gripe:%d — no containerized agent path "
            "available and unsandboxed run not acked (gr179498)",
            gripe_id,
        )
        ctx.append_chunk(
            "job_event",
            "diagnose_gripe skipped: no containerized agent path available "
            f"and {_UNSANDBOXED_ACK_ENV} is unset (gr179498 fail-closed).",
        )
        ctx.set_status("cancelled")
        return

    blocks = ctx.store.blocks.list_blocks_for_ref(gripe_id)
    if not blocks:
        ctx.record_failure(f"diagnose_gripe: gripe id={gripe_id} has no body chunk")
        return
    prompt = _compose_prompt(ref_title=ref.title, blocks=blocks)

    clone_dir = cfg.work_dir / "diagnose_clones" / f"gripe_{gripe_id}"
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        _git_clone_readonly(repo_dir, clone_dir)
    except subprocess.CalledProcessError as exc:
        ctx.record_failure(f"diagnose_gripe: clone failed: {exc.stderr or exc}")
        return

    model = os.environ.get("PRECIS_DIAGNOSE_CLAUDE_MODEL") or resolve_model(Tier.BIG)
    timeout_s = float(os.environ.get("PRECIS_DIAGNOSE_TIMEOUT_SECONDS", "900"))

    try:
        result = _spawn_claude(
            model=model, clone_dir=clone_dir, prompt=prompt, timeout_s=timeout_s
        )
    except (ValueError, ContainerRequiredError, ClaudeAgentError, RuntimeError) as exc:
        ctx.record_failure(f"diagnose_gripe: agent failed: {exc}")
        return
    finally:
        # Disposable — the clone is never needed again once the agent
        # call returns (success or failure alike).
        shutil.rmtree(clone_dir, ignore_errors=True)

    text = (result.final_text or "").strip()
    if not text:
        ctx.record_failure("diagnose_gripe: empty diagnosis")
        return

    confidence = _parse_confidence(text)
    comment = f"{_DIAGNOSIS_PREFIX.format(job_id=ctx.ref_id)}\n{text}"
    with ctx.store.pool.connection() as conn:
        _append_chunk(ctx.store, gripe_id, _GRIPE_COMMENT_KIND, comment, conn=conn)
        conn.commit()

    if confidence is not None:
        ctx.set_meta(confidence=confidence)
        if confidence >= _AUTOPROMOTE_THRESHOLD and _autopromote_enabled():
            from precis.store.types import Tag

            ctx.store.add_tag(gripe_id, Tag.open(_AUTOFIX_TAG), set_by="system")

    ctx.append_chunk(
        "job_summary",
        f"Diagnosed gripe:{gripe_id} (confidence="
        f"{confidence if confidence is not None else 'unknown'}).",
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="diagnose_gripe",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "load",
]
