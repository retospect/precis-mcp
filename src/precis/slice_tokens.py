"""Per-slice bearer tokens for the shared session MCP (gr543694).

A ``run_slice`` session reaches the session MCP
(``http://127.0.0.1:8765/mcp``) with whatever ``PRECIS_MCP_TOKEN`` its
launch env holds — historically the shared bearer every interactive
session uses, so its calls were indistinguishable from any other
agent's. This module is the per-slice credential: at submit the worker
mints a token file into the host-side registry dir, exports the token
as the session's ``PRECIS_MCP_TOKEN`` (``.mcp.json``'s
``bearerTokenEnvVar`` — no client change needed), and deletes the file
at poll — the revoke. The serving container mounts the same dir
read-only; its auth middleware validates a ``td<id>.<hex>`` bearer
against the file and stamps the grant (``precis.slice_id`` /
``precis.agentlog_id``) into the ASGI scope, where
``tools.core._ctx_run_context`` binds it as the call's run context —
``tool_calls.agentlog_id``/``source``, ``touched`` links, ``meta.slice``
on new refs and the ``slice-td<id>-`` session-id prefix follow from the
existing readers.

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

#: A slice id is ``td<todo_id>`` — the file name, so it must be
#: path-safe by construction (digits only, no separators).
_NAME_RE = re.compile(r"^td[0-9]+$")

#: The wire token: its own locator + 256 random bits.
_TOKEN_RE = re.compile(r"^td[0-9]+\.[0-9a-f]{64}$")


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


def mint(directory: Path, slice_id: str, *, agentlog_id: int | None = None) -> str:
    """Write the grant file ``<directory>/<slice_id>`` and return the
    bearer token to hand the session.

    The file's first whitespace-separated field is the expected token,
    the optional second the run's agentlog id — written atomically
    (temp + ``os.replace``) so the server never reads a half-written
    grant. Mode 600: it is a credential file.
    """
    if not _NAME_RE.match(slice_id):
        raise ValueError(f"slice id {slice_id!r} is not td<digits>")
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

    Only tokens in the slice shape (``td<id>.<64 hex>``) reach the file —
    everything else is a cheap miss for the shared-token path to 401 on.
    The file's first field is compared constant-time; its optional second
    field is the run's agentlog id, or ``None`` when absent/malformed.
    """
    if directory is None or not header_value:
        return None
    token = header_value[7:] if header_value.startswith("Bearer ") else ""
    if not _TOKEN_RE.match(token):
        return None
    slice_id = token.split(".", 1)[0]
    try:
        content = (Path(directory) / slice_id).read_text(encoding="utf-8")
    except OSError:
        return None
    fields = content.split()
    if not fields or not secrets.compare_digest(fields[0], token):
        return None
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
    "mint",
    "registry_dir",
    "revoke",
    "stamp_meta",
]
