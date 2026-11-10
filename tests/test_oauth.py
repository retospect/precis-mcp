"""Real-Postgres OAuth consent, credential races and per-connection isolation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from pydantic import AnyUrl

from precis.errors import BadInput, Denied
from precis.oauth.config import OAuthClient, OAuthConfig
from precis.oauth.provider import OAuthProvider
from precis.oauth.store import digest, resolve_access_token, revalidate_identity
from precis.store import Store
from precis.users import hash_password

_VERIFIER = "v" * 64
_REDIRECT = "https://client.example/callback"


@pytest.fixture
def oauth_config() -> OAuthConfig:
    return OAuthConfig(
        issuer="https://auth.example",
        resource="https://mcp.example/mcp",
        clients={
            "first": OAuthClient("first", "First app", (_REDIRECT,)),
            "second": OAuthClient("second", "Second app", (_REDIRECT,)),
        },
        scopes=("learning", "mcp:full"),
    )


def _user(store: Store, name: str = "alice") -> Any:
    return store.create_web_user(
        login=name, abbrev=name[:2], password=hash_password("password")
    )


def _params(
    config: OAuthConfig, scopes: list[str] | None = None
) -> AuthorizationParams:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(_VERIFIER.encode("utf-8")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return AuthorizationParams(
        state="return-to-client",
        scopes=scopes or ["learning"],
        code_challenge=challenge,
        redirect_uri=AnyUrl(_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=config.resource,
    )


def _request(
    provider: OAuthProvider, client_id: str = "first", scopes: list[str] | None = None
) -> str:
    client = asyncio.run(provider.get_client(client_id))
    assert client is not None
    url = asyncio.run(provider.authorize(client, _params(provider.config, scopes)))
    return parse_qs(urlsplit(url).query)["request"][0]


def _code(
    provider: OAuthProvider,
    user_id: int,
    client_id: str = "first",
    scopes: list[str] | None = None,
) -> tuple[Any, Any]:
    request_id = _request(provider, client_id, scopes)
    consent = provider.prepare_consent(request_id, user_id)
    url = provider.complete_consent(
        request_id, user_id, consent.csrf_token, allow=True, label="My app"
    )
    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["return-to-client"]
    client = asyncio.run(provider.get_client(client_id))
    assert client is not None
    code = asyncio.run(provider.load_authorization_code(client, query["code"][0]))
    assert code is not None
    return client, code


def _tokens(
    provider: OAuthProvider,
    user_id: int,
    client_id: str = "first",
    scopes: list[str] | None = None,
) -> Any:
    client, code = _code(provider, user_id, client_id, scopes)
    return asyncio.run(provider.exchange_authorization_code(client, code))


def test_oauth_config_is_explicit_and_supports_additional_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ISSUER", "RESOURCE", "CLIENTS", "SCOPES"):
        monkeypatch.delenv("PRECIS_OAUTH_" + name, raising=False)
    assert OAuthConfig.from_env() is None
    monkeypatch.setenv("PRECIS_OAUTH_ISSUER", "https://auth.example/")
    with pytest.raises(ValueError, match="requires both"):
        OAuthConfig.from_env()
    monkeypatch.setenv("PRECIS_OAUTH_RESOURCE", "https://mcp.example/mcp")
    monkeypatch.setenv(
        "PRECIS_OAUTH_CLIENTS",
        json.dumps([{"id": "other", "name": "Other", "redirect_uris": [_REDIRECT]}]),
    )
    config = OAuthConfig.from_env()
    assert config is not None and config.issuer == "https://auth.example"
    assert config.scopes == ("mcp:full",)
    assert set(config.clients) == {"precis-claude", "other"}
    monkeypatch.setenv("PRECIS_OAUTH_SCOPES", "learning mcp:full")
    config = OAuthConfig.from_env()
    assert config is not None and config.scopes == ("learning", "mcp:full")
    monkeypatch.setenv("PRECIS_OAUTH_SCOPES", "mcp:write")
    with pytest.raises(ValueError, match="scopes"):
        OAuthConfig.from_env()


@pytest.mark.parametrize(
    "uri",
    [
        "http://auth.example",
        "https://" + ":".join(("u", "p@auth.example")),
        "https://auth.example/#x",
        "https://auth.example/?x=y",
        "https://*.example",
        "https://auth.example/\\evil",
    ],
)
def test_oauth_config_rejects_unsafe_urls(oauth_config: OAuthConfig, uri: str) -> None:
    with pytest.raises(ValueError):
        replace(oauth_config, issuer=uri)


@pytest.mark.parametrize(
    "records",
    [
        "{}",
        "null",
        '[{"id":"x"}]',
        '[{"id":"x","name":"x","redirect_uris":"https://x.example"}]',
        '[{"id":"precis-claude","name":"x","redirect_uris":["https://x.example"]}]',
    ],
)
def test_oauth_config_rejects_invalid_client_json(
    monkeypatch: pytest.MonkeyPatch, records: str
) -> None:
    monkeypatch.setenv("PRECIS_OAUTH_ISSUER", "https://auth.example")
    monkeypatch.setenv("PRECIS_OAUTH_RESOURCE", "https://mcp.example/mcp")
    monkeypatch.setenv("PRECIS_OAUTH_CLIENTS", records)
    with pytest.raises(ValueError):
        OAuthConfig.from_env()


@pytest.mark.parametrize(
    (
        "issuer",
        "resource",
        "redirect",
        "expected_issuer",
        "expected_resource",
        "expected_redirect",
    ),
    [
        (
            "https://AUTH.example:443/",
            "https://MCP.example:443/mcp",
            "https://CLIENT.example:443",
            "https://auth.example",
            "https://mcp.example/mcp",
            "https://client.example/",
        ),
        (
            "https://AUTH.example:8443/",
            "https://MCP.example:8443/mcp",
            "https://CLIENT.example:8443/callback",
            "https://auth.example:8443",
            "https://mcp.example:8443/mcp",
            "https://client.example:8443/callback",
        ),
    ],
)
def test_oauth_config_normalizes_urls_once_at_environment_boundary(
    monkeypatch: pytest.MonkeyPatch,
    issuer: str,
    resource: str,
    redirect: str,
    expected_issuer: str,
    expected_resource: str,
    expected_redirect: str,
) -> None:
    monkeypatch.setenv("PRECIS_OAUTH_ISSUER", issuer)
    monkeypatch.setenv("PRECIS_OAUTH_RESOURCE", resource)
    monkeypatch.delenv("PRECIS_OAUTH_SCOPES", raising=False)
    monkeypatch.setenv(
        "PRECIS_OAUTH_CLIENTS",
        json.dumps(
            [{"id": "normalized", "name": "Normalized", "redirect_uris": [redirect]}]
        ),
    )
    config = OAuthConfig.from_env()
    assert config is not None
    assert config.issuer == expected_issuer
    assert config.resource == expected_resource
    assert config.clients["normalized"].redirect_uris == (expected_redirect,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://AUTH.example"),
        ("issuer", "https://auth.example:443"),
        ("resource", "https://MCP.example/mcp"),
        ("resource", "https://mcp.example:443/mcp"),
        ("redirect", "https://CLIENT.example/callback"),
        ("redirect", "https://client.example:443/callback"),
        ("redirect", "https://client.example"),
    ],
)
def test_oauth_config_direct_construction_refuses_noncanonical_urls(
    oauth_config: OAuthConfig, field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        if field == "redirect":
            replace(
                oauth_config, clients={"first": OAuthClient("first", "First", (value,))}
            )
        elif field == "issuer":
            replace(oauth_config, issuer=value)
        else:
            replace(oauth_config, resource=value)


def test_two_users_and_apps_revoke_independently(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    alice, bob = _user(store), _user(store, "bob")
    tokens = [
        _tokens(provider, alice.id, "first"),
        _tokens(provider, alice.id, "second"),
        _tokens(provider, bob.id, "first"),
        _tokens(provider, bob.id, "second"),
    ]
    identities = [
        resolve_access_token(store, t.access_token, oauth_config) for t in tokens
    ]
    assert all(identity is not None for identity in identities)
    first = identities[0]
    assert first is not None and first.user_id == alice.id
    assert revalidate_identity(store, first, oauth_config) == "al"
    assert len(provider.list_grants(alice.id)) == 2
    assert len(provider.list_grants(bob.id)) == 2
    assert not provider.revoke_grant(bob.id, first.grant_id)
    assert provider.revoke_grant(alice.id, first.grant_id)
    assert not provider.revoke_grant(alice.id, first.grant_id)
    assert resolve_access_token(store, tokens[0].access_token, oauth_config) is None
    with pytest.raises(Denied):
        revalidate_identity(store, first, oauth_config)
    assert all(
        resolve_access_token(store, t.access_token, oauth_config) for t in tokens[1:]
    )
    restarted = OAuthProvider(store, oauth_config)
    assert asyncio.run(restarted.load_access_token(tokens[1].access_token)) is not None
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT token_sha256 FROM mcp_oauth_tokens").fetchall()
    hashes = {row[0] for row in rows}
    for token in tokens:
        assert (
            digest(token.access_token) in hashes
            and digest(token.refresh_token) in hashes
        )
        assert token.access_token not in repr(rows)
        assert token.refresh_token not in repr(rows)


def test_consent_is_bound_single_use_and_can_deny(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    alice, bob = _user(store), _user(store, "bob")
    request_id = _request(provider)
    consent = provider.prepare_consent(request_id, alice.id)
    with pytest.raises(Denied):
        provider.prepare_consent(request_id, bob.id)
    with pytest.raises(Denied):
        provider.complete_consent(
            request_id, alice.id, "wrong", allow=True, label="app"
        )
    with pytest.raises(Denied):
        provider.complete_consent(
            request_id, bob.id, consent.csrf_token, allow=True, label="app"
        )
    with pytest.raises(BadInput):
        provider.complete_consent(
            request_id, alice.id, consent.csrf_token, allow=True, label="x" * 101
        )
    url = provider.complete_consent(
        request_id, alice.id, consent.csrf_token, allow=False, label=""
    )
    assert parse_qs(urlsplit(url).query) == {
        "error": ["access_denied"],
        "state": ["return-to-client"],
    }
    with pytest.raises(Denied):
        provider.complete_consent(
            request_id, alice.id, consent.csrf_token, allow=True, label="app"
        )
    assert provider.list_grants(alice.id) == []


def test_consent_and_code_store_only_secret_digests(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    user = _user(store)
    request_id = _request(provider)
    consent = provider.prepare_consent(request_id, user.id)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT request_sha256, csrf_sha256 FROM mcp_oauth_requests"
        ).fetchone()
    assert row == (digest(request_id), digest(consent.csrf_token))
    url = provider.complete_consent(
        request_id, user.id, consent.csrf_token, allow=True, label=""
    )
    code = parse_qs(urlsplit(url).query)["code"][0]
    with store.pool.connection() as conn:
        row = conn.execute("SELECT code_sha256 FROM mcp_oauth_codes").fetchone()
    assert row == (digest(code),)


def test_invalid_authorize_inputs_refuse_without_persistence(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    client = asyncio.run(provider.get_client("first"))
    assert client is not None
    for updates in (
        {"redirect_uri": AnyUrl("https://evil.example/callback")},
        {"resource": "https://other.example/mcp"},
        {"code_challenge": "plain"},
        {"scopes": ["mcp:write"]},
        {"state": "x" * 2049},
    ):
        with pytest.raises(AuthorizeError):
            asyncio.run(
                provider.authorize(
                    client, _params(oauth_config).model_copy(update=updates)
                )
            )
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM mcp_oauth_requests").fetchone() == (
            0,
        )
    assert asyncio.run(provider.get_client("missing")) is None


def test_code_only_exchanges_once_under_concurrency(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    client, code = _code(provider, _user(store).id)

    def exchange() -> Any:
        try:
            return asyncio.run(provider.exchange_authorization_code(client, code))
        except TokenError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(exchange), executor.submit(exchange)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(isinstance(result, TokenError) for result in results) == 1
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM mcp_oauth_tokens").fetchone() == (2,)
    assert asyncio.run(provider.load_authorization_code(client, code.code)) is None


def test_refresh_narrows_rotates_and_replay_revokes_only_its_family(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    user = _user(store)
    token = _tokens(provider, user.id, scopes=["learning", "mcp:full"])
    other = _tokens(provider, user.id, "second")
    client = asyncio.run(provider.get_client("first"))
    assert client is not None
    refresh = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert refresh is not None
    narrowed = asyncio.run(
        provider.exchange_refresh_token(client, refresh, ["learning"])
    )
    assert narrowed.refresh_token is not None
    assert narrowed.refresh_token != token.refresh_token
    assert resolve_access_token(store, token.access_token, oauth_config) is None
    identity = resolve_access_token(store, narrowed.access_token, oauth_config)
    assert identity is not None and identity.scopes == ("learning",)
    fresh = asyncio.run(provider.load_refresh_token(client, narrowed.refresh_token))
    assert fresh is not None
    with pytest.raises(TokenError, match="invalid_scope"):
        asyncio.run(provider.exchange_refresh_token(client, fresh, ["mcp:full"]))
    assert (
        asyncio.run(provider.load_refresh_token(client, token.refresh_token))
        is not None
    )
    with pytest.raises(TokenError, match="invalid_grant"):
        asyncio.run(provider.exchange_refresh_token(client, refresh, ["learning"]))
    assert resolve_access_token(store, narrowed.access_token, oauth_config) is None
    assert (
        asyncio.run(provider.load_refresh_token(client, narrowed.refresh_token)) is None
    )
    assert resolve_access_token(store, other.access_token, oauth_config) is not None


def test_wrong_client_resource_and_identity_refuse(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    user = _user(store)
    client, code = _code(provider, user.id)
    foreign = asyncio.run(provider.get_client("second"))
    assert foreign is not None
    assert asyncio.run(provider.load_authorization_code(foreign, code.code)) is None
    with pytest.raises(TokenError):
        asyncio.run(provider.exchange_authorization_code(foreign, code))
    token = asyncio.run(provider.exchange_authorization_code(client, code))
    assert token.refresh_token is not None
    assert (
        asyncio.run(provider.load_refresh_token(foreign, token.refresh_token)) is None
    )
    other_resource = replace(oauth_config, resource="https://other.example/mcp")
    assert resolve_access_token(store, token.access_token, other_resource) is None
    identity = resolve_access_token(store, token.access_token, oauth_config)
    assert identity is not None
    with pytest.raises(Denied):
        revalidate_identity(
            store, replace(identity, grant_id=identity.grant_id + 1), oauth_config
        )
    with pytest.raises(Denied):
        revalidate_identity(
            store, replace(identity, scopes=("mcp:full",)), oauth_config
        )
    provider.revoke_by_token("second", token.access_token)
    assert resolve_access_token(store, token.access_token, oauth_config) is not None


def test_disabled_users_and_removed_client_scopes_fail_closed(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    user = _user(store)
    token = _tokens(provider, user.id)
    store.set_web_user_disabled(user.login, disabled=True)
    assert resolve_access_token(store, token.access_token, oauth_config) is None
    assert provider.list_grants(user.id) == []
    with pytest.raises(Denied):
        provider.prepare_consent(_request(provider), user.id)
    store.set_web_user_disabled(user.login, disabled=False)
    assert (
        resolve_access_token(
            store, token.access_token, replace(oauth_config, scopes=("mcp:full",))
        )
        is None
    )
    assert (
        resolve_access_token(
            store,
            token.access_token,
            replace(oauth_config, clients={"second": oauth_config.clients["second"]}),
        )
        is None
    )


def test_expiry_and_expired_token_revocation(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    user = _user(store)
    request_id = _request(provider)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE mcp_oauth_requests SET expires_at = now() - interval '1 second'"
        )
    with pytest.raises(Denied):
        provider.prepare_consent(request_id, user.id)
    client, code = _code(provider, user.id)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE mcp_oauth_codes SET expires_at = now() - interval '1 second'"
        )
    assert asyncio.run(provider.load_authorization_code(client, code.code)) is None
    with pytest.raises(TokenError):
        asyncio.run(provider.exchange_authorization_code(client, code))
    token = _tokens(provider, user.id)
    identity = resolve_access_token(store, token.access_token, oauth_config)
    assert identity is not None
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE mcp_oauth_tokens SET expires_at = now() - interval '1 second' WHERE token_sha256 = %s",
            (digest(token.access_token),),
        )
    assert resolve_access_token(store, token.access_token, oauth_config) is None
    provider.revoke_by_token("first", token.access_token)
    assert asyncio.run(provider.load_refresh_token(client, token.refresh_token)) is None
    assert (
        next(
            g for g in provider.list_grants(user.id) if g.id == identity.grant_id
        ).revoked_at
        is not None
    )


def test_pending_request_capacity_is_bounded_and_expiry_releases_it(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    request_id = _request(provider)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO mcp_oauth_requests (request_sha256, client_id, params, expires_at)"
            " SELECT md5(n::text) || md5(n::text), client_id, params, expires_at"
            " FROM mcp_oauth_requests CROSS JOIN generate_series(1, 99) n"
            " WHERE request_sha256 = %s",
            (digest(request_id),),
        )
    with pytest.raises(AuthorizeError, match="temporarily_unavailable"):
        _request(provider)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE mcp_oauth_requests SET expires_at = now() - interval '1 second'"
        )
    assert _request(provider)
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM mcp_oauth_requests").fetchone() == (
            1,
        )


def test_concurrent_refresh_cannot_create_two_live_branches(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    token = _tokens(provider, _user(store).id)
    client = asyncio.run(provider.get_client("first"))
    assert client is not None
    refresh = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert refresh is not None

    def exchange() -> Any:
        try:
            return asyncio.run(
                provider.exchange_refresh_token(client, refresh, ["learning"])
            )
        except TokenError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(exchange), executor.submit(exchange)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(isinstance(result, TokenError) for result in results) == 1
    success = next(result for result in results if not isinstance(result, TokenError))
    assert resolve_access_token(store, success.access_token, oauth_config) is None
    assert (
        asyncio.run(provider.load_refresh_token(client, success.refresh_token)) is None
    )


def test_refresh_deadline_never_extends_grant_and_expired_grants_refuse(
    store: Store, oauth_config: OAuthConfig
) -> None:
    provider = OAuthProvider(store, oauth_config)
    token = _tokens(provider, _user(store).id)
    identity = resolve_access_token(store, token.access_token, oauth_config)
    assert identity is not None
    with store.pool.connection() as conn, conn.transaction():
        grant = conn.execute(
            "UPDATE mcp_oauth_grants SET expires_at = now() + interval '10 minutes'"
            " WHERE id = %s RETURNING expires_at",
            (identity.grant_id,),
        ).fetchone()
    assert grant is not None
    client = asyncio.run(provider.get_client("first"))
    assert client is not None
    refresh = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert refresh is not None
    rotated = asyncio.run(
        provider.exchange_refresh_token(client, refresh, ["learning"])
    )
    assert rotated.refresh_token is not None
    with store.pool.connection() as conn:
        deadlines = conn.execute(
            "SELECT expires_at FROM mcp_oauth_tokens WHERE token_sha256 IN (%s, %s)",
            (digest(rotated.access_token), digest(rotated.refresh_token)),
        ).fetchall()
    assert deadlines == [(grant[0],), (grant[0],)]
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE mcp_oauth_grants SET expires_at = now() - interval '1 second'"
            " WHERE id = %s",
            (identity.grant_id,),
        )
    assert resolve_access_token(store, rotated.access_token, oauth_config) is None
    assert (
        asyncio.run(provider.load_refresh_token(client, rotated.refresh_token)) is None
    )
