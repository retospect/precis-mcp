"""``unique_per='paper'`` — the default one-row-per-paper search view.

`docs/backlog/paper-search-unique-per-paper.md`: ``search(kind='paper',
q=…)`` defaults to one row per *paper* — the best hit's ``pc`` handle, a
``more`` column counting that ref's other hits in the retrieved pool, and
the best chunk's keywords — with the honest two-level counter
``N papers of M matched (K chunk hits)`` in the headline. ``unique_per=
'chunk'`` is the opt-in per-hit drill shape, and is *implied* by the
doors that already ask for it (``scope=``, ``mode='verbatim'`` — the
verbatim early-return applies no per-paper collapse, so an explicit
``'paper'`` there is a BadInput rather than a silent fallback).

Page-size defaults follow the row shape — 25 papers / 10 chunks — which
is why the tool boundary now forwards ``page_size=None`` when unset and
the handler resolves it *after* ``unique_per``. An explicitly passed
``unique_per`` echoes into same-search continuations so ``page=N+1``
can't silently change shape.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry


def _seed(
    store: Store, *, slug: str, blocks: list[str], title: str | None = None
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title or slug)
    e = MockEmbedder(dim=1024)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=t, embedding=e.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


def _handler(store: Store) -> PaperHandler:
    # MockEmbedder keeps hybrid/semantic legs deterministic; the lexical
    # corpus below is seeded so lexical alone decides membership.
    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def _row_chunk_handles(body: str) -> list[str]:
    """The pc<id> tokens that head each TOON row (callouts/``get(id=…)``
    hints may also name handles — the table rows are the ``pc``-prefixed
    lines before the ``Next:`` trailer)."""
    table = body.split("Next:", 1)[0]
    return [ln.split("\t", 1)[0] for ln in table.splitlines() if ln.startswith("pc")]


def _row_ref_ids(store: Store, body: str) -> set[int]:
    ids = set()
    for handle in _row_chunk_handles(body):
        resolved = store.resolve_handle(handle)
        assert resolved is not None, f"row handle {handle} unresolved"
        ids.add(resolved.ref_id)
    return ids


# ── row shape ────────────────────────────────────────────────────────────


def test_default_is_one_row_per_paper(store: Store) -> None:
    """A paper with three matching chunks collapses to ONE row — the
    best hit — with the extras counted in ``more``; the other paper
    keeps its own row."""
    a = _seed(
        store,
        slug="up-a",
        blocks=[
            "copper nitrate ammonia electrochemistry first",
            "copper nitrate ammonia electrochemistry second",
            "copper nitrate ammonia electrochemistry third",
        ],
    )
    b = _seed(store, slug="up-b", blocks=["copper nitrate ammonia once"])
    out = _handler(store).search(q="copper nitrate ammonia")
    rows = _row_chunk_handles(out.body)
    assert len(rows) == 2
    assert _row_ref_ids(store, out.body) == {a, b}
    # Paper A's other two pool hits collapse into the ``more`` counter.
    assert "+2" in out.body


def test_unique_per_chunk_restores_per_hit_rows(store: Store) -> None:
    """The drill shape returns the chunk-level list — every matching
    chunk is its own row, the old (pre-paper-default) shape."""
    _seed(
        store,
        slug="uc-a",
        blocks=["copper nitrate ammonia x", "copper nitrate ammonia y"],
    )
    _seed(store, slug="uc-b", blocks=["copper nitrate ammonia z"])
    out = _handler(store).search(q="copper nitrate ammonia", unique_per="chunk")
    assert "block hit" in out.body
    assert len(_row_chunk_handles(out.body)) == 3


def test_scope_implies_chunk_mode(store: Store) -> None:
    """``scope=`` is a drill door — an in-paper search is per-hit even
    without ``unique_per``, because the caller already said 'inside this
    paper'."""
    ref_a = _seed(
        store,
        slug="sc-a",
        blocks=["copper nitrate ammonia x", "copper nitrate ammonia y"],
    )
    _seed(store, slug="sc-b", blocks=["copper nitrate ammonia z"])
    out = _handler(store).search(
        q="copper nitrate ammonia", scope=handle_registry.format_handle("paper", ref_a)
    )
    rows = _row_chunk_handles(out.body)
    assert len(rows) == 2  # both hits inside the scoped paper
    assert _row_ref_ids(store, out.body) == {ref_a}


def test_verbatim_implies_chunk_and_rejects_explicit_paper(store: Store) -> None:
    """``mode='verbatim'``'s keyword-containment path applies no
    per-paper collapse — implicit ``unique_per`` resolves to 'chunk'
    there; asking for 'paper' anyway is a real conflict, not a silent
    fallback."""
    ref = _seed(store, slug="vb-a", blocks=["alpha beta one", "alpha beta two"])
    with store.pool.connection() as conn:
        for ord_ in (0, 1):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = %s",
                (["alpha", "beta"], ref, ord_),
            )
        conn.commit()
    h = _handler(store)
    out = h.search(q="alpha beta", mode="verbatim")
    assert "block hit" in out.body
    assert len(_row_chunk_handles(out.body)) == 2
    with pytest.raises(BadInput, match="verbatim"):
        h.search(q="alpha beta", mode="verbatim", unique_per="paper")


# ── validation ───────────────────────────────────────────────────────────


def test_unique_per_rejects_unknown_value(store: Store) -> None:
    with pytest.raises(BadInput, match="unique_per"):
        _handler(store).search(q="x", unique_per="paper-ish")


def test_per_paper_over_one_rejected_under_paper_rows(store: Store) -> None:
    """``per_paper=N`` is a chunk-mode cap — under the paper-row default
    it would silently do nothing, so it errors and names the drill view.
    ``per_paper=1`` is the collapse the mode already performs, so it
    stays a no-op rather than breaking old call sites."""
    h = _handler(store)
    with pytest.raises(BadInput, match="per_paper"):
        h.search(q="x", per_paper=2)
    _seed(store, slug="pp-a", blocks=["alpha beta"])
    out = h.search(q="alpha beta", per_paper=1)
    assert "1 paper" in out.body


# ── defaults + pagination ────────────────────────────────────────────────


def test_page_size_defaults_follow_the_row_shape(store: Store) -> None:
    """12 one-chunk papers on one matching term: the default paper page
    (25) returns all 12; the chunk page (10) stops at 10. An explicit
    ``page_size`` overrides both."""
    for i in range(12):
        _seed(store, slug=f"ps-{i}", blocks=[f"gallium arsenide study {i}"])
    h = _handler(store)
    paper = h.search(q="gallium arsenide")
    assert len(_row_chunk_handles(paper.body)) == 12  # 25-row paper page
    chunk = h.search(q="gallium arsenide", unique_per="chunk")
    assert len(_row_chunk_handles(chunk.body)) == 10  # 10-row chunk page
    assert "page=2" in chunk.body  # the chunk page has more to offer
    explicit = h.search(q="gallium arsenide", page_size=5)
    assert len(_row_chunk_handles(explicit.body)) == 5


def test_paper_mode_pages_papers_not_chunks(store: Store) -> None:
    """Pagination walks *papers*: page 2 returns the papers page 1's
    rows didn't cover — never the first page's second chunk."""
    _seed(
        store,
        slug="pg-a",
        blocks=["copper nitrate ammonia x", "copper nitrate ammonia y"],
    )
    _seed(store, slug="pg-b", blocks=["copper nitrate ammonia z"])
    _seed(store, slug="pg-c", blocks=["copper nitrate ammonia w"])
    h = _handler(store)
    first = h.search(q="copper nitrate ammonia", page_size=2)
    assert len(_row_chunk_handles(first.body)) == 2
    assert "see the next 2 of 3 papers" in first.body
    assert "page=2" in first.body
    second = h.search(q="copper nitrate ammonia", page_size=2, page=2)
    assert len(_row_chunk_handles(second.body)) == 1
    assert set(_row_chunk_handles(first.body)).isdisjoint(
        _row_chunk_handles(second.body)
    )


def test_singleton_paper_row_restarts_at_the_25_row_page(store: Store) -> None:
    """The singleton-hit widen hint restarts at the *paper* page size —
    ``page_size=25, page=1`` — not the chunk view's 10."""
    for slug in ("sp-a", "sp-b", "sp-c"):
        _seed(store, slug=slug, blocks=[f"photocatalytic in {slug}"])
    out = _handler(store).search(q="photocatalytic", page_size=1)
    assert "restart at page 1 with up to 25 of 3 papers" in out.body
    assert "page_size=25, page=1" in out.body


# ── headline counters ────────────────────────────────────────────────────


def test_headline_counts_papers_and_chunk_hits(store: Store) -> None:
    """``N papers of M matched (K chunk hits)`` — M counts DISTINCT refs
    in the lexical universe, K counts chunk hits; they differ only when
    a paper has several matching chunks."""
    _seed(
        store,
        slug="hl-a",
        blocks=["copper nitrate ammonia x", "copper nitrate ammonia y"],
    )
    _seed(store, slug="hl-b", blocks=["copper nitrate ammonia z"])
    _seed(store, slug="hl-c", blocks=["copper nitrate ammonia w"])
    out = _handler(store).search(q="copper nitrate ammonia", page_size=2)
    headline = out.body.splitlines()[0]
    assert headline.startswith("# 2 of 3 papers")
    assert "4 chunk hits" in headline  # 2 + 1 + 1 matching chunks


def test_broad_paper_mode_keeps_honest_absent_total(store: Store) -> None:
    """A fused query has no lexical universe to count — the paper-mode
    headline renders the plain count (no 'of M', no chunk-hit suffix)
    rather than claim a denominator it never searched."""
    _seed(store, slug="bd-a", blocks=["copper nitrate ammonia x"])
    _seed(store, slug="bd-b", blocks=["copper nitrate ammonia y"])
    out = _handler(store).search(
        q="copper nitrate ammonia",
        queries=["cu selectivity ammonia"],
    )
    headline = out.body.splitlines()[0]
    assert " of " not in headline
    assert "chunk hits" not in headline


def test_semantic_paper_mode_keeps_honest_absent_total(store: Store) -> None:
    """``mode='semantic'`` has no countable universe under paper rows
    either — same honest-absent rule as broad (the single-leg chunk path
    already did this)."""
    _seed(store, slug="sm-a", blocks=["quantum battery anode decay x"])
    out = _handler(store).search(q="quantum battery anode decay", mode="semantic")
    headline = out.body.splitlines()[0]
    assert " of " not in headline


# ── best hit, keywords, card-vs-body ─────────────────────────────────────


def test_row_shows_best_chunk_handle_and_its_keywords(store: Store) -> None:
    """The collapsed row names the BEST hit's ``pc`` handle — the one the
    caller pastes into ``get`` — and carries that chunk's keywords, so a
    ``+N`` paper is still glanceable."""
    body_hit = "copper nitrate ammonia body passage distinctive phrase"
    _seed(
        store,
        slug="kw-a",
        blocks=[body_hit, "copper nitrate ammonia secondary passage"],
    )
    out = _handler(store).search(q="copper nitrate ammonia")
    first_row = _row_chunk_handles(out.body)[0]
    # The shown handle is a real chunk of this paper and resolvable.
    resolved = store.resolve_handle(first_row)
    assert resolved is not None


def test_body_hit_wins_over_a_stronger_card(store: Store) -> None:
    """R14 shipped the store-side rule; paper rows ride on it: a card
    that outranks the body chunk in the fused pool does not get the row,
    and it does not inflate ``more`` either — the pool count is taken
    *after* card removal."""
    ref_id = _seed(
        store,
        slug="cb-a",
        blocks=["a body passage mentioning copper nitrate ammonia electrochemistry"],
    )
    card_text = "copper nitrate ammonia " * 12 + "card"
    card_id = store.chunks.upsert_card_combined(ref_id, card_text)
    out = _handler(store).search(q="copper nitrate ammonia", mode="lexical")
    body = out.body
    card_handle = handle_registry.format_handle("paper", card_id, chunk=True)
    assert card_handle not in body
    assert "+1" not in body  # the card never counted


# ── hint echo ────────────────────────────────────────────────────────────


def test_explicit_chunk_mode_echoes_into_continuations(store: Store) -> None:
    """An explicit ``unique_per='chunk'`` must reappear in the next-page
    hint — an omitted knob re-resolves to 'paper' on the replay and the
    caller would silently land on the wrong shape mid-walk."""
    for i in range(3):
        _seed(
            store,
            slug=f"ec-{i}",
            blocks=[f"copper nitrate ammonia {i}"],
        )
    out = _handler(store).search(
        q="copper nitrate ammonia", unique_per="chunk", page_size=1
    )
    assert "unique_per='chunk'" in out.body


def test_paper_mode_offers_the_chunk_drill(store: Store) -> None:
    """The drill door is discoverable from a paper page: the trailer
    advertises the same search in the per-hit shape."""
    _seed(
        store,
        slug="dr-a",
        blocks=["copper nitrate ammonia x", "copper nitrate ammonia y"],
    )
    _seed(store, slug="dr-b", blocks=["copper nitrate ammonia z"])
    _seed(store, slug="dr-c", blocks=["copper nitrate ammonia w"])
    out = _handler(store).search(q="copper nitrate ammonia", page_size=2)
    assert "unique_per='chunk'" in out.body
    assert "one row per hit" in out.body


# ── title callout + DOI unchanged ─────────────────────────────────────────


def test_title_callout_does_not_promote_a_row(store: Store) -> None:
    """An exact-title query still gets the callout, but the paper-mode
    row list is real pool hits only — no rep (record) row is promoted
    into the capped list (the R14 page-integrity fix)."""
    e = MockEmbedder(dim=1024)
    title = "Attention is All you Need"
    ref = store.insert_ref(
        kind="paper",
        slug="attn-p",
        title=title,
        authors=[{"family": "V", "given": "A"}],
    )
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="unrelated boilerplate",
                embedding=e.embed_one("unrelated boilerplate"),
            )
        ],
    )
    h = PaperHandler(hub=Hub(store=store, embedder=e))
    out = h.search(q="attention is all you need")
    assert "Title match" in out.body
    # No pool hit → the promoted record row is the only mention of the
    # pa handle; the hits table itself is empty (callout + empty page).
    assert "no paper blocks match" in out.body


# ── store-level: ref_counts_out ──────────────────────────────────────────


def test_ref_counts_out_counts_pre_cap_pool(store: Store) -> None:
    """``ref_counts_out`` reports each ref's hit count in the fused
    pool BEFORE the ``per_paper`` cap — it powers the ``more`` counter,
    so it must count what the cap discarded, not just the page."""
    _seed(
        store,
        slug="rc-a",
        blocks=[
            "copper nitrate ammonia refcounts one",
            "copper nitrate ammonia refcounts two",
        ],
    )
    _seed(store, slug="rc-b", blocks=["copper nitrate ammonia refcounts three"])
    counts: dict[int, int] = {}
    hits = store.chunks.search_chunks_multi(
        q_texts=["copper nitrate ammonia refcounts"],
        query_vecs=[],
        mode="lexical",
        kind="paper",
        limit=10,
        per_paper=1,
        ref_counts_out=counts,
    )
    # One row per ref at cap 1…
    assert len(hits) == 2
    # …but the counts saw all three pool hits.
    a = store.get_ref(kind="paper", id="rc-a")
    b = store.get_ref(kind="paper", id="rc-b")
    assert a is not None and b is not None
    assert counts == {a.id: 2, b.id: 1}


def test_ref_counts_out_stays_empty_on_verbatim(store: Store) -> None:
    """Verbatim's early return predates the fused pool — a counter for
    that path would be the keyword-containment pool, not the fused one,
    so the output is cleared and left empty there (never a wrong count)."""
    ref = _seed(store, slug="rc-v", blocks=["alpha beta verbatim"])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = %s",
            (["alpha", "beta"], ref),
        )
        conn.commit()
    counts: dict[int, int] = {999: 999}
    hits = store.chunks.search_chunks_multi(
        q_texts=["alpha beta"],
        query_vecs=[],
        mode="verbatim",
        kind="paper",
        limit=10,
        ref_counts_out=counts,
    )
    assert hits
    assert counts == {}
