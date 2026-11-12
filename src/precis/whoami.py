"""The whoami read — which identity this MCP session writes as (gr633675).

``get(kind='profile', id='me')`` answers "who does the server think I am":
the authenticated account (the OAuth subject or personal-token owner), the
credential carrying the session (an OAuth connection with its label, or a
personal MCP token with its label), the permission scope that credential
carries, and the exact write attribution the server records — the write
principal and revision actor a ``put``/``edit``/``tag`` from this session
stamps, plus the MCP client the session declared. A session answers for
itself only: the render reads the request-bound principal
(:func:`precis.user_context.current_principal`) and server-set stamps,
never caller input, so it can disclose nothing the caller does not already
own. Anonymous sessions (the shared install/slice/stdio credential) render
their generic attribution instead of a user — enough to see the session
is not personal.

Everything here is best-effort decoration around one ContextVar read: a
failed lookup renders ``(unavailable)``, never an error, because the
identity lines around it are the point of the call. Revalidation is the
dispatch's job (``_dispatch_as_user`` refuses a revoked credential before
any handler runs); whoami renders what the bound principal names.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis import principal as write_principal
from precis import tool_ledger
from precis.store.revision_context import current_revision_context, process_actor
from precis.user_context import Principal, current_principal

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The ``get(kind=…, id=…)`` payload every authenticated session may call,
#: whatever its scope — a connection that can only learn may still ask who
#: it is. ``_personal_call_allowed`` whitelists exactly this shape.
CALL = {"kind": "profile", "id": "me"}


def _degraded[T](read: Callable[[], T]) -> T | None:
    """A decoration lookup: on failure render the degraded form, never fail
    the whoami read."""
    try:
        return read()
    except Exception:
        log.debug("whoami lookup unavailable", exc_info=True)
        return None


def _utc(dt: datetime) -> str:
    """House timestamp form: UTC, ``Z``-labelled (docs/conventions/time.md)."""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _user_line(store: Store, user_id: int) -> str:
    user = _degraded(lambda: store.get_web_user_by_id(user_id))
    if user is None:
        return f"user: id {user_id} (account row unavailable)"
    return f"user: {user.abbrev} (login: {user.login}, id: {user.id})"


def _credential_lines(store: Store, owner: Principal) -> list[str]:
    """The credential + permission lines for a bound principal."""
    oauth = owner.oauth
    if oauth is not None:
        from precis.oauth.store import grant_label

        label = _degraded(lambda: grant_label(store, oauth.grant_id)) or "(unavailable)"
        return [
            f"credential: OAuth connection '{label}'"
            f" (client: {oauth.client_id}, grant: {oauth.grant_id})",
            f"token: access token {oauth.access_token_id},"
            f" expires {_utc(oauth.expires_at)}",
            "scopes: " + (", ".join(oauth.scopes) or "(none)"),
            (
                "permission: full MCP (all verbs)"
                if "mcp:full" in oauth.scopes
                else "permission: learning calls, learning help, whoami"
            ),
        ]
    if owner.token_id is not None:
        from precis.mcp_identity import list_user_tokens

        tokens = _degraded(lambda: list_user_tokens(store, owner.user_id)) or ()
        token_label = next((t.label for t in tokens if t.id == owner.token_id), None)
        return [
            f"credential: personal MCP token {owner.token_id}"
            + (f" '{token_label}'" if token_label else " (label unavailable)"),
            "permission: learning calls, learning help, whoami",
        ]
    return ["credential: account-bound session (no token detail)"]


def render(store: Store | None) -> str:
    """The whoami body for the currently-bound request, or the in-process call.

    Lines: the authenticated user (abbrev, login, id), the credential and
    its label, the permission scope, then the write attribution every verb
    from this session records (principal + revision actor) and the declared
    MCP client. Anonymous sessions render the shared-credential shape."""
    lines = ["# whoami — this MCP session's identity"]
    owner = current_principal()
    if owner is None or store is None:
        lines.append("user: (anonymous — no authenticated account on this session)")
        lines.append("credential: shared server credential, not a personal session")
        lines.append("permission: shared-credential scope (install / slice / agent)")
    else:
        lines.append(_user_line(store, owner.user_id))
        lines.extend(_credential_lines(store, owner))
    ctx = current_revision_context()
    actor = ctx.actor if ctx is not None else None
    lines.append(f"writes recorded as: {write_principal.current()}")
    lines.append(f"revision actor: {actor or process_actor() or 'agent'}")
    client = tool_ledger.current_client()
    lines.append(f"client: {client or '(none declared)'}")
    return "\n".join(lines)


__all__ = ["CALL", "render"]
