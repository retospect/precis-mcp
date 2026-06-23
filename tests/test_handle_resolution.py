"""ADR 0036 handle persistence + surface dispatch — DB-backed.

Proves the resolution slice end-to-end: every new ref is minted a handle
(``insert_ref``), the store resolves it back (``resolve_handle``), and the
verb surface infers ``kind`` from a handle id with no ``kind=`` — routing
identically to the explicit ``kind=`` path.
"""

from __future__ import annotations

import importlib.util

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry
from precis.workers import chunk_handles

# The full-hub fixture (runtime_with_store) boots every handler, which needs
# the [paper] extra (pysbd). Absent on host; present in the container gate.
_NEEDS_PAPER_EXTRA = pytest.mark.skipif(
    importlib.util.find_spec("pysbd") is None,
    reason="full-hub boot needs the [paper] extra",
)


def _handle_of(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT handle FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] if row is not None else None


def test_insert_ref_mints_a_well_formed_typed_handle(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="handle test")
    h = _handle_of(store, ref.id)
    assert h is not None
    assert handle_registry.is_well_formed(h)
    assert h[:2] == handle_registry.KIND_CODES["memory"]  # 'me'


def test_resolve_handle_numeric_kind(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="resolve me")
    h = _handle_of(store, ref.id)
    assert h is not None
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.kind == "memory"
    assert resolved.ref_id == ref.id
    assert resolved.public_id == str(ref.id)  # numeric → str(ref_id)


def test_resolve_handle_slug_kind(store: Store) -> None:
    ref = store.insert_ref(kind="oracle", slug="adr36-oracle", title="o")
    h = _handle_of(store, ref.id)
    assert h is not None
    assert h[:2] == handle_registry.KIND_CODES["oracle"]  # 'or'
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.public_id == "adr36-oracle"  # slug kind → slug


def test_resolve_handle_rejects_unminted_legacy_and_chunk(store: Store) -> None:
    assert store.resolve_handle("me0000000") is None  # well-formed but unminted
    assert store.resolve_handle("miller23") is None  # legacy slug
    assert store.resolve_handle("dc4m8p1rz") is None  # chunk handle (reserved)


def test_resolve_handle_is_case_insensitive(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="fold")
    h = _handle_of(store, ref.id)
    assert h is not None
    assert store.resolve_handle(h.upper()) is not None  # Crockford case-fold


@_NEEDS_PAPER_EXTRA
def test_surface_get_by_handle_routes_like_explicit_kind(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="dispatch eq")
    h = _handle_of(store, ref.id)
    assert h is not None
    # get with NO kind= — the handle self-identifies its kind.
    via_handle = runtime_with_store.dispatch("get", {"id": h})
    via_explicit = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": str(ref.id)}
    )
    assert via_handle == via_explicit


# --- chunk handles (ADR 0036 backfill pass + resolution) -----------------


def _insert_chunk(store: Store, ref_id: int, *, ord_: int = 0) -> int:
    with store.pool.connection() as c:
        row = c.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, "chunk body"),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _chunk_handle(store: Store, chunk_id: int) -> str | None:
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT handle FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return row[0] if row is not None else None


def test_chunk_handle_mint_and_resolve(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-chunk-paper", title="p")
    chunk_id = _insert_chunk(store, ref.id)
    assert _chunk_handle(store, chunk_id) is None  # precondition
    # Mint deterministically on this chunk (avoids the pass's chunk_id-ASC
    # claim ordering, which wouldn't reach a freshly-inserted row).
    with store.pool.connection() as c:
        assert chunk_handles._mint_one(c, chunk_id, "paper") is True
    h = _chunk_handle(store, chunk_id)
    assert h is not None
    assert handle_registry.is_well_formed(h)
    assert h[:2] == handle_registry.CHUNK_CODES["paper"]  # 'pc'
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.kind == "paper"
    assert resolved.chunk_id == chunk_id
    assert resolved.ref_id == ref.id
    assert resolved.public_id == "adr36-chunk-paper"  # owning ref's slug


def test_chunk_handles_pass_is_healthy(store: Store) -> None:
    # Ensure there's at least one handle-less corpus chunk, then drain a
    # batch; assert the pass succeeds (every claimed chunk minted). No
    # assertion on *which* chunks — claim order is chunk_id ASC.
    ref = store.insert_ref(kind="paper", slug="adr36-pass-health", title="p")
    _insert_chunk(store, ref.id)
    r = chunk_handles.run_chunk_handles_pass(store, batch_size=200)
    assert r["failed"] == 0
    assert r["ok"] == r["claimed"]


def test_pass_excludes_draft() -> None:
    # Drafts keep their ADR-0033 base-58 handle until the wipe.
    assert "draft" not in chunk_handles._KINDS
    assert "paper" in chunk_handles._KINDS


@_NEEDS_PAPER_EXTRA
def test_surface_get_chunk_handle_routes_to_selector(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-chunk-surface", title="p")
    chunk_id = _insert_chunk(store, ref.id, ord_=0)
    with store.pool.connection() as c:
        chunk_handles._mint_one(c, chunk_id, "paper")
    h = _chunk_handle(store, chunk_id)
    assert h is not None
    # get(id='pc…') with no kind= → translated to the `slug~ord` selector,
    # returning the chunk identically to the explicit selector.
    via_handle = runtime_with_store.dispatch("get", {"id": h})
    via_selector = runtime_with_store.dispatch("get", {"id": "adr36-chunk-surface~0"})
    assert via_handle == via_selector
