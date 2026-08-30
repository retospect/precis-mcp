"""sandbox_run — an open-ended coding task in a throwaway container.

The mint → claim → launch → poll → terminal spine, run by the
:mod:`precis.workers.executors.claude_docker` executor; ``mode:run``
(stage the harvested tarball, ``uv sync`` + ``RUN.json.cmd``, no
claude/OAuth); and ``precis_access:read`` (a per-run, token'd,
read-only MCP callback, gated by ``PRECIS_SANDBOX_READ_MCP`` and built by
:mod:`precis.workers.executors._sandbox_read_mcp`). Ships **dark** — the
executor pass is registered only under ``PRECIS_SANDBOX_ENABLED``, so a
merge/deploy touches nothing in prod until a human enables it on a
sandbox host.

The job_type module is a pure declaration + helpers:

* ``PARAMS_SCHEMA`` / ``COMPATIBLE_EXECUTORS`` / ``REQUIRES`` — the
  registry metadata (validated at ``put`` time by the JobHandler).
* ``validate_submit`` — the **fail-closed** submit gate: rejects an
  unsupported ``mode``, ``precis_access:read`` without
  ``PRECIS_SANDBOX_READ_MCP``, a ``secrets`` list, a non-sandbox /
  melchior ``target_node``, ``mode:build`` with no ``prompt``,
  ``mode:run`` with no ``artifact`` (a prior build's ``folder`` ref
  id), or (``mode:build`` only) a missing ``CLAUDE_CODE_OAUTH_TOKEN`` in
  the daemon env. The claude_docker executor re-checks the same
  conditions at launch (defence in depth for jobs minted by
  ``dispatch`` from a todo, which don't pass through the JobHandler put
  path).
* ``resolve_sandbox_model`` — model via the LLM routing seam router
  (``Tier.FRONTIER``) with a ``PRECIS_SANDBOX_MODEL`` override; never
  a private constant.
* ``compose_prompt`` — the ``/work/PROMPT.md`` body (task + harvest
  contract) the executor stages into the run dir (``mode:build`` only —
  ``mode:run`` never gets a prompt).

Trust model: the executor stages a run dir and mounts it ``/work`` —
the volume mount is the whole IN/OUT bus. The executor (trusted, DB
creds) is the only thing touching both the DB and ``/work``; the
container (untrusted, **no DB creds**) reads/writes only files, so a
prompt-injected agent can spend its capped tokens and scribble in
``/work`` but cannot reach the database. Env evaporates (keys ride
``--env``, never land on ``/work``); deps shrink to ``uv.lock`` (the
``.venv`` is scratch, never harvested). Executor-user (the
creds-bearing ``deploy``) ≠ container-user (the locked-down rootless
``agent_sandbox``), so an escape lands on the latter. A future slurm /
aws-batch backend is a ``ComputeBackend`` adapter
({submit, poll, collect, kill}) + a staging location, not a rewrite —
the poll lifecycle already is the submit→poll shape those need.
The container runs Claude with a
**dedicated** long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` (Max, *not*
``--bare`` / ``ANTHROPIC_API_KEY``), no DB creds, cgroup-capped, never a
GPU. melchior is excluded (it holds OAuth / gateway / creds — an escape
target); only ``agent_sandbox_host`` nodes may run it. ``mode:run``
drops the OAuth token entirely — no claude is spawned, so there is
nothing to authenticate.

**``params.artifact``** (``mode:run`` only) is the harvested ``folder``
ref id from a prior ``mode:build`` run — deliberately an *id*, not a raw
sha256 string, so a submitter can only point at something harvest
already produced (and recorded provenance for), never an arbitrary
content hash that happens to collide with something on the artifact
root.
"""

from __future__ import annotations

import os
import re
from typing import Any

from precis.utils.llm.router import Tier, resolve_model

# ── Declared metadata (read by the dispatcher and the executor) ────

#: Nodes that may never be a sandbox host regardless of the allowlist —
#: melchior holds the OAuth token / gateway / DB creds, so a container
#: escape there is the whole threat model. Hard-excluded even if an
#: operator mistakenly lists it in ``PRECIS_SANDBOX_HOSTS``.
_EXCLUDED_NODES: frozenset[str] = frozenset({"melchior", "melchior.local"})

#: ``build`` (a fresh claude coding task) and ``run`` (re-run a prior
#: build's harvested tarball, no claude).
_SUPPORTED_MODES = frozenset({"build", "run"})
_DEFAULT_MODE = "build"

#: ``none`` (default, no precis DB access at all) and ``read`` (a
#: per-run, token'd, read-only MCP callback — gated further by
#: :func:`read_mcp_enabled` on the ops capability flag).
_SUPPORTED_PRECIS_ACCESS_VALUES = frozenset({"none", "read"})
_DEFAULT_PRECIS_ACCESS = "none"

#: Accepted shape for an agent-supplied ``image`` param. The container
#: runtime is exec'd (no shell), so the only real risk is *argument*
#: injection — an ``image`` beginning with ``-`` is parsed by podman's
#: cobra parser as a flag (``--privileged``, ``--help``), not the IMAGE
#: positional (gr179503). We require it to start alphanumeric and contain
#: only image-reference characters, which rejects a leading ``-`` and any
#: whitespace / shell metacharacter. Covers ``code-task:latest``,
#: ``localhost:5000/ns/name:tag``, ``repo@sha256:<hex>``. Anchored with
#: ``\Z`` (not ``$``, which also matches before a single trailing newline —
#: so ``"code-task:latest\n"`` would slip through and taint the meta/log).
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The open-ended coding task handed to Claude in the container.
        # Required for mode:build, ignored for mode:run — enforced by
        # semantic_rejection, not the schema (a schema-level "required"
        # can't be conditional on a sibling field's value).
        "prompt": {"type": "string"},
        # Which sandbox host runs it — pins the claim to that node's
        # worker (params.target_node is the shared node-gate key).
        "target_node": {"type": "string"},
        # Resource budget — currently just the hard wall-clock ceiling
        # (seconds), which sizes the lease + deadline. Nested (not a flat
        # ``wall_seconds``) to match the shared job-budget key used by
        # ssh_node / coordinator / quest.compute / precis_pathway.
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
            "required": ["wall_seconds"],
        },
        # Container image tag (built in place per host by the ops play).
        "image": {"type": "string"},
        # Model override; unset → resolve_model(Tier.FRONTIER). Ignored
        # for mode:run (no claude is spawned).
        "model": {"type": "string"},
        # ``build`` (default) | ``run``.
        "mode": {"type": "string"},
        # mode:run only — the harvested ``folder`` ref id from a prior
        # mode:build run (see the module docstring for why this is an
        # id, not a raw sha256).
        "artifact": {"type": "integer"},
        # ``none`` (default) | ``read`` — ``read`` additionally requires
        # PRECIS_SANDBOX_READ_MCP=1 in the daemon env (an ops capability
        # flag; see read_mcp_enabled()).
        "precis_access": {"type": "string"},
        "secrets": {"type": "array"},
    },
    "required": ["target_node", "resources"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_docker"})

REQUIRES: frozenset[str] = frozenset({"podman", "claude_oauth"})

DESCRIPTION: str = (
    "Run an open-ended coding task (mode:build) or re-run a prior build's "
    "harvested tarball (mode:run) inside a throwaway, cgroup-capped "
    "container on an agent_sandbox_host and keep minimal forensics. "
    "precis_access:read (mode:build only) gives it a per-run, token'd, "
    "read-only MCP callback when PRECIS_SANDBOX_READ_MCP=1. Registered "
    "only under PRECIS_SANDBOX_ENABLED."
)


# ── Config helpers ─────────────────────────────────────────────────


def _sandbox_hosts() -> frozenset[str]:
    """Allowlist of ``agent_sandbox_host`` node names.

    Read from ``PRECIS_SANDBOX_HOSTS`` (comma- or whitespace-separated).
    Empty when unset — ``validate_submit`` then fails closed (a job can't
    target a host we can't confirm is a sandbox).
    """
    raw = os.environ.get("PRECIS_SANDBOX_HOSTS", "")
    return frozenset(h.strip() for h in raw.replace(",", " ").split() if h.strip())


def read_mcp_enabled() -> bool:
    """Ops capability flag for ``precis_access:read`` (``precis serve``'s
    optional network transport + the read-only MCP callback).
    Default OFF (fail-closed) — reads
    ``PRECIS_SANDBOX_READ_MCP`` (``1``/``true``/``yes``).
    """
    return os.environ.get("PRECIS_SANDBOX_READ_MCP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def resolve_wall_seconds(params: dict[str, Any]) -> Any:
    """Read the wall-clock budget: ``params.resources.wall_seconds``
    (nested — matches ssh_node/coordinator/quest.compute/precis_pathway).
    Returns whatever was stored (unvalidated type) or ``None``. Migration
    0147 backfilled every job row still carrying the legacy flat
    ``params.wall_seconds`` into this nested shape, so the read-both shim
    this function used to be is retired (vocab-compaction Stage C).
    """
    resources = params.get("resources")
    if isinstance(resources, dict):
        return resources.get("wall_seconds")
    return None


def resolve_sandbox_model() -> str:
    """Model for the container run.

    ``PRECIS_SANDBOX_MODEL`` override wins; otherwise the LLM routing seam
    ``Tier.FRONTIER`` opus pin (``PRECIS_MODEL_OPUS`` /
    ``claude-opus-4-7`` default). Never a private constant.
    """
    return os.environ.get("PRECIS_SANDBOX_MODEL") or resolve_model(Tier.FRONTIER)


def default_image() -> str:
    """Default container image tag.

    ``PRECIS_SANDBOX_IMAGE`` override, else ``code-task:latest``. The ops
    play builds the image in place per host and tags it by git sha
    (``code-task:<sha>``); the default here is the movable ``latest`` tag
    it also stamps. The ``image`` param overrides per job.
    """
    return os.environ.get("PRECIS_SANDBOX_IMAGE") or "code-task:latest"


# ── Fail-closed submit / launch gate ───────────────────────────────


def semantic_rejection(params: dict[str, Any]) -> str | None:
    """Return a fail-closed rejection reason for ``params``, or ``None``.

    The single source of truth for the submit-time gates, shared by
    ``validate_submit`` (put time) and the claude_docker executor
    (launch time). Rejects, in order: an unsupported ``mode``, an
    unsupported ``precis_access`` value, ``precis_access:read`` without
    ``PRECIS_SANDBOX_READ_MCP``, a non-empty ``secrets`` list, an
    invalid ``image``, a missing / melchior / non-allowlisted
    ``target_node``, a non-positive ``resources.wall_seconds``, a
    ``mode:build`` with no ``prompt``, and a ``mode:run`` with no
    (positive integer) ``artifact``.
    """
    mode = str(params.get("mode") or _DEFAULT_MODE)
    if mode not in _SUPPORTED_MODES:
        return (
            f"sandbox_run: mode:{mode!r} is not supported "
            f"(mode:{{{'|'.join(sorted(_SUPPORTED_MODES))}}} only)"
        )
    precis_access = str(params.get("precis_access") or _DEFAULT_PRECIS_ACCESS)
    if precis_access not in _SUPPORTED_PRECIS_ACCESS_VALUES:
        return (
            f"sandbox_run: precis_access:{precis_access!r} is not a "
            f"supported value (precis_access:"
            f"{{{'|'.join(sorted(_SUPPORTED_PRECIS_ACCESS_VALUES))}}})"
        )
    if precis_access == "read" and not read_mcp_enabled():
        return (
            "sandbox_run: precis_access:read requires PRECIS_SANDBOX_READ_MCP=1 "
            "in the daemon env (the read-only MCP callback ops capability "
            "flag) — fail-closed until it's set"
        )
    secrets = params.get("secrets") or []
    if secrets:
        return (
            "sandbox_run: params.secrets is not supported yet "
            "(task secrets are a later slice)"
        )
    image = params.get("image")
    if image is not None:
        if not isinstance(image, str) or not image.strip():
            return "sandbox_run: params.image must be a non-empty string"
        if not _IMAGE_RE.match(image):
            return (
                f"sandbox_run: params.image {image!r} is not a valid image "
                "reference (must start alphanumeric; no leading '-', "
                "whitespace, or shell metacharacters)"
            )
    target_node = params.get("target_node")
    if not target_node or not isinstance(target_node, str):
        return "sandbox_run: params.target_node is required (an agent_sandbox_host)"
    if target_node in _EXCLUDED_NODES:
        return (
            f"sandbox_run: target_node {target_node!r} is excluded — it holds "
            "the OAuth token / gateway / DB creds (an escape target), so it is "
            "never a sandbox host"
        )
    allowed = _sandbox_hosts()
    if not allowed:
        return (
            "sandbox_run: no PRECIS_SANDBOX_HOSTS configured, so no node can be "
            "confirmed an agent_sandbox_host (fail-closed)"
        )
    if target_node not in allowed:
        return (
            f"sandbox_run: target_node {target_node!r} is not an "
            f"agent_sandbox_host (allowed: {sorted(allowed)})"
        )
    wall = resolve_wall_seconds(params)
    if not isinstance(wall, int) or isinstance(wall, bool) or wall <= 0:
        return "sandbox_run: params.resources.wall_seconds must be a positive integer"
    if mode == "build":
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return "sandbox_run: params.prompt is required for mode:build"
    else:  # mode == "run"
        artifact = params.get("artifact")
        if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
            return (
                "sandbox_run: params.artifact (a prior mode:build run's "
                "harvested folder ref id) is required for mode:run"
            )
    return None


def validate_submit(
    # unused (deleted below) -- kept for the registry's uniform
    # validate_submit signature; tests pass None directly.
    store: Any,
    *,
    gripe_id: int | None = None,
    params: dict[str, Any],
) -> str | None:
    """Submit-time fail-closed gate. Returns an error string or ``None``.

    ``gripe_id`` is ignored — sandbox_run parents on a todo, not a gripe;
    it's kept for the registry's uniform ``validate_submit`` signature.
    The JobHandler surfaces a non-``None`` return as a ``BadInput`` at
    ``put(kind='job', ...)`` time.
    """
    del store, gripe_id
    reason = semantic_rejection(params)
    if reason is not None:
        return reason
    mode = str(params.get("mode") or _DEFAULT_MODE)
    if mode != "build":
        # mode:run spawns no claude — nothing to authenticate.
        return None
    from precis import secrets as _secrets

    if not _secrets.get_secret("CLAUDE_CODE_OAUTH_TOKEN"):
        return (
            "sandbox_run: CLAUDE_CODE_OAUTH_TOKEN is not set in the daemon "
            "env. The container authenticates Claude via a dedicated "
            "long-lived OAuth token (Max) inherited through --env; without "
            "it the run can't authenticate. Set it on the sandbox host's "
            "worker daemon."
        )
    return None


# ── Prompt composition ─────────────────────────────────────────────


def compose_prompt(task: str) -> str:
    """Build the ``/work/PROMPT.md`` body: task + the harvest contract.

    ``mode:build`` only — ``mode:run`` re-runs a harvested tarball's
    ``RUN.json.cmd`` directly and never gets a prompt.
    """
    lines = [
        "# Coding task",
        "",
        "You are an autonomous engineer working inside a throwaway "
        "container with a real toolchain (uv, tests, network). You have "
        "no database access. Do the task below, then leave your work "
        "under `/work/out/`.",
        "",
        "## Task",
        "",
        task.strip(),
        "",
        "## Harvest contract (`/work/out/`)",
        "",
        "- `<code>` — the code you write (a folder tree).",
        "- `tests/` — tests that prove it (run them; green is the proof).",
        "- `pyproject.toml` + `uv.lock` — the dependency recipe.",
        "- `RUN.json` — `{cmd, inputs, outputs, image}` to re-run it.",
        "- `RESULT.md` — a short answer, if the task produced one.",
        "",
        "Env keys (your OAuth token) are passed via `--env`, never on "
        "`/work`. The `.venv` is scratch — it is never harvested "
        "(reconstructible from `uv.lock`).",
    ]
    return "\n".join(lines)


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "compose_prompt",
    "default_image",
    "read_mcp_enabled",
    "resolve_sandbox_model",
    "resolve_wall_seconds",
    "semantic_rejection",
    "validate_submit",
]
