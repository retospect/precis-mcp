"""Revocable per-user and per-client MCP credentials; only SHA-256 digests
are stored.

The raw credential is returned by mint once and never vaulted, logged or
embedded in a URL. Personal tokens identify stable web_users IDs
independently of mutable login/display names. Trusted ASGI stamps are
consumed per request, never retained on FastMCP's reusable session object.

Capability tokens (gr595554) are the third secret class: they name a
consumer (a claude.ai chat connector, a podcast listener), not a person.
The owning account only mints, lists and revokes them; a presented token
never binds a personal principal. Each carries a ``scopes`` allow-list —
``read`` (the full ``get``/``search`` read surface, the mint default),
``read:<kind>``/``write:<kind>`` for a named kind, and ``feed`` for the
podcast ``?t=`` credential that was this class's informal first instance.
On the MCP transport they ride ``Authorization: Bearer`` only — the MCP
authorization rules and the claude.ai connector's own guidance forbid
credentials in the query string; URL-embedded carriage stays on the
/podcast feed routes, whose clients cannot set headers.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.errors import BadInput, Denied
from precis.user_context import Principal

if TYPE_CHECKING:
    from precis.store import Store

SCOPE_USER_ID = "precis.authenticated_user_id"
SCOPE_TOKEN_ID = "precis.authenticated_token_id"
SCOPE_OAUTH_IDENTITY = "precis.authenticated_oauth_identity"
SCOPE_CAPABILITY = "precis.authenticated_capability"
TOKEN_PREFIX = "precis_user_"
_TOKEN_SHAPE = re.compile(r"precis_user_[A-Za-z0-9_-]{43,128}\Z")
CAP_TOKEN_PREFIX = "precis_cap_"
_CAP_TOKEN_SHAPE = re.compile(r"precis_cap_[A-Za-z0-9_-]{43,128}\Z")

#: Verbs a capability scope may name — the seven-verb surface, nothing else.
_READ_VERBS = frozenset({"get", "search"})
_WRITE_VERBS = frozenset({"put", "edit", "delete", "tag", "link"})
_ALL_VERBS = _READ_VERBS | _WRITE_VERBS

#: ``read:<kind>``/``write:<kind>`` kind spelling — syntactic only: an
#: unknown or retired kind simply never matches a call.
_SCOPE_KIND = re.compile(r"[a-z0-9_-]{1,64}\Z")

#: The mint default: the full read surface.
DEFAULT_CAP_SCOPES: tuple[str, ...] = ("read",)


@dataclass(frozen=True, slots=True)
class UserToken:
    """Credential metadata safe to show on the owning account page."""

    id: int
    user_id: int
    label: str
    created_at: datetime
    revoked_at: datetime | None


def mint_user_token(store: Store, user_id: int, label: str) -> tuple[int, str]:
    """Mint one independent 256-bit token for an enabled account."""
    label = label.strip()
    if not label or len(label) > 100:
        raise BadInput(
            "MCP token label must contain 1–100 characters",
            next="provide a short label identifying the client using this token",
        )
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO web_user_mcp_tokens (user_id, label, token_sha256)"
                " SELECT id, %s, %s FROM web_users"
                " WHERE id = %s AND disabled_at IS NULL RETURNING id",
                (label, digest, user_id),
            ).fetchone()
    if row is None:
        raise Denied(
            "cannot mint a token for an inactive account",
            next="sign in with an enabled account at /account",
        )
    return int(row[0]), raw


def list_user_tokens(store: Store, user_id: int) -> list[UserToken]:
    """List only the requested owner's credential metadata, newest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.id, t.user_id, t.label, t.created_at, t.revoked_at"
            " FROM web_user_mcp_tokens t JOIN web_users u ON u.id = t.user_id"
            " WHERE t.user_id = %s AND u.disabled_at IS NULL"
            " ORDER BY t.created_at DESC, t.id DESC",
            (user_id,),
        ).fetchall()
    return [
        UserToken(int(r[0]), int(r[1]), r[2], r[3].astimezone(UTC), r[4]) for r in rows
    ]


def revoke_user_token(store: Store, user_id: int, token_id: int) -> bool:
    """Revoke one active, owned token; foreign and missing IDs are identical."""
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "UPDATE web_user_mcp_tokens SET revoked_at = now()"
                " WHERE id = %s AND user_id = %s AND revoked_at IS NULL"
                " AND EXISTS (SELECT 1 FROM web_users u"
                " WHERE u.id = %s AND u.disabled_at IS NULL) RETURNING id",
                (token_id, user_id, user_id),
            ).fetchone()
    return row is not None


def resolve_user_token(store: Store, raw: str) -> Principal | None:
    """Resolve a live token/account without retaining the raw credential."""
    if not _TOKEN_SHAPE.fullmatch(raw):
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.user_id, t.id FROM web_user_mcp_tokens t"
            " JOIN web_users u ON u.id = t.user_id"
            " WHERE t.token_sha256 = %s AND t.revoked_at IS NULL"
            " AND u.disabled_at IS NULL",
            (digest,),
        ).fetchone()
    return Principal(int(row[0]), int(row[1])) if row else None


@dataclass(frozen=True, slots=True)
class CapabilityToken:
    """Owned credential metadata safe to show on the minter's account page."""

    id: int
    user_id: int
    label: str
    scopes: tuple[str, ...]
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class CapabilityIdentity:
    """The validated consumer stamp — never a personal :class:`Principal`."""

    token_id: int
    user_id: int
    label: str
    scopes: tuple[str, ...]


def parse_capability_scopes(text: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate a scope allow-list; the empty form falls back to the default.

    Entries: ``read`` (every kind's ``get``/``search`` surface),
    ``read:<kind>`` (the read verbs on one kind), ``write:<kind>``
    (read + ``put``/``edit``/``delete``/``tag``/``link`` on one kind —
    there is no whole-corpus write capability) and ``feed`` (the
    podcast ``?t=`` credential — no MCP verb).
    """
    items = text.split(",") if isinstance(text, str) else list(text)
    scopes = tuple(s.strip() for s in items if s.strip())
    if not scopes:
        return DEFAULT_CAP_SCOPES
    for scope in scopes:
        head, _, kind = scope.partition(":")
        valid = (kind == "" and scope in ("read", "feed")) or (
            head in ("read", "write") and bool(_SCOPE_KIND.fullmatch(kind))
        )
        if not valid:
            raise BadInput(
                f"unknown capability scope {scope!r}",
                next="use read, read:<kind>, write:<kind> or feed — comma-separated",
            )
    return tuple(dict.fromkeys(scopes))


def _scope_covers(scope: str, verb: str, kind: str) -> bool:
    """One allow-list entry against one requested (verb, kind) pair."""
    head, _, named = scope.partition(":")
    if head == "read":
        return verb in _READ_VERBS and (named == "" or named == kind)
    if head == "write":
        # ``write:<kind>`` implies read on the same kind — a grant that
        # can write records it cannot read is a footgun, not a boundary.
        return verb in _ALL_VERBS and named == kind
    return False


def capability_call_allowed(
    scopes: tuple[str, ...], verb: str, kinds: set[str] | None
) -> bool:
    """The dispatch check: every named kind must be covered for ``verb``.

    ``kinds=None`` is the whole-surface call (e.g. ``search`` with no
    ``kind=``) — only the bare ``read`` entry covers it. ``feed`` grants
    no MCP verb; it is judged by the podcast route, not here.
    """
    if verb not in _ALL_VERBS:
        return False
    if kinds is None:
        return verb in _READ_VERBS and "read" in scopes
    return all(
        any(_scope_covers(scope, verb, kind) for scope in scopes) for kind in kinds
    )


def mint_capability_token(
    store: Store, user_id: int, label: str, scopes: tuple[str, ...]
) -> tuple[int, str]:
    """Mint one independent 256-bit consumer token for an enabled account."""
    label = label.strip()
    if not label or len(label) > 100:
        raise BadInput(
            "capability token label must contain 1–100 characters",
            next="provide a short label naming the consumer, e.g. claude.ai chat",
        )
    if not scopes:
        raise BadInput(
            "capability token needs at least one scope",
            next="use read, read:<kind>, write:<kind> or feed",
        )
    raw = CAP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO web_user_capability_tokens"
                " (user_id, label, token_sha256, scopes)"
                " SELECT id, %s, %s, %s FROM web_users"
                " WHERE id = %s AND disabled_at IS NULL RETURNING id",
                (label, digest, list(scopes), user_id),
            ).fetchone()
    if row is None:
        raise Denied(
            "cannot mint a token for an inactive account",
            next="sign in with an enabled account at /account",
        )
    return int(row[0]), raw


def list_capability_tokens(store: Store, user_id: int) -> list[CapabilityToken]:
    """List only the requested owner's credential metadata, newest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.id, t.user_id, t.label, t.scopes, t.created_at, t.revoked_at"
            " FROM web_user_capability_tokens t JOIN web_users u ON u.id = t.user_id"
            " WHERE t.user_id = %s AND u.disabled_at IS NULL"
            " ORDER BY t.created_at DESC, t.id DESC",
            (user_id,),
        ).fetchall()
    return [
        CapabilityToken(
            int(r[0]), int(r[1]), r[2], tuple(r[3]), r[4].astimezone(UTC), r[5]
        )
        for r in rows
    ]


def revoke_capability_token(store: Store, user_id: int, token_id: int) -> bool:
    """Revoke one active, owned token; foreign and missing IDs are identical."""
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "UPDATE web_user_capability_tokens SET revoked_at = now()"
                " WHERE id = %s AND user_id = %s AND revoked_at IS NULL"
                " AND EXISTS (SELECT 1 FROM web_users u"
                " WHERE u.id = %s AND u.disabled_at IS NULL) RETURNING id",
                (token_id, user_id, user_id),
            ).fetchone()
    return row is not None


def resolve_capability_token(store: Store, raw: str) -> CapabilityIdentity | None:
    """Resolve a live consumer token without retaining the raw credential."""
    if not _CAP_TOKEN_SHAPE.fullmatch(raw):
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.id, t.user_id, t.label, t.scopes"
            " FROM web_user_capability_tokens t"
            " JOIN web_users u ON u.id = t.user_id"
            " WHERE t.token_sha256 = %s AND t.revoked_at IS NULL"
            " AND u.disabled_at IS NULL",
            (digest,),
        ).fetchone()
    if row is None:
        return None
    return CapabilityIdentity(int(row[0]), int(row[1]), str(row[2]), tuple(row[3]))
