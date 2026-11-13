"""Client capability tokens (class 3, gr595554): mint, scope, revoke, feed."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from precis import mcp_identity
from precis.errors import BadInput
from precis.mcp_identity import (
    list_user_tokens,
    mint_capability_token,
    resolve_feed_token,
    resolve_user_token,
    revoke_user_token,
)
from precis.store import Store
from precis.tools import core
from precis.user_context import Capability, user_scope
from precis.users import hash_password
from precis.whoami import CALL as WHOAMI


def _alice(store: Store) -> int:
    return store.create_web_user(
        login="alice", abbrev="al", password=hash_password("password")
    ).id


def _allowed(cap: Capability, verb: str, payload: dict[str, Any]) -> bool:
    with user_scope(1, 1, capability=cap):
        return core._personal_call_allowed(verb, payload)


def test_capability_token_is_digest_only_and_resolves_with_allow_list(
    store: Store,
) -> None:
    uid = _alice(store)
    token_id, raw = mint_capability_token(
        store, uid, "claude.ai chat", kinds=["Paper", "memory"], write=True
    )
    assert raw.startswith(mcp_identity.CAP_PREFIX)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT token_sha256, token_class FROM web_user_mcp_tokens WHERE id = %s",
            (token_id,),
        ).fetchone()
    assert row == (hashlib.sha256(raw.encode()).hexdigest(), "capability")
    principal = resolve_user_token(store, raw)
    assert principal is not None and principal.capability is not None
    assert principal.capability.kinds == frozenset({"paper", "memory"})
    assert "put" in principal.capability.verbs
    listed = list_user_tokens(store, uid)
    assert listed[0].token_class == "capability" and listed[0].label == "claude.ai chat"
    assert revoke_user_token(store, uid, token_id)
    assert resolve_user_token(store, raw) is None


def test_default_capability_is_the_read_surface_minus_private_kinds() -> None:
    cap = Capability(frozenset({"get", "search"}))
    assert _allowed(cap, "get", {"kind": "paper", "id": "pa1"})
    assert _allowed(cap, "search", {"q": "x"})
    assert not _allowed(cap, "put", {"kind": "paper", "text": "x"})
    for kind in ("learning", "email", "profile", "paper,learning"):
        assert not _allowed(cap, "get", {"kind": kind})
    assert not _allowed(cap, "get", {"id": "learning:1"})
    assert _allowed(cap, "get", WHOAMI)


def test_named_kinds_restrict_and_grant_writes() -> None:
    cap = Capability(
        frozenset({"get", "search", "put"}), frozenset({"memory", "learning"})
    )
    assert _allowed(cap, "put", {"kind": "memory", "text": "x"})
    assert _allowed(cap, "get", {"kind": "learning"})
    assert not _allowed(cap, "get", {"kind": "paper"})
    assert not _allowed(cap, "search", {"q": "x"})
    assert not _allowed(cap, "edit", {"kind": "memory", "id": "mc1"})
    assert not _allowed(cap, "get", {"kind": ["memory"]})


def test_write_without_kinds_and_bad_kind_names_refuse(store: Store) -> None:
    uid = _alice(store)
    with pytest.raises(BadInput):
        mint_capability_token(store, uid, "x", write=True)
    with pytest.raises(BadInput):
        mint_capability_token(store, uid, "x", kinds=["Bad Kind!"])
    with pytest.raises(BadInput):
        mint_capability_token(store, uid, "  ")


def test_feed_access_only_for_feed_capability_tokens(store: Store) -> None:
    uid = _alice(store)
    _tid, feed_raw = mint_capability_token(store, uid, "phone", feed=True)
    token_id, chat_raw = mint_capability_token(store, uid, "chat")
    assert resolve_feed_token(store, feed_raw)
    assert not resolve_feed_token(store, chat_raw)
    assert not resolve_feed_token(store, "precis_cap_" + "x" * 43)
    revoke_user_token(store, uid, _tid)
    assert not resolve_feed_token(store, feed_raw)
    assert token_id != _tid
