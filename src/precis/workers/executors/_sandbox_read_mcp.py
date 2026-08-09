"""sandbox_run read-only MCP callback — the ``precis_access:read`` dial.

Slice 3 of ``sandbox_run`` (the ``precis_access`` ``none``|``read``
dial). Called by
:mod:`precis.workers.executors.claude_docker` only for a ``mode:build``
job whose ``params.precis_access == "read"`` (gated at submit/launch time
by ``sandbox_run.semantic_rejection`` on ``PRECIS_SANDBOX_READ_MCP``;
``mode:run`` never reaches this module at all — it gets no ``mcp.json``
ever).

**The trust boundary stays the same as always**: the sandboxed container
NEVER gets a DB DSN, a write-capable role, or the daemon's own secrets —
its only corpus path is a per-run, per-token, read-only HTTP MCP endpoint
bound to the executor host's loopback interface. Three pieces:

1. **DSN derivation** (:func:`read_only_database_url`) — the daemon's own
   base DSN (``store.dsn``, captured post-``secrets.adopt_process_store``)
   with ``user`` swapped to ``agent_ro`` and the password stripped. The
   host running the spawned ``precis serve`` resolves the ``agent_ro``
   password from its own ``~/.pgpass`` (password-free, mirroring §L) —
   this process never holds or forwards an ``agent_ro`` password.
2. **Spawn** (:func:`spawn_read_mcp`) — a per-run ``python -m precis
   serve --transport streamable-http`` child, `agent_ro`-DSN'd,
   bound to ``127.0.0.1:<ephemeral port>``, gated by a fresh per-run
   token. ``/work/mcp.json`` is written pointing the CONTAINER at it —
   under rootless podman's slirp4netns user-mode networking with
   ``--network slirp4netns:allow_host_loopback=true``
   (:data:`READ_MCP_NETWORK`), the container reaches the executor
   host's loopback at :data:`CONTAINER_HOST_LOOPBACK` (``10.0.2.2``,
   slirp4netns's fixed host-loopback address). Bridge-gateway networks
   route this differently (the bridge's gateway IP, not a fixed
   constant) — an ops-play concern once a non-slirp4netns network mode
   is actually used; :data:`READ_MCP_NETWORK` is the only mode this
   module wires today.
3. **Teardown** (:func:`reap_read_mcp`) — identity-checked
   (:func:`_read_mcp_identity_ok` — a bare pid persisted hours earlier
   could have been recycled onto an unrelated process by the OS, so
   teardown verifies the live process's command line names ``precis``
   AND ``serve`` before signaling anything) SIGTERM → grace → SIGKILL by
   PID, mirroring ``cli/watch.py::reap_tracked_process_groups``'s
   escalation shape. Only the PID is persisted (``job.meta.read_mcp_pid``)
   — never the token or port, which would otherwise leak a live
   endpoint's credential into a broadly-DB-readable job ref (any agent
   with ordinary read access to `get(kind='job', ...)` would see it).
   ``claude_docker`` calls this from every terminal path (``_terminate``
   — covers both a normal exit AND a deadline kill, since both route
   through it) and from ``reconcile_orphans`` (boot recovery for a
   container that's already gone).
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _pysecrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: slirp4netns's fixed host-loopback address (rootless podman's default
#: user-mode network stack) — reachable from inside the container only
#: when launched with ``allow_host_loopback=true`` (:data:`READ_MCP_NETWORK`).
#: A bridge network's equivalent is the bridge's gateway IP, not a fixed
#: constant — a future non-slirp4netns network mode needs its own
#: resolution, not this one.
CONTAINER_HOST_LOOPBACK = "10.0.2.2"

#: The ``podman run --network`` value a ``precis_access:read`` launch uses
#: INSTEAD OF the normal ``_network_mode()`` default — the only network
#: mode this module gives the container a route back to the executor
#: host's loopback bind.
READ_MCP_NETWORK = "slirp4netns:allow_host_loopback=true"

#: FastMCP's streamable-http path (server.py's ``mcp.streamable_http_app()``
#: default) — the URL path component of every ``mcp.json`` this module
#: writes.
_MCP_PATH = "/mcp"


@dataclass(frozen=True, slots=True)
class ReadMcpHandle:
    """What :func:`spawn_read_mcp` started.

    ``token``/``port`` are used once — to write ``/work/mcp.json`` — and
    are NEVER persisted to the DB; only ``pid`` is (``job.meta.read_mcp_pid``),
    the sole thing teardown needs.
    """

    pid: int
    port: int
    token: str
    host: str = "127.0.0.1"


# ── pure helpers (argv / env / DSN / mcp.json — asserted by tests) ──


def read_only_database_url(base_url: str | None) -> str | None:
    """Derive the ``agent_ro`` DSN from the daemon's own base DSN — same
    host/port/db, ``user`` swapped to ``agent_ro``, password stripped.
    ``None`` in ⇒ ``None`` out (nothing to derive from — the caller fails
    the launch rather than falling back to a writable DSN).
    """
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    netloc = parsed.hostname or "localhost"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    netloc = f"agent_ro@{netloc}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def build_serve_argv(*, host: str, port: int, token: str) -> list[str]:
    """The per-run ``precis serve`` child's argv.

    ``python -m precis`` (not the bare ``precis`` console script) mirrors
    ``__main__.py``'s documented rationale — canonical regardless of
    ``$PATH`` / venv layout, which a spawned-from-a-worker-daemon child
    can't assume either way.
    """
    return [
        sys.executable,
        "-m",
        "precis",
        "serve",
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--port",
        str(port),
        "--token",
        token,
    ]


#: Env vars stripped from the copied parent env before overrides are
#: applied (Finding 2): the ``claude_docker`` launch path
#: ``os.environ.setdefault``s ``CLAUDE_CODE_OAUTH_TOKEN`` into the daemon
#: process's OWN env (so podman can pass it by key), and this per-run
#: ``precis serve`` sidecar is network-reachable from the untrusted
#: container it serves — so a verbatim ``dict(os.environ)`` copy would
#: hand that live Claude Max OAuth token (and any other credential the
#: parent happens to be carrying) straight to it. Exact names plus any
#: var whose name ends with one of the credential-shaped suffixes.
_CREDENTIAL_ENV_EXACT = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"})
_CREDENTIAL_ENV_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")


def _is_credential_env_name(name: str) -> bool:
    return name in _CREDENTIAL_ENV_EXACT or name.endswith(_CREDENTIAL_ENV_SUFFIXES)


def build_serve_env(base_env: dict[str, str], *, ro_dsn: str) -> dict[str, str]:
    """The per-run ``precis serve`` child's env: the daemon's own env
    (inherits ``PATH``, ``HOME``, any pinned ``PGPASSFILE``, ``PRECIS_*``
    config, etc.) with every credential-shaped var stripped
    (:data:`_CREDENTIAL_ENV_EXACT` / :data:`_CREDENTIAL_ENV_SUFFIXES` —
    Finding 2) and ``PRECIS_DATABASE_URL`` replaced by the read-only DSN
    and ``PRECIS_MCP_DB_ROLE=agent_ro`` advisory-set — the actual
    enforcement is the ``agent_ro`` LOGIN role's own DB-level grants (a
    real connection-time boundary), not the cooperative tier-2 ``SET
    ROLE`` :func:`precis.store.pool._apply_db_role` performs for the
    shared in-process pool.
    """
    env = {k: v for k, v in base_env.items() if not _is_credential_env_name(k)}
    env["PRECIS_DATABASE_URL"] = ro_dsn
    env["PRECIS_MCP_DB_ROLE"] = "agent_ro"
    return env


def mcp_json_payload(*, url: str, token: str) -> dict[str, Any]:
    """The ``/work/mcp.json`` body a container's ``claude --mcp-config``
    reads (once the code-task image wires that flag — an ops-half
    prerequisite, see the design doc's build-plan slice 4): a single
    remote HTTP MCP server, the token in a header — never in the URL
    (query strings land in access logs), never anywhere else on disk.
    """
    return {
        "mcpServers": {
            "precis": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def write_mcp_json(
    work_dir: Path,
    *,
    port: int,
    token: str,
    container_host: str = CONTAINER_HOST_LOOPBACK,
) -> Path:
    """Write ``/work/mcp.json`` pointing at the host-reachable URL."""
    url = f"http://{container_host}:{port}{_MCP_PATH}"
    path = work_dir / "mcp.json"
    path.write_text(
        json.dumps(mcp_json_payload(url=url, token=token), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


# ── impure: spawn / reap the per-run serve child ────────────────────


def _free_port(host: str = "127.0.0.1") -> int:
    """Pick an available loopback port.

    TOCTOU: the port could be taken between this ``close()`` and the
    child's ``bind()`` by an unrelated process on the same host —
    acceptable for a dev-host-local, single-purpose child (the failure
    mode is the spawned ``precis serve`` exiting immediately, which
    surfaces as a launch failure a fresh dispatch retries, not a silent
    hang).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def spawn_read_mcp(
    store: Store,
    *,
    work_dir: Path,
    host: str = "127.0.0.1",
    port_picker: Any = _free_port,
) -> ReadMcpHandle:
    """Spawn a per-run ``precis serve`` child bound to ``host:<ephemeral
    port>``, ``agent_ro``-DSN'd, per-run-token'd, and write
    ``/work/mcp.json`` pointing the container at it.

    Raises ``ValueError`` when no base DSN is available to derive the
    read-only one from (fails the launch — never silently falls back to
    a writable DSN, and never spawns a child with no DB to talk to).
    """
    base_dsn = getattr(store, "dsn", None)
    ro_dsn = read_only_database_url(base_dsn)
    if ro_dsn is None:
        raise ValueError(
            "sandbox_run: precis_access:read has no base DSN to derive the "
            "read-only agent_ro DSN from (store.dsn is unset)"
        )
    token = _pysecrets.token_urlsafe(32)
    port = int(port_picker(host))
    argv = build_serve_argv(host=host, port=port, token=token)
    env = build_serve_env(dict(os.environ), ro_dsn=ro_dsn)
    proc = subprocess.Popen(  # argv is built entirely from build_serve_argv, no shell
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_mcp_json(work_dir, port=port, token=token)
    log.info(
        "sandbox_run: spawned read-only MCP callback pid=%d port=%d", proc.pid, port
    )
    return ReadMcpHandle(pid=proc.pid, port=port, token=token, host=host)


def _read_mcp_identity_ok(pid: int) -> bool:
    """True when the live process at ``pid`` is plausibly the ``precis
    serve`` callback we spawned, not some unrelated process that has
    since reused the pid (Finding 3: ``job.meta.read_mcp_pid`` can be
    persisted hours before teardown runs — long enough for the OS to
    recycle the pid onto an innocent process, which a bare ``os.kill``
    would then SIGTERM/SIGKILL).

    Portable ``ps -p <pid> -o command=`` (macOS + Linux); a dead pid, an
    unreadable one, or a live one whose command line doesn't contain both
    ``precis`` and ``serve`` is NOT ours — return ``False`` so the caller
    skips rather than guesses.
    """
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return False
    if res.returncode != 0:
        return False
    cmd = (res.stdout or "").strip()
    return "precis" in cmd and "serve" in cmd


def reap_read_mcp(pid: int, *, grace_s: float = 5.0) -> None:
    """Verify identity, then SIGTERM → wait up to ``grace_s`` → SIGKILL.

    Mirrors ``cli/watch.py::reap_tracked_process_groups``'s escalation
    shape, scoped to a single bare pid (the serve child has no children
    of its own worth tracking, so no process group is needed). Idempotent
    — an already-dead pid (or one that fails the
    :func:`_read_mcp_identity_ok` check) is a no-op, so every terminal
    path (normal exit, deadline kill, boot reconcile) can call this
    unconditionally without first checking liveness.
    """
    if not _read_mcp_identity_ok(pid):
        log.info("sandbox_run: read-mcp pid %d no longer ours — skipping reap", pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:  # pragma: no cover - defensive
        log.warning("sandbox_run: SIGTERM to read-mcp pid %d denied", pid)
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
        log.warning(
            "sandbox_run: SIGKILLed read-mcp pid %d after %.1fs grace", pid, grace_s
        )
    except ProcessLookupError:
        pass


__all__ = [
    "CONTAINER_HOST_LOOPBACK",
    "READ_MCP_NETWORK",
    "ReadMcpHandle",
    "build_serve_argv",
    "build_serve_env",
    "mcp_json_payload",
    "read_only_database_url",
    "reap_read_mcp",
    "spawn_read_mcp",
    "write_mcp_json",
]
