"""OAuth resource authentication, per-connection sessions and capability limits."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from precis import mcp_identity, principal, server
from precis.errors import Denied
from precis.oauth.config import OAuthConfig
from precis.oauth.store import (
    ACCESS_PREFIX,
    OAuthIdentity,
    digest,
    resolve_access_token,
)
from precis.oauth_transport import configure_transport
from precis.store import Store
from precis.tools import core
from precis.user_context import current_principal, require_user, user_scope
from precis.users import hash_password


@pytest.fixture
def oauth_config(monkeypatch: pytest.MonkeyPatch) -> OAuthConfig:
    monkeypatch.setenv("PRECIS_OAUTH_ISSUER", "https://auth.example")
    monkeypatch.setenv("PRECIS_OAUTH_RESOURCE", "https://mcp.example:8443/mcp")
    monkeypatch.setenv("PRECIS_OAUTH_SCOPES", "learning mcp:full")
    monkeypatch.delenv("PRECIS_OAUTH_CLIENTS", raising=False)
    config = OAuthConfig.from_env()
    assert config is not None
    return config


def _user(store: Store, login: str):
    return store.create_web_user(
        login=login, abbrev=login[:2], password=hash_password("test password")
    )


def _grant(
    store: Store,
    config: OAuthConfig,
    user_id: int,
    scopes: tuple[str, ...] = ("learning",),
) -> tuple[str, OAuthIdentity]:
    """Seed persisted credentials independently of authorization-provider tests."""
    raw = ACCESS_PREFIX + secrets.token_urlsafe(32)
    with store.pool.connection() as conn:
        grant = conn.execute(
            "INSERT INTO mcp_oauth_grants (user_id, client_id, label, scopes,"
            " resource, expires_at) VALUES (%s, 'precis-claude', 'Test app',"
            " %s, %s, now() + interval '30 days') RETURNING id",
            (user_id, list(scopes), config.resource),
        ).fetchone()
        assert grant is not None
        conn.execute(
            "INSERT INTO mcp_oauth_tokens (token_sha256, grant_id, token_kind,"
            " scopes, expires_at) VALUES (%s, %s, 'access', %s,"
            " now() + interval '1 hour')",
            (digest(raw), grant[0], list(scopes)),
        )
        conn.commit()
    identity = resolve_access_token(store, raw, config)
    assert identity is not None
    return raw, identity


def _headers(raw: str, session: str | None = None) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {raw}",
        "Accept": "application/json, text/event-stream",
    }
    if session is not None:
        result["Mcp-Session-Id"] = session
    return result


async def _initialize(client: httpx.AsyncClient, raw: str) -> str:
    response = await client.post(
        "/mcp",
        headers=_headers(raw),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "oauth-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session = response.headers["mcp-session-id"]
    ready = await client.post(
        "/mcp",
        headers=_headers(raw, session),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert ready.status_code == 202, ready.text
    return session


async def _call(client, raw, session, call_id, verb="get", arguments=None):
    return await client.post(
        "/mcp",
        headers=_headers(raw, session),
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": verb,
                "arguments": arguments or {"kind": "learning", "id": "/activity"},
            },
        },
    )


def _app(store, config, runtime, monkeypatch, profile="typed"):
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime)
    mcp = FastMCP("oauth-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: profile)
    server._register_tools_from_registry()
    configure_transport(mcp, config)
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store, oauth_config=config)
    return app


def test_oauth_discovery_and_public_host_origin_checks(
    store: Store,
    oauth_config: OAuthConfig,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(store, oauth_config, runtime_with_store, monkeypatch)
    with TestClient(app, base_url="https://mcp.example:8443") as client:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            response = client.get(path, headers={"Host": "untrusted.example"})
            assert response.status_code == 200
            assert response.json() == {
                "resource": oauth_config.resource,
                "authorization_servers": [oauth_config.issuer],
                "scopes_supported": ["learning", "mcp:full"],
                "bearer_methods_supported": ["header"],
            }
        response = client.get("/mcp")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            'Bearer resource_metadata="https://mcp.example:8443/'
            '.well-known/oauth-protected-resource/mcp", scope="learning mcp:full"'
        )
        assert (
            client.get(
                "/mcp", headers={**_headers("install"), "Host": "attacker.example"}
            ).status_code
            == 421
        )
        assert (
            client.get(
                "/mcp",
                headers={**_headers("install"), "Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        for origin in ("https://mcp.example:8443", "https://auth.example"):
            # No session is supplied, but a permitted host/origin reaches the SDK.
            response = client.get(
                "/mcp", headers={**_headers("install"), "Origin": origin}
            )
            assert response.status_code == 400


def test_oauth_sessions_are_bound_to_user_and_connection(
    store: Store,
    oauth_config: OAuthConfig,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice, bob = _user(store, "alice"), _user(store, "bob")
    first, identity = _grant(store, oauth_config, alice.id, ("mcp:full",))
    other_app, _ = _grant(store, oauth_config, alice.id, ("mcp:full",))
    other_user, _ = _grant(store, oauth_config, bob.id, ("mcp:full",))

    def dispatch(*args, **kwargs):
        owner = current_principal()
        assert owner is not None and owner.oauth is not None
        assert principal.current() == f"oauth:{owner.oauth.grant_id}"
        return f"{require_user(store)}:{owner.oauth.grant_id}", False

    monkeypatch.setattr(runtime_with_store, "dispatch_with_status", dispatch)
    app = _app(store, oauth_config, runtime_with_store, monkeypatch)

    async def exercise():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=oauth_config.resource.removesuffix("/mcp"),
            ) as client,
        ):
            session = await _initialize(client, first)
            sessions = {
                raw: await _initialize(client, raw) for raw in (other_app, other_user)
            }
            valid = await _call(client, first, session, 2)
            assert (
                valid.json()["result"]["content"][0]["text"]
                == f"{alice.id}:{identity.grant_id}"
            )
            for raw in (other_app, other_user, "install"):
                denied = await _call(client, raw, session, 3)
                assert denied.status_code == 404
                assert denied.headers["mcp-session-error"] == "reinitialize"
                for method in ("GET", "DELETE"):
                    denied = await client.request(
                        method, "/mcp", headers=_headers(raw, session)
                    )
                    assert denied.status_code == 404
            # Foreign DELETE never destroys the rightful owner's session.
            assert (await _call(client, first, session, 4)).status_code == 200
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE mcp_oauth_grants SET revoked_at = now() WHERE id = %s",
                    (identity.grant_id,),
                )
                conn.commit()
            assert (await _call(client, first, session, 5)).status_code == 401
            for raw, own_session in sessions.items():
                result = await _call(client, raw, own_session, 6)
                assert result.status_code == 200 and not result.json()["result"].get(
                    "isError"
                )
            store.set_web_user_disabled(bob.login, disabled=True)
            assert (
                await _call(client, other_user, sessions[other_user], 7)
            ).status_code == 401

    asyncio.run(exercise())
    assert current_principal() is None


@pytest.mark.parametrize("profile", ["typed", "command"])
@pytest.mark.parametrize("grant_scope", ["learning", "mcp:full"])
def test_oauth_capability_is_identical_at_wire_and_dispatch(
    store: Store,
    oauth_config: OAuthConfig,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    grant_scope: str,
) -> None:
    alice = _user(store, "alice")
    raw, identity = _grant(store, oauth_config, alice.id, (grant_scope,))

    def dispatch(*args, **kwargs):
        owner = current_principal()
        assert owner is not None and owner.oauth == identity
        assert owner.token_id is None
        assert require_user(store) == alice.id
        assert principal.current() == f"oauth:{identity.grant_id}"
        return "permitted operation", False

    dispatched = Mock(side_effect=dispatch)
    monkeypatch.setattr(runtime_with_store, "dispatch_with_status", dispatched)
    app = _app(store, oauth_config, runtime_with_store, monkeypatch, profile)
    full = grant_scope == "mcp:full"
    cases: list[tuple[str, dict[str, Any], bool]] = [
        ("get", {"kind": "learning", "id": "/activity"}, True),
        ("get", {"id": "learning:/activity"}, True),
        ("get", {"kind": "skill", "id": "precis-learning-help"}, True),
        ("get", {"kind": "skill", "id": "precis-oauth-help"}, True),
        ("get", {"kind": "profile", "id": "me"}, True),
        ("get", {"kind": "paper", "id": "pa42"}, full),
        ("get", {"kind": "memory", "id": "me42"}, full),
        ("get", {"kind": "email", "id": 42}, full),
        ("search", {"kind": "paper", "q": "test", "page_size": 10}, full),
        ("search", {"kind": "email", "q": "test"}, full),
        ("get", {"kind": "skill", "id": "toc"}, full),
        ("get", {"kind": "paper", "id": "pa42", "extent": "fisheye+2hop"}, full),
        ("get", {"kind": "paper", "id": ["pa42"]}, full),
        ("get", {"kind": "paper", "id": "pa42", "args": {}}, full),
        ("put", {"kind": "memory", "text": "new note"}, full),
        ("edit", {"kind": "memory", "id": "me42", "find": "old", "text": "new"}, full),
        ("delete", {"kind": "memory", "id": "me42"}, full),
        ("tag", {"kind": "memory", "id": "me42", "add": ["test"]}, full),
        ("link", {"kind": "memory", "id": "me42", "target": "memory:me43"}, full),
    ]

    async def exercise():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=oauth_config.resource.removesuffix("/mcp"),
            ) as client,
        ):
            session = await _initialize(client, raw)
            for number, (verb, arguments, allowed) in enumerate(cases, 2):
                with user_scope(alice.id, oauth=identity):
                    assert core._personal_call_allowed(verb, arguments) is allowed
                name, wire = verb, arguments
                if profile == "command":
                    params = ", ".join(
                        f"{key}={value!r}" for key, value in arguments.items()
                    )
                    name, wire = "precis", {"command": f"{verb}({params})"}
                before = dispatched.call_count
                response = await _call(client, raw, session, number, name, wire)
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert bool(result.get("isError")) is not allowed, (
                    profile,
                    arguments,
                    response.text,
                )
                assert dispatched.call_count == before + int(allowed)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "invalidate", ["expired_token", "expired_grant", "revoked_grant", "disabled_user"]
)
def test_oauth_dispatch_revalidates_an_already_authenticated_request(
    store: Store,
    oauth_config: OAuthConfig,
    invalidate: str,
) -> None:
    alice = _user(store, "alice")
    _raw, identity = _grant(store, oauth_config, alice.id)
    with user_scope(alice.id, oauth=replace(identity, scopes=("learning", "mcp:full"))):
        with pytest.raises(Denied):
            require_user(store)
    with user_scope(alice.id, oauth=identity):
        assert require_user(store) == alice.id
        with store.pool.connection() as conn:
            if invalidate == "expired_token":
                conn.execute(
                    "UPDATE mcp_oauth_tokens SET expires_at = now() - interval '1 second' WHERE id = %s",
                    (identity.access_token_id,),
                )
            elif invalidate == "expired_grant":
                conn.execute(
                    "UPDATE mcp_oauth_grants SET expires_at = now() - interval '1 second' WHERE id = %s",
                    (identity.grant_id,),
                )
            elif invalidate == "revoked_grant":
                conn.execute(
                    "UPDATE mcp_oauth_grants SET revoked_at = now() WHERE id = %s",
                    (identity.grant_id,),
                )
            else:
                conn.execute(
                    "UPDATE web_users SET disabled_at = now() WHERE id = %s",
                    (alice.id,),
                )
            conn.commit()
        with pytest.raises(Denied, match="no longer active"):
            require_user(store)
    assert current_principal() is None


def test_oauth_authentication_clears_stale_install_and_personal_stamps(
    store: Store,
    oauth_config: OAuthConfig,
) -> None:
    alice = _user(store, "alice")
    raw, identity = _grant(store, oauth_config, alice.id, ("mcp:full",))

    async def probe(request):
        assert principal.SCOPE_INSTALL not in request.scope
        assert mcp_identity.SCOPE_TOKEN_ID not in request.scope
        assert mcp_identity.SCOPE_USER_ID not in request.scope
        assert request.scope[mcp_identity.SCOPE_OAUTH_IDENTITY] == identity
        return JSONResponse({"ok": True})

    class StaleStamps:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            scope[principal.SCOPE_INSTALL] = True
            scope[mcp_identity.SCOPE_TOKEN_ID] = 99
            scope[mcp_identity.SCOPE_USER_ID] = 99
            await self.app(scope, receive, send)

    app = Starlette(routes=[Route("/mcp", probe)])
    server._install_token_auth(
        app, token="install", store=store, oauth_config=oauth_config
    )
    app.add_middleware(StaleStamps)
    with TestClient(app) as client:
        assert client.get("/mcp", headers=_headers(raw)).json() == {"ok": True}
        assert client.get("/mcp").status_code == 401


def test_oauth_legacy_transport_and_unsupported_sdk_fail_closed(
    store: Store,
    oauth_config: OAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = _user(store, "alice")
    raw, _identity = _grant(store, oauth_config, alice.id)

    async def probe(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/sse", probe)])
    server._install_token_auth(
        app, token="install", store=store, personal_path=None, oauth_config=oauth_config
    )
    with TestClient(app) as client:
        assert client.get("/sse", headers=_headers(raw)).status_code == 403
        assert client.get("/sse", headers=_headers("install")).status_code == 200
        assert client.get("/.well-known/oauth-protected-resource").status_code == 401
    monkeypatch.setattr(server, "_sdk_session_ownership_supported", lambda: False)
    app = Starlette(routes=[Route("/mcp", probe)])
    server._install_token_auth(
        app, token="install", store=store, oauth_config=oauth_config
    )
    with TestClient(app) as client:
        assert client.get("/mcp", headers=_headers(raw)).status_code == 503
        assert client.get("/mcp", headers=_headers("install")).status_code == 200


def test_oauth_context_keeps_pat_and_oauth_namespaces_separate(
    store: Store,
    oauth_config: OAuthConfig,
) -> None:
    alice = _user(store, "alice")
    _raw, identity = _grant(store, oauth_config, alice.id)
    ctx = cast(
        Context,
        SimpleNamespace(
            request_context=SimpleNamespace(
                request=SimpleNamespace(
                    scope={mcp_identity.SCOPE_OAUTH_IDENTITY: identity}
                )
            )
        ),
    )
    with core._ctx_user_scope(ctx):
        bound = current_principal()
        assert bound is not None and bound.token_id is None and bound.oauth == identity
        assert require_user(store) == alice.id
    with pytest.raises(Denied, match="invalid authenticated"):
        with user_scope(alice.id, 12, oauth=identity):
            pass
    assert current_principal() is None


@pytest.mark.parametrize("grant_scope", ["learning", "mcp:full"])
def test_whoami_over_oauth_wire_names_user_connection_and_scope(
    store: Store,
    oauth_config: OAuthConfig,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
    grant_scope: str,
) -> None:
    """The whoami read answers for any OAuth connection, full or narrow."""
    alice = _user(store, "alice")
    raw, identity = _grant(store, oauth_config, alice.id, (grant_scope,))
    app = _app(store, oauth_config, runtime_with_store, monkeypatch)

    async def exercise():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=oauth_config.resource.removesuffix("/mcp"),
            ) as client,
        ):
            session = await _initialize(client, raw)
            response = await _call(
                client,
                raw,
                session,
                2,
                arguments={"kind": "profile", "id": "me"},
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError", False), result
            body = result["content"][0]["text"]
            assert f"user: al (login: alice, id: {alice.id})" in body
            assert "credential: OAuth connection 'Test app'" in body
            assert f"(client: precis-claude, grant: {identity.grant_id})" in body
            assert f"scopes: {grant_scope}" in body
            if grant_scope == "mcp:full":
                assert "permission: full MCP (all verbs)" in body
            else:
                assert "permission: learning calls, learning help, whoami" in body
            assert f"writes recorded as: oauth:{identity.grant_id}" in body
            assert "revision actor: user:al" in body
            assert "client: oauth-test/1" in body

    asyncio.run(exercise())
    assert current_principal() is None


def test_oauth_write_records_user_principal_and_client(
    store: Store,
    oauth_config: OAuthConfig,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write over the OAuth connector lands tied to the account (gr633675):
    the revision actor is the user's ``user:<abbrev>``, the writer stamp
    carries the OAuth principal and the MCP client's own declaration, and
    the ``written:`` line a later get renders names the user."""
    alice = _user(store, "alice")
    raw, identity = _grant(store, oauth_config, alice.id, ("mcp:full",))
    app = _app(store, oauth_config, runtime_with_store, monkeypatch)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=oauth_config.resource.removesuffix("/mcp"),
            ) as client,
        ):
            session = await _initialize(client, raw)
            created = await _call(
                client,
                raw,
                session,
                2,
                verb="put",
                arguments={
                    "kind": "memory",
                    "text": "oauth attribution probe",
                    "title": "oauth attribution probe",
                },
            )
            assert created.status_code == 200, created.text
            result = created.json()["result"]
            assert not result.get("isError", False), result
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT r.target_id, r.actor, r.writer FROM revisions r"
                    " JOIN refs f ON f.ref_id = r.target_id"
                    " WHERE r.target_kind = 'ref' AND f.kind = 'memory'"
                    " ORDER BY r.revision_id DESC LIMIT 1"
                ).fetchone()
            assert row is not None
            target_id, actor, writer = row
            assert actor == "user:al"
            assert writer["actor"] == "user:al"
            assert writer["principal"] == f"oauth:{identity.grant_id}"
            assert writer["client"] == "oauth-test/1"
            assert store.first_last_writer("ref", int(target_id)) is not None
            got = await _call(
                client,
                raw,
                session,
                3,
                arguments={"kind": "memory", "id": int(target_id)},
            )
            assert got.status_code == 200, got.text
            body = got.json()["result"]["content"][0]["text"]
            assert "written: user:al, oauth-test" in body, body

    asyncio.run(exercise())
    assert current_principal() is None
