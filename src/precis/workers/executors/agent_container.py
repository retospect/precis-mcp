"""General agentic container executor — the default agentic executor (§13).

The vaulted OAuth token (slice 0) makes ``claude -p`` stateless — its only
inputs are an env var, the prompt, and a reachable MCP server — so an agentic
job can run in a throwaway container instead of in-process. This module is that
path's PURE, testable core: it translates a per-todo **envelope** (slice 8) into
the ``docker run`` knobs, so "one execution primitive, three knobs" (§13) is
literal code:

* **tier-2 (write)** → :func:`precis.workers.envelope.db_role` → the container's
  ``PRECIS_MCP_DB_ROLE`` env (the same var the in-proc ``call_claude_agent``
  already advertises; the container's ``precis serve`` ``SET ROLE``s to it, so a
  write is refused by the database, not merely by a missing tool).
* **tier-3 (egress)** → :func:`precis.workers.envelope.network_mode` → the
  ``--network`` args + the api-only allowlist (``api.anthropic.com`` + the
  pgbouncer ``host:port`` from the DSN) that the container's egress policy pins.

Distinct from :mod:`claude_docker` — that is the pure-compute ``sandbox_run``
box (``/work`` files, no DB, output-only). This is the containerized
:mod:`claude_inproc`: an agentic ``claude -p`` with the precis MCP + the DB,
isolated by network namespace + DB role instead of cooperative tool-deny.

**Ships DARK, policy-gated OFF.** :func:`container_agent_enabled` is False
unless ``PRECIS_AGENT_CONTAINER=1``, so every agentic job still runs in-process
— byte-identical to today. The Phase-2 window flips the policy on ("container =
default agentic executor") once the OAuth token is vaulted and the
``precis-agent`` image is resident. Until then this is authored + tested but
never selected.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from precis.workers import envelope as _envelope

log = logging.getLogger(__name__)

#: Anthropic API host — the one external endpoint an api-only agent may reach
#: (besides its DB). Overridable for a local-model endpoint.
_ANTHROPIC_HOST = "api.anthropic.com"


# ── Policy gate (the dark switch) ──────────────────────────────────


def container_agent_enabled() -> bool:
    """Whether agentic jobs run in a container (§13). DARK default: OFF.

    Reads ``PRECIS_AGENT_CONTAINER`` (``1``/``true``/``yes`` → on). Off → the
    caller uses the in-process executor exactly as today. The Phase-2 window
    sets this on the agent hosts once the OAuth token is vaulted.
    """
    return os.environ.get("PRECIS_AGENT_CONTAINER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def default_agent_image() -> str:
    """The digest-pinned ``precis-agent`` image (wheel + claude CLI + skills).
    ``PRECIS_AGENT_IMAGE`` pins a digest in prod; the tag is the dev fallback."""
    return os.environ.get("PRECIS_AGENT_IMAGE") or "precis-agent:latest"


def _container_bin() -> str:
    return os.environ.get("PRECIS_PODMAN_BIN") or "podman"


# ── Tier-3: egress → --network + allowlist (pure) ──────────────────


@dataclass(frozen=True, slots=True)
class NetworkPlan:
    """The tier-3 network shape for one envelope. ``docker_args`` go straight
    onto ``docker/podman run``; ``allowlist`` is the set of ``host`` /
    ``host:port`` an ``api-only`` box may reach — the data an egress firewall /
    ``pasta`` policy (provisioned by ansible, §15h) pins. Empty allowlist means
    the mode needs no allowlist (``none`` = deny-all, ``open`` = allow-all)."""

    mode: str
    docker_args: tuple[str, ...] = ()
    allowlist: tuple[str, ...] = ()


def _pgbouncer_endpoint(dsn: str | None) -> str | None:
    """``host:port`` of the DB from a DSN, for the api-only allowlist."""
    if not dsn:
        return None
    try:
        from psycopg.conninfo import conninfo_to_dict

        d = conninfo_to_dict(dsn)
    except Exception:  # pragma: no cover — never break the builder on a DSN quirk
        return None
    host = d.get("host")
    if not host:
        return None
    port = d.get("port") or "5432"
    return f"{host}:{port}"


def resolve_network(env: _envelope.Envelope, *, dsn: str | None = None) -> NetworkPlan:
    """Map an envelope's ``egress`` axis to a :class:`NetworkPlan` (tier-3).

    ``none`` → ``--network none`` (the only true egress denial, for a
    pure-compute box); ``api-only`` → bridge networking gated to a 2-entry
    allowlist (Anthropic + the pgbouncer ``host:port``); ``open`` → default
    networking, no restriction.
    """
    mode = _envelope.network_mode(env)  # "none" | "api-only" | None
    if mode == "none":
        return NetworkPlan(mode="none", docker_args=("--network", "none"))
    if mode == "api-only":
        net = os.environ.get("PRECIS_AGENT_NETWORK") or "bridge"
        allow = [os.environ.get("PRECIS_AGENT_LLM_HOST") or _ANTHROPIC_HOST]
        pg = _pgbouncer_endpoint(dsn)
        if pg:
            allow.append(pg)
        return NetworkPlan(
            mode="api-only",
            docker_args=("--network", net),
            allowlist=tuple(allow),
        )
    # open — today's default reachability.
    return NetworkPlan(mode="open")


# ── Tier-2 + secrets: the container env (pure) ─────────────────────


@dataclass(frozen=True, slots=True)
class ContainerEnv:
    """The env a container run injects. ``secret_keys`` are passed by KEY only
    (``--env NAME`` — the value is inherited from the executor process, never in
    argv / ref_events); ``values`` are non-secret ``NAME=value`` pairs."""

    secret_keys: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)


def agent_run_mode() -> str:
    """``"oauth"`` (``claude -p`` on the Max token — the default, ~90% cheaper)
    or ``"api"`` (metered ``ANTHROPIC_API_KEY``, the rate-limit/pinned-model
    escape hatch). Chosen by ``PRECIS_AGENT_MODE``; defaults to oauth."""
    return "api" if os.environ.get("PRECIS_AGENT_MODE") == "api" else "oauth"


def container_env(
    env: _envelope.Envelope,
    *,
    model: str,
    mode: str | None = None,
) -> ContainerEnv:
    """Build the container's env for an envelope (tiers 2 + secrets).

    Injects the auth secret **by key** (OAuth token or API key per ``mode``),
    ``PRECIS_DATABASE_URL`` by key (value inherited), the tier-2
    ``PRECIS_MCP_DB_ROLE`` from :func:`envelope.db_role` (``agent_ro`` for a
    read-only box → the container's ``precis serve`` ``SET ROLE``s to it), and
    the non-secret model.
    """
    mode = mode or agent_run_mode()
    auth_key = "ANTHROPIC_API_KEY" if mode == "api" else "CLAUDE_CODE_OAUTH_TOKEN"
    return ContainerEnv(
        secret_keys=(auth_key, "PRECIS_DATABASE_URL"),
        values={
            "PRECIS_MCP_DB_ROLE": _envelope.db_role(env),
            "PRECIS_AGENT_MODE": mode,
            "PRECIS_AGENT_MODEL": model,
        },
    )


# ── The run argv (pure — asserted by tests) ────────────────────────


def default_agent_mcp_config() -> str:
    """Path (inside the container) to the baked MCP config that wires the
    ``precis`` server over stdio for the agent — ``claude -p`` spawns
    ``precis serve`` per call (no daemon). Overridable via
    ``PRECIS_AGENT_MCP_CONFIG``."""
    return os.environ.get("PRECIS_AGENT_MCP_CONFIG") or "/etc/precis/agent-mcp.json"


def build_claude_command(
    *,
    model: str,
    prompt: str,
    mode: str,
    disallowed_tools: Sequence[str] = (),
    mcp_config: str | None = None,
    max_turns: int = 20,
    permission_mode: str = "bypassPermissions",
    output_format: str = "stream-json",
) -> list[str]:
    """The ``claude -p …`` argv the container execs (its entrypoint is
    ``exec "$@"``, so the run argv's trailing command becomes the container's
    command). Mirrors the in-proc :func:`precis.utils.claude_agent.call_claude_agent`
    argv so a containerized run is the SAME invocation — minus the binary path
    (``claude`` is on the image PATH).

    ``api`` mode adds ``--bare`` (forces ``ANTHROPIC_API_KEY``, no OAuth
    discovery); ``oauth`` uses the token passed by key. The tier-1 deny list
    rides ``--settings`` JSON, never the variadic ``--disallowed-tools`` (which
    greedily eats the prompt — the 2026-06-17 dream incident). ``stream-json``
    output so the executor can reconstruct the transcript.
    """
    argv = [
        "claude",
        "-p",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        permission_mode,
        "--output-format",
        output_format,
    ]
    if mode == "api":
        argv.append("--bare")
    argv += ["--mcp-config", mcp_config or default_agent_mcp_config()]
    argv.append("--strict-mcp-config")
    if disallowed_tools:
        argv += [
            "--settings",
            json.dumps({"permissions": {"deny": list(disallowed_tools)}}),
        ]
    argv.append(prompt)
    return argv


def build_agent_run_argv(
    *,
    container_bin: str,
    name: str,
    image: str,
    cenv: ContainerEnv,
    net: NetworkPlan,
    detached: bool = True,
    command: Sequence[str] = (),
) -> list[str]:
    """Assemble the ``docker/podman run`` argv for one agentic job.

    Invariants (asserted by tests): ``--name <name>``; every ``secret_keys``
    entry passed ``--env NAME`` (KEY only — no secret in argv); every
    ``values`` entry ``--env NAME=value``; the tier-3 ``net.docker_args``
    present (so ``egress:none`` → ``--network none``); the resolved ``image``
    then the container ``command`` (the ``claude -p …`` tail, empty ⇒ the
    image's default ``CMD``). No ``--device`` (never a GPU — agentic work is
    CPU + network).
    """
    argv = [container_bin, "run"]
    if detached:
        argv += ["-d"]
    argv += ["--rm", "--name", name]
    for key in cenv.secret_keys:
        argv += ["--env", key]
    for k, v in cenv.values.items():
        argv += ["--env", f"{k}={v}"]
    argv += list(net.docker_args)
    argv.append(image)
    argv += list(command)
    return argv


def build_agent_run(
    env: _envelope.Envelope,
    *,
    name: str,
    model: str,
    prompt: str | None = None,
    dsn: str | None = None,
    image: str | None = None,
    detached: bool = True,
    mode: str | None = None,
    max_turns: int = 20,
    permission_mode: str = "bypassPermissions",
    output_format: str = "stream-json",
) -> list[str]:
    """Convenience: envelope → full run argv, resolving image + env + network +
    (when ``prompt`` is given) the ``claude -p`` command. The one call a live
    executor makes; the pieces are exposed separately so the tier wiring is
    unit-testable. ``prompt=None`` ⇒ no command appended (the image's default
    ``CMD``), the shape 13-code shipped."""
    mode = mode or agent_run_mode()
    command: list[str] = []
    if prompt is not None:
        command = build_claude_command(
            model=model,
            prompt=prompt,
            mode=mode,
            disallowed_tools=tuple(_envelope.disallowed_tools(env)),
            max_turns=max_turns,
            permission_mode=permission_mode,
            output_format=output_format,
        )
    return build_agent_run_argv(
        container_bin=_container_bin(),
        name=name,
        image=image or default_agent_image(),
        cenv=container_env(env, model=model, mode=mode),
        net=resolve_network(env, dsn=dsn),
        detached=detached,
        command=command,
    )


def containerize_claude_argv(
    host_argv: Sequence[str],
    env: _envelope.Envelope,
    *,
    name: str,
    model: str,
    dsn: str | None = None,
    image: str | None = None,
    mode: str | None = None,
) -> list[str]:
    """Wrap an already-built host ``claude -p …`` argv into a synchronous
    ``docker/podman run`` that execs the SAME command inside the container.

    The general seam a live executor uses to containerize an in-proc agentic
    call WITHOUT re-deriving the command: it takes the caller's exact
    ``host_argv`` (``host_argv[0]`` is the host ``claude`` binary — dropped, the
    image has ``claude`` on PATH; every flag after it is preserved verbatim,
    so ``--max-budget-usd`` / ``--append-system-prompt`` / ``--settings`` etc.
    all carry through) and prepends the tier-2 env + tier-3 network from the
    envelope. ``detached=False`` — a foreground run whose stdout the caller
    captures exactly as it captured the in-proc subprocess's (stream-json is
    byte-identical), so the result parsing is unchanged.

    The container inherits the runner's env for the by-key secrets
    (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``, ``PRECIS_DATABASE_URL``),
    so the caller must pass its ``proc_env`` to the subprocess as today.
    """
    command = ["claude", *list(host_argv)[1:]]
    return build_agent_run_argv(
        container_bin=_container_bin(),
        name=name,
        image=image or default_agent_image(),
        cenv=container_env(env, model=model, mode=mode),
        net=resolve_network(env, dsn=dsn),
        detached=False,
        command=command,
    )


__all__ = [
    "ContainerEnv",
    "NetworkPlan",
    "agent_run_mode",
    "build_agent_run",
    "build_agent_run_argv",
    "build_claude_command",
    "container_agent_enabled",
    "container_env",
    "containerize_claude_argv",
    "default_agent_image",
    "default_agent_mcp_config",
    "resolve_network",
]
