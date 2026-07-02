"""Handler-level broad retrieval — ``PaperHandler.search(queries=,
answers=, per_paper=)``.

Covers the code-review fixes on the broad path: the pagination trailer
echoes the broad knobs (a bare ``page=2`` continuation would run the
single-leg path — a different ordering), the handler re-enforces the
MCP leg caps (the agentic tier calls it directly), all broad embeds go
through ONE batch call (no serial per-text embeds, no double-embed of
``q``), and a *raising* embedder degrades the broad search to its
lexical legs instead of escaping as a 500.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store

_BLOCKS_A = [
    "Single-atom copper boosts nitrate to ammonia selectivity.",
    "Nitrate to ammonia conversion rises on isolated copper sites.",
    "Hydrogen evolution competes with nitrate to ammonia pathways.",
]
_BLOCKS_B = [
    "Isolated Cu sites raise nitrate to ammonia faradaic efficiency.",
    "Nitrate to ammonia selectivity depends on Cu coordination.",
    "A nitrate to ammonia study of copper single-atom catalysts.",
]


def _seed(store: Store, *, slug: str, blocks: list[str], embed: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    rows = [
        BlockInsert(pos=i, text=t, embedding=(e.embed_one(t) if embed else None))
        for i, t in enumerate(blocks)
    ]
    store.insert_blocks(ref.id, rows)
    return ref.id


def _handler(store: Store, embedder: object | None = None) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=embedder))


class CountingEmbedder(MockEmbedder):
    """MockEmbedder that records batch vs per-text embed calls.

    ``embed`` bypasses the instance's ``embed_one`` so a batch call is
    recorded exactly once and never inflates ``one_calls``.
    """

    def __init__(self) -> None:
        super().__init__(dim=1024)
        self.batch_calls: list[list[str]] = []
        self.one_calls: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [MockEmbedder.embed_one(self, t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        self.one_calls.append(text)
        return MockEmbedder.embed_one(self, text)


class BoomEmbedder:
    """Embedder whose every call raises — the down-backend case."""

    dim = 1024
    model = "boom"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder down")

    def embed_one(self, text: str) -> list[float]:
        raise RuntimeError("embedder down")


# ── (a) broad continuation trailer echoes the broad knobs ──────────


def test_broad_next_page_trailer_echoes_broad_params(store: Store) -> None:
    _seed(store, slug="brA", blocks=_BLOCKS_A)
    _seed(store, slug="brB", blocks=_BLOCKS_B)
    h = _handler(store, MockEmbedder(dim=1024))
    resp = h.search(
        q="nitrate ammonia",
        queries=["copper selectivity"],
        answers=["Isolated Cu sites raise ammonia faradaic efficiency."],
        per_paper=2,
        page_size=2,
    )
    body = resp.body
    # Headline: broad mode has no honest lexical total — no "of K".
    headline = body.splitlines()[0]
    assert " of " not in headline
    # The page=2 continuation must repeat queries=/answers=/per_paper=,
    # otherwise the caller lands on the single-leg ordering.
    assert "page=2" in body
    assert "queries=['copper selectivity']" in body
    assert "answers=[" in body
    assert "per_paper=2" in body


def test_plain_search_trailer_has_no_broad_echo(store: Store) -> None:
    _seed(store, slug="plA", blocks=_BLOCKS_A, embed=False)
    _seed(store, slug="plB", blocks=_BLOCKS_B, embed=False)
    h = _handler(store)
    resp = h.search(q="nitrate ammonia", page_size=2)
    # The page=2 continuation exists but carries no broad-arg echo.
    # (NB "per_paper=2" not bare "per_paper=" — the broad-retrieval
    # discoverability hint legitimately names the knobs.)
    assert "page=2" in resp.body
    assert "queries=[" not in resp.body
    assert "per_paper=2" not in resp.body


# ── (b) handler-level caps (agentic tier bypasses the MCP checks) ──


def test_handler_rejects_more_than_eight_queries(store: Store) -> None:
    h = _handler(store)
    with pytest.raises(BadInput, match="max 8"):
        h.search(q="x", queries=[f"variant {i}" for i in range(9)])


def test_handler_rejects_more_than_eight_answers(store: Store) -> None:
    h = _handler(store)
    with pytest.raises(BadInput, match="max 8"):
        h.search(q="x", answers=[f"passage {i}" for i in range(9)])


def test_handler_rejects_bool_per_paper(store: Store) -> None:
    # isinstance(True, int) is True — a bool must not sneak in as cap 1.
    h = _handler(store)
    with pytest.raises(BadInput, match="positive integer"):
        h.search(q="x", per_paper=True)


def test_handler_rejects_nonpositive_per_paper(store: Store) -> None:
    h = _handler(store)
    with pytest.raises(BadInput, match="positive integer"):
        h.search(q="x", per_paper=0)


# ── (c) one batched embed call; q embedded exactly once ────────────


def test_broad_embeds_all_texts_in_one_batch_call(store: Store) -> None:
    _seed(store, slug="emA", blocks=_BLOCKS_A)
    emb = CountingEmbedder()
    h = _handler(store, emb)
    q = "nitrate ammonia"
    h.search(
        q=q,
        queries=["copper selectivity"],
        answers=["Isolated Cu sites raise ammonia efficiency."],
        page_size=5,
    )
    assert len(emb.batch_calls) == 1  # exactly one embed round trip
    assert emb.one_calls == []  # no serial per-text embeds
    batch = emb.batch_calls[0]
    assert batch == [
        q,
        "copper selectivity",
        "Isolated Cu sites raise ammonia efficiency.",
    ]
    assert batch.count(q) == 1  # q is not embedded twice


def test_broad_lexical_mode_skips_embedding(store: Store) -> None:
    _seed(store, slug="lxA", blocks=_BLOCKS_A, embed=False)
    emb = CountingEmbedder()
    h = _handler(store, emb)
    resp = h.search(q="nitrate ammonia", queries=["copper"], mode="lexical")
    assert emb.batch_calls == []
    assert emb.one_calls == []
    assert "block hit" in resp.body


# ── (d) raising embedder degrades broad search to lexical legs ─────


def test_broad_search_degrades_when_embedder_raises(store: Store) -> None:
    _seed(store, slug="dgA", blocks=_BLOCKS_A, embed=False)
    h = _handler(store, BoomEmbedder())
    resp = h.search(
        q="nitrate ammonia",
        queries=["hydrogen evolution"],
        answers=["Copper suppresses the hydrogen evolution reaction."],
    )
    # No exception; the lexical legs still answer.
    assert "block hit" in resp.body
    assert "no paper blocks match" not in resp.body
