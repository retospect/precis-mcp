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
