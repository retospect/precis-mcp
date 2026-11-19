"""Client capability tokens (gr595554): the third bearer class.

A ``precis_cap_`` credential names a consumer — a chat connector, a feed
listener — never a person. These cover the store surface (digest-only
persistence, owner-scoped mint/list/revoke, fail-closed resolution), the
scope grammar and the (verb, kinds) judgement, and the feed fold: the
podcast ``?t=`` credential is now a ``feed``-scoped row on the same table.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast
from unittest.mock import Mock

import pytest

from precis.errors import BadInput, Denied
from precis.mcp_identity import (
    CAP_TOKEN_PREFIX,
    CREDENTIAL_CLASSES,
    CapabilityIdentity,
    capability_call_allowed,
    credential_classes,
    list_capability_tokens,
    mint_capability_token,
    parse_capability_scopes,
    resolve_capability_token,
    revoke_capability_token,
)
from precis.store import Store
from precis.users import FEED_TOKEN_PREFIX, hash_password, mint_feed_token


def _user(store: Store, login: str = "alice"):
    return store.create_web_user(
        login=login, abbrev=login[:2], password=hash_password("password")
    )


def test_mint_persists_only_the_digest_and_owner_metadata(store: Store) -> None:
    alice, bob = _user(store), _user(store, "bob")
    token_id, raw = mint_capability_token(store, alice.id, "claude.ai chat", ("read",))
    _, other_raw = mint_capability_token(store, alice.id, "reader", ("read",))
    assert raw.startswith(CAP_TOKEN_PREFIX) and raw != other_raw
    resolved = resolve_capability_token(store, raw)
    assert resolved is not None
    assert resolved.token_id == token_id and resolved.user_id == alice.id
    assert resolved.label == "claude.ai chat" and resolved.scopes == ("read",)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT token_sha256 FROM web_user_capability_tokens WHERE id = %s",
            (token_id,),
        ).fetchone()
    assert row == (hashlib.sha256(raw.encode("utf-8")).hexdigest(),)
    assert {t.label for t in list_capability_tokens(store, alice.id)} == {
        "claude.ai chat",
        "reader",
    }
    assert list_capability_tokens(store, bob.id) == []


def test_revocation_and_disabled_or_deleted_owners_fail_closed(store: Store) -> None:
    alice = _user(store)
    token_id, raw = mint_capability_token(store, alice.id, "listener", ("feed",))
    assert resolve_capability_token(store, raw) is not None
    assert revoke_capability_token(store, alice.id, token_id)
    assert not revoke_capability_token(store, alice.id, token_id)
    assert resolve_capability_token(store, raw) is None

    _, raw2 = mint_capability_token(store, alice.id, "other", ("read",))
    store.set_web_user_disabled("alice", disabled=True)
    assert resolve_capability_token(store, raw2) is None
    store.set_web_user_disabled("alice", disabled=False)
    assert resolve_capability_token(store, raw2) is not None
    store.delete_web_user("alice")
    assert resolve_capability_token(store, raw2) is None


def test_owner_scoped_revoke_cannot_touch_a_foreign_token(store: Store) -> None:
    alice, bob = _user(store), _user(store, "bob")
    token_id, raw = mint_capability_token(store, alice.id, "mine", ("read",))
    assert not revoke_capability_token(store, bob.id, token_id)
    assert resolve_capability_token(store, raw) is not None


def test_malformed_and_unprefixed_bearers_never_resolve(store: Store) -> None:
    alice = _user(store)
    _, raw = mint_capability_token(store, alice.id, "x", ("read",))
    for bad in (
        "",
        "precis_cap_",
        "precis_user_" + raw.removeprefix(CAP_TOKEN_PREFIX),
        raw[:-1] + ("A" if raw[-1] != "A" else "B"),
        "precis_cap_" + "!" * 43,
    ):
        assert resolve_capability_token(store, bad) is None


def test_label_validation(store: Store) -> None:
    alice = _user(store)
    for label in ("", "   ", "x" * 101):
        with pytest.raises(BadInput):
            mint_capability_token(store, alice.id, label, ("read",))
    token_id, _ = mint_capability_token(store, alice.id, "  ok  ", ("read",))
    assert list_capability_tokens(store, alice.id)[0].label == "ok"


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_credential_classes_blank_means_every_class(value: str | None) -> None:
    assert credential_classes(value) == frozenset(CREDENTIAL_CLASSES)
    assert set(CREDENTIAL_CLASSES) == {
        "install",
        "slice",
        "personal",
        "capability",
        "oauth",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("oauth", {"oauth"}),
        ("oauth,install", {"oauth", "install"}),
        ("oauth install", {"oauth", "install"}),
        (" oauth ,\tslice\n capability ", {"oauth", "slice", "capability"}),
        ("OAuth, PERSONAL", {"oauth", "personal"}),
        ("oauth,oauth", {"oauth"}),
    ],
)
def test_credential_classes_separators_and_case(value: str, expected: set[str]) -> None:
    assert credential_classes(value) == frozenset(expected)


@pytest.mark.parametrize(
    "value", ["bearer", "oauth,bearer", "oauth;install", "oauth:", ",", " , ,\n"]
)
def test_credential_classes_unknown_name_names_the_variable(value: str) -> None:
    with pytest.raises(ValueError, match="PRECIS_MCP_CREDENTIAL_CLASSES") as err:
        credential_classes(value)
    for name in CREDENTIAL_CLASSES:
        assert name in str(err.value)


def test_scope_parser_accepts_the_grammar_and_dedupes() -> None:
    assert parse_capability_scopes("") == ("read",)
    assert parse_capability_scopes("  , ") == ("read",)
    assert parse_capability_scopes("feed") == ("feed",)
    assert parse_capability_scopes("feed, feed") == ("feed",)
    assert parse_capability_scopes(["write:memory", "read:paper"]) == (
        "write:memory",
        "read:paper",
    )
    for bad in (
        "write",
        "read:",
        "kind:paper",
        "read:UPPER",
        "read:" + "k" * 65,
        "delete:memory",
        "admin",
    ):
        with pytest.raises(BadInput):
            parse_capability_scopes(bad)


def test_scope_parser_keeps_feed_exclusive() -> None:
    """``feed`` rides in a URL, so it never shares a token with an MCP
    scope (gr730564); the refusal points at a separate feed-only mint."""
    for bad in (
        "read,feed",
        "feed,read",
        "feed,write:memory",
        "feed,read:paper",
        ["write:memory", "feed"],
    ):
        with pytest.raises(BadInput, match="feed") as exc:
            parse_capability_scopes(bad)
        assert "separate feed-only token" in (exc.value.next or "")


def test_the_minter_keeps_feed_exclusive_too() -> None:
    """A caller that skips the parser (gr730564) cannot mint a mixed token:
    the refusal fires before the store is touched."""
    store = Mock()
    for bad in (("read", "feed"), ("feed", "write:memory"), ("feed", "read:paper")):
        with pytest.raises(BadInput, match="feed") as exc:
            mint_capability_token(cast(Any, store), 1, "listener", bad)
        assert "separate feed-only token" in (exc.value.next or "")
    store.pool.connection.assert_not_called()


def test_scope_parser_refuses_private_kinds() -> None:
    # A capability token binds no personal principal — a private-kind
    # scope could never authorize the call, so mint refuses the lie.
    for bad in ("read:learning", "write:learning"):
        with pytest.raises(BadInput, match="private kind"):
            parse_capability_scopes(bad)


def test_capability_call_allowed_matrix() -> None:
    read = ("read",)
    assert capability_call_allowed(read, "get", {"paper"})
    assert capability_call_allowed(read, "search", {"paper", "memory"})
    assert capability_call_allowed(read, "search", None)
    assert capability_call_allowed(read, "get", None)
    assert not capability_call_allowed(read, "put", {"paper"})

    assert capability_call_allowed(("read:paper",), "get", {"paper"})
    assert not capability_call_allowed(("read:paper",), "search", {"paper", "memory"})
    assert not capability_call_allowed(("read:paper",), "search", None)

    # write:<kind> covers all seven verbs on that kind, including reads.
    wr = ("write:memory",)
    for verb in ("put", "edit", "delete", "tag", "link", "get", "search"):
        assert capability_call_allowed(wr, verb, {"memory"})
    assert not capability_call_allowed(wr, "put", {"paper"})

    # feed grants no MCP verb; an unknown verb is always refused.
    assert not capability_call_allowed(("feed",), "get", {"paper"})
    assert not capability_call_allowed(("read",), "whoami-verb", {"profile"})


def test_feed_fold_mints_resolves_and_rotates_in_the_class_table(store: Store) -> None:
    alice = _user(store)
    token, digest = mint_feed_token()
    assert token.startswith(FEED_TOKEN_PREFIX)
    assert store.set_web_user_feed_token("alice", digest)
    found = store.get_web_user_by_feed_token(digest)
    assert found is not None and found.login == "alice"
    reloaded = store.get_web_user("alice")
    assert reloaded is not None and reloaded.has_feed_token

    # The folded row is visible as a labelled feed-scoped capability token.
    listed = list_capability_tokens(store, alice.id)
    assert len(listed) == 1
    assert listed[0].label == "podcast feed" and listed[0].scopes == ("feed",)

    # Rotation revokes the old feed credential; clearing revokes the rest.
    _, second = mint_feed_token()
    store.set_web_user_feed_token("alice", second)
    assert store.get_web_user_by_feed_token(digest) is None
    assert store.get_web_user_by_feed_token(second) is not None
    assert store.set_web_user_feed_token("alice", None)
    reloaded = store.get_web_user("alice")
    assert reloaded is not None and not reloaded.has_feed_token
    assert store.get_web_user_by_feed_token(second) is None


def test_cap_minted_feed_scope_resolves_on_the_podcast_path(store: Store) -> None:
    """A /account ``precis_cap_`` with the feed scope is a ``?t=`` credential."""
    alice = _user(store)
    _, raw = mint_capability_token(store, alice.id, "phone app", ("feed",))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    found = store.get_web_user_by_feed_token(digest)
    assert found is not None and found.id == alice.id


def _insert_mixed_feed_row(store: Store, user_id: int, scopes: list[str]) -> str:
    """A legacy row minted before feed turned exclusive: feed plus an MCP scope."""
    _, digest = mint_feed_token()
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO web_user_capability_tokens"
            " (user_id, label, token_sha256, scopes) VALUES (%s, %s, %s, %s)",
            (user_id, "legacy mix", digest, scopes),
        )
    return digest


def test_feed_only_row_authenticates_and_sets_has_feed_token(store: Store) -> None:
    alice = _user(store)
    _, raw = mint_capability_token(store, alice.id, "phone app", ("feed",))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    found = store.get_web_user_by_feed_token(digest)
    assert found is not None and found.has_feed_token


def test_mixed_scope_row_is_not_a_feed_credential(store: Store) -> None:
    alice = _user(store)
    for scopes in (["read", "feed"], ["feed", "write:memory"], ["feed", "read:paper"]):
        digest = _insert_mixed_feed_row(store, alice.id, scopes)
        assert store.get_web_user_by_feed_token(digest) is None, scopes
    reloaded = store.get_web_user("alice")
    assert reloaded is not None and not reloaded.has_feed_token


def test_rotating_the_feed_token_still_revokes_a_mixed_row(store: Store) -> None:
    alice = _user(store)
    digest = _insert_mixed_feed_row(store, alice.id, ["read", "feed"])
    _, fresh = mint_feed_token()
    assert store.set_web_user_feed_token("alice", fresh)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT revoked_at IS NOT NULL FROM web_user_capability_tokens"
            " WHERE token_sha256 = %s",
            (digest,),
        ).fetchone()
    assert row == (True,)
    assert store.get_web_user_by_feed_token(fresh) is not None


def test_dispatch_backstop_judges_the_normalized_kinds() -> None:
    """``_capability_refusal`` allows inside the allow-list, refuses outside."""
    from precis.tools.core import _capability_refusal

    cap = CapabilityIdentity(1, 1, "reader", ("read",))
    assert _capability_refusal(cap, "search", {"q": "x"}) is None
    denied = _capability_refusal(cap, "put", {"kind": "memory"})
    assert isinstance(denied, Denied)
    cap_w = CapabilityIdentity(2, 1, "writer", ("write:memory",))
    assert _capability_refusal(cap_w, "put", {"kind": "memory"}) is None
    assert isinstance(_capability_refusal(cap_w, "put", {"kind": "paper"}), Denied)
    # A non-CapabilityIdentity stamp fails closed.
    assert isinstance(_capability_refusal(object(), "get", {"kind": "paper"}), Denied)


def test_mint_for_a_missing_or_disabled_account_refuses(store: Store) -> None:
    with pytest.raises(Denied):
        mint_capability_token(store, 999999, "ghost", ("read",))
    alice = _user(store)
    store.set_web_user_disabled("alice", disabled=True)
    with pytest.raises(Denied):
        mint_capability_token(store, alice.id, "ghost", ("read",))


# ── over the wire ────────────────────────────────────────────────────


def _headers(raw: str, session: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {raw}",
    }
    if session is not None:
        headers["Mcp-Session-Id"] = session
    return headers


async def _initialize(client, raw: str) -> str:
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
                "clientInfo": {"name": "cap-test", "version": "1"},
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


async def _call(client, session, raw, call_id, name, arguments):
    return await client.post(
        "/mcp",
        headers=_headers(raw, session),
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def test_capability_bearer_gates_calls_over_http(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``precis_cap_`` session reaches only its allow-list, end to end."""
    import asyncio

    import httpx
    from mcp.server.fastmcp import FastMCP

    from precis import server
    from precis.tools import core

    alice = _user(store)
    _, reader = mint_capability_token(store, alice.id, "reader", ("read",))
    _, writer = mint_capability_token(store, alice.id, "mem-writer", ("write:memory",))
    dispatched = Mock(return_value=("handler reached", False))
    monkeypatch.setattr(runtime_with_store, "dispatch_with_status", dispatched)
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    mcp = FastMCP("cap-scope-test", json_response=True)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: "typed")
    server._register_tools_from_registry()
    app = mcp.streamable_http_app()
    server._install_token_auth(app, token="install", store=store)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:8000",
            ) as client,
        ):
            session = await _initialize(client, reader)
            ok = await _call(client, session, reader, 2, "search", {"q": "anything"})
            assert ok.json()["result"]["content"][0]["text"] == "handler reached"
            refused = await _call(
                client, session, reader, 3, "put", {"kind": "memory", "text": "x"}
            )
            result = refused.json()["result"]
            assert (
                result["isError"]
                and "does not permit this call" in (result["content"][0]["text"])
            )

            session = await _initialize(client, writer)
            ok = await _call(
                client, session, writer, 4, "put", {"kind": "memory", "text": "x"}
            )
            assert ok.json()["result"]["content"][0]["text"] == "handler reached"
            refused = await _call(
                client, session, writer, 5, "put", {"kind": "paper", "text": "x"}
            )
            assert refused.json()["result"]["isError"]

            # A revoked credential stops authenticating on the next request.
            token_id = next(
                t.id
                for t in list_capability_tokens(store, alice.id)
                if t.label == "reader"
            )
            assert revoke_capability_token(store, alice.id, token_id)
            dead = await client.post(
                "/mcp",
                headers=_headers(reader),
                json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}},
            )
            assert dead.status_code == 401

            # Capability credentials refuse the legacy SSE path like
            # personal ones do.
            legacy = await client.post("/messages/", headers=_headers(writer))
            assert legacy.status_code == 403

    asyncio.run(exercise())
    assert dispatched.call_count == 2


def test_kind_gate_rejudges_the_kind_a_handle_resolves_to() -> None:
    """A ``write:paper`` token must not edit a datasheet through a converted
    ``pa<id>`` handle: the runtime re-runs the gate once handle routing has
    rewritten ``kind=``, before any handler runs."""
    from precis.runtime.dispatch import _KIND_GATE, DispatchMixin
    from precis.tools.core import _capability_refusal

    cap = CapabilityIdentity(3, 1, "papers", ("write:paper",))

    def gate_for(stamp: CapabilityIdentity):
        def run(verb: str, args: dict) -> None:
            denied = _capability_refusal(stamp, verb, args)
            if denied is not None:
                raise denied

        return run

    class _Rt:
        def _maybe_split_prefixed_id(self, args, *, verb=None):
            args["kind"] = "datasheet"  # the converted handle's new kind

        def _dispatch_resolved_kind(self, verb, args):
            raise AssertionError("handler reached past the gate")

    token = _KIND_GATE.set(gate_for(cap))
    try:
        with pytest.raises(Denied):
            DispatchMixin._dispatch_inner_core(
                cast(Any, _Rt()),
                "edit",
                {"kind": "paper", "id": "pa12"},
            )
    finally:
        _KIND_GATE.reset(token)


def test_kind_gate_rejudges_the_kind_a_retired_memory_redirects_to() -> None:
    """A ``read:memory`` token must not follow a retired memory's successor
    into ``md``: the gate re-runs after the tombstone redirect."""
    from precis.runtime.dispatch import _KIND_GATE, DispatchMixin
    from precis.tools.core import _capability_refusal

    cap = CapabilityIdentity(3, 1, "mem", ("read:memory",))

    def run(verb: str, args: dict) -> None:
        denied = _capability_refusal(cap, verb, args)
        if denied is not None:
            raise denied

    class _Rt:
        def _maybe_split_prefixed_id(self, args, *, verb=None):
            pass

        def _redirect_retired_memory(self, args):
            args["kind"], args["id"] = "md", "docs/x.md"

        def _dispatch_resolved_kind(self, verb, args):
            raise AssertionError("handler reached past the gate")

    token = _KIND_GATE.set(run)
    try:
        with pytest.raises(Denied):
            DispatchMixin._dispatch_inner_core(
                cast(Any, _Rt()), "get", {"kind": "memory", "id": "12"}
            )
    finally:
        _KIND_GATE.reset(token)


def test_recall_live_feed_token_requires_the_exact_vaulted_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another active feed row must not vouch for a revoked vaulted token."""
    from precis import users

    token, digest = mint_feed_token()
    monkeypatch.setattr(users, "recall_feed_token", lambda login, *, store: token)
    store = Mock()
    store.get_web_user_by_feed_token.return_value = None  # vaulted one revoked
    assert users.recall_live_feed_token("alice", store=store) is None
    store.get_web_user_by_feed_token.assert_called_once_with(digest)

    store.get_web_user_by_feed_token.return_value = Mock(login="alice")
    assert users.recall_live_feed_token("alice", store=store) == token
    store.get_web_user_by_feed_token.return_value = Mock(login="bob")
    assert users.recall_live_feed_token("alice", store=store) is None


@pytest.mark.parametrize("successor", ["md", "python", "memory"])
@pytest.mark.parametrize("fails", [False, True])
def test_resource_guard_binds_resolved_kind_gate(
    monkeypatch: pytest.MonkeyPatch, successor: str, fails: bool
) -> None:
    """Resource dispatch cannot escape read:memory through a successor."""
    import asyncio
    from types import SimpleNamespace

    import anyio
    from mcp import types
    from mcp.server.fastmcp import FastMCP
    from pydantic import AnyUrl

    from precis import server
    from precis.mcp_identity import SCOPE_CAPABILITY
    from precis.mcp_modalities import _read_resource
    from precis.response import Response
    from precis.runtime.dispatch import _KIND_GATE, DispatchMixin

    cap = CapabilityIdentity(3, 1, "mem", ("read:memory",))
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(scope={SCOPE_CAPABILITY: cap})
        )
    )
    mcp = FastMCP("resource-gate-test")
    monkeypatch.setattr(mcp, "get_context", lambda: ctx)
    reached: list[str] = []

    class Runtime:
        hub = SimpleNamespace(handler_for=lambda kind: None)

        def dispatch(self, verb: str, args: dict[str, Any], *, fold: bool) -> str:
            return DispatchMixin._dispatch_inner_core(cast(Any, self), verb, args).body

        def _maybe_split_prefixed_id(self, args: dict[str, Any], *, verb: str) -> None:
            pass

        def _redirect_retired_memory(self, args: dict[str, Any]) -> None:
            args["kind"], args["id"] = successor, "successor"

        def _dispatch_resolved_kind(self, verb: str, args: dict[str, Any]) -> Response:
            reached.append(args["kind"])
            if fails:
                raise RuntimeError("handler failed")
            return Response(body="allowed")

    async def handler(request: Any) -> Any:
        # SDK synchronous resource callbacks may run in a worker thread.
        body = await anyio.to_thread.run_sync(
            _read_resource, cast(Any, Runtime()), str(request.params.uri)
        )
        return body

    mcp._mcp_server.request_handlers[types.ReadResourceRequest] = handler
    server._install_personal_scope_gate(mcp)

    async def exercise() -> None:
        assert _KIND_GATE.get() is None
        request = types.ReadResourceRequest(
            method="resources/read",
            params=types.ReadResourceRequestParams(uri=AnyUrl("precis://memory/12")),
        )
        guarded = mcp._mcp_server.request_handlers[types.ReadResourceRequest]
        if successor != "memory":
            with pytest.raises(Denied):
                await guarded(request)
            assert reached == []
        elif fails:
            with pytest.raises(RuntimeError, match="handler failed"):
                await guarded(request)
            assert reached == ["memory"]
        else:
            assert await guarded(request) == "allowed"
            assert reached == ["memory"]
        assert _KIND_GATE.get() is None

    asyncio.run(exercise())
