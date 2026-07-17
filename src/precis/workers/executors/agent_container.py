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

import logging
import os
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


def build_agent_run_argv(
    *,
    container_bin: str,
    name: str,
    image: str,
    cenv: ContainerEnv,
    net: NetworkPlan,
    detached: bool = True,
) -> list[str]:
    """Assemble the ``docker/podman run`` argv for one agentic job.

    Invariants (asserted by tests): ``--name <name>``; every ``secret_keys``
    entry passed ``--env NAME`` (KEY only — no secret in argv); every
    ``values`` entry ``--env NAME=value``; the tier-3 ``net.docker_args``
    present (so ``egress:none`` → ``--network none``); the resolved ``image``
    last. No ``--device`` (never a GPU — agentic work is CPU + network).
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
    return argv


def build_agent_run(
    env: _envelope.Envelope,
    *,
    name: str,
    model: str,
    dsn: str | None = None,
    image: str | None = None,
    detached: bool = True,
) -> list[str]:
    """Convenience: envelope → full run argv, resolving image + env + network.
    The one call a live executor makes; the pieces are exposed separately so the
    tier wiring is unit-testable."""
    return build_agent_run_argv(
        container_bin=_container_bin(),
        name=name,
        image=image or default_agent_image(),
        cenv=container_env(env, model=model),
        net=resolve_network(env, dsn=dsn),
        detached=detached,
    )


__all__ = [
    "ContainerEnv",
    "NetworkPlan",
    "agent_run_mode",
    "build_agent_run",
    "build_agent_run_argv",
    "container_agent_enabled",
    "container_env",
    "default_agent_image",
    "resolve_network",
]
