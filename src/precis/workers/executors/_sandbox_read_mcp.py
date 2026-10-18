"""sandbox_run read-only MCP callback — the ``precis_access:read`` dial.

Used by :mod:`precis.workers.executors.claude_docker` for a
``mode:script`` job whose ``params.precis_access == "read"`` (gated at
submit and launch by ``sandbox_run.semantic_rejection`` on
``PRECIS_SANDBOX_READ_MCP``). ``mode:build`` refuses the dial until a
socket bridge ships (Claude Code's ``mcp.json`` has no unix-socket
transport); ``mode:run`` never gets one.

**The trust boundary**: the sandboxed container NEVER gets a DB DSN, a
write-capable role, a network, or the daemon's own secrets — its only
corpus path is a per-run, per-token, read-only MCP endpoint on a unix
socket. Pieces:

1. **DSN derivation** (:func:`read_only_database_url`) — the daemon's own
   base DSN (``store.dsn``, captured post-``secrets.adopt_process_store``)
   with ``user`` swapped to ``agent_ro`` and the password stripped. The
   host running the spawned ``precis serve`` resolves the ``agent_ro``
   password from its own ``~/.pgpass`` (password-free, mirroring §L) —
   this process never holds or forwards an ``agent_ro`` password. The DB
   role is the second wall behind the verb filter.
2. **Spawn** (:func:`spawn_read_mcp`) — a per-run ``python -m precis
   serve --transport streamable-http --uds <sock> --read-only`` child.
   ``--read-only`` removes every tool but ``get``/``search``/``more``
   inside that host process (``server._restrict_to_read_verbs``), so a
   write verb sent over the socket is answered "Unknown tool" whatever
   the container sends. The same host process refuses any ``get``/
   ``search`` ``kind=`` outside :data:`READ_KIND_ALLOWLIST` (live-fetch
   and paid kinds, gr496511). The socket lives in a per-run host dir
   (:func:`socket_dir`, a sibling of the run dir) that the executor
   bind-mounts read-only at :data:`CONTAINER_SOCKET_DIR`; the container
   keeps ``--network none``. A fresh per-run token rides the child's env
   (never argv, which other host users can read in ``ps``) and is written
   with the socket path to ``/work/mcp.json``; :data:`CLIENT_SOURCE`
   (stdlib only) is staged as ``/work/precis_client.py`` for scripts.
3. **Teardown** (:func:`reap_read_mcp`) — identity-checked
   (:func:`_read_mcp_identity_ok` — a bare pid persisted hours earlier
   could have been recycled onto an unrelated process by the OS, so
   teardown verifies the live process's command line names ``precis``
   AND ``serve`` before signaling anything) SIGTERM → grace → SIGKILL by
   PID, mirroring ``cli/watch.py::reap_tracked_process_groups``'s
   escalation shape. Only the PID is persisted (``job.meta.read_mcp_pid``)
   — never the token, which would otherwise leak a live endpoint's
   credential into a broadly-DB-readable job ref.
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
import shutil
import signal
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

#: Kinds the read-only serve answers (deny by default, gr496511). Corpus,
#: file and pure-local compute kinds only. Excluded on purpose: every kind
#: that fetches live on a cache miss (web, youtube, wikipedia, news,
#: datasheet, paper, patent, edgar, chem, protein, taxon, ...) — a
#: ``--network none`` script must not get public egress or a data-out
#: channel through the host — and every paid kind (perplexity-*, websearch,
#: llm, oracle). A fetch kind joins only with a cache-hit-only handler path.
READ_KIND_ALLOWLIST = frozenset(
    {
        "calc",
        "cad",
        "checklist",
        "citation",
        "component",
        "concept",
        "draft",
        "figure",
        "finding",
        "folder",
        "gripe",
        "markdown",
        "material",
        "math",
        "md",
        "measure",
        "memory",
        "mermaid",
        "part",
        "pathway",
        "pcb",
        "plaintext",
        "plan",
        "pres",
        "profile",
        "provenance",
        "python",
        "quest",
        "random",
        "route",
        "rxn",
        "se",
        "skill",
        "structure",
        "tag",
        "tex",
        "time",
        "todo",
    }
)


def kind_refusal(verb: str, kind: Any) -> str | None:
    """The refusal text when a read-only ``get``/``search`` names a kind
    outside :data:`READ_KIND_ALLOWLIST` (or none at all), else ``None``.
    ``more`` carries no kind and is never gated.
    """
    if verb not in ("get", "search"):
        return None
    names = [k.strip() for k in str(kind or "").split(",") if k.strip()]
    if names and all(n in READ_KIND_ALLOWLIST for n in names):
        return None
    shown = ", ".join(names) if names else "(no kind)"
    return (
        f"[error:BadInput] {verb}(kind={shown!r}) is not served by the sandbox "
        "read-only MCP: it answers corpus and local kinds only, never live-fetch "
        "or paid kinds\n"
        f"  options: {', '.join(sorted(READ_KIND_ALLOWLIST))}\n"
        f"  next: {verb}(kind=<one of the options>, ...); fetch the page or "
        "paid result outside the sandbox and pass it in as a script input"
    )


#: Where the per-run socket dir is mounted (read-only) in the container.
CONTAINER_SOCKET_DIR = "/work/mcp"

#: The socket's file name inside :func:`socket_dir`.
SOCKET_NAME = "precis.sock"

#: FastMCP's streamable-http path (server.py's ``mcp.streamable_http_app()``
#: default).
_MCP_PATH = "/mcp"


@dataclass(frozen=True, slots=True)
class ReadMcpHandle:
    """What :func:`spawn_read_mcp` started.

    ``token`` is used once — to write ``/work/mcp.json`` — and is NEVER
    persisted to the DB; only ``pid`` is (``job.meta.read_mcp_pid``), the
    sole thing teardown needs. ``socket_dir`` is the host dir the
    executor mounts at :data:`CONTAINER_SOCKET_DIR`.
    """

    pid: int
    socket_dir: Path
    token: str


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


def build_serve_argv(*, uds: str) -> list[str]:
    """The per-run ``precis serve`` child's argv.

    ``--read-only`` is the host-side verb filter; ``--uds`` binds the
    socket the container reaches through its bind mount. The token is
    not here — it rides the env (:func:`build_serve_env`). ``python -m
    precis`` (not the bare ``precis`` console script) mirrors
    ``__main__.py``'s documented rationale — canonical regardless of
    ``$PATH`` / venv layout.
    """
    return [
        sys.executable,
        "-m",
        "precis",
        "serve",
        "--transport",
        "streamable-http",
        "--uds",
        uds,
        "--read-only",
    ]


#: Env vars stripped from the copied parent env before overrides are
#: applied (Finding 2): the daemon process's own env can carry
#: ``CLAUDE_CODE_OAUTH_TOKEN`` and other credentials, and this per-run
#: ``precis serve`` sidecar is reachable from the untrusted container it
#: serves — so a verbatim ``dict(os.environ)`` copy would hand them
#: straight to it. Exact names plus any var whose name ends with one of
#: the credential-shaped suffixes.
_CREDENTIAL_ENV_EXACT = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"})
_CREDENTIAL_ENV_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")


def _is_credential_env_name(name: str) -> bool:
    return name in _CREDENTIAL_ENV_EXACT or name.endswith(_CREDENTIAL_ENV_SUFFIXES)


def build_serve_env(
    base_env: dict[str, str], *, ro_dsn: str, token: str
) -> dict[str, str]:
    """The per-run ``precis serve`` child's env: the daemon's own env
    (inherits ``PATH``, ``HOME``, any pinned ``PGPASSFILE``, ``PRECIS_*``
    config, etc.) with every credential-shaped var stripped
    (:data:`_CREDENTIAL_ENV_EXACT` / :data:`_CREDENTIAL_ENV_SUFFIXES` —
    Finding 2) and ``PRECIS_DATABASE_URL`` replaced by the read-only DSN
    and ``PRECIS_MCP_DB_ROLE=agent_ro`` advisory-set — the actual
    enforcement is the ``agent_ro`` LOGIN role's own DB-level grants (a
    real connection-time boundary), not the cooperative tier-2 ``SET
    ROLE`` :func:`precis.store.pool._apply_db_role` performs for the
    shared in-process pool. ``PRECIS_MCP_TOKEN`` carries the per-run
    bearer token (set after the strip, so it is the only token present);
    ``PRECIS_MCP_PROFILE`` is pinned to ``typed`` because ``--read-only``
    refuses the command profile.
    """
    env = {k: v for k, v in base_env.items() if not _is_credential_env_name(k)}
    env["PRECIS_DATABASE_URL"] = ro_dsn
    env["PRECIS_MCP_DB_ROLE"] = "agent_ro"
    env["PRECIS_MCP_PROFILE"] = "typed"
    env["PRECIS_MCP_TOKEN"] = token
    return env


def mcp_json_payload(*, token: str) -> dict[str, Any]:
    """The ``/work/mcp.json`` body :data:`CLIENT_SOURCE` reads: the
    in-container socket path, the HTTP path and the bearer token."""
    return {
        "socket": f"{CONTAINER_SOCKET_DIR}/{SOCKET_NAME}",
        "path": _MCP_PATH,
        "token": token,
    }


def write_mcp_json(work_dir: Path, *, token: str) -> Path:
    """Write ``/work/mcp.json`` (:func:`mcp_json_payload`)."""
    path = work_dir / "mcp.json"
    path.write_text(
        json.dumps(mcp_json_payload(token=token), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def socket_dir(work_dir: Path) -> Path:
    """The per-run host dir holding the socket: a sibling of ``work_dir``
    (``<run>-mcp``), so the rw ``/work`` mount never contains it and the
    container sees it only through the read-only bind."""
    return work_dir.parent / f"{work_dir.name}-mcp"


#: The in-container client, staged as ``/work/precis_client.py``. Stdlib
#: only, so a script needs no ``deps`` entry to use it: ``import
#: precis_client; precis_client.call("search", kind="structure", q="…")``
#: returns the tool's text. Speaks MCP streamable-http over the socket
#: (initialize, initialized, then ``tools/call``) and retries the connect
#: while the serve child is still starting.
CLIENT_SOURCE = '''"""precis read-only MCP client for sandbox scripts (stdlib only).

    import precis_client
    text = precis_client.call("search", kind="structure", q="perovskite")

Only get, search and more are served; any other verb is refused by the
host. Settings come from /work/mcp.json.
"""

import http.client
import json
import socket
import time

_CFG_PATH = "/work/mcp.json"
_session = None
_next_id = 0


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", 80, timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _cfg():
    with open(_CFG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _post(cfg, body, timeout):
    headers = {
        # FastMCP's DNS-rebinding guard wants a host:port Host header.
        "Host": "localhost:80",
        "Authorization": "Bearer " + cfg["token"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session:
        headers["mcp-session-id"] = _session
    conn = _UnixConnection(cfg["socket"], timeout)
    try:
        conn.request("POST", cfg["path"], json.dumps(body), headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, resp.getheader("mcp-session-id"), raw
    finally:
        conn.close()


def _message(raw):
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def _rpc(method, params, timeout, notify=False):
    global _next_id
    cfg = _cfg()
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notify:
        _next_id += 1
        body["id"] = _next_id
    status, sid, raw = _post(cfg, body, timeout)
    if status >= 400:
        raise RuntimeError("precis MCP HTTP %d: %s" % (status, raw[:500]))
    return sid, _message(raw)


def _connect(wait_s):
    global _session
    deadline = time.monotonic() + wait_s
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "precis_client", "version": "1"},
    }
    while True:
        try:
            sid, _ = _rpc("initialize", params, 30)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    _session = sid
    _rpc("notifications/initialized", {}, 30, notify=True)


def call(verb, wait_s=120, timeout=300, **arguments):
    """Call one read verb; return its text, raise RuntimeError on an error."""
    if _session is None:
        _connect(wait_s)
    _, msg = _rpc("tools/call", {"name": verb, "arguments": arguments}, timeout)
    if msg is None:
        raise RuntimeError("precis MCP: empty response")
    if "error" in msg:
        raise RuntimeError("precis MCP: %s" % msg["error"].get("message"))
    result = msg.get("result") or {}
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        raise RuntimeError(text or "precis MCP: tool error")
    return text
'''


# ── impure: spawn / reap the per-run serve child ────────────────────


#: ``sun_path`` holds 104 bytes on macOS and 108 on Linux, NUL included.
_MAX_SOCKET_PATH_BYTES = 100


def spawn_read_mcp(
    store: Store, *, work_dir: Path, ready_s: float = 20.0
) -> ReadMcpHandle:
    """Spawn a per-run ``--read-only`` ``precis serve`` child on a unix
    socket in :func:`socket_dir`, ``agent_ro``-DSN'd and per-run-token'd,
    after writing ``/work/mcp.json`` and ``/work/precis_client.py`` (so a
    failed write cannot orphan a child).

    Waits up to ``ready_s`` for the socket to appear; a child that exits
    first fails the launch. A child still booting after that is left to
    the client's connect retry.

    Raises ``ValueError`` when no base DSN is available to derive the
    read-only one from (fails the launch — never silently falls back to
    a writable DSN, and never spawns a child with no DB to talk to), when
    the socket path is too long to bind, or when the child dies early.
    """
    base_dsn = getattr(store, "dsn", None)
    ro_dsn = read_only_database_url(base_dsn)
    if ro_dsn is None:
        raise ValueError(
            "sandbox_run: precis_access:read has no base DSN to derive the "
            "read-only agent_ro DSN from (store.dsn is unset)"
        )
    sock_dir = socket_dir(work_dir)
    sock_path = sock_dir / SOCKET_NAME
    if len(str(sock_path).encode("utf-8")) > _MAX_SOCKET_PATH_BYTES:
        raise ValueError(
            f"sandbox_run: read MCP socket path {sock_path} is longer than "
            f"{_MAX_SOCKET_PATH_BYTES} bytes and cannot be bound; next: set "
            "PRECIS_SANDBOX_WORK_DIR to a shorter path"
        )
    if sock_dir.exists():
        shutil.rmtree(sock_dir, ignore_errors=True)
    sock_dir.mkdir(parents=True)
    # Traversable by the container's subordinate uid; uvicorn makes the
    # socket itself 0o666. The token sits in the world-readable /work, so
    # it does not keep other host users out; what they could reach is the
    # same corpus read the agent_ro role already grants.
    sock_dir.chmod(0o711)
    token = _pysecrets.token_urlsafe(32)
    write_mcp_json(work_dir, token=token)
    (work_dir / "precis_client.py").write_text(CLIENT_SOURCE, encoding="utf-8")
    argv = build_serve_argv(uds=str(sock_path))
    env = build_serve_env(dict(os.environ), ro_dsn=ro_dsn, token=token)
    proc = subprocess.Popen(  # argv is built entirely from build_serve_argv, no shell
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + ready_s
    while time.monotonic() < deadline and not sock_path.exists():
        if proc.poll() is not None:
            raise ValueError(
                f"sandbox_run: read MCP child exited ({proc.returncode}) before "
                "binding its socket; next: run `precis serve --transport "
                "streamable-http --uds /tmp/t.sock --read-only` on the host "
                "to see why"
            )
        time.sleep(0.1)
    log.info("sandbox_run: spawned read-only MCP callback pid=%d (uds)", proc.pid)
    return ReadMcpHandle(pid=proc.pid, socket_dir=sock_dir, token=token)


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
            encoding="utf-8",
            errors="replace",
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
    "CLIENT_SOURCE",
    "CONTAINER_SOCKET_DIR",
    "SOCKET_NAME",
    "ReadMcpHandle",
    "build_serve_argv",
    "build_serve_env",
    "mcp_json_payload",
    "read_only_database_url",
    "reap_read_mcp",
    "socket_dir",
    "spawn_read_mcp",
    "write_mcp_json",
]
