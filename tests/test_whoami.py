"""The whoami read — get(kind='profile', id='me') (gr633675).

Answers "which user does the server think this session is": the
authenticated account, the credential carrying it (label included), the
permission scope, and the write attribution every verb from the session
records. Anonymous sessions render their shared-credential shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import Context

from precis import mcp_identity
from precis.mcp_identity import mint_user_token
from precis.store import Store
from precis.tools import core
from precis.user_context import current_principal
from precis.users import hash_password


def _user(store: Store, login: str = "alice"):
    return store.create_web_user(
        login=login, abbrev=login[:2], password=hash_password("test password")
    )


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


def test_whoami_anonymous_renders_shared_credential(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    body = cast(str, core.get(kind="profile", id="me"))
    assert "# whoami" in body
    assert "user: (anonymous — no authenticated account on this session)" in body
    assert "shared server credential" in body
    # The in-process fallback: no bound request, no ambient source.
    assert "writes recorded as: worker:cli" in body
    assert "revision actor: agent" in body
    assert "client: (none declared)" in body
    assert current_principal() is None


def test_whoami_refuses_view(store: Store, runtime_with_store, monkeypatch) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    result = cast(Any, core.get(kind="profile", id="me", view="raw"))
    assert result.isError and "[error:Unsupported]" in result.content[0].text
    assert "takes no view=" in result.content[0].text


def test_whoami_personal_token_names_owner_label_and_scope(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = _user(store)
    token_id, _raw = mint_user_token(store, alice.id, "Laptop")
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    ctx = _principal_ctx(alice.id, token_id)
    body = cast(str, core.get(kind="profile", id="me", ctx=ctx))
    assert f"user: al (login: alice, id: {alice.id})" in body
    assert f"credential: personal MCP token {token_id} 'Laptop'" in body
    assert "permission: learning calls, learning help, whoami" in body
    assert f"writes recorded as: personal:{token_id}" in body
    assert "revision actor: user:al" in body
    # The bare profile listing stays outside the personal-token boundary.
    refused = cast(Any, core.get(kind="profile", ctx=ctx))
    assert refused.isError and "[error:Denied]" in refused.content[0].text
    assert current_principal() is None


def test_whoami_does_not_resolve_a_branding_profile_named_me(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'me' is reserved for the whoami read even when a profile carries the
    name — the session identity is the only 'me' the wire should resolve."""
    from precis.handlers.profile import ProfileHandler

    _user(store, "alice")
    store.upsert_kinds([ProfileHandler.spec])
    store.insert_ref(
        kind="profile",
        slug=None,
        title="me",
        meta={"profile": {"name": "me", "palette": {}}},
        owner_login="alice",
    )
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    body = cast(str, core.get(kind="profile", id="me"))
    assert "# whoami" in body


def test_whoami_label_lookup_failure_still_renders_identity(
    store: Store, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead label query degrades the label line, never the read."""
    alice = _user(store)
    token_id, _raw = mint_user_token(store, alice.id, "Laptop")
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    ctx = _principal_ctx(alice.id, token_id)
    monkeypatch.setattr(
        store, "get_web_user_by_id", Mock(side_effect=RuntimeError("db gone"))
    )
    body = cast(str, core.get(kind="profile", id="me", ctx=ctx))
    assert "account row unavailable" in body
    assert f"credential: personal MCP token {token_id} 'Laptop'" in body
