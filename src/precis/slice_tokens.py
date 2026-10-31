"""Per-slice bearer tokens for the shared session MCP (gr543694).

A ``run_slice`` session reaches the session MCP
(``http://127.0.0.1:8765/mcp``) with whatever ``PRECIS_MCP_TOKEN`` its
launch env holds — historically the shared bearer every interactive
session uses, so its calls were indistinguishable from any other
agent's. This module is the per-slice credential: at submit the worker
mints a token file into the host-side registry dir, exports the token
as the session's ``PRECIS_MCP_TOKEN`` (``.mcp.json``'s
``bearerTokenEnvVar`` — no client change needed), and deletes the file
at poll or kill — the revoke. The serving container mounts the same dir
read-only; its auth middleware validates a ``td<id>.<hex>`` bearer
against the file and stamps the grant (``precis.slice_id`` /
``precis.agentlog_id``) into the ASGI scope, where
``tools.core._ctx_run_context`` binds it as the call's run context —
``tool_calls.agentlog_id``/``source``, ``touched`` links, ``meta.slice``
on new refs and the ``slice-td<id>-`` session-id prefix follow from the
existing readers.

The grant is ATTRIBUTION, not a security boundary: a slice session runs
as the same host user with the same MCP access as the shared token — the
bearer only names whose calls they are.

Revocation is three-layered because every layer can be skipped: poll and
``kill`` both delete the file; a grant older than the run_slice
wall-clock cap (mtime, :func:`grant_ttl_s`) is no grant at the server;
and each mint sweeps files past that cap (the worker can delete — the
container mount is read-only). Grant files are job-qualified
(``td<todo>j<job>``) so a resubmitted todo never overwrites a live
grant nor loses its own file to the older run's revoke; the bare
``td<todo>`` shape still validates so in-flight grants drain.

The dir is the deploy handshake: the mcp-http ensure script creates
``${STATE_DIR}/slice-tokens`` and mounts it at
``/run/precis-slice-tokens`` with ``PRECIS_MCP_SLICE_TOKEN_DIR`` set in
the backend env. A host without it mints nothing and the slice keeps the
shared token (status quo) rather than failing closed — an un-upgraded
server would 401 a slice token, which is worse than shared attribution.
The token names its file (``td<id>.<64 hex>``), so a grant check is one
read, not a directory scan; the hex half is 256 bits from
``secrets.token_hex`` and compared constant-time.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Env var naming the minted-token registry dir — on the writer (worker)
#: it is the host path the ensure script's state dir holds; in the
#: serving container it is the read-only mount ``/run/precis-slice-tokens``.
ENV_DIR = "PRECIS_MCP_SLICE_TOKEN_DIR"

#: Host-side fallback registry: the shared server's state dir
#: (``PRECIS_MCP_HTTP_STATE`` or its default). Only consulted when
#: :data:`ENV_DIR` is unset, and only when it exists — existence is the
#: handshake that says this host's server validates slice tokens.
_STATE_ENV = "PRECIS_MCP_HTTP_STATE"
_DEFAULT_STATE = ".cache/precis-mcp-http"
_SUBDIR = "slice-tokens"

#: ASGI scope keys the auth middleware sets on a validated slice grant;
#: ``tools.core._ctx_run_context`` reads them back off the same scope.
SCOPE_SLICE_ID = "precis.slice_id"
SCOPE_AGENTLOG_ID = "precis.agentlog_id"

#: Env var carrying the run_slice wall-clock cap, duplicated here so the
#: serving container and the minting worker share the one knob. A grant
#: file older than it (mtime) is no grant — revoke lives in poll/kill, so
#: a worker that dies first would otherwise leave the file valid forever.
_TTL_ENV = "PRECIS_RUN_SLICE_WALL_S"
_DEFAULT_TTL_S = 4 * 3600  # mirrors run_slice._DEFAULT_WALL_S

#: A slice id is ``td<todo_id>``, qualified by the job that minted it —
#: ``td<todo_id>j<job_id>`` (or ``td<todo_id>a<agentlog_id>`` when the job
#: id is unavailable): two sessions may run for one todo, and a bare
#: ``td<id>`` file name would let a resubmit overwrite the live grant and
#: the old run's revoke delete the new one. The bare ``td<id>`` shape is
#: still accepted so grants minted before the qualifier can drain. The id
#: is the file name — path-safe by construction (no separators).
_NAME_RE = re.compile(r"^td[0-9]+(?:[ja][0-9]+)?$")

#: The wire token: its own locator + 256 random bits.
_TOKEN_RE = re.compile(r"^td[0-9]+(?:[ja][0-9]+)?\.[0-9a-f]{64}$")


def grant_ttl_s() -> float:
    """A grant file's maximum age (mtime → now), the run_slice wall-clock
    cap. Server and worker read the same env var so the TTL on both sides
    of the mount matches (``_TTL_ENV`` is exported to the backend by the
    ensure script)."""
    try:
        n = int(os.environ.get(_TTL_ENV, ""))
    except ValueError:
        return float(_DEFAULT_TTL_S)
    return float(n if n > 0 else _DEFAULT_TTL_S)


def registry_dir() -> Path | None:
    """The dir minted tokens are written to and the server reads.

    ``PRECIS_MCP_SLICE_TOKEN_DIR`` wins when it names an existing dir —
    a set-but-missing path is a misconfig (warn, treat as unconfigured),
    never silently created on the writer's host: the serving container
    mounts a fixed path, and a dir nothing reads turns every slice call
    into a 401. With the var unset the default under the mcp-http state
    dir is used the same way — again only when the ensure script already
    created it. ``None`` = no per-slice tokens on this host.
    """
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        d = Path(env)
        if d.is_dir():
            return d
        log.warning("%s=%s is not a directory; slice tokens disabled", ENV_DIR, env)
        return None
    base = os.environ.get(_STATE_ENV, "").strip() or str(Path.home() / _DEFAULT_STATE)
    d = Path(base) / _SUBDIR
    return d if d.is_dir() else None


def _sweep_stale(directory: Path) -> None:
    """Delete grant files (and mint temp files) older than the TTL —
    best-effort, called on each mint. This is the reaper for grants whose
    revoke never ran (worker died, job cancelled, kill raced a crash);
    the server's own TTL check in :func:`grant` is the authoritative one,
    and the container mounts the dir read-only so it cannot unlink."""
    cutoff = time.time() - grant_ttl_s()
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if not _NAME_RE.match(name) and not (
            name.startswith(".td") and name.endswith(".tmp")
        ):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def mint(directory: Path, slice_id: str, *, agentlog_id: int | None = None) -> str:
    """Write the grant file ``<directory>/<slice_id>`` and return the
    bearer token to hand the session.

    The file's first whitespace-separated field is the expected token,
    the optional second the run's agentlog id — written atomically
    (temp + ``os.replace``) so the server never reads a half-written
    grant. Mode 600: it is a credential file. Each mint also sweeps files
    that outlived the TTL (:func:`_sweep_stale`).
    """
    if not _NAME_RE.match(slice_id):
        raise ValueError(f"slice id {slice_id!r} is not td<digits>[ja<digits>]")
    _sweep_stale(directory)
    token = f"{slice_id}.{secrets.token_hex(32)}"
    content = token + (f" {int(agentlog_id)}" if agentlog_id else "") + "\n"
    target = Path(directory) / slice_id
    tmp = target.with_name(f".{slice_id}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, target)
    return token


def revoke(directory: Path, slice_id: str) -> bool:
    """Delete the grant file — the token 401s from the next request on."""
    try:
        (Path(directory) / slice_id).unlink()
        return True
    except OSError:
        return False


def grant(
    directory: Path | None, header_value: str | None
) -> tuple[str, int | None] | None:
    """``(slice_id, agentlog_id)`` the bearer token names, or ``None``.

    Only tokens in the slice shape (``td<id>.<64 hex>``, the id optionally
    job/agentlog-qualified) reach the file — everything else is a cheap
    miss for the shared-token path to 401 on. A file older than the
    wall-clock TTL (:func:`grant_ttl_s`) is no grant, and so is one that
    does not decode or compare cleanly (a 401, never a 500). The file's
    first field is compared constant-time; its optional second field is
    the run's agentlog id, or ``None`` when absent/malformed.
    """
    if directory is None or not header_value:
        return None
    token = header_value[7:] if header_value.startswith("Bearer ") else ""
    if not _TOKEN_RE.match(token):
        return None
    slice_id = token.split(".", 1)[0]
    path = Path(directory) / slice_id
    try:
        if time.time() - path.stat().st_mtime > grant_ttl_s():
            return None  # outlived the run's wall-clock cap
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    fields = content.split()
    try:
        if not fields or not secrets.compare_digest(fields[0], token):
            return None
    except TypeError:
        return None  # non-ASCII field: no grant, never a 500
    agentlog_id: int | None = None
    if len(fields) > 1:
        try:
            v = int(fields[1])
            agentlog_id = v if v > 0 else None
        except ValueError:
            pass
    return slice_id, agentlog_id


def current_slice_id() -> str | None:
    """The slice id bound to the in-flight call's run context, or ``None``."""
    from precis.utils import inproc_context

    ctx = inproc_context.current()
    return getattr(ctx, "slice_id", None) if ctx is not None else None


def stamp_meta(meta: dict | None) -> dict | None:
    """``meta`` with ``slice: td<id>`` added on a slice-attributed call —
    the ``worktree_server.stamp_meta`` twin, keyed off the request's
    token grant rather than a server-wide env var."""
    sid = current_slice_id()
    if not sid:
        return meta
    out = dict(meta or {})
    out.setdefault("slice", sid)
    return out


__all__ = [
    "ENV_DIR",
    "SCOPE_AGENTLOG_ID",
    "SCOPE_SLICE_ID",
    "current_slice_id",
    "grant",
    "grant_ttl_s",
    "mint",
    "registry_dir",
    "revoke",
    "stamp_meta",
]
