"""``user_secret:<handle>`` read gate — the one owner of "who may see this ref".

``user:<handle>`` is a display / preamble-routing tag and gates nothing.
``user_secret:<handle>`` is the access-controlled sibling (gr556002): a ref
carrying it is returned only to the viewer whose account handle matches, or
to an admin. The check lives here, in server code on the store read path,
not in Postgres; direct SQL through a privileged role bypasses it, and the
standing order is that agents do not read ``user_secret`` rows that way.

The viewer comes from the browser auth middleware, the revision actor
set from a revalidated principal, or the stdio process actor. A
transport supplies the viewer after authentication. Without a viewer,
secret refs are hidden.

Admins are the abbrevs listed in :data:`ADMIN_ENV` (comma separated); an
admin read past the gate is logged at WARNING on the ``precis.secret_scope``
logger.

:func:`secret_fence` is a parameter-free ``NOT EXISTS``/``EXISTS`` predicate,
safe under the fused-search CTE's double splice like the other fences in
:mod:`precis.store._tag_filter`. The handle is spliced as a literal only
after it matches :data:`_HANDLE_RE`; anything else counts as no viewer.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection

log = logging.getLogger("precis.secret_scope")

#: Open-namespace tag prefix that makes a ref private to one handle.
SECRET_PREFIX = "user_secret:"

#: Comma-separated account abbrevs that read past the gate (logged).
ADMIN_ENV = "PRECIS_SECRET_ADMINS"

_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_web_viewer: ContextVar[str | None] = ContextVar("secret_web_viewer", default=None)


@contextmanager
def viewing_as_authenticated_user(abbrev: str) -> Iterator[None]:
    """Bind the authenticated browser viewer without changing write attribution."""
    token = _web_viewer.set(abbrev)
    try:
        yield
    finally:
        _web_viewer.reset(token)


def viewer_handle() -> str | None:
    """The acting user's handle, or ``None`` for an anonymous/agent caller."""
    from precis.acting_user import ACTOR_PREFIX, actor_for
    from precis.store.revision_context import current_revision_context, process_actor

    ctx = current_revision_context()
    web_viewer = _web_viewer.get()
    actor = (
        actor_for(web_viewer)
        if web_viewer is not None
        else (ctx.actor if ctx is not None else None) or process_actor()
    )
    if not actor or not actor.startswith(ACTOR_PREFIX):
        return None
    handle = actor[len(ACTOR_PREFIX) :].strip().lower()
    return handle if _HANDLE_RE.match(handle) else None


def is_admin(handle: str | None) -> bool:
    """True iff ``handle`` is listed in :data:`ADMIN_ENV`."""
    if handle is None:
        return False
    listed = {a.strip().lower() for a in os.environ.get(ADMIN_ENV, "").split(",")}
    return handle in listed


def _secret_exists(ref_expr: str, extra: str = "") -> str:
    return (
        "EXISTS (SELECT 1 FROM ref_tags srt "
        "JOIN tags st ON st.tag_id = srt.tag_id "
        f"WHERE srt.ref_id = {ref_expr} "
        "AND st.namespace = 'OPEN' "
        f"AND starts_with(st.value, '{SECRET_PREFIX}'){extra} "
        "AND (srt.expires_at IS NULL OR srt.expires_at > now()))"
    )


def secret_fence_for(ref_id_expr: str, *, path: str = "read") -> str:
    """SQL predicate: the current viewer may read the ref ``ref_id_expr`` names.

    ``TRUE`` for an admin (the bypass is logged with ``path``), otherwise
    ``NOT EXISTS`` any secret tag, or ``EXISTS`` the viewer's own. Splice
    into an ``AND``-joined clause list; ``ref_id_expr`` is trusted SQL.
    """
    handle = viewer_handle()
    if is_admin(handle):
        log.warning("user_secret admin read past the gate: %s via %s", handle, path)
        return "TRUE"
    anyone = _secret_exists(ref_id_expr)
    if handle is None:
        return f"NOT {anyone}"
    mine = _secret_exists(ref_id_expr, f" AND st.value = '{SECRET_PREFIX}{handle}'")
    return f"(NOT {anyone} OR {mine})"


def secret_fence(ref_alias: str = "r", *, path: str = "read") -> str:
    """:func:`secret_fence_for` on ``<ref_alias>.ref_id``."""
    return secret_fence_for(f"{ref_alias}.ref_id", path=path)


def link_fence(
    src_expr: str = "l.src_ref_id",
    dst_expr: str = "l.dst_ref_id",
    *,
    path: str = "links",
) -> str:
    """A link is readable only when both its ref endpoints are."""
    return (
        f"({secret_fence_for(src_expr, path=path)} "
        f"AND {secret_fence_for(dst_expr, path=path)})"
    )


def ref_visible(store: Any, ref_id: int, *, conn: Connection | None = None) -> bool:
    """True iff the current viewer may read ref ``ref_id`` (point reads)."""
    sql = "SELECT 1 FROM refs r WHERE r.ref_id = %s AND " + secret_fence(
        "r", path="point"
    )
    if conn is not None:
        return conn.execute(sql, (ref_id,)).fetchone() is not None
    with store.pool.connection() as c:
        return c.execute(sql, (ref_id,)).fetchone() is not None


__all__ = [
    "ADMIN_ENV",
    "SECRET_PREFIX",
    "is_admin",
    "link_fence",
    "ref_visible",
    "secret_fence",
    "secret_fence_for",
    "viewer_handle",
    "viewing_as_authenticated_user",
]
