"""Personal credentials, per-call revocation and real HTTP MCP isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP

from precis import mcp_identity, server
from precis.errors import BadInput, Denied, Upstream
from precis.mcp_identity import (
    list_user_tokens,
    mint_user_token,
    resolve_user_token,
    revoke_user_token,
)
from precis.store import Store
from precis.store.revision_context import current_revision_context, revision_context
from precis.tools import core, get_tool_names
from precis.user_context import (
    Principal,
    current_principal,
    require_user,
    user_actor_scope,
    user_scope,
)
from precis.users import hash_password


def _user(store: Store, login: str = "alice"):
    return store.create_web_user(
        login=login, abbrev=login[:2], password=hash_password("password")
    )


def test_user_token_only_persists_digest_and_owned_metadata(store: Store) -> None:
    alice, bob = _user(store), _user(store, "bob")
    token_id, raw = mint_user_token(store, alice.id, "Laptop")
    other_id, other_raw = mint_user_token(store, alice.id, "Desktop")
    assert re.fullmatch(r"precis_user_[A-Za-z0-9_-]{43}", raw)
    assert raw != other_raw and token_id != other_id
    assert resolve_user_token(store, raw) == Principal(alice.id, token_id)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT token_sha256 FROM web_user_mcp_tokens WHERE id = %s", (token_id,)
        ).fetchone()
    assert row == (hashlib.sha256(raw.encode("utf-8")).hexdigest(),)
    tokens = list_user_tokens(store, alice.id)
    assert {token.label for token in tokens} == {"Laptop", "Desktop"}
    assert list_user_tokens(store, bob.id) == []
    assert raw not in repr(tokens)
    assert not revoke_user_token(store, bob.id, token_id)
    assert resolve_user_token(store, raw) is not None
    assert revoke_user_token(store, alice.id, token_id)
    assert not revoke_user_token(store, alice.id, token_id)
    assert resolve_user_token(store, raw) is None
    assert resolve_user_token(store, other_raw) == Principal(alice.id, other_id)
    assert next(
        t for t in list_user_tokens(store, alice.id) if t.id == token_id
    ).revoked_at


def test_disabled_deleted_and_malformed_tokens_fail_closed(store: Store) -> None:
    alice = _user(store)
    token_id, raw = mint_user_token(store, alice.id, "client")
    for invalid in ("", "precis_user_short", raw + "!", "install-token", "td1.secret"):
        assert resolve_user_token(store, invalid) is None
    store.set_web_user_disabled(alice.login, disabled=True)
    assert resolve_user_token(store, raw) is None
    with pytest.raises(Denied):
        mint_user_token(store, alice.id, "another")
    with user_scope(alice.id, token_id), pytest.raises(Denied):
        require_user(store)
    store.set_web_user_disabled(alice.login, disabled=False)
    with user_scope(alice.id, token_id):
        assert require_user(store) == alice.id
    store.delete_web_user(alice.login)
    assert resolve_user_token(store, raw) is None
    with user_scope(alice.id), pytest.raises(Denied):
        require_user(store)


@pytest.mark.parametrize("label", ["", "   ", "x" * 101])
def test_token_label_validation(store: Store, label: str) -> None:
    with pytest.raises(BadInput):
        mint_user_token(store, _user(store).id, label)


def test_principal_scopes_reset_and_revalidate_each_call(store: Store) -> None:
    alice, bob = _user(store), _user(store, "bob")
    token_id, _raw = mint_user_token(store, alice.id, "client")
    assert current_principal() is None
    with user_scope(alice.id, token_id):
        assert require_user(store) == alice.id
        with user_scope(None), pytest.raises(Denied):
            require_user(store)
        with user_scope(bob.id, token_id), pytest.raises(Denied):
            require_user(store)
        with pytest.raises(RuntimeError), user_scope(bob.id):
            raise RuntimeError("scope exits on failure")
        assert current_principal() == Principal(alice.id, token_id)
        revoke_user_token(store, alice.id, token_id)
        with pytest.raises(Denied):
            require_user(store)
    assert current_principal() is None


def test_personal_actor_uses_live_abbrev_and_preserves_ambient_fields(
    store: Store,
) -> None:
    alice = _user(store)
    token_id, _raw = mint_user_token(store, alice.id, "client")
    with (
        revision_context(
            reason="outer reason", actor="outer-agent", model="selected-model"
        ) as outer,
        user_scope(alice.id, token_id),
    ):
        with user_actor_scope(store):
            inner = current_revision_context()
            assert inner is not None
            assert inner.actor == "user:al"
            assert inner.reason == outer.reason and inner.model == outer.model
            assert inner.trust_origin == outer.trust_origin is None
            assert inner.trust_actor_type == outer.trust_actor_type is None
        assert current_revision_context() == outer
        store.update_web_user(alice.login, abbrev="ax")
        with store.pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE web_users SET login = %s WHERE id = %s", ("renamed", alice.id)
            )
        with user_actor_scope(store):
            renamed = current_revision_context()
            assert renamed is not None and renamed.actor == "user:ax"
        with pytest.raises(RuntimeError), user_actor_scope(store):
            raise RuntimeError("tool failed")
        assert current_revision_context() == outer
        store.set_web_user_disabled("renamed", disabled=True)
        with pytest.raises(Denied), user_actor_scope(store):
            pytest.fail("disabled account acquired a revision actor")
        store.set_web_user_disabled("renamed", disabled=False)
        revoke_user_token(store, alice.id, token_id)
        with pytest.raises(Denied), user_actor_scope(store):
            pytest.fail("revoked token acquired a revision actor")
        with pytest.raises(Upstream), user_actor_scope(None):
            pytest.fail("missing account store acquired a revision actor")
        assert current_revision_context() == outer
    assert current_revision_context() is None


def test_anonymous_actor_scope_preserves_existing_attribution() -> None:
    with revision_context(reason="legacy", actor="agent", model="existing") as outer:
        with user_scope(None), user_actor_scope(None):
            assert current_revision_context() == outer
        assert current_revision_context() == outer


def _principal_ctx(user_id: int, token_id: int) -> Context:
    return cast(
        Context,
        SimpleNamespace(
            session=object(),
            request_context=SimpleNamespace(
                request=SimpleNamespace(
                    scope={
                        mcp_identity.SCOPE_USER_ID: user_id,
                        mcp_identity.SCOPE_TOKEN_ID: token_id,
                    }
                )
            ),
        ),
    )


def test_personal_mcp_token_refuses_even_owned_profiles(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers.profile import ProfileHandler

    alice, bob = _user(store), _user(store, "bob")
    token_id, _raw = mint_user_token(store, alice.id, "client")
    store.upsert_kinds([ProfileHandler.spec])
    own = store.insert_ref(
        kind="profile",
        slug=None,
        title="Alice palette",
        meta={"profile": {"name": "Alice palette", "palette": {}}},
        owner_login=alice.login,
    )
    foreign = store.insert_ref(
        kind="profile",
        slug=None,
        title="Bob private palette",
        meta={"profile": {"name": "Bob private palette", "palette": {}}},
        owner_login=bob.login,
    )
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    ctx = _principal_ctx(alice.id, token_id)
    # The install/unbound path retains the established generic-agent behavior.
    assert "Bob private palette" in core.get(kind="profile", id=foreign.id)
    owned = cast(Any, core.get(kind="profile", id=own.id, ctx=ctx))
    assert owned.isError and "Denied" in owned.content[0].text
    denied = cast(Any, core.get(kind="profile", id=foreign.id, ctx=ctx))
    assert denied.isError and "Denied" in denied.content[0].text
    refused = cast(
        Any, core.edit(kind="profile", id=foreign.id, sponsor_line="unwanted", ctx=ctx)
    )
    assert refused.isError and "Denied" in refused.content[0].text
    unchanged = store.get_ref(kind="profile", id=foreign.id)
    assert unchanged is not None and "sponsor_line" not in unchanged.meta["profile"]
    # A scope captured before revocation must fail before entering a legacy handler.
    revoke_user_token(store, alice.id, token_id)
    revoked = cast(Any, core.get(kind="profile", id=own.id, ctx=ctx))
    assert revoked.isError and "Denied" in revoked.content[0].text
    assert "no longer active" in revoked.content[0].text
    assert current_principal() is None and current_revision_context() is None


@pytest.mark.parametrize("user_id,token_id", [(True, 1), ("1", 1), (1, "2"), (1, None)])
def test_malformed_http_stamps_cannot_authorize(user_id: Any, token_id: Any) -> None:
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                scope={
                    mcp_identity.SCOPE_USER_ID: user_id,
                    mcp_identity.SCOPE_TOKEN_ID: token_id,
                }
            )
        )
    )
    with user_scope(42), core._ctx_user_scope(cast(Context, ctx)):
        assert current_principal() is None


def test_unbound_tool_clears_ambient_principal_and_resets_on_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []

    def dispatch(*args, **kwargs):
        observed.append(current_principal())
        raise RuntimeError("dispatch failure")

    monkeypatch.setattr(core, "_dispatch_as_user", dispatch)
    with user_scope(42, 7):
        with pytest.raises(RuntimeError):
            core._dispatch("get", {"kind": "learning"})
        assert current_principal() == Principal(42, 7)
    assert observed == [None]
    assert current_principal() is None


def test_learning_logs_redact_every_field_on_success_and_error(caplog) -> None:
    secret = "my private study note"
    for error in (False, True):
        with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
            core._log_tool_call(
                verb="put",
                payload={
                    "kind": "learning",
                    "id": secret,
                    "q": secret,
                    "text": secret,
                    "args": {"note": secret},
                    "__extras__": {"note": secret},
                    "owner": secret,
                    "unexpected": secret,
                },
                duration_ms=1,
                error=error,
            )
    assert secret not in caplog.text
    assert "learning" in caplog.text
    assert "note" not in caplog.text


def _headers(raw: str, session: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {raw}",
    }
    if session is not None:
        headers["Mcp-Session-Id"] = session
    return headers


def _rpc_body(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return json.loads(
        next(
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
    )


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
                "clientInfo": {"name": "identity-test", "version": "1"},
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


async def _call(client, session, raw, call_id, address="/activity", **params):
    return await client.post(
        "/mcp",
        headers=_headers(raw, session),
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": "get",
                "arguments": {"kind": "learning", "id": address},
                **params,
            },
        },
    )


_PUBLIC_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "get": {"id": "PRIVATE_SCOPE_SENTINEL"},
    "search": {"q": "PRIVATE_SCOPE_SENTINEL"},
    "put": {"text": "PRIVATE_SCOPE_SENTINEL"},
    "edit": {"id": "PRIVATE_SCOPE_SENTINEL", "text": "replacement"},
    "delete": {"id": "PRIVATE_SCOPE_SENTINEL"},
    "tag": {"id": "PRIVATE_SCOPE_SENTINEL", "add": ["test"]},
    "link": {"id": "PRIVATE_SCOPE_SENTINEL", "target": "memory:me1"},
}


@pytest.mark.parametrize("verb", get_tool_names())
@pytest.mark.parametrize("profile", ["typed", "command"])
def test_registered_tools_scope_personal_tokens_only(
    store: Store,
    runtime_with_store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    verb: str,
    profile: str,
) -> None:
    """Exercise production registration and authentication, including expansion."""
    from precis import slice_tokens

    assert verb in _PUBLIC_TOOL_ARGS, "new tool needs a personal-scope test case"
    alice = _user(store)
    _token_id, personal = mint_user_token(store, alice.id, "scope test")
    slice_raw = slice_tokens.mint(tmp_path, "td42")
    dispatched = Mock(return_value=("handler reached", False))
    monkeypatch.setattr(runtime_with_store, "dispatch_with_status", dispatched)
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    mcp = FastMCP("personal-scope-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: profile)
    server._register_tools_from_registry()
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    expected = {"precis"} if profile == "command" else set(_PUBLIC_TOOL_ARGS)
    assert names == expected, "every registered tool must be covered by scope tests"
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store, slice_dir=tmp_path)
    arguments = {"kind": "memory", **_PUBLIC_TOOL_ARGS[verb]}
    name = verb
    if profile == "command":
        command_args = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        arguments = {"command": f"{verb}({command_args})"}
        name = "precis"

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            session = await _initialize(client, personal)
            refused = await _call(
                client, session, personal, 2, name=name, arguments=arguments
            )
            result = refused.json()["result"]
            body = result["content"][0]["text"]
            assert result["isError"] and "[error:Denied]" in body
            assert "get(kind='learning')" in body
            assert "get(kind='skill', id='precis-learning-help')" in body
            assert "PRIVATE_SCOPE_SENTINEL" not in body
            assert "PRIVATE_SCOPE_SENTINEL" not in caplog.text
            dispatched.assert_not_called()
            for credential in ("install", slice_raw):
                session = await _initialize(client, credential)
                response = await _call(
                    client, session, credential, 3, name=name, arguments=arguments
                )
                result = response.json()["result"]
                assert not result.get("isError", False)
                assert result["content"][0]["text"] == "handler reached"
            assert dispatched.call_count == 2

    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        asyncio.run(exercise())
    with user_scope(alice.id, _token_id):
        assert (
            getattr(core, verb)(kind="memory", **_PUBLIC_TOOL_ARGS[verb])
            == "handler reached"
        )
        assert current_principal() == Principal(alice.id, _token_id)
    assert dispatched.call_count == 3
    assert current_principal() is None


@pytest.mark.parametrize("profile", ["typed", "command"])
def test_personal_token_learning_and_exact_help_exception(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    alice = _user(store)
    token_id, _raw = mint_user_token(store, alice.id, "scope test")
    ctx = _principal_ctx(alice.id, token_id)
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)

    def call(verb: str, arguments: dict[str, Any]) -> Any:
        if profile == "command":
            command_args = ", ".join(
                f"{key}={value!r}" for key, value in arguments.items()
            )
            return server.precis(f"{verb}({command_args})", ctx=ctx)
        return core.get(ctx=ctx, **arguments)

    for address in ({"kind": "learning"}, {"id": "learning:/activity"}):
        body = call("get", address)
        assert isinstance(body, str) and "Your recorded study activity" in body
    help_args = {"kind": "skill", "id": "precis-learning-help"}
    body = call("get", help_args)
    assert isinstance(body, str) and "private study activity" in body
    whoami_args = {"kind": "profile", "id": "me"}
    body = call("get", whoami_args)
    assert isinstance(body, str) and "whoami" in body and "user: al" in body
    for arguments in (
        {"kind": "skill", "id": "precis-overview"},
        {"kind": "skill", "id": "toc"},
        {"kind": "profile"},
        {**whoami_args, "view": "raw"},
        {**help_args, "view": "verbatim"},
        {**help_args, "args": {}},
        {**help_args, "args": {"have": "PRIVATE_SCOPE_SENTINEL"}},
        {**help_args, "full": True},
        {**help_args, "id": ["precis-learning-help"]},
    ):
        refused = call("get", arguments)
        assert refused.isError and "[error:Denied]" in refused.content[0].text
        assert "PRIVATE_SCOPE_SENTINEL" not in refused.content[0].text
    revoked_args = {"kind": "skill", "id": "precis-overview"}
    revoke_user_token(store, alice.id, token_id)
    revoked = call("get", revoked_args)
    assert revoked.isError and "no longer active" in revoked.content[0].text
    assert current_principal() is None


@pytest.mark.parametrize("profile", ["typed", "command"])
def test_every_registered_request_handler_has_personal_scope_guard(
    runtime_with_store, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from mcp import types

    mcp = FastMCP("request-coverage-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: profile)
    server._register_tools_from_registry()
    server._wire_modalities(runtime_with_store)

    handlers = mcp._mcp_server.request_handlers
    assert types.CallToolRequest in handlers
    for request_type, handler in handlers.items():
        if request_type in {types.PingRequest, types.ListToolsRequest}:
            continue
        assert handler.__qualname__ == (
            "_install_personal_scope_gate.<locals>.guarded.<locals>.call"
        ), f"unguarded content request: {request_type.__name__}"


@pytest.mark.parametrize("personal", [False, True])
def test_personal_request_guard_only_offloads_personal_callers(
    monkeypatch: pytest.MonkeyPatch, personal: bool
) -> None:
    from mcp import types

    mcp = FastMCP("guard-thread-test")
    ctx = _principal_ctx(7, 11) if personal else cast(Context, SimpleNamespace())
    monkeypatch.setattr(mcp, "get_context", lambda: ctx)
    refusal_threads: list[int] = []
    handler_threads: list[int] = []

    def refusal(_request: Any, _ctx: Context) -> None:
        refusal_threads.append(threading.get_ident())

    expected = types.ServerResult(types.ListPromptsResult(prompts=[]))

    async def handler(_request: Any) -> types.ServerResult:
        handler_threads.append(threading.get_ident())
        return expected

    monkeypatch.setattr(server, "_personal_request_refusal", refusal)
    mcp._mcp_server.request_handlers[types.ListPromptsRequest] = handler
    server._install_personal_scope_gate(mcp)
    event_loop_thread = threading.get_ident()

    async def exercise() -> None:
        result = await mcp._mcp_server.request_handlers[types.ListPromptsRequest](
            types.ListPromptsRequest(method="prompts/list")
        )
        assert result is expected

    asyncio.run(exercise())
    assert handler_threads == [event_loop_thread]
    if personal:
        assert len(refusal_threads) == 1
        assert refusal_threads[0] != event_loop_thread
    else:
        assert refusal_threads == []
    assert current_principal() is None


@pytest.mark.parametrize("profile", ["typed", "command"])
def test_personal_token_mixed_wire_envelopes_never_reach_public_dispatch(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    alice = _user(store)
    _token_id, personal = mint_user_token(store, alice.id, "mixed envelope test")
    public = Mock(side_effect=AssertionError("private call reached public dispatch"))
    monkeypatch.setattr(runtime_with_store, "_dispatch_status", public)
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    mcp = FastMCP("mixed-envelope-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: profile)
    server._register_tools_from_registry()
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store)
    payloads: tuple[dict[str, Any], ...] = (
        {"kind": "learning", "args": {"kind": "memory"}},
        {"kind": "learning", "args": {"kinds": ["memory"]}},
        {"kind": "memory", "id": "learning:PRIVATE_SCOPE_SENTINEL"},
    )

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            session = await _initialize(client, personal)
            for call_id, payload in enumerate(payloads, start=2):
                name, arguments = "get", payload
                if profile == "command":
                    parts = ", ".join(
                        f"{key}={value!r}" for key, value in payload.items()
                    )
                    name, arguments = "precis", {"command": f"get({parts})"}
                response = await _call(
                    client, session, personal, call_id, name=name, arguments=arguments
                )
                result = response.json()["result"]
                assert result["isError"]
                assert "[error:BadInput]" in result["content"][0]["text"]
                assert "PRIVATE_SCOPE_SENTINEL" not in response.text
                public.assert_not_called()

    asyncio.run(exercise())


def test_personal_token_cannot_bypass_tools_through_modalities(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = _user(store)
    _token_id, personal = mint_user_token(store, alice.id, "modality test")
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    mcp = FastMCP("personal-modality-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: "typed")
    server._register_tools_from_registry()
    server._wire_modalities(runtime_with_store)
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store)
    requests: tuple[tuple[str, dict[str, Any]], ...] = (
        ("prompts/list", {}),
        ("resources/list", {}),
        ("resources/templates/list", {}),
        ("prompts/get", {"name": "precis-learning-help"}),
        ("resources/read", {"uri": "precis://skill/precis-learning-help"}),
    )

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            for credential in (personal, "install"):
                session = await _initialize(client, credential)
                for call_id, (method, params) in enumerate(requests, start=2):
                    response = await client.post(
                        "/mcp",
                        headers=_headers(credential, session),
                        json={
                            "jsonrpc": "2.0",
                            "id": call_id,
                            "method": method,
                            "params": params,
                        },
                    )
                    envelope = response.json()
                    if credential == personal:
                        assert "[error:Denied]" in envelope["error"]["message"]
                        assert "get(kind='learning')" in envelope["error"]["message"]
                    else:
                        assert "result" in envelope and "error" not in envelope

    asyncio.run(exercise())


@pytest.mark.parametrize("profile", ["typed", "command"])
def test_personal_help_exception_checks_supplied_wire_keys(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    alice = _user(store)
    _token_id, personal = mint_user_token(store, alice.id, "wire help test")
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    mcp = FastMCP("personal-help-wire-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: profile)
    server._register_tools_from_registry()
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            session = await _initialize(client, personal)
            extras: tuple[dict[str, Any], ...] = (
                {},
                {"view": "verbatim"},
                {"view": None},
                {"args": {}},
                {"args": None},
                {"unknown": "PRIVATE_SCOPE_SENTINEL"},
                {"unknown": None},
            )
            for call_id, extra in enumerate(extras, start=2):
                arguments = {"kind": "skill", "id": "precis-learning-help", **extra}
                name = "get"
                if profile == "command":
                    parts = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
                    arguments = {"command": f"get({parts})"}
                    name = "precis"
                response = await _call(
                    client, session, personal, call_id, name=name, arguments=arguments
                )
                result = response.json()["result"]
                if extra:
                    assert result["isError"]
                    assert "[error:Denied]" in result["content"][0]["text"]
                    assert "PRIVATE_SCOPE_SENTINEL" not in response.text
                else:
                    assert not result.get("isError", False)
                    assert "private study activity" in result["content"][0]["text"]

    asyncio.run(exercise())


def test_http_mcp_sessions_pin_owner_and_separate_concurrent_requests(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Separate sessions isolate owners; every reused session checks its owner."""
    alice, bob = _user(store), _user(store, "bob")
    token_id, alice_raw = mint_user_token(store, alice.id, "Alice client")
    _bob_token_id, bob_raw = mint_user_token(store, bob.id, "Bob client")
    barrier = threading.Barrier(2)

    class Runtime:
        def __init__(self) -> None:
            self.store = store

        def dispatch_with_status(self, verb, payload, *, fold=False):
            owner = require_user(self.store)
            if payload.get("id") == "/concurrent":
                barrier.wait(timeout=10)
                assert require_user(self.store) == owner
            if payload.get("id") == "/error":
                raise RuntimeError("private failure")
            return str(owner), False

        @staticmethod
        def render_error(exc):
            return f"[error:{type(exc).__name__}]"

    monkeypatch.setattr(core, "_get_runtime", lambda: Runtime())
    mcp = FastMCP("personal-identity-test", json_response=True)
    mcp.tool(**server._TOOL_KW)(server._offload_sync(core.get))
    app = mcp.streamable_http_app()
    from precis import slice_tokens

    slice_raw = slice_tokens.mint(tmp_path, "td42")
    server._install_token_auth(app, token="install", store=store, slice_dir=tmp_path)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            session = await _initialize(client, alice_raw)
            bob_session = await _initialize(client, bob_raw)
            install_session = await _initialize(client, "install")
            slice_session = await _initialize(client, slice_raw)
            first = await _call(client, session, alice_raw, 2)
            assert first.json()["result"]["content"][0]["text"] == str(alice.id)
            second = await _call(client, bob_session, bob_raw, 3)
            assert second.json()["result"]["content"][0]["text"] == str(bob.id)
            anonymous = await _call(
                client,
                install_session,
                "install",
                4,
                _meta={"precis": {"user_id": alice.id, "owner": "alice"}},
            )
            assert anonymous.json()["result"]["isError"] is True
            assert "Denied" in anonymous.text
            anonymous_slice = await _call(client, slice_session, slice_raw, 4)
            assert "Denied" in anonymous_slice.text
            for raw, target in (
                (bob_raw, session),
                ("install", session),
                (alice_raw, install_session),
            ):
                forbidden = await _call(client, target, raw, 4)
                assert forbidden.status_code == 404
                assert forbidden.headers["mcp-session-error"] == "reinitialize"
            assert (await _call(client, slice_session, "install", 4)).status_code == 404
            assert (
                await _call(client, install_session, slice_raw, 4)
            ).status_code == 404
            unknown = await _call(client, "client-chosen-session", alice_raw, 4)
            assert unknown.status_code == 404
            assert unknown.headers["mcp-session-error"] == "reinitialize"
            a, b = await asyncio.gather(
                _call(client, session, alice_raw, 5, "/concurrent"),
                _call(client, bob_session, bob_raw, 5, "/concurrent"),
            )
            assert a.json()["result"]["content"][0]["text"] == str(alice.id)
            assert b.json()["result"]["content"][0]["text"] == str(bob.id)
            failure = await _call(client, session, alice_raw, 7, "/error")
            assert failure.json()["result"]["isError"] is True
            after_error = await _call(client, install_session, "install", 8)
            assert "Denied" in after_error.text
            revoke_user_token(store, alice.id, token_id)
            revoked = await _call(client, session, alice_raw, 9)
            assert revoked.status_code == 401
            store.set_web_user_disabled(bob.login, disabled=True)
            disabled = await _call(client, bob_session, bob_raw, 10)
            assert disabled.status_code == 401

    asyncio.run(exercise())
    assert current_principal() is None


@pytest.mark.parametrize("json_response", [True, False])
def test_duplicate_rpc_id_cannot_redirect_private_response_or_stream(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog, json_response: bool
) -> None:
    """Attack the real SDK response-ID router while a private call is blocked."""
    alice, bob = _user(store), _user(store, "bob")
    _alice_token, alice_raw = mint_user_token(store, alice.id, "Alice")
    _bob_token, bob_raw = mint_user_token(store, bob.id, "Bob")
    secret = "ALICE_PRIVATE_LEARNING_NOTE_DO_NOT_LOG"
    started, release = threading.Event(), threading.Event()
    entered: list[int | None] = []

    class Runtime:
        def __init__(self) -> None:
            self.store = store

        def dispatch_with_status(self, verb, payload, *, fold=False):
            principal = current_principal()
            entered.append(principal.user_id if principal else None)
            if principal is None:
                raise Denied("no owner", next="use a personal token")
            if payload.get("id") == "/blocked":
                started.set()
                assert release.wait(timeout=10), "test did not release private request"
            return secret, False

        @staticmethod
        def render_error(exc):
            return f"[error:{type(exc).__name__}]"

    monkeypatch.setattr(core, "_get_runtime", lambda: Runtime())
    mcp = FastMCP("response-routing-test", json_response=json_response)
    mcp.tool(**server._TOOL_KW)(server._offload_sync(core.get))
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            session = await _initialize(client, alice_raw)
            blocked = asyncio.create_task(
                _call(client, session, alice_raw, 99, "/blocked")
            )
            try:
                assert await asyncio.to_thread(started.wait, 5)
                for raw in ("install", bob_raw):
                    attack = await _call(client, session, raw, 99)
                    assert attack.status_code == 404, attack.text
                    assert attack.headers["mcp-session-error"] == "reinitialize"
                    assert secret not in attack.text
                    # GET can subscribe to responses and DELETE terminates
                    # a session; neither may bypass the immutable owner.
                    for method in ("GET", "DELETE"):
                        attack = await client.request(
                            method, "/mcp", headers=_headers(raw, session)
                        )
                        assert attack.status_code == 404
                        assert secret not in attack.text
            finally:
                release.set()
            response = await blocked
            assert response.status_code == 200
            assert _rpc_body(response)["result"]["content"][0]["text"] == secret
            assert entered == [alice.id], (
                "a mismatched request reached SDK tool dispatch"
            )
            anonymous = await _initialize(client, "install")
            denied = await _call(
                client,
                anonymous,
                "install",
                101,
                arguments={"kind": "learning", "q": secret},
            )
            assert _rpc_body(denied)["result"]["isError"] is True
            terminated = await client.delete(
                "/mcp", headers=_headers(alice_raw, session)
            )
            assert terminated.status_code == 200
            expired = await _call(client, session, alice_raw, 102)
            assert expired.status_code == 404
            assert expired.headers["mcp-session-error"] == "reinitialize"

    with caplog.at_level(logging.DEBUG):
        asyncio.run(exercise())
        logging.getLogger("identity-test-application").debug(
            "application diagnostics alive"
        )
    assert secret not in caplog.text
    assert "application diagnostics alive" in caplog.text
    assert current_principal() is None


def test_personal_tokens_refuse_legacy_sse_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def legacy(request):
        return PlainTextResponse("legacy install access")

    monkeypatch.setattr(
        mcp_identity, "resolve_user_token", lambda store, raw: Principal(7, 1)
    )
    app = Starlette(
        routes=[Route("/sse", legacy), Route("/messages/", legacy, methods=["POST"])]
    )
    server._install_token_auth(app, token="install", store=object(), personal_path=None)
    with TestClient(app) as client:
        for method, path in (("GET", "/sse"), ("POST", "/messages/")):
            personal = client.request(
                method, path, headers={"Authorization": "Bearer precis_user_test"}
            )
            assert personal.status_code == 403
            assert "streamable HTTP" in personal.text
            assert (
                client.request(
                    method, path, headers={"Authorization": "Bearer install"}
                ).status_code
                == 200
            )


def test_unsupported_sdk_disables_personal_tokens_but_preserves_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def probe(request):
        assert mcp_identity.SCOPE_USER_ID not in request.scope
        return PlainTextResponse("install works")

    monkeypatch.setattr(server, "_sdk_session_ownership_supported", lambda: False)
    app = Starlette(routes=[Route("/mcp", probe)])
    server._install_token_auth(app, token="install", store=object())
    with TestClient(app) as client:
        personal = client.get(
            "/mcp", headers={"Authorization": "Bearer precis_user_test"}
        )
        assert personal.status_code == 503
        assert "uv sync" in personal.json()["next"]
        assert (
            client.get("/mcp", headers={"Authorization": "Bearer install"}).status_code
            == 200
        )


def test_sdk_owner_capability_requires_version_and_actual_validation_hook(
    monkeypatch,
) -> None:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    assert server._sdk_session_ownership_supported()
    monkeypatch.setattr("importlib.metadata.version", lambda package: "1.29.0")
    assert not server._sdk_session_ownership_supported()
    monkeypatch.setattr("importlib.metadata.version", lambda package: "1.30.0")
    monkeypatch.setattr(
        StreamableHTTPSessionManager, "_handle_stateful_request", lambda *args: None
    )
    assert not server._sdk_session_ownership_supported()
