"""Real SDK OAuth exchanges through the web login and PostgreSQL grant store."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.oauth.config import OAuthConfig
from precis.oauth.store import resolve_access_token
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

ISSUER = "https://precis.example"
RESOURCE = "https://precis.example:8443/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = secrets.token_urlsafe(24)
VERIFIER = "a" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


def _auth(login: str = "alice") -> dict[str, str]:
    credential = base64.b64encode(f"{login}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {credential}"}


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("PRECIS_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("PRECIS_OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setenv("PRECIS_OAUTH_SCOPES", "learning")
    monkeypatch.delenv("PRECIS_OAUTH_CLIENTS", raising=False)
    return OAuthConfig.from_env()


@pytest.fixture
def client(store, oauth_env):
    for login, abbrev in (("alice", "al"), ("bob", "bo")):
        store.create_web_user(
            login=login, abbrev=abbrev, password=hash_password(PASSWORD)
        )
    app = create_app(
        runtime=SimpleNamespace(store=store),
        web_config=WebConfig(corpus_dir=None, auth_required=True),
    )
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as client:
        yield client


def _request(client: TestClient, **overrides: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "precis-claude",
            "redirect_uri": CALLBACK,
            "scope": "learning",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "application-state",
            **overrides,
        },
    )
    assert response.status_code in {302, 303}, response.text
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/oauth/consent?")
    return location


def _consent_form(
    client: TestClient, location: str, login: str = "alice"
) -> dict[str, str]:
    response = client.get(location, headers=_auth(login))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    fields = dict(
        re.findall(r'name="(request|csrf_token)" value="([^"]+)"', response.text)
    )
    assert len(fields) == 2
    return {key: html.unescape(value) for key, value in fields.items()}


def _code(
    client: TestClient, *, login: str = "alice", label: str = "Claude laptop"
) -> str:
    fields = _consent_form(client, _request(client), login)
    response = client.post(
        "/oauth/consent",
        data={**fields, "decision": "allow", "label": label},
        headers={**_auth(login), "Origin": ISSUER},
    )
    assert response.status_code == 303, response.text
    redirect = urlsplit(response.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == CALLBACK
    query = parse_qs(redirect.query)
    assert query["state"] == ["application-state"]
    return query["code"][0]


def _exchange(client: TestClient, code: str, **overrides: str):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "precis-claude",
            "redirect_uri": CALLBACK,
            "code": code,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **overrides,
        },
    )


def test_discovery_and_exact_public_exemptions(client):
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == ISSUER
    assert metadata["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert metadata["token_endpoint"] == f"{ISSUER}/token"
    assert metadata["revocation_endpoint"] == f"{ISSUER}/revoke"
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in metadata
    for path in (
        "/token-extra",
        "/token/private",
        "/oauth/consent",
        "/account/connections",
    ):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")
    refused = client.post("/token", data={"client_id": "unknown"})
    assert refused.status_code == 401
    assert "www-authenticate" not in refused.headers
    preflight = client.options(
        "/token",
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"


def test_two_connections_per_user_and_owner_only_revoke(client, store, oauth_env):
    alice_first = _exchange(client, _code(client, label="Alice one"))
    alice_second = _exchange(client, _code(client, label="Alice two"))
    bob = _exchange(client, _code(client, login="bob", label="Bob app"))
    assert alice_first.status_code == alice_second.status_code == bob.status_code == 200
    first, second, bob_tokens = alice_first.json(), alice_second.json(), bob.json()
    assert (
        len({first["access_token"], second["access_token"], bob_tokens["access_token"]})
        == 3
    )
    assert alice_first.headers["cache-control"] == "no-store"
    page = client.get("/account/connections", headers=_auth())
    assert page.status_code == 200
    assert "Alice one" in page.text and "Alice two" in page.text
    assert "Bob app" not in page.text
    for raw in (first["access_token"], first["refresh_token"], second["access_token"]):
        assert raw not in page.text
    identity = resolve_access_token(store, first["access_token"], oauth_env)
    bob_identity = resolve_access_token(store, bob_tokens["access_token"], oauth_env)
    assert identity is not None and bob_identity is not None
    foreign = client.post(
        "/account/connections/revoke",
        data={"grant_id": bob_identity.grant_id},
        headers=_auth(),
    )
    missing = client.post(
        "/account/connections/revoke", data={"grant_id": 999999999}, headers=_auth()
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    revoked = client.post(
        "/account/connections/revoke",
        data={"grant_id": identity.grant_id},
        headers=_auth(),
    )
    assert revoked.status_code == 303
    assert resolve_access_token(store, first["access_token"], oauth_env) is None
    assert resolve_access_token(store, second["access_token"], oauth_env) is not None
    assert (
        resolve_access_token(store, bob_tokens["access_token"], oauth_env) is not None
    )


def test_consent_nonce_owner_origin_denial_and_replay(client):
    location = _request(client)
    fields = _consent_form(client, location)
    form = {**fields, "decision": "allow", "label": "My app"}
    for changes, headers in (
        ({"csrf_token": "forged"}, _auth()),
        ({}, _auth("bob")),
        ({}, {**_auth(), "Origin": "https://evil.example"}),
    ):
        response = client.post(
            "/oauth/consent", data={**form, **changes}, headers=headers
        )
        assert response.status_code == 403
    denied = client.post(
        "/oauth/consent", data={**form, "decision": "deny"}, headers=_auth()
    )
    assert denied.status_code == 303
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == [
        "access_denied"
    ]
    assert client.post("/oauth/consent", data=form, headers=_auth()).status_code == 403


def test_sdk_rejects_wrong_pkce_resource_redirect_and_code_replay(client):
    code = _code(client)
    assert _exchange(client, code, code_verifier="b" * 43).status_code == 400
    wrong_resource = _exchange(client, code, resource="https://other.example/mcp")
    assert wrong_resource.status_code == 400
    assert wrong_resource.json()["error"] == "invalid_target"
    assert (
        _exchange(
            client, code, redirect_uri="https://evil.example/callback"
        ).status_code
        == 400
    )
    assert _exchange(client, code).status_code == 200
    assert _exchange(client, code).status_code == 400
    redirect = client.get(
        "/authorize",
        params={
            "client_id": "precis-claude",
            "response_type": "code",
            "redirect_uri": "https://evil.example/callback",
            "code_challenge": CHALLENGE,
        },
    )
    assert redirect.status_code == 400
    assert "location" not in redirect.headers


def test_public_client_revoke_does_not_require_a_secret(client, store, oauth_env):
    token = _exchange(client, _code(client)).json()
    response = client.post(
        "/revoke", data={"client_id": "precis-claude", "token": token["refresh_token"]}
    )
    assert response.status_code == 200
    assert resolve_access_token(store, token["access_token"], oauth_env) is None
    assert (
        client.post(
            "/revoke", data={"client_id": "precis-claude", "token": "unknown"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/revoke", data={"client_id": "unknown", "token": "unknown"}
        ).status_code
        == 401
    )


def test_forms_are_bounded_and_duplicate_fields_refuse(client):
    huge = client.post(
        "/token",
        content=b"token=" + b"x" * (64 * 1024),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert huge.status_code == 413
    duplicate = client.post(
        "/token",
        content="resource=a&resource=b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 400


@pytest.mark.parametrize(
    "grant_id", ["9223372036854775808", "9999999999999999999", "0", "-1"]
)
def test_revoke_refuses_ids_outside_postgres_bigint(client, grant_id):
    response = client.post(
        "/account/connections/revoke", data={"grant_id": grant_id}, headers=_auth()
    )
    assert response.status_code == 400


def test_full_mcp_consent_explains_shared_access_and_writes(
    store, monkeypatch, oauth_env
):
    monkeypatch.setenv("PRECIS_OAUTH_SCOPES", "mcp:full learning")
    store.create_web_user(login="alice", abbrev="al", password=hash_password(PASSWORD))
    app = create_app(
        runtime=SimpleNamespace(store=store), web_config=WebConfig(auth_required=True)
    )
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as client:
        location = _request(client, scope="mcp:full")
        response = client.get(location, headers=_auth())
        assert response.status_code == 200
        assert "Full MCP access" in response.text
        assert "tools that write data" in response.text
        assert "not limited to documents you own" in response.text
        assert (
            "Personal features with account-specific access use your signed-in account"
            in response.text
        )


def test_oauth_off_keeps_protocol_paths_behind_web_auth(store, monkeypatch):
    monkeypatch.delenv("PRECIS_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("PRECIS_OAUTH_RESOURCE", raising=False)
    monkeypatch.delenv("PRECIS_OAUTH_SCOPES", raising=False)
    monkeypatch.delenv("PRECIS_OAUTH_CLIENTS", raising=False)
    store.create_web_user(login="alice", abbrev="al", password=hash_password(PASSWORD))
    app = create_app(
        runtime=SimpleNamespace(store=store), web_config=WebConfig(auth_required=True)
    )
    with TestClient(app) as client:
        assert app.state.oauth_config is None
        assert client.get("/.well-known/oauth-authorization-server").status_code == 401
        assert client.post("/token", data={}).status_code == 401
        assert client.get("/account/connections", headers=_auth()).status_code == 404


def test_consent_refuses_when_browser_auth_is_disabled(store, oauth_env):
    app = create_app(
        runtime=SimpleNamespace(store=store), web_config=WebConfig(auth_required=False)
    )
    with TestClient(app, base_url=ISSUER) as client:
        assert client.get("/oauth/consent").status_code == 503
        assert client.get("/account/connections").status_code == 503
