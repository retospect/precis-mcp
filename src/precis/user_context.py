"""Authenticated personal-data identity, separate from revision attribution.

HTTP MCP binds only the bearer gate's request-scope stamps; trusted web
routes may bind their authenticated account directly. A session, actor,
environment variable or wire argument never supplies a personal owner.
The account and optional token are rechecked for each operation so a
long-lived MCP session cannot outlive revocation. ContextVar scopes reset
on every exit and travel through anyio's worker-thread context copy.
Personal MCP dispatch also maps the live stable account to its current
legacy revision actor, preserving existing owner checks without claiming
that an agent's writes were typed by a human.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.errors import Denied, Upstream

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class Principal:
    """Stable account identity, with an optional revocable MCP credential."""

    user_id: int
    token_id: int | None = None


_PRINCIPAL: ContextVar[Principal | None] = ContextVar("precis_user", default=None)


def current_principal() -> Principal | None:
    """The current trusted principal, or no authenticated personal owner."""
    return _PRINCIPAL.get()


@contextmanager
def user_scope(user_id: int | None, token_id: int | None = None) -> Iterator[None]:
    """Bind a trusted owner; ``None`` explicitly clears any ambient owner."""
    if user_id is not None and (
        type(user_id) is not int
        or user_id <= 0
        or (token_id is not None and (type(token_id) is not int or token_id <= 0))
    ):
        raise Denied(
            "invalid authenticated user identity",
            next="authenticate with a personal MCP token from /account",
        )
    token = _PRINCIPAL.set(
        Principal(user_id, token_id) if user_id is not None else None
    )
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)


def require_user(store: Store) -> int:
    """Return a live owner, refusing missing, disabled or revoked identity."""
    return _require_account(store)[0]


def _require_account(store: Store | None) -> tuple[int, str]:
    """Revalidate identity and resolve mutable attribution in the same read."""
    principal = current_principal()
    if principal is None:
        raise Denied(
            "personal data requires an authenticated user",
            next="mint a personal MCP token at /account and use it as the bearer token",
        )
    sql = "SELECT id, abbrev FROM web_users WHERE id = %s AND disabled_at IS NULL"
    params: tuple[int, ...] = (principal.user_id,)
    if principal.token_id is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM web_user_mcp_tokens t"
            " WHERE t.id = %s AND t.user_id = web_users.id"
            " AND t.revoked_at IS NULL)"
        )
        params += (principal.token_id,)
    try:
        if store is None:
            raise RuntimeError("account store unavailable")
        with store.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
    except Exception as exc:
        raise Upstream(
            "authenticated user verification is unavailable",
            next="retry when the account database is available",
        ) from exc
    if row is None:
        raise Denied(
            "authenticated user or token is no longer active",
            next="sign in at /account and mint a new personal MCP token",
        )
    return int(row[0]), str(row[1])


@contextmanager
def user_actor_scope(store: Store | None) -> Iterator[None]:
    """Map a live principal to legacy owner checks; preserve ambient fields.

    Anonymous install/slice callers retain their existing attribution.
    Failure to revalidate a personal caller refuses the entire tool call,
    so it can never silently acquire the generic-agent legacy fallback.
    """
    if current_principal() is None:
        yield
        return
    from precis.acting_user import actor_for
    from precis.store.revision_context import revision_context

    _user_id, abbrev = _require_account(store)
    with revision_context(actor=actor_for(abbrev)):
        yield
