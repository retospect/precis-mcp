"""``DREAM:speculative`` fencing in default search.

Inspirations (tagged ``DREAM:speculative``) must stay out of
authoritative results and surface only on explicit ask
(docs/design/dreaming.md §Inspire — fencing). Consolidated dream
memories carry no such tag and stay visible. Covered across all three
block-search paths plus the pure fence-decision helper.
"""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.store import BlockInsert, Store
from precis.store._blocks_ops import BlocksMixin
from precis.store._tag_filter import (
    SPECULATIVE_TAG,
    is_speculative_tag,
    speculative_fence,
)
from precis.store.types import Tag

_EMB = MockEmbedder(dim=1024)


def _memory(store: Store, text: str, *, speculative: bool) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=text[:20])
    store.insert_blocks(
        ref.id, [BlockInsert(pos=0, text=text, embedding=_EMB.embed_one(text))]
    )
    if speculative:
        store.add_tag(ref.id, Tag.closed("DREAM", "speculative"))
    return ref.id


# ── pure helper ─────────────────────────────────────────────────────


def test_is_speculative_tag() -> None:
    assert is_speculative_tag("DREAM:speculative")
    assert is_speculative_tag("  DREAM:speculative  ")
    assert not is_speculative_tag("DREAM:consolidated")
    assert not is_speculative_tag("topic:quantum")


def test_speculative_fence_is_parameterless_not_exists() -> None:
    frag = speculative_fence("r")
    assert "%s" not in frag  # no binds → safe under double-splice
    assert frag.startswith("NOT EXISTS")
    assert "r.ref_id" in frag


def test_fence_decision() -> None:
    decide = BlocksMixin._fence_speculative
    assert decide(None, False) is True
    assert decide(["topic:x"], False) is True
    assert decide(None, True) is False  # forced include
    assert decide([SPECULATIVE_TAG], False) is False  # explicit opt-in


# ── lexical ─────────────────────────────────────────────────────────


def test_lexical_fences_speculative_by_default(store: Store) -> None:
    plain = _memory(store, "quantum annealing notes", speculative=False)
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    ids = {ref.id for _b, ref, _s in store.search_blocks_lexical(q="quantum")}
    assert plain in ids
    assert spec not in ids


def test_lexical_shows_speculative_on_explicit_tag(store: Store) -> None:
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    _memory(store, "quantum annealing notes", speculative=False)
    ids = {
        ref.id
        for _b, ref, _s in store.search_blocks_lexical(
            q="quantum", tags=[SPECULATIVE_TAG]
        )
    }
    assert ids == {spec}


def test_lexical_include_flag_shows_both(store: Store) -> None:
    plain = _memory(store, "quantum annealing notes", speculative=False)
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    ids = {
        ref.id
        for _b, ref, _s in store.search_blocks_lexical(
            q="quantum", include_speculative=True
        )
    }
    assert {plain, spec} <= ids


# ── semantic ────────────────────────────────────────────────────────


def test_semantic_fences_speculative_by_default(store: Store) -> None:
    plain = _memory(store, "quantum annealing notes", speculative=False)
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    qv = _EMB.embed_one("quantum annealing")
    ids = {
        ref.id
        for _b, ref, _s in store.search_blocks_semantic(query_vec=qv, max_distance=None)
    }
    assert plain in ids
    assert spec not in ids


def test_semantic_include_flag_shows_speculative(store: Store) -> None:
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    qv = _EMB.embed_one("quantum annealing inspiration")
    ids = {
        ref.id
        for _b, ref, _s in store.search_blocks_semantic(
            query_vec=qv, max_distance=None, include_speculative=True
        )
    }
    assert spec in ids


# ── fused (double-spliced WHERE — the param-safety case) ────────────


def test_fused_fences_speculative_by_default(store: Store) -> None:
    plain = _memory(store, "quantum annealing notes", speculative=False)
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    qv = _EMB.embed_one("quantum annealing")
    ids = {
        ref.id for _b, ref, _s in store.search_blocks_fused(q="quantum", query_vec=qv)
    }
    assert plain in ids
    assert spec not in ids


def test_fused_include_flag_shows_both(store: Store) -> None:
    plain = _memory(store, "quantum annealing notes", speculative=False)
    spec = _memory(store, "quantum annealing inspiration", speculative=True)
    qv = _EMB.embed_one("quantum annealing")
    ids = {
        ref.id
        for _b, ref, _s in store.search_blocks_fused(
            q="quantum", query_vec=qv, include_speculative=True
        )
    }
    assert {plain, spec} <= ids
