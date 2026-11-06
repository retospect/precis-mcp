"""Creation origin survives real puts without becoming caller-editable metadata."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from precis import agentlog, filing_origin, serve_ledger, tool_ledger
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert
from precis.utils.inproc_context import TickContext, tick_context


class _Session:
    def __init__(self, model: str | None = None) -> None:
        self.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="origin-test", version="1"),
            meta=SimpleNamespace(model=model),
        )


@pytest.fixture(autouse=True)
def _clean_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(agentlog.ENV_VAR, raising=False)
    monkeypatch.delenv(agentlog.SOURCE_ENV_VAR, raising=False)
    with tick_context(TickContext()), serve_ledger.session_scope(None):
        yield


def _put(runtime: PrecisRuntime, kind: str) -> dict[str, Any]:
    title = f"origin {uuid4().hex}"
    args: dict[str, Any] = {"kind": kind, "text": title}
    if kind == "finding":
        assert runtime.store is not None
        paper = runtime.store.insert_ref(
            kind="paper", slug=f"origin-{uuid4().hex}", title="Origin test source"
        )
        runtime.store.chunks.insert_chunks(
            paper.id, [ChunkInsert(ord=0, text="Origin test passage")]
        )
        args.update(title=title, cited_in=f"pa{paper.id}")
    body, error = runtime.dispatch_with_status("put", args)
    assert not error, body
    assert runtime.store is not None
    with runtime.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE kind = %s AND title = %s",
            (kind, title),
        ).fetchone()
    assert row is not None, body
    return dict(row[0])


@pytest.mark.parametrize("kind", ["gripe", "todo", "finding", "memory"])
def test_bound_put_records_session_and_actor(
    runtime_with_store: PrecisRuntime, kind: str
) -> None:
    with (
        revision_context(actor="origin-tester"),
        serve_ledger.session_scope(_Session()),
    ):
        session_id = tool_ledger._session_id()
        meta = _put(runtime_with_store, kind)
    assert meta["filed_by"] == {
        "actor": "origin-tester",
        "client": "origin-test/1",
        "session_id": session_id,
        "principal": "worker:cli",
    }


def test_unbound_cli_put_records_actor_and_principal(
    runtime_with_store: PrecisRuntime,
) -> None:
    with revision_context(actor="cli-operator"):
        meta = _put(runtime_with_store, "gripe")
    assert meta["filed_by"] == {
        "actor": "cli-operator",
        "principal": "worker:cli",
    }


def test_declared_model_is_labelled(runtime_with_store: PrecisRuntime) -> None:
    with serve_ledger.session_scope(_Session("declared-model")):
        origin = _put(runtime_with_store, "gripe")["filed_by"]
    assert origin["model"] == "declared-model"
    assert origin["model_source"] == "declared"


@pytest.mark.parametrize("kind", ["gripe", "todo", "finding", "memory"])
@pytest.mark.parametrize(
    "model, client",
    [
        ("bad\x00model", None),
        ("bad\ud800model", None),
        ("bad\udfffmodel", None),
        ("m" * 513, f"origin-test/1 (model:{'m' * 80})"),
    ],
    ids=["nul", "high-surrogate", "low-surrogate", "oversized"],
)
def test_invalid_declared_model_does_not_prevent_put(
    runtime_with_store: PrecisRuntime, kind: str, model: str, client: str | None
) -> None:
    with (
        revision_context(actor="origin-tester"),
        serve_ledger.session_scope(_Session(model)),
    ):
        session_id = tool_ledger._session_id()
        meta = _put(runtime_with_store, kind)
    expected: dict[str, Any] = {
        "actor": "origin-tester",
        "session_id": session_id,
        "principal": "worker:cli",
    }
    # The ledger bounds its client label, but NULs/surrogates still invalidate it.
    if client is not None:
        expected["client"] = client
    assert meta["filed_by"] == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("model-\U0001f680", "model-\U0001f680"),
        ("m" * 512, "m" * 512),
        ("m" * 513, None),
        ("bad\x00model", None),
        ("bad\ud800model", None),
        ("bad\udfffmodel", None),
        (10**512 - 1, 10**512 - 1),
        (10**512, None),
        (10**5000, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        ({"model": "nested"}, None),
    ],
    ids=[
        "unicode",
        "string-limit",
        "string-over-limit",
        "nul",
        "high-surrogate",
        "low-surrogate",
        "integer-limit",
        "integer-over-limit",
        "unserializable-integer",
        "nan",
        "infinity",
        "boolean",
        "nested",
    ],
)
def test_identity_values_are_bounded_jsonb_scalars(
    value: Any, expected: str | int | None
) -> None:
    assert filing_origin._read(lambda: value) == expected


def test_oversized_agentlog_id_does_not_prevent_put(store: Store) -> None:
    with tick_context(TickContext(agentlog_id=10**5000, source="test-tick")):
        ref = store.insert_ref(kind="todo", slug=None, title="oversized origin")
    assert ref.meta["filed_by"] == {
        "source": "test-tick",
        "principal": "worker:test-tick",
    }


def test_agentlog_model_and_source_are_recorded(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    run = store.insert_ref(
        kind="agentlog",
        slug=None,
        title="origin test run",
        meta={"model": "requested-model", "model_actual": "recorded-model"},
    )
    with tick_context(TickContext(agentlog_id=run.id, source="test-tick")):
        origin = _put(runtime_with_store, "todo")["filed_by"]
    assert origin["agentlog_id"] == run.id
    assert origin["source"] == "test-tick"
    assert origin["model"] == "recorded-model"
    assert origin["model_source"] == "agentlog"


@pytest.mark.parametrize("verb", ["put", "edit", "tag"])
@pytest.mark.parametrize("via_args", [False, True])
def test_callers_cannot_supply_filed_by(
    runtime_with_store: PrecisRuntime, verb: str, via_args: bool
) -> None:
    payload: dict[str, Any] = {"meta": {"filed_by": {"actor": "forged"}}}
    args: dict[str, Any] = {"kind": "todo", "text": "forge probe"}
    assert runtime_with_store.store is not None
    original = runtime_with_store.store.insert_ref(
        kind="todo", slug=None, title=f"forge target {uuid4().hex}"
    )
    if verb != "put":
        args["id"] = original.id
    if via_args:
        args["__extras__"] = payload
    else:
        args.update(payload)
    body, error = runtime_with_store.dispatch_with_status(verb, args)
    assert error
    assert "meta.filed_by is server-owned" in body
    unchanged = runtime_with_store.store.get_ref(kind="todo", id=original.id)
    assert unchanged is not None and unchanged.meta == original.meta


def test_storage_replaces_carried_origin(store: Store) -> None:
    with revision_context(actor="real-actor"):
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="copied origin",
            meta={"filed_by": {"actor": "forged"}, "other": "retained"},
        )
    assert ref.meta["filed_by"] == {
        "actor": "real-actor",
        "principal": "worker:cli",
    }
    assert ref.meta["other"] == "retained"


@pytest.mark.parametrize(
    "field", ["current_client", "_session_id", "current_client_model"]
)
def test_identity_read_failure_does_not_prevent_put(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    def fail() -> None:
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(tool_ledger, field, fail)
    with revision_context(actor="still-known"):
        origin = _put(runtime_with_store, "gripe")["filed_by"]
    assert origin == {"actor": "still-known", "principal": "worker:cli"}


def test_failed_agentlog_query_keeps_filing_transaction_usable(store: Store) -> None:
    # An out-of-range bigint reaches Postgres and aborts the savepoint;
    # the same connection must still accept the numeric ref INSERT.
    with tick_context(TickContext(agentlog_id=10**100, source="test-tick")):
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="todo", slug=None, title="query failure", conn=conn
            )
    assert ref.meta["filed_by"]["source"] == "test-tick"
    assert "model" not in ref.meta["filed_by"]


def test_credential_shaped_identity_is_omitted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filing_origin, "mask_secrets", lambda value, **kw: "masked")
    with serve_ledger.session_scope(_Session("model")):
        assert filing_origin.current_filed_by(store.pool) == {}


def test_readonly_gripe_keeps_existing_tags_without_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.dispatch import Hub
    from precis.handlers.gripe import GripeHandler

    handler = GripeHandler(hub=Hub(store=store))
    monkeypatch.setattr(handler, "_is_ro", lambda: True)
    run = f"origin-{uuid4().hex}"
    monkeypatch.setattr("precis.handlers.gripe._ro_gripe_source", lambda: run)
    with revision_context(actor="readonly-test"):
        result = handler.put(text=f"Read-only origin {uuid4().hex}")
    ref_id = int(result.body.split("id=", 1)[1].split()[0])
    ref = store.get_ref(kind="gripe", id=ref_id)
    assert ref is not None and "filed_by" not in ref.meta
    assert {f"agent:{run}", "source:agent_ro"} <= {
        str(tag) for tag in store.tags_for(ref_id)
    }
