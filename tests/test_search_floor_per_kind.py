"""Per-kind semantic-only floors on the cross-kind ``search_hits`` streams.

gr514956 (Reto ruling 2026-10-29): ``SEMANTIC_ONLY_FOLD_DISTANCE`` carries a
measured floor per kind; paper, patent and web apply theirs as a hard cut on
hits no lexical leg corroborated. Kinds absent from the table are unaffected,
and ``mode='lexical'`` never reaches the semantic leg.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.handlers.patent import PatentHandler
from precis.handlers.web import WebHandler
from precis.handlers.wikipedia import WikipediaHandler
from precis.store import SEMANTIC_ONLY_FOLD_DISTANCE, ChunkInsert, Store


def _vec_at(dim: int, distance: float) -> list[float]:
    """Unit vector at cosine ``distance`` from the query vector e0."""
    c = 1.0 - distance
    v = [0.0] * dim
    v[0] = c
    v[1] = math.sqrt(1.0 - c * c)
    return v


class _FixedEmbedder:
    def __init__(self, dim: int) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return "fixed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        v = [0.0] * self._dim
        v[0] = 1.0
        return v

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


def _seed(store: Store, kind: str, slug: str, distance: float) -> int:
    """One ref with one body chunk at ``distance`` from the query, text
    sharing no term with the query so no lexical leg corroborates it."""
    ref = store.insert_ref(kind=kind, slug=f"{kind}-{slug}", title=f"{kind} {slug}")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="prose about wombats entirely unrelated",
                embedding=_vec_at(store.embedding_dim(), distance),
            )
        ],
    )
    return ref.id


def _handlers(store: Store) -> dict[str, Any]:
    hub = Hub(store=store, embedder=_FixedEmbedder(store.embedding_dim()))
    return {
        "paper": PaperHandler(hub=hub),
        "patent": PatentHandler(hub=hub),
        "web": WebHandler(hub=hub),
        "wikipedia": WikipediaHandler(hub=hub),
    }


def test_table_has_measured_kinds_and_default_is_no_floor() -> None:
    assert SEMANTIC_ONLY_FOLD_DISTANCE["memory"] == 0.42
    assert SEMANTIC_ONLY_FOLD_DISTANCE["paper"] == 0.55
    assert SEMANTIC_ONLY_FOLD_DISTANCE["patent"] == 0.50
    assert SEMANTIC_ONLY_FOLD_DISTANCE["web"] == 0.55
    assert SEMANTIC_ONLY_FOLD_DISTANCE.get("wikipedia") is None


@pytest.mark.parametrize(
    ("kind", "floor"),
    [("paper", 0.55), ("patent", 0.50), ("web", 0.55)],
)
def test_hit_above_kind_floor_dropped_below_kept(
    store: Store, kind: str, floor: float
) -> None:
    near = _seed(store, kind, "near-one", floor - 0.10)
    far = _seed(store, kind, "far-one", floor + 0.05)  # still < 0.65 corpus floor
    hits = _handlers(store)[kind].search_hits(q="alpha beta", page_size=10)
    ids = {h.ref_id for h in hits}
    assert near in ids
    assert far not in ids


def test_floors_differ_per_kind(store: Store) -> None:
    """Distance 0.52 sits between patent's 0.50 and paper's 0.55."""
    paper = _seed(store, "paper", "mid-one", 0.52)
    patent = _seed(store, "patent", "mid-one", 0.52)
    handlers = _handlers(store)
    assert paper in {h.ref_id for h in handlers["paper"].search_hits(q="alpha beta")}
    assert patent not in {
        h.ref_id for h in handlers["patent"].search_hits(q="alpha beta")
    }


def test_unlisted_kind_keeps_hit_up_to_corpus_floor(store: Store) -> None:
    far = _seed(store, "wikipedia", "far-one", 0.60)
    hits = _handlers(store)["wikipedia"].search_hits(q="alpha beta")
    assert far in {h.ref_id for h in hits}


def test_lexically_corroborated_hit_survives_far_distance(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="lex-one", title="paper lex-one")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="alpha beta both appear here",
                embedding=_vec_at(store.embedding_dim(), 0.60),
            )
        ],
    )
    hits = _handlers(store)["paper"].search_hits(q="alpha beta")
    assert ref.id in {h.ref_id for h in hits}


def test_lexical_mode_unaffected(store: Store) -> None:
    ref = store.insert_ref(kind="web", slug="lexmode-one", title="web lexmode-one")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="alpha only here", embedding=None)],
    )
    hits = _handlers(store)["web"].search_hits(q="alpha", mode="lexical")
    assert ref.id in {h.ref_id for h in hits}
