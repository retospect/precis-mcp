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


# --- input acceptance (ADR 0036 cutover): handles round-trip ------------


def test_parse_link_target_accepts_record_handle(store: Store) -> None:
    from precis.handlers._link_target import parse_link_target

    ref = store.insert_ref(kind="memory", slug=None, title="link me")
    h = handle_registry.format_handle("memory", ref.id)  # 'me<id>'
    tgt = parse_link_target(h, store=store)
    assert tgt.ref_id == ref.id
    assert tgt.kind == "memory"
    assert tgt.pos is None  # record handle → ref-level


def test_parse_link_target_accepts_chunk_handle(store: Store) -> None:
    from precis.handlers._link_target import parse_link_target

    ref = store.insert_ref(kind="paper", slug="adr36-link-chunk", title="p")
    chunk_id = _insert_chunk(store, ref.id, ord_=2)
    h = handle_registry.format_handle("paper", chunk_id, chunk=True)  # 'pc<id>'
    tgt = parse_link_target(h, store=store)
    assert tgt.ref_id == ref.id
    assert tgt.pos == 2  # chunk handle → block pos (== chunks.ord)


def test_parse_link_target_unresolvable_handle_is_not_found(store: Store) -> None:
    from precis.errors import NotFound
    from precis.handlers._link_target import parse_link_target

    with pytest.raises(NotFound):
        parse_link_target("me999999", store=store)  # well-formed, no such ref


def test_parse_link_target_legacy_kindslug_still_works(store: Store) -> None:
    # The canonical kind:slug form keeps working (its ':' defeats the
    # handle parse, so it falls through to the legacy grammar).
    from precis.handlers._link_target import parse_link_target

    ref = store.insert_ref(kind="memory", slug=None, title="legacy")
    tgt = parse_link_target(f"memory:{ref.id}", store=store)
    assert tgt.ref_id == ref.id


@_NEEDS_PAPER_EXTRA
def test_paper_exclude_accepts_handle(store: Store) -> None:
    from precis.handlers.paper import _normalise_exclude_slug

    ref = store.insert_ref(kind="paper", slug="adr36-excl", title="p")
    chunk_id = _insert_chunk(store, ref.id)
    h = handle_registry.format_handle("paper", chunk_id, chunk=True)
    # A chunk handle resolves to the owning paper slug (coarse, ref-level).
    assert _normalise_exclude_slug(h, store=store) == "adr36-excl"
    # A record handle too.
    rh = handle_registry.format_handle("paper", ref.id)
    assert _normalise_exclude_slug(rh, store=store) == "adr36-excl"


# --- relative navigation (ADR 0036) — flat ord kinds --------------------


def test_resolve_relative_sibling_steps(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-rel", title="p")
    cids = [_insert_chunk(store, ref.id, ord_=i) for i in range(4)]
    h1 = handle_registry.format_handle("paper", cids[1], chunk=True)  # ord 1
    assert store.resolve_relative(f"{h1}+1") == ("paper", "adr36-rel~2")
    assert store.resolve_relative(f"{h1}+2") == ("paper", "adr36-rel~3")
    assert store.resolve_relative(f"{h1}-1") == ("paper", "adr36-rel~0")
    assert store.resolve_relative(f"{h1}++") == ("paper", "adr36-rel~2")


def test_resolve_relative_out_of_range_is_none(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-rel-edge", title="p")
    cids = [_insert_chunk(store, ref.id, ord_=i) for i in range(3)]
    last = handle_registry.format_handle("paper", cids[2], chunk=True)  # ord 2 = max
    first = handle_registry.format_handle("paper", cids[0], chunk=True)  # ord 0
    assert store.resolve_relative(f"{last}+1") is None  # past the end
    assert store.resolve_relative(f"{first}-1") is None  # before the start


def test_resolve_relative_span_clamps(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-span", title="p")
    cids = [_insert_chunk(store, ref.id, ord_=i) for i in range(5)]
    h2 = handle_registry.format_handle("paper", cids[2], chunk=True)  # ord 2
    assert store.resolve_relative(f"{h2}-1..1") == ("paper", "adr36-span~1..3")
    # clamps to the document bounds [0, 4]
    assert store.resolve_relative(f"{h2}-9..9") == ("paper", "adr36-span~0..4")


def test_resolve_relative_ancestor_on_flat_kind_is_none(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-flat", title="p")
    cid = _insert_chunk(store, ref.id, ord_=0)
    h = handle_registry.format_handle("paper", cid, chunk=True)
    assert store.resolve_relative(f"{h}^") is None  # papers have no hierarchy


def test_resolve_relative_non_relative_is_none(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-abs", title="p")
    cid = _insert_chunk(store, ref.id, ord_=0)
    h = handle_registry.format_handle("paper", cid, chunk=True)
    assert store.resolve_relative(h) is None  # absolute handle, no operator


@_NEEDS_PAPER_EXTRA
def test_surface_record_handle_carries_chunk_selector(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-pa-sel", title="p")
    for i in range(3):
        _insert_chunk(store, ref.id, ord_=i)
    pa = handle_registry.format_handle("paper", ref.id)  # 'pa<id>'
    # A record handle with a trailing chunk selector resolves like the slug
    # form: pa<id>~0..2 == slug~0..2.
    assert runtime_with_store.dispatch("get", {"id": f"{pa}~0..2"}) == (
        runtime_with_store.dispatch("get", {"id": "adr36-pa-sel~0..2"})
    )


@_NEEDS_PAPER_EXTRA
def test_surface_get_relative_routes_to_sibling(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="paper", slug="adr36-rel-surface", title="p")
    cids = [_insert_chunk(store, ref.id, ord_=i) for i in range(3)]
    h0 = handle_registry.format_handle("paper", cids[0], chunk=True)
    # get(id='pc<id0>+1') with no kind= → the next chunk, same as the explicit
    # slug~1 selector.
    via_relative = runtime_with_store.dispatch("get", {"id": f"{h0}+1"})
    via_selector = runtime_with_store.dispatch("get", {"id": "adr36-rel-surface~1"})
    assert via_relative == via_selector


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
