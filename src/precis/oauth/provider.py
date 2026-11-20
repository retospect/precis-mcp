"""SDK OAuth provider with transactional, digest-only connection persistence."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb
from pydantic import AnyHttpUrl, AnyUrl

from precis.errors import BadInput, Denied
from precis.oauth.config import OAuthConfig
from precis.oauth.store import (
    ACCESS_PREFIX,
    CODE_PREFIX,
    REFRESH_PREFIX,
    REQUEST_PREFIX,
    Connection,
    digest,
    resolve_access_token,
    secret_shape,
)

if TYPE_CHECKING:
    from precis.store import Store

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_REQUEST_TTL = timedelta(minutes=10)
_CODE_TTL = timedelta(minutes=5)
_ACCESS_TTL = timedelta(hours=1)
_GRANT_TTL = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Consent:
    """A browser consent challenge, never a bearer credential."""

    request_id: str
    csrf_token: str = field(repr=False)
    client_name: str
    scopes: tuple[str, ...]


class StoredCode(AuthorizationCode):
    grant_id: int


class StoredRefresh(RefreshToken):
    grant_id: int


class StoredAccess(AccessToken):
    grant_id: int


def _denied() -> Denied:
    return Denied(
        "OAuth consent request is invalid or expired",
        next="restart connection setup from your application's connector settings",
    )


class _TokenFailure(Exception):
    """Carry an SDK error through contextlib without its frozen dataclass."""

    def __init__(self, error: TokenError) -> None:
        super().__init__(error.error, error.error_description)
        self.error = error


class _AuthorizationFailure(Exception):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


def _invalid_grant() -> _TokenFailure:
    return _TokenFailure(
        TokenError("invalid_grant", "credential is invalid, expired or revoked")
    )


class OAuthProvider(
    OAuthAuthorizationServerProvider[StoredCode, StoredRefresh, StoredAccess]
):
    """Keep protocol validation in the SDK and race-sensitive state in SQL."""

    def __init__(self, store: Store, config: OAuthConfig) -> None:
        self.store = store
        self.config = config

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self.config.clients.get(client_id)
        if client is None:
            return None
        from precis import icons

        return OAuthClientInformationFull(
            client_id=client.id,
            client_name=client.name,
            # Registration is disabled, so these records are operator-owned;
            # the logo_uri declares the precis mark (served on the issuer)
            # for any connector UI that reads client metadata (gr633600).
            logo_uri=AnyHttpUrl(icons.https_url(self.config.issuer)),
            redirect_uris=[AnyUrl(uri) for uri in client.redirect_uris],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(self.config.scopes),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise RegistrationError(
            "invalid_client_metadata", "client registration is disabled"
        )

    def _params(
        self, client_id: str, params: AuthorizationParams
    ) -> AuthorizationParams:
        client = self.config.clients.get(client_id)
        if client is None or str(params.redirect_uri) not in client.redirect_uris:
            raise AuthorizeError(
                "invalid_request", "unregistered client or redirect URI"
            )
        if params.resource not in (None, self.config.resource):
            raise AuthorizeError(
                "invalid_request", "resource does not match this MCP endpoint"
            )
        if not _CHALLENGE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "S256 PKCE challenge is required")
        scopes = list(dict.fromkeys(params.scopes or self.config.scopes))
        if not scopes or not set(scopes) <= set(self.config.scopes):
            raise AuthorizeError("invalid_scope", "requested scope is not enabled")
        if params.state is not None and len(params.state) > 2048:
            raise AuthorizeError("invalid_request", "state is too long")
        return params.model_copy(
            update={"scopes": scopes, "resource": self.config.resource}
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        params = self._params(client.client_id or "", params)
        try:
            request_id = await anyio.to_thread.run_sync(
                self._create_request, client.client_id, params
            )
        except _AuthorizationFailure as exc:
            raise AuthorizeError("temporarily_unavailable", exc.description) from exc
        return construct_redirect_uri(
            self.config.issuer + "/oauth/consent", request=request_id
        )

    def _create_request(
        self, client_id: str | None, params: AuthorizationParams
    ) -> str:
        raw = REQUEST_PREFIX + secrets.token_urlsafe(32)
        with self.store.pool.connection() as conn, conn.transaction():
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 621409))",
                ("precis-oauth-request:" + str(client_id),),
            ).fetchone()
            if locked is None or not locked[0]:
                raise _AuthorizationFailure("retry connector setup shortly")
            conn.execute("DELETE FROM mcp_oauth_requests WHERE expires_at <= now()")
            count = conn.execute(
                "SELECT count(*) FROM mcp_oauth_requests"
                " WHERE client_id = %s AND consumed_at IS NULL",
                (client_id,),
            ).fetchone()
            if count is not None and count[0] >= 100:
                raise _AuthorizationFailure("too many pending authorization requests")
            conn.execute(
                "INSERT INTO mcp_oauth_requests (request_sha256, client_id, params, expires_at)"
                " VALUES (%s, %s, %s, %s)",
                (
                    digest(raw),
                    client_id,
                    Jsonb(params.model_dump(mode="json")),
                    datetime.now(UTC) + _REQUEST_TTL,
                ),
            )
        return raw

    def _request(self, conn: Any, request_id: str) -> Any:
        if not secret_shape(request_id, REQUEST_PREFIX):
            raise _denied()
        row = conn.execute(
            "SELECT client_id, params, user_id, csrf_sha256 FROM mcp_oauth_requests"
            " WHERE request_sha256 = %s AND consumed_at IS NULL"
            " AND expires_at > now() FOR UPDATE",
            (digest(request_id),),
        ).fetchone()
        if row is None:
            raise _denied()
        try:
            params = self._params(row[0], AuthorizationParams.model_validate(row[1]))
        except (AuthorizeError, ValueError) as exc:
            raise _denied() from exc
        return row, params

    def prepare_consent(self, request_id: str, user_id: int) -> Consent:
        """Bind the request and a fresh nonce to the signed-in account."""
        csrf = secrets.token_urlsafe(32)
        with self.store.pool.connection() as conn, conn.transaction():
            row, params = self._request(conn, request_id)
            user = conn.execute(
                "SELECT id FROM web_users WHERE id = %s AND disabled_at IS NULL",
                (user_id,),
            ).fetchone()
            if user is None or row[2] not in (None, user_id):
                raise _denied()
            conn.execute(
                "UPDATE mcp_oauth_requests SET user_id = %s, csrf_sha256 = %s"
                " WHERE request_sha256 = %s",
                (user_id, digest(csrf), digest(request_id)),
            )
        return Consent(
            request_id,
            csrf,
            self.config.clients[row[0]].name,
            tuple(params.scopes or ()),
        )

    def complete_consent(
        self, request_id: str, user_id: int, csrf_token: str, *, allow: bool, label: str
    ) -> str:
        """Atomically consume user-bound consent, then issue one new connection."""
        with self.store.pool.connection() as conn, conn.transaction():
            row, params = self._request(conn, request_id)
            if (
                row[2] != user_id
                or not row[3]
                or not secrets.compare_digest(row[3], digest(csrf_token))
            ):
                raise _denied()
            user = conn.execute(
                "SELECT id FROM web_users WHERE id = %s AND disabled_at IS NULL FOR SHARE",
                (user_id,),
            ).fetchone()
            if user is None:
                raise _denied()
            label = label.strip() or self.config.clients[row[0]].name
            if len(label) > 100:
                raise BadInput(
                    "connection label must contain 1–100 characters",
                    next="choose a shorter app label",
                )
            conn.execute(
                "UPDATE mcp_oauth_requests SET consumed_at = now(), csrf_sha256 = NULL"
                " WHERE request_sha256 = %s",
                (digest(request_id),),
            )
            if not allow:
                return construct_redirect_uri(
                    str(params.redirect_uri), error="access_denied", state=params.state
                )
            grant = conn.execute(
                "INSERT INTO mcp_oauth_grants (user_id, client_id, label, scopes, resource, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    user_id,
                    row[0],
                    label,
                    params.scopes,
                    self.config.resource,
                    datetime.now(UTC) + _GRANT_TTL,
                ),
            ).fetchone()
            assert grant is not None
            code = CODE_PREFIX + secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO mcp_oauth_codes (code_sha256, grant_id, code_challenge,"
                " redirect_uri, redirect_uri_explicit, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    digest(code),
                    grant[0],
                    params.code_challenge,
                    str(params.redirect_uri),
                    params.redirect_uri_provided_explicitly,
                    datetime.now(UTC) + _CODE_TTL,
                ),
            )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    def _scopes_live(self, client_id: str, scopes: list[str]) -> bool:
        return (
            client_id in self.config.clients
            and bool(scopes)
            and set(scopes) <= set(self.config.scopes)
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredCode | None:
        return await anyio.to_thread.run_sync(
            self._load_code, client.client_id, authorization_code
        )

    def _load_code(self, client_id: str | None, raw: str) -> StoredCode | None:
        if not secret_shape(raw, CODE_PREFIX):
            return None
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT g.id, g.scopes, c.expires_at, c.code_challenge, c.redirect_uri,"
                " c.redirect_uri_explicit, g.user_id FROM mcp_oauth_codes c"
                " JOIN mcp_oauth_grants g ON g.id = c.grant_id"
                " JOIN web_users u ON u.id = g.user_id"
                " WHERE c.code_sha256 = %s AND g.client_id = %s AND g.resource = %s"
                " AND c.consumed_at IS NULL AND c.expires_at > now()"
                " AND g.revoked_at IS NULL AND g.expires_at > now() AND u.disabled_at IS NULL",
                (digest(raw), client_id, self.config.resource),
            ).fetchone()
        if row is None or not self._scopes_live(client_id or "", row[1]):
            return None
        return StoredCode(
            code=raw,
            grant_id=int(row[0]),
            scopes=row[1],
            expires_at=row[2].timestamp(),
            client_id=client_id or "",
            code_challenge=row[3],
            redirect_uri=AnyUrl(row[4]),
            redirect_uri_provided_explicitly=row[5],
            resource=self.config.resource,
            subject=str(row[6]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredCode
    ) -> OAuthToken:
        try:
            return await anyio.to_thread.run_sync(
                self._exchange_code, client.client_id, authorization_code
            )
        except _TokenFailure as exc:
            raise exc.error from exc

    def _exchange_code(self, client_id: str | None, code: StoredCode) -> OAuthToken:
        with self.store.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT g.id, g.scopes, g.expires_at FROM mcp_oauth_codes c"
                " JOIN mcp_oauth_grants g ON g.id = c.grant_id"
                " JOIN web_users u ON u.id = g.user_id"
                " WHERE c.code_sha256 = %s AND g.id = %s AND g.client_id = %s"
                " AND g.resource = %s AND c.consumed_at IS NULL AND c.expires_at > now()"
                " AND g.revoked_at IS NULL AND g.expires_at > now() AND u.disabled_at IS NULL"
                " FOR UPDATE OF g, c",
                (digest(code.code), code.grant_id, client_id, self.config.resource),
            ).fetchone()
            if row is None or not self._scopes_live(client_id or "", row[1]):
                raise _invalid_grant()
            conn.execute(
                "UPDATE mcp_oauth_codes SET consumed_at = now() WHERE code_sha256 = %s",
                (digest(code.code),),
            )
            return self._issue_pair(conn, int(row[0]), row[1], row[2])

    def _issue_pair(
        self, conn: Any, grant_id: int, scopes: list[str], expires: datetime
    ) -> OAuthToken:
        now = datetime.now(UTC)
        access_expires = min(now + _ACCESS_TTL, expires)
        access, refresh = (
            ACCESS_PREFIX + secrets.token_urlsafe(32),
            REFRESH_PREFIX + secrets.token_urlsafe(32),
        )
        for kind, raw, deadline in (
            ("access", access, access_expires),
            ("refresh", refresh, expires),
        ):
            conn.execute(
                "INSERT INTO mcp_oauth_tokens (token_sha256, grant_id, token_kind, scopes, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (digest(raw), grant_id, kind, scopes, deadline),
            )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=max(1, int((access_expires - now).total_seconds())),
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefresh | None:
        return await anyio.to_thread.run_sync(
            self._load_refresh, client.client_id, refresh_token
        )

    def _load_refresh(self, client_id: str | None, raw: str) -> StoredRefresh | None:
        if not secret_shape(raw, REFRESH_PREFIX):
            return None
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT g.id, t.scopes, t.expires_at, g.user_id FROM mcp_oauth_tokens t"
                " JOIN mcp_oauth_grants g ON g.id = t.grant_id JOIN web_users u ON u.id = g.user_id"
                " WHERE t.token_sha256 = %s AND t.token_kind = 'refresh'"
                " AND g.client_id = %s AND g.resource = %s AND t.expires_at > now()"
                " AND g.expires_at > now() AND g.revoked_at IS NULL AND u.disabled_at IS NULL",
                (digest(raw), client_id, self.config.resource),
            ).fetchone()
        # Consumed refresh rows deliberately load: exchange must detect replay
        # and commit grant-family revocation, not merely return invalid_grant.
        if row is None or not self._scopes_live(client_id or "", row[1]):
            return None
        return StoredRefresh(
            token=raw,
            grant_id=int(row[0]),
            scopes=row[1],
            client_id=client_id or "",
            expires_at=int(row[2].timestamp()),
            resource=self.config.resource,
            subject=str(row[3]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefresh,
        scopes: list[str],
    ) -> OAuthToken:
        try:
            return await anyio.to_thread.run_sync(
                self._exchange_refresh, client.client_id, refresh_token, scopes
            )
        except _TokenFailure as exc:
            raise exc.error from exc

    def _exchange_refresh(
        self, client_id: str | None, token: StoredRefresh, scopes: list[str]
    ) -> OAuthToken:
        result = None
        with self.store.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT g.id, t.scopes, g.expires_at, t.consumed_at FROM mcp_oauth_tokens t"
                " JOIN mcp_oauth_grants g ON g.id = t.grant_id JOIN web_users u ON u.id = g.user_id"
                " WHERE t.token_sha256 = %s AND t.token_kind = 'refresh' AND g.id = %s"
                " AND g.client_id = %s AND g.resource = %s AND t.expires_at > now()"
                " AND g.expires_at > now() AND g.revoked_at IS NULL AND u.disabled_at IS NULL"
                " FOR UPDATE OF g, t",
                (digest(token.token), token.grant_id, client_id, self.config.resource),
            ).fetchone()
            if row is None or not self._scopes_live(client_id or "", row[1]):
                raise _invalid_grant()
            if row[3] is not None:
                conn.execute(
                    "UPDATE mcp_oauth_grants SET revoked_at = now() WHERE id = %s",
                    (row[0],),
                )
            elif not scopes or not set(scopes) <= set(row[1]):
                raise _TokenFailure(
                    TokenError("invalid_scope", "refresh cannot add permissions")
                )
            else:
                conn.execute(
                    "UPDATE mcp_oauth_tokens SET consumed_at = now() WHERE token_sha256 = %s",
                    (digest(token.token),),
                )
                conn.execute(
                    "UPDATE mcp_oauth_tokens SET consumed_at = now()"
                    " WHERE grant_id = %s AND token_kind = 'access' AND consumed_at IS NULL",
                    (row[0],),
                )
                result = self._issue_pair(
                    conn, int(row[0]), list(dict.fromkeys(scopes)), row[2]
                )
        # Raise after COMMIT: raising within transaction would undo replay revocation.
        if result is None:
            raise _invalid_grant()
        return result

    async def load_access_token(self, token: str) -> StoredAccess | None:
        identity = await anyio.to_thread.run_sync(
            resolve_access_token, self.store, token, self.config
        )
        if identity is None:
            return None
        return StoredAccess(
            token=token,
            grant_id=identity.grant_id,
            client_id=identity.client_id,
            scopes=list(identity.scopes),
            expires_at=int(identity.expires_at.timestamp()),
            resource=self.config.resource,
            subject=str(identity.user_id),
        )

    async def revoke_token(self, token: StoredAccess | StoredRefresh) -> None:
        await anyio.to_thread.run_sync(
            self.revoke_by_token, token.client_id, token.token
        )

    def revoke_by_token(self, client_id: str, raw: str) -> None:
        """Revoke a known client's grant, including through an expired token."""
        if client_id not in self.config.clients or not (
            secret_shape(raw, ACCESS_PREFIX) or secret_shape(raw, REFRESH_PREFIX)
        ):
            return
        with self.store.pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE mcp_oauth_grants g SET revoked_at = now()"
                " WHERE g.client_id = %s AND g.resource = %s AND g.revoked_at IS NULL"
                " AND EXISTS (SELECT 1 FROM mcp_oauth_tokens t"
                " WHERE t.grant_id = g.id AND t.token_sha256 = %s)",
                (client_id, self.config.resource, digest(raw)),
            )

    def list_grants(self, user_id: int) -> list[Connection]:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT g.id, g.client_id, g.label, g.scopes, g.created_at, g.expires_at, g.revoked_at"
                " FROM mcp_oauth_grants g JOIN web_users u ON u.id = g.user_id"
                " WHERE g.user_id = %s AND u.disabled_at IS NULL ORDER BY g.created_at DESC, g.id DESC",
                (user_id,),
            ).fetchall()
        return [
            Connection(
                int(r[0]),
                self.config.clients[r[1]].name if r[1] in self.config.clients else r[1],
                r[2],
                tuple(r[3]),
                r[4],
                r[5],
                r[6],
            )
            for r in rows
        ]

    def revoke_grant(self, user_id: int, grant_id: int) -> bool:
        with self.store.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE mcp_oauth_grants g SET revoked_at = now()"
                " WHERE g.id = %s AND g.user_id = %s AND g.revoked_at IS NULL"
                " AND EXISTS (SELECT 1 FROM web_users u WHERE u.id = %s AND u.disabled_at IS NULL) RETURNING g.id",
                (grant_id, user_id, user_id),
            ).fetchone()
        return row is not None
