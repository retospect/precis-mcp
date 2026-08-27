"""``STATUS:refuted`` fencing in default search — the do-not-repropose
ledger (docs/backlog/quest-dossier-dialectic.md §"Refuted lifecycle").

A refuted finding must stay out of default and cross-kind search results
and surface only on explicit ask (``status='refuted'`` — which desugars to
the ``STATUS:refuted`` tag — or an explicit ``tags=['STATUS:refuted']``
opt-in). Covered across all four block-search paths plus the pure
fence-decision helper, mirroring ``test_speculative_fence.py`` /
``test_wikipedia.py``'s coverage shape for the sibling fences.
"""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.store import BlockInsert, Store
from precis.store._blocks_ops import BlockStore
from precis.store._tag_filter import REFUTED_TAG, is_refuted_tag, refuted_fence
from precis.store.types import Tag

_EMB = MockEmbedder(dim=1024)


def _finding(store: Store, text: str, *, refuted: bool) -> int:
    ref = store.insert_ref(kind="finding", slug=None, title=text[:60])
    store.blocks.insert_blocks(
        ref.id,
        [BlockInsert(pos=0, text=text, embedding=_EMB.embed_one(text))],
    )
    if refuted:
        store.add_tag(ref.id, Tag.closed("STATUS", "refuted"), replace_prefix=True)
    return ref.id


# ── pure helper ─────────────────────────────────────────────────────


def test_is_refuted_tag() -> None:
    assert is_refuted_tag("STATUS:refuted")
    assert is_refuted_tag("  STATUS:refuted  ")
    assert not is_refuted_tag("STATUS:established")
    assert not is_refuted_tag("topic:quantum")


def test_refuted_tag_constant() -> None:
    assert REFUTED_TAG == "STATUS:refuted"


def test_refuted_fence_is_parameterless_not_exists() -> None:
    frag = refuted_fence("r")
    assert "%s" not in frag  # no binds → safe under double-splice
    assert frag.startswith("NOT EXISTS")
    assert "r.ref_id" in frag
    assert "STATUS" in frag and "refuted" in frag


def test_fence_decision() -> None:
    decide = BlockStore._fence_refuted
    assert decide(None) is True
    assert decide(["topic:x"]) is True
    assert decide([REFUTED_TAG]) is False  # explicit opt-in


# ── lexical ─────────────────────────────────────────────────────────


def test_lexical_fences_refuted_by_default(store: Store) -> None:
    live = _finding(store, "gate dielectric withstands 2.4 kV", refuted=False)
    dead = _finding(store, "gate dielectric fails above 2.4 kV", refuted=True)
    ids = {ref.id for _b, ref, _s in store.blocks.search_blocks_lexical(q="dielectric")}
    assert live in ids
    assert dead not in ids


def test_lexical_shows_refuted_on_explicit_tag(store: Store) -> None:
    dead = _finding(store, "gate dielectric fails above 2.4 kV", refuted=True)
    _finding(store, "gate dielectric withstands 2.4 kV", refuted=False)
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_lexical(
            q="dielectric", tags=[REFUTED_TAG]
        )
    }
    assert ids == {dead}


# ── keywords ────────────────────────────────────────────────────────


def test_keywords_fences_refuted_by_default(store: Store) -> None:
    live = _finding(store, "annealing improves yield", refuted=False)
    dead = _finding(store, "annealing degrades yield", refuted=True)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = ANY(%s)",
            [["annealing"], [live, dead]],
        )
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_keywords(terms=["annealing"])
    }
    assert live in ids
    assert dead not in ids


# ── semantic ────────────────────────────────────────────────────────


def test_semantic_fences_refuted_by_default(store: Store) -> None:
    live = _finding(store, "quantum annealing improves fidelity", refuted=False)
    dead = _finding(store, "quantum annealing does not improve fidelity", refuted=True)
    qv = _EMB.embed_one("quantum annealing fidelity")
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_semantic(
            query_vec=qv, max_distance=None
        )
    }
    assert live in ids
    assert dead not in ids


def test_semantic_shows_refuted_on_explicit_tag(store: Store) -> None:
    dead = _finding(store, "quantum annealing does not improve fidelity", refuted=True)
    qv = _EMB.embed_one("quantum annealing fidelity")
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_semantic(
            query_vec=qv, max_distance=None, tags=[REFUTED_TAG]
        )
    }
    assert dead in ids


# ── fused (double-spliced WHERE — the param-safety case) ────────────


def test_fused_fences_refuted_by_default(store: Store) -> None:
    live = _finding(store, "barrier height 0.9 eV confirmed", refuted=False)
    dead = _finding(store, "barrier height 0.9 eV refuted", refuted=True)
    qv = _EMB.embed_one("barrier height")
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_fused(q="barrier", query_vec=qv)
    }
    assert live in ids
    assert dead not in ids


def test_fused_shows_refuted_on_explicit_tag(store: Store) -> None:
    dead = _finding(store, "barrier height 0.9 eV refuted", refuted=True)
    _finding(store, "barrier height 0.9 eV confirmed", refuted=False)
    qv = _EMB.embed_one("barrier height")
    ids = {
        ref.id
        for _b, ref, _s in store.blocks.search_blocks_fused(
            q="barrier", query_vec=qv, tags=[REFUTED_TAG]
        )
    }
    assert ids == {dead}


# ── cross-kind fan-out (search_blocks_multi / search_chunks_across_kinds
#    both reuse the single-leg lexical/semantic methods, so they inherit
#    the fence transitively — no separate splice site to wire) ─────────


def test_cross_kind_fanout_fences_refuted_by_default(store: Store) -> None:
    live = _finding(store, "photocatalytic rate unaffected by pH", refuted=False)
    dead = _finding(store, "photocatalytic rate not affected by pH", refuted=True)
    qv = _EMB.embed_one("photocatalytic rate pH")
    hits = store.blocks.search_chunks_across_kinds(
        kinds=["finding"], q="photocatalytic", query_vec=qv
    )
    ids = {ref.id for _b, ref, _s in hits}
    assert live in ids
    assert dead not in ids
