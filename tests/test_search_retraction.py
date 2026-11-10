"""End-to-end paper-search retraction handling.

Covers the main consumer — ``PaperHandler.search()`` → ``FusedBlockSearch``
+ ``PaperSearchResultRenderer`` — which builds its TOON table directly
from ``(block, ref, score)`` triples and never touches ``SearchHit``.
The lower-level ``SearchHit`` RRF-fusion + rendering behaviour has its
own unit coverage in ``test_search_merge.py``.

Judgement call under test: ``retracted`` is a HARD downrank
(annotate + sink, never exclude — a retracted paper is often exactly
what the search is looking for); ``corrected`` /
``expression_of_concern`` are SOFT (annotate, mild downrank only).
``retraction_status IS NULL`` (the sparse-coverage default for most of
the corpus) must be a complete no-op — that's the regression risk.

The paper-search TOON table renders the chunk *handle* (``pc<id>``),
never the ref slug — so these tests resolve each rendered handle back
to its owning ref via ``store.resolve_handle`` rather than grepping
for a slug string in the response body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers._paper_search import _apply_retraction_downrank, _retraction_flag
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store

_HANDLE_RE = re.compile(r"\bpc\d+\b")


def _seed(store: Store, *, slug: str, text: str, embedder: MockEmbedder) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, embedding=embedder.embed_one(text))],
    )
    return ref.id


def _handler(store: Store, embedder: MockEmbedder) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=embedder))


def _ranked_ref_ids(store: Store, body: str) -> list[int]:
    """Chunk handles in TOON-table render order, resolved to their
    owning ``ref_id``.

    Only scans the table (before the ``Next:`` trailer) — the
    trailer echoes the top hit's own handle again
    (``get(id='pc1')``), which would otherwise double-count it as a
    second, spurious row.
    """
    table_only = body.split("\nNext:", 1)[0]
    out = []
    for handle in _HANDLE_RE.findall(table_only):
        resolved = store.resolve_handle(handle)
        assert resolved is not None, f"unresolvable handle {handle!r}"
        out.append(resolved.ref_id)
    return out


def test_retracted_paper_ranks_below_clean_equivalent(store: Store) -> None:
    """Two papers, near-identical text, one retracted — the retracted
    one must rank second even though the underlying lex+sem fusion
    would otherwise treat them as roughly tied."""
    e = MockEmbedder(dim=1024)
    text = "single-atom copper catalyst nitrate reduction ammonia"
    rid_bad = _seed(store, slug="bad-paper", text=text, embedder=e)
    rid_good = _seed(store, slug="good-paper", text=text, embedder=e)
    store.set_retraction_status(rid_bad, status="retracted")

    h = _handler(store, e)
    resp = h.search(q="copper nitrate ammonia", page_size=10)
    order = _ranked_ref_ids(store, resp.body)
    assert rid_bad in order and rid_good in order
    assert order.index(rid_good) < order.index(rid_bad)


def test_retracted_paper_annotated_in_paper_search_toon(store: Store) -> None:
    """``PaperSearchResultRenderer``'s TOON table is the primary
    consumer per the task brief — it must not silently drop the flag
    even though it doesn't route through ``SearchHit``."""
    e = MockEmbedder(dim=1024)
    text = "graphene oxide membrane water desalination performance"
    rid = _seed(store, slug="flagged-paper", text=text, embedder=e)
    store.set_retraction_status(rid, status="retracted")

    h = _handler(store, e)
    resp = h.search(q="graphene oxide desalination", page_size=10)
    assert "RETRACTED" in resp.body


def test_soft_status_annotates_in_paper_search_toon(store: Store) -> None:
    """``corrected`` / ``expression_of_concern`` still surface the
    annotation through the real search path (the severity/ordering
    behaviour itself is pinned deterministically against
    ``_apply_retraction_downrank`` directly below — the DB-backed
    lex+sem fusion score is realistic but not a precise enough dial to
    assert an exact rank delta against)."""
    e = MockEmbedder(dim=1024)
    text = "perovskite solar cell efficiency stability improvement"
    rid = _seed(store, slug="corrected-paper", text=text, embedder=e)
    store.set_retraction_status(rid, status="corrected")

    h = _handler(store, e)
    resp = h.search(q="perovskite solar cell efficiency", page_size=10)
    assert "CORRECTED" in resp.body


# ---------------------------------------------------------------------------
# _apply_retraction_downrank / _retraction_flag — deterministic unit
# coverage of the actual severity split + ordering, independent of
# store lexical/semantic fusion behaviour.
# ---------------------------------------------------------------------------


@dataclass
class _FakeRef:
    retraction_status: str | None = None


def test_downrank_retracted_sinks_below_a_much_weaker_clean_hit() -> None:
    """The hard case: even a *dominant* retracted hit (score 0.9) must
    sink below a comparatively weak clean hit (score 0.1) — proving
    the 0.02 factor is strong enough to invert a real strength gap,
    not just a knife's-edge tie."""
    strong_retracted: tuple[Any, Any, float] = (
        object(),
        _FakeRef("retracted"),
        0.9,
    )
    weak_clean: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.1)
    out = _apply_retraction_downrank([strong_retracted, weak_clean])
    assert out == [weak_clean, strong_retracted]


def test_downrank_soft_status_preserves_order_against_weaker_hit() -> None:
    """The soft case: the same dominant/weak pairing, but ``corrected``
    — the 0.85 factor must NOT be strong enough to invert a real
    strength gap. Direct contrast with the hard case above."""
    strong_corrected: tuple[Any, Any, float] = (
        object(),
        _FakeRef("corrected"),
        0.9,
    )
    weak_clean: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.1)
    out = _apply_retraction_downrank([strong_corrected, weak_clean])
    assert out == [strong_corrected, weak_clean]


def test_downrank_expression_of_concern_same_as_corrected() -> None:
    strong_eoc: tuple[Any, Any, float] = (
        object(),
        _FakeRef("expression_of_concern"),
        0.9,
    )
    weak_clean: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.1)
    out = _apply_retraction_downrank([strong_eoc, weak_clean])
    assert out == [strong_eoc, weak_clean]


def test_downrank_null_status_is_a_complete_no_op() -> None:
    """Regression guard at the unit level: an all-``None`` hit list
    must come back byte-identical (order and objects), matching the
    ``factor == 1.0`` no-entry branch exactly."""
    a: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.9)
    b: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.5)
    c: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.1)
    assert _apply_retraction_downrank([a, b, c]) == [a, b, c]


def test_downrank_empty_list_is_a_no_op() -> None:
    assert _apply_retraction_downrank([]) == []


def test_retraction_flag_labels() -> None:
    assert "RETRACTED" in _retraction_flag(_FakeRef("retracted"))
    assert "CORRECTED" in _retraction_flag(_FakeRef("corrected"))
    assert "EXPRESSION OF CONCERN" in _retraction_flag(
        _FakeRef("expression_of_concern")
    )


def test_retraction_flag_empty_when_unset() -> None:
    assert _retraction_flag(_FakeRef(None)) == ""


def test_null_retraction_status_does_not_change_ranking(store: Store) -> None:
    """Regression guard: unrelated, unchecked papers — order and
    annotation must be exactly what plain relevance produces, matching
    the pre-feature behaviour pinned by ``test_block_search.py`` /
    ``test_paper_search_broad.py``."""
    e = MockEmbedder(dim=1024)
    _seed(
        store,
        slug="a-exact",
        text="nitrate reduction copper electrode selectivity",
        embedder=e,
    )

    h = _handler(store, e)
    resp = h.search(
        q="nitrate reduction copper electrode",
        page_size=10,
        unique_per="chunk",
    )
    body = resp.body
    assert "block hit" in body
    assert "⚠" not in body
    assert "RETRACTED" not in body
    assert "CORRECTED" not in body


# ---------------------------------------------------------------------------
# gr613850 — mode='semantic' scores are cosine DISTANCE (smaller = better).
# The downrank used to sort every mode by ``-(score * factor)``, which
# reversed a semantic page farthest-first.
# ---------------------------------------------------------------------------


def test_downrank_ascending_keeps_nearest_first_and_sinks_retracted() -> None:
    """Distance scores: no-status hits keep nearest-first, and a retracted
    hit's distance gets WORSE (larger), not better."""
    near: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.10)
    mid: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.20)
    far: tuple[Any, Any, float] = (object(), _FakeRef(None), 0.30)
    assert _apply_retraction_downrank([near, mid, far], ascending=True) == [
        near,
        mid,
        far,
    ]
    # retracted nearest hit: 0.10 / 0.02 = 5.0 -> behind both clean hits.
    bad_near: tuple[Any, Any, float] = (object(), _FakeRef("retracted"), 0.10)
    assert _apply_retraction_downrank([bad_near, mid, far], ascending=True) == [
        mid,
        far,
        bad_near,
    ]
    # soft status (factor 0.85): 0.10 / 0.85 = 0.118 stays ahead of 0.20.
    soft_near: tuple[Any, Any, float] = (object(), _FakeRef("corrected"), 0.10)
    assert _apply_retraction_downrank([soft_near, mid], ascending=True) == [
        soft_near,
        mid,
    ]


class _AxisEmbedder(MockEmbedder):
    """Query = unit x-axis; a stored text ``"theta:<t>"`` embeds at angle
    ``t`` off the x-axis, so its cosine distance is ``1 - cos(t)`` —
    strictly increasing in ``t``. Any other text embeds on the x-axis."""

    def embed_one(self, text: str) -> list[float]:
        import math

        theta = float(text.split(":", 1)[1]) if text.startswith("theta:") else 0.0
        vec = [0.0] * self.dim
        vec[0] = math.cos(theta)
        vec[1] = math.sin(theta)
        return vec


def _seed_angled(store: Store, e: _AxisEmbedder, thetas: list[float]) -> list[int]:
    """One paper per angle, nearest first; returns their ref ids."""
    out = []
    for i, t in enumerate(thetas):
        text = f"theta:{t}"
        ref = store.insert_ref(kind="paper", slug=f"angle-{i}", title=f"angle-{i}")
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text=text, embedding=e.embed_one(text))]
        )
        out.append(ref.id)
    return out


def test_semantic_paper_search_returns_nearest_first(store: Store) -> None:
    e = _AxisEmbedder(dim=1024)
    rids = _seed_angled(store, e, [0.1, 0.3, 0.5, 0.7, 0.9])

    h = _handler(store, e)
    resp = h.search(q="query", mode="semantic", page_size=10, unique_per="chunk")
    assert _ranked_ref_ids(store, resp.body) == rids


def test_semantic_paper_search_downranks_retracted_not_promotes(
    store: Store,
) -> None:
    """The retracted paper is the NEAREST hit (distance 0.005); /0.02 puts
    it at 0.25, between the 0.7 (0.235) and 0.9 (0.378) angles — behind
    the clean nearer papers, still present."""
    e = _AxisEmbedder(dim=1024)
    rids = _seed_angled(store, e, [0.1, 0.3, 0.5, 0.7, 0.9])
    store.set_retraction_status(rids[0], status="retracted")

    h = _handler(store, e)
    resp = h.search(q="query", mode="semantic", page_size=10, unique_per="chunk")
    assert _ranked_ref_ids(store, resp.body) == [
        rids[1],
        rids[2],
        rids[3],
        rids[0],
        rids[4],
    ]


def test_semantic_paper_search_paginates_nearest_first(store: Store) -> None:
    """Page 1 holds ranks 1-2, page 2 ranks 3-4 (the bench saw ranks 6-10
    on page 1)."""
    e = _AxisEmbedder(dim=1024)
    rids = _seed_angled(store, e, [0.1, 0.3, 0.5, 0.7, 0.9])

    h = _handler(store, e)
    p1 = h.search(q="query", mode="semantic", page_size=2, unique_per="chunk")
    p2 = h.search(q="query", mode="semantic", page_size=2, page=2, unique_per="chunk")
    assert _ranked_ref_ids(store, p1.body) == rids[:2]
    assert _ranked_ref_ids(store, p2.body) == rids[2:4]


def test_semantic_mode_without_embedder_keeps_lexical_order(store: Store) -> None:
    """``mode='semantic'`` with no embedder degrades to the lexical leg,
    whose score is ts_rank (bigger = better): the downrank must stay
    descending there, so a retracted hit still sinks below a clean one."""
    e = MockEmbedder(dim=1024)
    text = "single-atom copper catalyst nitrate reduction ammonia"
    rid_bad = _seed(store, slug="bad-paper", text=text, embedder=e)
    rid_good = _seed(store, slug="good-paper", text=text, embedder=e)
    store.set_retraction_status(rid_bad, status="retracted")

    h = PaperHandler(hub=Hub(store=store, embedder=None))
    resp = h.search(
        q="copper nitrate ammonia", mode="semantic", page_size=10, unique_per="chunk"
    )
    order = _ranked_ref_ids(store, resp.body)
    assert order.index(rid_good) < order.index(rid_bad)


def test_lexical_mode_still_downranks_retracted(store: Store) -> None:
    """Lexical (ts_rank, bigger = better) keeps the descending path."""
    e = MockEmbedder(dim=1024)
    text = "single-atom copper catalyst nitrate reduction ammonia"
    rid_bad = _seed(store, slug="bad-paper", text=text, embedder=e)
    rid_good = _seed(store, slug="good-paper", text=text, embedder=e)
    store.set_retraction_status(rid_bad, status="retracted")

    h = _handler(store, e)
    resp = h.search(
        q="copper nitrate ammonia", mode="lexical", page_size=10, unique_per="chunk"
    )
    order = _ranked_ref_ids(store, resp.body)
    assert order.index(rid_good) < order.index(rid_bad)
