"""Attribute a stdio server's writes to the human it is serving.

asa_bot runs one ``claude -p`` per Discord turn, and that run spawns its own
``precis serve`` over stdio (``deploy/roles/asa_bot/templates/
claude_mcp.json.j2``). When the speaker has linked a precis account, asa sets
:data:`ENV_VAR` to the account's ``abbrev`` on that run; the mcp.json passes
it through to ``serve``, which resolves it here at boot and makes
``user:<abbrev>`` the process's fallback write actor
(:func:`precis.store.revision_context.set_process_actor`). Verb writes then
land in the ``revisions`` log as that account instead of the generic
``agent``. The in-process OSS lane has no ``serve`` subprocess; asa wraps that
turn in ``revision_context(actor=actor_for(abbrev))`` instead.

**Trust boundary.** Only :func:`install_from_env` reads :data:`ENV_VAR`, and
it honours it only for ``transport="stdio"``. A stdio server's env is set by
whoever spawned it, and that party already holds the server's DB credential
(it could ``set_config('precis.actor', ...)`` itself), so trusting it grants
nothing new. A network transport (the shared streamable-http server) ignores
the variable and logs a warning: one process serves many sessions there, and
no MCP request field carries an identity, so no client of the shared server
can claim to be a user.

The ``abbrev`` must name a ``web_users`` row; an unknown one, a storeless
server or a lookup failure keeps ``agent``. ``set_by`` columns are unchanged:
``actors`` is a closed vocabulary (``ActorSlug``), and open-ended identities
live in the plain-text ``revisions.actor`` / ``reviews.actor`` (migration
0185).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

#: Env var carrying the speaker's account ``abbrev`` into a stdio ``serve``.
ENV_VAR = "PRECIS_ACTING_USER"

#: Prefix that marks a human account in ``revisions.actor``, mirroring asa's
#: ``user:<handle>`` memory tag. Keeps an abbrev from reading as a worker slug.
ACTOR_PREFIX = "user:"


def actor_for(abbrev: str) -> str:
    """The revision actor for a precis account ``abbrev``."""
    return f"{ACTOR_PREFIX}{abbrev.strip().lower()}"


def resolve_from_env(
    store: Any, environ: Mapping[str, str] | None = None
) -> str | None:
    """``user:<abbrev>`` for a known account named by :data:`ENV_VAR`, else None."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_VAR) or "").strip().lower()
    if not raw:
        return None
    if store is None:
        log.warning("%s=%r ignored: no store to check the account", ENV_VAR, raw)
        return None
    try:
        user = store.get_web_user_by_abbrev(raw)
    except Exception:
        log.warning("%s=%r ignored: account lookup failed", ENV_VAR, raw, exc_info=True)
        return None
    if user is None:
        log.warning("%s=%r ignored: no account with that abbrev", ENV_VAR, raw)
        return None
    return actor_for(user.abbrev)


def install_from_env(
    store: Any, *, transport: str, environ: Mapping[str, str] | None = None
) -> str | None:
    """Set the process write actor from :data:`ENV_VAR`; stdio only.

    Returns the actor installed, or ``None`` (the process stays ``agent``).
    """
    # Lazy: asa_bot imports this module for the constants, and the store
    # package is a heavy import it does not otherwise need per turn.
    from precis.store.revision_context import set_process_actor

    env = os.environ if environ is None else environ
    if transport != "stdio":
        if (env.get(ENV_VAR) or "").strip():
            log.warning(
                "%s ignored on transport %r: a shared server never acts as one user",
                ENV_VAR,
                transport,
            )
        set_process_actor(None)
        return None
    actor = resolve_from_env(store, env)
    set_process_actor(actor)
    if actor:
        log.info("writes attributed to %s", actor)
    return actor


__all__ = [
    "ACTOR_PREFIX",
    "ENV_VAR",
    "actor_for",
    "install_from_env",
    "resolve_from_env",
]
