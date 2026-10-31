"""Exact-handle text dereference with same-session read receipts."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Iterator
from typing import Any

import pytest

from precis import serve_ledger, server
from precis.runtime import PrecisRuntime
from precis.runtime.handle_deref import reset_read_receipts
from precis.store import ChunkInsert, Store
from precis.tools import core as tools_core
from precis.utils import handle_registry
from tests._mcp_session import connected_sessions, text_of
from tests._refs import insert_gripe


@pytest.fixture(autouse=True)
def _clean_receipts() -> Iterator[None]:
    reset_read_receipts()
    yield
    reset_read_receipts()


def _paper(
    store: Store, *, text: str = "Nanobud claim\n\\alpha_1 + \\beta_2"
) -> tuple[Any, Any]:
    ref = store.insert_ref(kind="paper", slug="deref-paper", title="Deref paper")
    block = store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])[0]
    return ref, block


def _handle(kind: str, ref_id: int, chunk_id: int | None = None) -> str:
    return handle_registry.format_handle(
        kind, ref_id if chunk_id is None else chunk_id, chunk=chunk_id is not None
    )


def _memory_id(body: str) -> int:
    match = re.search(r"\bid=(\d+)", body)
    if match is None:
        match = re.search(r"\b(?:me|fi)(\d+)", body)
    assert match is not None, body
    return int(match.group(1))


def test_missing_receipt_refuses_before_mutation(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    before = len(store.list_refs(kind="memory"))
    with serve_ledger.session_scope(object()):
        out, is_error = runtime_with_store.dispatch_with_status(
            "put",
            {"kind": "memory", "text": {"$ref": _handle("paper", ref.id, block.id)}},
        )
    assert is_error
    assert "[error:Denied]" in out
    assert len(store.list_refs(kind="memory")) == before


def test_exact_chunk_round_trip_and_ack(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    with serve_ledger.session_scope(object()):
        read, read_error = runtime_with_store.dispatch_with_status(
            "get", {"id": handle}
        )
        assert not read_error
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert not is_error, out
    assert f"dereferenced text <- {handle}" in out
    digest = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    assert f"sha256:{digest}" in out
    stored = store.chunks.live_body_text_for_ref(_memory_id(out))
    assert stored == [block.text]


def test_record_handle_joins_live_body_chunks(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="deref-record", title="Record")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="first"), ChunkInsert(ord=1, text="second")],
    )
    handle = _handle("paper", ref.id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert not is_error, out
    assert store.chunks.live_body_text_for_ref(_memory_id(out)) == ["first\n\nsecond"]


def test_record_uses_current_bytes_after_source_edit(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    source_body = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "version one \\alpha"}
    )
    source_id = _memory_id(source_body)
    handle = _handle("memory", source_id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        runtime_with_store.dispatch(
            "edit",
            {
                "kind": "memory",
                "id": source_id,
                "mode": "replace",
                "text": "version two \\beta",
            },
        )
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert not is_error, out
    assert store.chunks.live_body_text_for_ref(_memory_id(out)) == [
        "version two \\beta"
    ]
    digest = hashlib.sha256(b"version two \\beta").hexdigest()
    assert digest in out


def test_cross_session_read_does_not_permit_write(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
    with serve_ledger.session_scope(object()):
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert is_error and "[error:Denied]" in out


def test_restart_loss_requires_fresh_read(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    session = object()
    with serve_ledger.session_scope(session):
        runtime_with_store.dispatch("get", {"id": handle})
        reset_read_receipts()
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert is_error and "[error:Denied]" in out


def test_dry_run_and_edit_follow_same_receipt_rules(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    target_body = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "target"}
    )
    target_id = _memory_id(target_body)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        preview, preview_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "memory",
                "id": target_id,
                "mode": "find-replace",
                "find": "target",
                "text": {"$ref": handle},
                "dry_run": True,
            },
        )
        assert not preview_error, preview
        assert "dereferenced text" in preview
        assert store.chunks.live_body_text_for_ref(target_id) == ["target"]
        out, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "memory",
                "id": target_id,
                "mode": "find-replace",
                "find": "target",
                "text": {"$ref": handle},
            },
        )
    assert not is_error, out
    assert store.chunks.live_body_text_for_ref(target_id) == [block.text]


@pytest.mark.parametrize(
    "envelope",
    [{"$ref": "pc1", "extra": True}, {"$ref": ["pc1"]}, {"$ref": "pc1~0"}],
)
def test_malformed_envelopes_refuse_before_mutation(
    runtime_with_store: PrecisRuntime, store: Store, envelope: dict[str, Any]
) -> None:
    before = len(store.list_refs(kind="memory"))
    out, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": envelope}
    )
    assert is_error and "[error:BadInput]" in out
    assert len(store.list_refs(kind="memory")) == before


def test_unknown_and_bodyless_handles_refuse(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    with serve_ledger.session_scope(object()):
        unknown, unknown_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": "pc999999"}}
        )
        assert unknown_error and "[error:NotFound]" in unknown
        skill_id = store.ensure_skill_ref("precis-search-help")
        handle = _handle("skill", skill_id)
        read, read_error = runtime_with_store.dispatch_with_status(
            "get", {"id": handle, "view": "toc"}
        )
        assert not read_error, read
        bodyless, bodyless_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
        assert bodyless_error and "no live body chunks" in bodyless


def test_exact_unreadable_chunk_points_to_record(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    gripe = insert_gripe(store, "chunk unreadable")
    block = store.chunks.insert_chunks(
        gripe.id, [ChunkInsert(ord=0, text="gripe body")]
    )[0]
    handle = _handle("gripe", gripe.id, block.id)
    out, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": {"$ref": handle}}
    )
    assert is_error and "no exact-handle get read" in out
    assert _handle("gripe", gripe.id) in out


def test_oversize_expansion_refuses(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    monkeypatch.setattr("precis.runtime.handle_deref.TEXT_PAYLOAD_MAX_BYTES", 10)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert is_error and "exceeds the 10-byte cap" in out


def test_audit_retains_envelope_not_expanded_text(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    seen: dict[str, Any] = {}

    def capture(
        verb: str,
        args: dict[str, Any],
        body: str,
        is_error: bool,
        started: float,
        result_count: int | None = None,
        *,
        budget_fitted: bool = False,
    ) -> None:
        seen.update(args)

    monkeypatch.setattr(runtime_with_store, "_record_tool_call", capture)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert not is_error, out
    assert seen["text"] == {"$ref": handle}
    assert block.text not in str(seen["text"])


def test_merged_record_redirect_uses_survivor_and_names_both_handles(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    loser_body = runtime_with_store.dispatch("put", {"kind": "memory", "text": "loser"})
    survivor_body = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "survivor"}
    )
    loser_id = _memory_id(loser_body)
    survivor_id = _memory_id(survivor_body)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now(), "
            "meta = jsonb_set(meta, '{superseded_by}', to_jsonb(%s)) WHERE ref_id = %s",
            (survivor_id, loser_id),
        )
    old_handle = _handle("memory", loser_id)
    new_handle = _handle("memory", survivor_id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": old_handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": old_handle}}
        )
    assert not is_error, out
    assert f"{old_handle} -> {new_handle}" in out
    assert store.chunks.live_body_text_for_ref(_memory_id(out)) == ["survivor"]


def test_record_expansion_excludes_retired_chunks(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="retired-chunks", title="Retired")
    blocks = store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="retired"), ChunkInsert(ord=1, text="live")],
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (blocks[0].id,)
        )
    handle = _handle("paper", ref.id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": {"$ref": handle}}
        )
    assert not is_error, out
    assert store.chunks.live_body_text_for_ref(_memory_id(out)) == ["live"]


def test_no_mcp_session_refuses_reference(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    out, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {"kind": "memory", "text": {"$ref": _handle("paper", ref.id, block.id)}},
    )
    assert is_error and "requires a live MCP session" in out


def test_finding_receives_expanded_text_through_body_alias(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store)
    handle = _handle("paper", ref.id, block.id)
    with serve_ledger.session_scope(object()):
        runtime_with_store.dispatch("get", {"id": handle})
        out, is_error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "finding",
                "title": "Nanobud claim",
                "text": {"$ref": handle},
                "cited_in": handle,
            },
        )
    assert not is_error, out
    finding_id = _memory_id(out)
    body = store.chunks.live_body_text_for_ref(finding_id)
    assert "Nanobud claim" in "\n".join(body)
    assert "\\alpha_1" in "\n".join(body)


def test_real_mcp_two_session_latex_round_trip(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref, block = _paper(store, text="Nanobud proof\n\\frac{\\alpha}{\\beta} \\\\ text")
    handle = _handle("paper", ref.id, block.id)
    saved = (server._runtime, tools_core._runtime)
    server._runtime = runtime_with_store
    tools_core._runtime = runtime_with_store

    async def _run() -> tuple[str, str, str]:
        async with connected_sessions(server.mcp, 2) as (reader, writer):
            read = text_of(await reader.call_tool("get", {"id": handle}))
            denied = text_of(
                await writer.call_tool(
                    "put", {"kind": "memory", "text": {"$ref": handle}}
                )
            )
            written = text_of(
                await reader.call_tool(
                    "put", {"kind": "memory", "text": {"$ref": handle}}
                )
            )
            return read, denied, written

    try:
        read, denied, written = asyncio.run(_run())
    finally:
        server._runtime, tools_core._runtime = saved

    assert "Nanobud proof" in read
    assert "[error:Denied]" in denied
    assert "dereferenced text" in written
    digest = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    assert digest in written
    stored = store.chunks.live_body_text_for_ref(_memory_id(written))
    assert stored == [block.text]
