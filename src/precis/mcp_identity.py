"""Revocable per-user MCP credentials and client capability tokens; only SHA-256 digests are stored.

The raw credential is returned by mint once and never vaulted, logged or
embedded in a URL. Tokens identify stable web_users IDs independently of
mutable login/display names. Trusted ASGI stamps are consumed per request,
never retained on FastMCP's reusable session object.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.errors import BadInput, Denied
from precis.user_context import READ_VERBS, WRITE_VERBS, Capability, Principal

if TYPE_CHECKING:
    from precis.store import Store

SCOPE_USER_ID = "precis.authenticated_user_id"
SCOPE_TOKEN_ID = "precis.authenticated_token_id"
SCOPE_OAUTH_IDENTITY = "precis.authenticated_oauth_identity"
SCOPE_CAPABILITY = "precis.authenticated_capability"
TOKEN_PREFIX = "precis_user_"
#: Class 3 (gr595554): a per-consumer capability token, never a person's.
CAP_PREFIX = "precis_cap_"
_TOKEN_SHAPE = re.compile(r"precis_(?:user|cap)_[A-Za-z0-9_-]{43,128}\Z")
_KIND_SHAPE = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")


@dataclass(frozen=True, slots=True)
class UserToken:
    """Credential metadata safe to show on the owning account page."""

    id: int
    user_id: int
    label: str
    created_at: datetime
    revoked_at: datetime | None
    token_class: str = "personal"
    capability: Capability | None = None


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


def _capability(
    token_class: str, verbs: list[str] | None, kinds: list[str] | None, feed: bool
) -> Capability | None:
    if token_class != "capability":
        return None
    return Capability(
        frozenset(verbs or READ_VERBS), frozenset(kinds) if kinds else None, feed
    )


def mint_capability_token(
    store: Store,
    user_id: int,
    label: str,
    *,
    kinds: list[str] | None = None,
    write: bool = False,
    feed: bool = False,
) -> tuple[int, str]:
    """Mint a client capability token (class 3) for one consumer.

    No ``kinds`` = the default read surface (every non-private kind, get and
    search). Naming kinds restricts the token to them; ``write`` additionally
    admits the write verbs on those kinds and needs kinds named. ``feed``
    admits the token as the podcast feed credential. Shown once, digest only.
    """
    label = label.strip()
    if not label or len(label) > 100:
        raise BadInput(
            "token label must contain 1–100 characters",
            next="name the consumer using this token, for example 'claude.ai chat'",
        )
    names = sorted({k.strip().lower() for k in kinds or [] if k.strip()})
    if any(not _KIND_SHAPE.fullmatch(k) for k in names):
        raise BadInput(
            "kind names must be lowercase words",
            next="list kinds such as paper, memory, markdown",
        )
    if write and not names:
        raise BadInput(
            "write access needs explicit kinds",
            next="name the kinds the token may write, or drop write access",
        )
    verbs = sorted(READ_VERBS | (WRITE_VERBS if write else frozenset()))
    raw = CAP_PREFIX + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO web_user_mcp_tokens (user_id, label, token_sha256,"
                " token_class, allow_verbs, allow_kinds, feed_access)"
                " SELECT id, %s, %s, 'capability', %s, %s, %s FROM web_users"
                " WHERE id = %s AND disabled_at IS NULL RETURNING id",
                (label, digest, verbs, names or None, feed, user_id),
            ).fetchone()
    if row is None:
        raise Denied(
            "cannot mint a token for an inactive account",
            next="sign in with an enabled account at /account",
        )
    return int(row[0]), raw


def resolve_feed_token(store: Store, raw: str) -> bool:
    """True when ``raw`` is a live capability token admitted to the feed."""
    if not raw.startswith(CAP_PREFIX) or not _TOKEN_SHAPE.fullmatch(raw):
        return False
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM web_user_mcp_tokens t JOIN web_users u ON u.id = t.user_id"
            " WHERE t.token_sha256 = %s AND t.token_class = 'capability'"
            " AND t.feed_access AND t.revoked_at IS NULL AND u.disabled_at IS NULL",
            (digest,),
        ).fetchone()
    return row is not None


def list_user_tokens(store: Store, user_id: int) -> list[UserToken]:
    """List only the requested owner's credential metadata, newest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.id, t.user_id, t.label, t.created_at, t.revoked_at,"
            " t.token_class, t.allow_verbs, t.allow_kinds, t.feed_access"
            " FROM web_user_mcp_tokens t JOIN web_users u ON u.id = t.user_id"
            " WHERE t.user_id = %s AND u.disabled_at IS NULL"
            " ORDER BY t.created_at DESC, t.id DESC",
            (user_id,),
        ).fetchall()
    return [
        UserToken(
            int(r[0]),
            int(r[1]),
            r[2],
            r[3].astimezone(UTC),
            r[4],
            r[5],
            _capability(r[5], r[6], r[7], bool(r[8])),
        )
        for r in rows
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
            "SELECT t.user_id, t.id, t.token_class, t.allow_verbs, t.allow_kinds,"
            " t.feed_access FROM web_user_mcp_tokens t"
            " JOIN web_users u ON u.id = t.user_id"
            " WHERE t.token_sha256 = %s AND t.revoked_at IS NULL"
            " AND u.disabled_at IS NULL",
            (digest,),
        ).fetchone()
    if row is None:
        return None
    return Principal(
        int(row[0]),
        int(row[1]),
        capability=_capability(row[2], row[3], row[4], bool(row[5])),
    )
