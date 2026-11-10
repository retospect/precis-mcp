"""Persisted OAuth identity checks shared by the web and MCP processes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis.errors import Denied, Upstream

if TYPE_CHECKING:
    from precis.oauth.config import OAuthConfig
    from precis.store import Store

ACCESS_PREFIX = "precis_oauth_"
REFRESH_PREFIX = "precis_refresh_"
CODE_PREFIX = "precis_code_"
REQUEST_PREFIX = "precis_auth_"
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def secret_shape(raw: str, prefix: str) -> bool:
    """Reject malformed bearer input before hashing or querying the database."""
    return raw.startswith(prefix) and bool(_SECRET.fullmatch(raw[len(prefix) :]))


def digest(raw: str) -> str:
    """The persisted representation of an opaque credential or consent nonce."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OAuthIdentity:
    """Trusted request identity; token IDs never share the PAT namespace."""

    user_id: int
    grant_id: int
    access_token_id: int
    client_id: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Connection:
    """Public-to-owner metadata for one independent app connection."""

    id: int
    client_name: str
    label: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


def _identity(row: Any, config: OAuthConfig) -> OAuthIdentity | None:
    if row is None or row[3] not in config.clients:
        return None
    scopes = tuple(row[4])
    if not scopes or not set(scopes) <= set(config.scopes):
        return None
    return OAuthIdentity(int(row[0]), int(row[1]), int(row[2]), row[3], scopes, row[5])


_ACTIVE_ACCESS = (
    "SELECT g.user_id, g.id, t.id, g.client_id, t.scopes, t.expires_at, u.abbrev"
    " FROM mcp_oauth_tokens t JOIN mcp_oauth_grants g ON g.id = t.grant_id"
    " JOIN web_users u ON u.id = g.user_id"
    " WHERE t.token_kind = 'access' AND t.consumed_at IS NULL"
    " AND t.expires_at > now() AND g.expires_at > now()"
    " AND g.revoked_at IS NULL AND u.disabled_at IS NULL"
    " AND g.resource = %s"
)


def resolve_access_token(
    store: Store, raw: str, config: OAuthConfig
) -> OAuthIdentity | None:
    """Resolve an active account/grant/token for this precise MCP resource."""
    if not secret_shape(raw, ACCESS_PREFIX):
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            _ACTIVE_ACCESS + " AND t.token_sha256 = %s",
            (config.resource, digest(raw)),
        ).fetchone()
    return _identity(row, config)


def revalidate_identity(
    store: Store, identity: OAuthIdentity, config: OAuthConfig
) -> str:
    """Recheck grant, expiry and exact scopes immediately before dispatch."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                _ACTIVE_ACCESS + " AND t.id = %s",
                (config.resource, identity.access_token_id),
            ).fetchone()
    except Exception as exc:
        raise Upstream(
            "OAuth account verification is unavailable",
            next="retry when the account database is available",
        ) from exc
    live = _identity(row, config)
    if row is None or live is None or live != identity:
        raise Denied(
            "OAuth connection or credential is no longer active",
            next="reconnect this application from /account/connections",
        )
    return str(row[6])
