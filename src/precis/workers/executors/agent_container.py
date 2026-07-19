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
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from precis.utils.container_limits import container_limit_flags
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
    """The container CLI to invoke: an explicit ``PRECIS_CONTAINER_BIN`` /
    ``PRECIS_PODMAN_BIN`` wins (even if not on PATH — it still goes in the argv),
    else the detected runtime (podman on Linux, docker/OrbStack on the Macs),
    else ``podman`` as the argv default."""
    explicit = os.environ.get("PRECIS_CONTAINER_BIN") or os.environ.get(
        "PRECIS_PODMAN_BIN"
    )
    if explicit:
        return explicit
    from precis.workers.capability_probe import container_runtime

    return container_runtime() or "podman"


# ── Verified-capability probe + run-health latch (§15d/§15h) ───────
#
# ``container_agent_enabled()`` is a bare policy read — it says the operator
# *opted in*, not that this host can actually run the container. A host that
# lacks the runtime, the image, or a resolvable auth token would containerize
# every agentic pass and fail at ``docker run`` — the spark DSN retry-storm's
# shape (2026-07-19). So the live selection seam gates the opt-in behind THIS
# verified probe: the runtime+daemon reachable ∧ the ``precis-agent`` image
# resident ∧ an auth token actually resolvable. Any leg missing / any probe
# erroring ⇒ ``False`` ⇒ the caller runs in-process (byte-identical to today),
# never a container it can't launch. Per-process cached (~60s) so the probe's
# subprocesses don't ride every pass; the run-failure breaker
# (:func:`trip_container_unhealthy`) latches a ~10-min ``False`` when a
# containerized run dies at the infra level (melchior's OOM case, where
# pre-flight passes but the box is SIGKILLed at run).

_CAPABILITY_TTL_S = 60.0
_CAPABILITY_PROBE_TIMEOUT_S = 5.0
_UNHEALTHY_COOLDOWN_S = 600.0

#: (mono_ts, ok) per (bin, image, mode) — the last probe result within TTL.
_CAPABILITY_CACHE: dict[str, tuple[float, bool]] = {}
#: monotonic deadline the container path is latched unhealthy until (0 = healthy).
_UNHEALTHY_UNTIL: float = 0.0


def trip_container_unhealthy(*, _now: float | None = None) -> None:
    """Latch the container path unhealthy for ~10 min (per process).

    The live executor calls this when a containerized run dies at the *infra*
    level (image missing, daemon unreachable, socket perm, OOM 137) — as
    distinct from a claude/model error. While latched,
    :func:`container_capability_ok` returns ``False`` so every pass falls back
    in-process instead of retry-storming a box that just proved it can't run the
    container. Self-clears when the cooldown elapses (the next probe
    re-verifies from scratch)."""
    global _UNHEALTHY_UNTIL
    now = _now if _now is not None else time.monotonic()
    _UNHEALTHY_UNTIL = now + _UNHEALTHY_COOLDOWN_S


def reset_capability_cache() -> None:
    """Clear the probe cache + health latch (tests, or a forced re-probe)."""
    global _UNHEALTHY_UNTIL
    _CAPABILITY_CACHE.clear()
    _UNHEALTHY_UNTIL = 0.0


def _auth_token_present(mode: str | None = None) -> bool:
    """Whether an auth token for ``mode`` is actually resolvable — mirrors the
    run path's resolution (env → ``~/.claude_oauth_token`` → vault for oauth;
    env only for the api key) so the probe agrees with what a real run sees."""
    mode = mode or agent_run_mode()
    from precis.utils import claude_oauth as _oauth

    if mode == "api":
        return bool(os.environ.get(_oauth.API_KEY_VAR, "").strip())
    probe_env = dict(os.environ)
    _oauth.ensure_oauth_token(probe_env)
    return bool(probe_env.get(_oauth.ENV_VAR, "").strip())


def _probe_container_capability(image: str) -> bool:
    """The uncached probe: an auth token resolvable ∧ ``<bin> info`` exit 0
    (runtime+daemon reachable) ∧ ``<bin> image inspect <image>`` exit 0 (the
    image resident). Short subprocess timeouts; ANY exception → ``False``
    (fail-safe → in-proc)."""
    if not _auth_token_present():
        return False
    bin_ = _container_bin()
    try:
        for probe in ([bin_, "info"], [bin_, "image", "inspect", image]):
            res = subprocess.run(
                probe,
                capture_output=True,
                timeout=_CAPABILITY_PROBE_TIMEOUT_S,
                check=False,
            )
            if res.returncode != 0:
                return False
    except Exception:
        # OSError (bin absent), TimeoutExpired (daemon wedged), anything — the
        # host can't be *verified* to containerize, so it doesn't.
        return False
    return True


def container_capability_ok(
    image: str | None = None, *, _now: float | None = None
) -> bool:
    """Whether THIS host can actually run an agentic container right now (§15d).

    The verified capability the live selection seam gates the
    ``PRECIS_AGENT_CONTAINER`` opt-in behind: unlike
    :func:`container_agent_enabled` (a bare policy read), this confirms the
    runtime is live, the image is resident, and an auth token resolves — so an
    opted-in host that *can't* containerize falls back in-process instead of
    failing every pass. Per-process cached (~60s); the health latch
    (:func:`trip_container_unhealthy`) forces ``False`` through a post-failure
    cooldown. Fail-safe: any uncertainty ⇒ ``False`` ⇒ in-proc."""
    now = _now if _now is not None else time.monotonic()
    if now < _UNHEALTHY_UNTIL:
        return False
    image = image or default_agent_image()
    key = f"{_container_bin()}\x00{image}\x00{agent_run_mode()}"
    cached = _CAPABILITY_CACHE.get(key)
    if cached is not None and now - cached[0] < _CAPABILITY_TTL_S:
        return cached[1]
    ok = _probe_container_capability(image)
    _CAPABILITY_CACHE[key] = (now, ok)
    return ok


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
    argv += container_limit_flags()
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
    "container_capability_ok",
    "container_env",
    "containerize_claude_argv",
    "default_agent_image",
    "default_agent_mcp_config",
    "reset_capability_cache",
    "resolve_network",
    "trip_container_unhealthy",
]
