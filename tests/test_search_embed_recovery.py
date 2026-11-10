"""Regression coverage for the td614745 review finding (td615186).

``search``/``search_hits`` resolve the query vector once and hand it to
``_card_search``/``_search_local``. Those helpers used to retry
``embed_query`` on ``query_vec=None``: a transient first-call failure
followed by a retry success ran the store's ``mode='semantic'`` leg
(cosine *distance*, smaller = better) while the caller's
``score_is_distance`` flag — computed from its own ``None`` — stayed
False, re-emitting raw distances as ``SearchHit.score`` (the reversed
tie-break bug back, gripe family gr614312).

The helpers now never embed: ``query_vec=None`` means "the lexical leg
ran". These tests pin both halves of the contract across all six
affected handlers (cad / pcb / structure / se card search, patent /
edgar block search):

- recovery: a fail-once embedder yields exactly ONE embed call — no
  hidden retry can resurrect distance scores behind a false flag — and
  the lexical fallback's ts_rank keeps the descending convention;
- healthy: ``mode='semantic'`` still resolves the vec at the entry
  point (a hit with zero lexical overlap is found) and the emitted
  score is the converted similarity, not a raw distance.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.cad import CadHandler
from precis.handlers.edgar import EdgarHandler
from precis.handlers.patent import PatentHandler
from precis.handlers.pcb import PcbHandler
from precis.handlers.structure import StructureHandler
from precis.store import ChunkInsert, Store
from precis_se.handler import SeHandler

#: kind → (handler class, True when hits live in a ``card_combined`` chunk).
_HANDLERS: dict[str, tuple[Any, bool]] = {
    "cad": (CadHandler, True),
    "pcb": (PcbHandler, True),
    "structure": (StructureHandler, True),
    "se": (SeHandler, True),
    "patent": (PatentHandler, False),
    "edgar": (EdgarHandler, False),
}


def _axis_vec(theta: float, *, dim: int = 1024) -> list[float]:
    """Unit vector at angle ``theta`` off the x-axis: cosine distance to
    a query on the x-axis is ``1 - cos(theta)`` — a controlled dial."""
    vec = [0.0] * dim
    vec[0] = math.cos(theta)
    vec[1] = math.sin(theta)
    return vec


class _AxisEmbedder(MockEmbedder):
    """Queries embed on the x-axis; a stored text ``"theta:<t>"`` embeds
    at angle ``t``. (Same trick as ``test_search_retraction``'s.)
    ``calls`` makes a hidden second embed visible."""

    def __init__(self, *, dim: int = 1024) -> None:
        super().__init__(dim=dim)
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        theta = float(text.split(":", 1)[1]) if text.startswith("theta:") else 0.0
        return _axis_vec(theta, dim=self.dim)


class _FailOnceEmbedder(_AxisEmbedder):
    """Transient upstream hiccup: the first ``embed_one`` raises; later
    calls succeed."""

    def embed_one(self, text: str) -> list[float]:
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("transient embed failure")
        return super().embed_one(text)


def _handler(store: Store, embedder: Any, kind: str) -> Any:
    cls, _card = _HANDLERS[kind]
    return cls(hub=Hub(store=store, embedder=embedder))


def _seed(store: Store, kind: str, slug: str, text: str, theta: float) -> None:
    """One ref + one embedded chunk. Card kinds get the synthetic
    ``card_combined`` chunk their ``_card_search`` scopes to; block kinds
    an ordinary body chunk."""
    ref = store.insert_ref(kind=kind, slug=slug, title=slug)
    _cls, card = _HANDLERS[kind]
    chunk = (
        ChunkInsert(
            ord=-1,
            text=text,
            embedding=_axis_vec(theta),
            meta={"chunk_kind": "card_combined"},
        )
        if card
        else ChunkInsert(ord=0, text=text, embedding=_axis_vec(theta))
    )
    store.chunks.insert_chunks(ref.id, [chunk])


@pytest.mark.parametrize("kind", sorted(_HANDLERS))
def test_failed_first_embed_is_not_retried_inside_search(
    store: Store, kind: str
) -> None:
    """The recovery case: first embed fails, a retry would succeed.
    Resolve-once means exactly one embed call; the lexical fallback's
    ts_rank scores stay descending (never raw ascending distances)."""
    e = _FailOnceEmbedder(dim=1024)
    _seed(store, kind, f"{kind}-near", "flange widget theta:0.05", 0.05)
    _seed(store, kind, f"{kind}-far", "flange widget theta:0.40", 0.40)

    hits = _handler(store, e, kind).search_hits(
        q="flange", mode="semantic", page_size=10
    )

    assert e.calls == 1, "a second embed inside _card_search/_search_local"
    assert {h.slug for h in hits} == {f"{kind}-near", f"{kind}-far"}
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("kind", sorted(_HANDLERS))
def test_semantic_search_hits_scores_are_converted_similarity(
    store: Store, kind: str
) -> None:
    """Healthy embedder + ``mode='semantic'``: the vec resolves once at
    the entry point, so a card/block with ZERO lexical overlap is found
    by the vector leg, and the emitted score is ``1 - distance`` — the
    bigger-is-better convention ``SearchHit.score`` promises."""
    e = _AxisEmbedder(dim=1024)
    _seed(store, kind, f"{kind}-near", "theta:0.05", 0.05)
    _seed(store, kind, f"{kind}-far", "theta:0.40", 0.40)

    hits = _handler(store, e, kind).search_hits(
        q="zzz-no-lexical-overlap", mode="semantic", page_size=10
    )

    assert hits and hits[0].slug == f"{kind}-near"
    assert hits[0].score == pytest.approx(math.cos(0.05), abs=1e-6)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize(
    "kind", sorted(k for k, (_c, card) in _HANDLERS.items() if card)
)
def test_card_search_verb_resolves_query_vec_once(store: Store, kind: str) -> None:
    """``search()`` passed ``query_vec=None`` and relied on the helper's
    internal embed. Resolution moved up: one embed call, and the semantic
    leg still fires (a card with no lexical overlap is found)."""
    e = _AxisEmbedder(dim=1024)
    _seed(store, kind, f"{kind}-a", "theta:0.05", 0.05)

    resp = _handler(store, e, kind).search(
        q="zzz-no-lexical-overlap", mode="semantic", page_size=10
    )

    assert e.calls == 1
    assert f"{kind}-a" in resp.body


@pytest.mark.parametrize("kind", ["patent", "edgar"])
def test_local_search_verb_resolves_query_vec_once(store: Store, kind: str) -> None:
    """Patent/edgar ``search(reach='local')``: the local leg resolves the
    vec once in the caller — the same vec that feeds the
    ``score_is_distance`` flag on the merged stream."""
    e = _AxisEmbedder(dim=1024)
    _seed(store, kind, f"{kind}-a", "theta:0.05", 0.05)

    resp = _handler(store, e, kind).search(
        q="zzz-no-lexical-overlap", mode="semantic", reach="local", page_size=10
    )

    assert e.calls == 1
    assert resp.result_count == 1
    assert f"{kind}-a" in resp.body
