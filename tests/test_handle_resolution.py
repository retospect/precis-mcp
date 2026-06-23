"""ADR 0036 handle resolution + surface dispatch — DB-backed.

Handles are *computed*, not stored: ``<2-char code><decimal pk>``. This
proves the resolution slice end-to-end — the store decodes a handle back
to its ref/chunk (``resolve_handle``), and the verb surface infers
``kind`` from a handle id with no ``kind=``, routing identically to the
explicit ``kind=`` path.
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


def test_resolve_handle_numeric_kind(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="resolve me")
    h = handle_registry.format_handle("memory", ref.id)  # 'me<ref_id>'
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.kind == "memory"
    assert resolved.ref_id == ref.id
    assert resolved.public_id == str(ref.id)  # numeric → str(ref_id)


def test_resolve_handle_slug_kind(store: Store) -> None:
    ref = store.insert_ref(kind="oracle", slug="adr36-oracle", title="o")
    h = handle_registry.format_handle("oracle", ref.id)  # 'or<ref_id>'
    assert h[:2] == handle_registry.KIND_CODES["oracle"]
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.public_id == "adr36-oracle"  # slug kind → slug


def test_resolve_handle_kind_mismatch_is_rejected(store: Store) -> None:
    # A memory's id under the *todo* code must not resolve — the prefix
    # claims a kind the row doesn't have (typo guard).
    ref = store.insert_ref(kind="memory", slug=None, title="mismatch")
    wrong = handle_registry.format_handle("todo", ref.id)  # 'td<ref_id>'
    assert store.resolve_handle(wrong) is None


def test_resolve_handle_rejects_unknown_legacy_and_unresolvable(store: Store) -> None:
    assert store.resolve_handle("me0") is None  # no ref_id 0
    assert store.resolve_handle("miller23") is None  # legacy slug
    assert store.resolve_handle("tg42") is None  # tag code (not refs-backed)
    assert store.resolve_handle("pc999999") is None  # no such chunk


def test_resolve_handle_folds_prefix_case(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="fold")
    h = handle_registry.format_handle("memory", ref.id)
    assert store.resolve_handle(h.upper()) is not None  # 'ME5' → memory


@_NEEDS_PAPER_EXTRA
def test_surface_get_by_handle_routes_like_explicit_kind(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="dispatch eq")
    h = handle_registry.format_handle("memory", ref.id)
    # get with NO kind= — the handle self-identifies its kind.
    via_handle = runtime_with_store.dispatch("get", {"id": h})
    via_explicit = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": str(ref.id)}
    )
    assert via_handle == via_explicit


# --- chunk handles (ADR 0036 — computed from chunk_id) -------------------


def _insert_chunk(store: Store, ref_id: int, *, ord_: int = 0) -> int:
    with store.pool.connection() as c:
        row = c.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, "chunk body"),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_chunk_handle_resolves_to_chunk_and_owning_ref(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-chunk-paper", title="p")
    chunk_id = _insert_chunk(store, ref.id)
    h = handle_registry.format_handle("paper", chunk_id, chunk=True)  # 'pc<id>'
    assert h[:2] == handle_registry.CHUNK_CODES["paper"]  # 'pc'
    resolved = store.resolve_handle(h)
    assert resolved is not None
    assert resolved.kind == "paper"
    assert resolved.chunk_id == chunk_id
    assert resolved.ref_id == ref.id
    assert resolved.chunk_ord == 0
    assert resolved.public_id == "adr36-chunk-paper"  # owning ref's slug


def test_chunk_handle_kind_mismatch_is_rejected(store: Store) -> None:
    # A paper chunk's id under the *draft* chunk code must not resolve.
    ref = store.insert_ref(kind="paper", slug="adr36-mismatch", title="p")
    chunk_id = _insert_chunk(store, ref.id)
    wrong = handle_registry.format_handle("draft", chunk_id, chunk=True)  # 'dc<id>'
    assert store.resolve_handle(wrong) is None


def test_render_prefers_computed_uhandle_over_legacy() -> None:
    # Pure render test (no DB): a hit with a universal handle emits the
    # handle and NOT the legacy slug~pos (ADR 0036 cutover).
    from precis.utils.search_merge import SearchHit, _render_hit

    hit = SearchHit(
        score=1.0,
        kind="paper",
        title="t",
        preview="p",
        slug="miller23",
        pos=4,
        uhandle="pc40",
    )
    out = _render_hit(1, hit, show_label=False)
    assert "pc40" in out  # universal handle emitted
    assert "miller23~4" not in out  # legacy form not emitted

    # No uhandle (code-less kind) → header falls back to legacy.
    bare = _render_hit(
        1,
        SearchHit(score=1.0, kind="paper", title="t", preview="p", slug="x", pos=0),
        show_label=False,
    )
    header = bare.strip().splitlines()[0]
    assert header == "## 1. x~0"


def test_block_emitter_computes_chunk_handle() -> None:
    # The block→SearchHit adapter computes uhandle from (kind, chunk_id).
    from precis.utils.search_merge import block_hits_to_search_hits

    class _Block:
        id = 40
        pos = 4
        text = "body"
        keywords: list[str] = []

    class _Ref:
        id = 7
        slug = "miller23"
        title = "t"

    hits = block_hits_to_search_hits([(_Block(), _Ref(), 1.0)], kind="paper")
    assert hits[0].uhandle == "pc40"


@_NEEDS_PAPER_EXTRA
def test_surface_get_chunk_handle_routes_to_selector(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-chunk-surface", title="p")
    chunk_id = _insert_chunk(store, ref.id, ord_=0)
    h = handle_registry.format_handle("paper", chunk_id, chunk=True)
    # get(id='pc…') with no kind= → translated to the `slug~ord` selector,
    # returning the chunk identically to the explicit selector.
    via_handle = runtime_with_store.dispatch("get", {"id": h})
    via_selector = runtime_with_store.dispatch("get", {"id": "adr36-chunk-surface~0"})
    assert via_handle == via_selector
