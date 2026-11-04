"""The private MCP kind uses owned records without entering the corpus."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.handlers.learning import LearningHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.user_context import user_scope
from precis.users import PasswordRecord

pytestmark = pytest.mark.db


@pytest.fixture
def learning_runtime(store: Store) -> tuple[PrecisRuntime, int, int]:
    password = PasswordRecord("unused", "unused", "scrypt-v1")
    first = store.create_web_user(login="learner-a", abbrev="la", password=password)
    second = store.create_web_user(login="learner-b", abbrev="lb", password=password)
    hub = Hub(store=store)
    LearningHandler(hub=hub)._register_with(hub)
    return PrecisRuntime(config=PrecisConfig(), hub=hub), first.id, second.id


def _call(runtime: PrecisRuntime, verb: str, **args: Any) -> str:
    body, error = runtime.dispatch_with_status(verb, {"kind": "learning", **args})
    assert not error, body
    return body


def test_mcp_checkin_read_settings_and_foreign_refusal(
    learning_runtime: tuple[PrecisRuntime, int, int],
) -> None:
    runtime, alice, bob = learning_runtime
    with user_scope(alice):
        ack = _call(
            runtime,
            "put",
            id="/checkin",
            __extras__={
                "status": "studied",
                "minutes": 2,
                "note": "Owner-only reading note",
                "idempotency_key": "one-real-action",
            },
        )
        handle = re.search(r"le[0-9]+", ack)
        assert handle
        ident = handle.group()
        record = json.loads(_call(runtime, "get", id=ident))
        assert record["note"] == "Owner-only reading note"
        assert record["minutes"] == 2
        activity = _call(runtime, "get", id="/activity", __extras__={"days": 2})
        assert "studied" in activity and "Owner-only reading note" not in activity
        assert len(activity.encode("utf-8")) < 3500
        settings = json.loads(
            _call(
                runtime,
                "edit",
                id="/settings",
                mode="replace",
                __extras__={"typical_session_minutes": None},
            )
        )
        assert settings["typical_session_minutes"] is None
    with user_scope(bob):
        foreign, bad_foreign = runtime.dispatch_with_status(
            "get", {"kind": "learning", "id": ident}
        )
        missing, bad_missing = runtime.dispatch_with_status(
            "get", {"kind": "learning", "id": "le9000000000"}
        )
        assert bad_foreign and bad_missing and foreign == missing
        assert "Owner-only reading note" not in foreign
        assert (
            json.loads(_call(runtime, "get", id="/settings"))["typical_session_minutes"]
            == 30
        )


def test_owner_args_and_shared_read_modes_cannot_bypass_private_handler(
    learning_runtime: tuple[PrecisRuntime, int, int], store: Store
) -> None:
    runtime, alice, bob = learning_runtime
    with user_scope(alice):
        body, error = runtime.dispatch_with_status(
            "put",
            {
                "kind": "learning",
                "id": "/checkin",
                "__extras__": {
                    "status": "studied",
                    "owner": bob,
                    "idempotency_key": "forged-owner",
                },
            },
        )
        assert error and "owner" in body
        body, error = runtime.dispatch_with_status(
            "get", {"kind": "learning", "id": "/activity", "extent": "fisheye"}
        )
        assert error and "[error:Unsupported]" in body
        body, error = runtime.dispatch_with_status(
            "get",
            {"kind": "learning", "id": "/activity", "__extras__": {"unknown": True}},
        )
        assert error and "[error:BadInput]" in body and "unknown" in body
        body, error = runtime.dispatch_with_status(
            "search", {"kind": "learning", "q": "private"}
        )
        assert error and "[error:Unsupported]" in body
    with store.pool.connection() as conn:
        events = conn.execute("SELECT count(*) FROM learning_events").fetchone()
        assert events is not None and events[0] == 0
        refs = conn.execute(
            "SELECT count(*) FROM refs WHERE kind='learning'"
        ).fetchone()
        assert refs is not None and refs[0] == 0
        calls = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE kind='learning'"
        ).fetchone()
        assert calls is not None and calls[0] == 0
