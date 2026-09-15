"""``search(kind='paper', q=<a paper's title>)`` must name the paper.

Regression: typing an exact title (``attention is all you need``) into
paper search returned a table of chunk handles and per-chunk keywords
with nothing recognisable in it — Postgres FTS strips the query to
``'attent' & 'need'``, so content-dense bodies of *other* papers win on
``ts_rank``. The title introducer already promoted the right paper's
block to row 1, but the row renders as ``pc<id>`` plus that chunk's
keywords (for the Transformer paper, its boilerplate permissions
chunk), so the promotion was invisible and the caller concluded the
paper wasn't held.

The fix is a ``Title match —`` callout above the table naming the paper
*record* (``pa`` handle + one-line citation), plus a ``Next:`` entry
opening it. These tests pin the callout on both the has-hits and the
no-hits branch, and pin the tight gating — an ordinary keyword query
must not grow a callout.

gr244679 (fresh 2026-09-14 user report) sharpened the bar: the callout
alone made the *answer* legible but left the promoted row itself
unreadable. Two follow-on fixes, both pinned here:

1. Representative-block selection (``_representative_block_for_ref``)
   prefers the paper's ``card_combined`` card (title+authors+abstract,
   ``ord=-1``) over the ``pos=0`` chunk, since publisher PDFs routinely
   put copyright/permissions boilerplate first.
2. The promoted row itself now renders the paper's own (``pa``) handle
   plus a one-line citation — not a chunk handle (``pc<id>``) plus that
   chunk's keywords — so a title-shaped query returns the paper as a
   legible row, not chunk soup, regardless of what the representative
   block's raw text happens to be.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers._paper_search import _representative_block_for_ref
from precis.handlers.paper import PaperHandler
from precis.ingest.cards import combined_card_text
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry

_TITLE = "Attention is All you Need"


def _seed(
    store: Store,
    *,
    slug: str,
    title: str,
    text: str,
    embedder: MockEmbedder,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"family": "Vaswani", "given": "Ashish"}],
        year=2017,
    )
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, embedding=embedder.embed_one(text))],
    )
    return ref.id


def _handler(store: Store, embedder: MockEmbedder) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=embedder))


def test_title_query_names_the_paper_record(store: Store) -> None:
    """The exact-title query surfaces the paper's own ``pa`` handle and
    citation, even though the block that got promoted is boilerplate."""
    e = MockEmbedder(dim=1024)
    rid = _seed(
        store,
        slug="vaswani17",
        title=_TITLE,
        text="google hereby grants permission to reproduce the tables and figures",
        embedder=e,
    )
    # A decoy the lexical leg genuinely prefers for the stripped query
    # ('attent' & 'need') — without the callout this is all the caller
    # would see.
    _seed(
        store,
        slug="decoy25",
        title="Order parameters need attention",
        text=(
            "attention needs constant attention; the parameters need attention "
            "and further attention across every needed axis"
        ),
        embedder=e,
    )

    resp = _handler(store, e).search(q="attention is all you need", page_size=5)
    assert "Title match" in resp.body
    assert _TITLE in resp.body
    assert "Vaswani" in resp.body
    assert handle_registry.format_handle("paper", rid) in resp.body


def test_ordinary_keyword_query_has_no_callout(store: Store) -> None:
    """The gate is tight: a topical query must render unchanged."""
    e = MockEmbedder(dim=1024)
    _seed(
        store,
        slug="cu-cat26",
        title="Single-atom copper catalysts for nitrate reduction",
        text="single-atom copper catalyst nitrate reduction ammonia faradaic",
        embedder=e,
    )
    resp = _handler(store, e).search(q="copper nitrate ammonia", page_size=5)
    assert "Title match" not in resp.body


def test_retracted_title_match_keeps_its_notice(store: Store) -> None:
    """The callout promotes a paper above its own hit row — it must not
    be the one place the ``⚠`` retraction notice goes missing."""
    e = MockEmbedder(dim=1024)
    rid = _seed(
        store,
        slug="bad-title26",
        title="Room-temperature superconductivity in a nickelate",
        text="nickelate lattice resistivity measurement apparatus",
        embedder=e,
    )
    store.set_retraction_status(rid, status="retracted")

    resp = _handler(store, e).search(
        q="room-temperature superconductivity in a nickelate", page_size=5
    )
    callout = [ln for ln in resp.body.splitlines() if "Room-temperature" in ln]
    assert callout, resp.body
    assert "RETRACTED" in callout[0]


def test_representative_block_prefers_card_over_boilerplate(store: Store) -> None:
    """gr244679: representative-block selection prefers the paper's
    ``card_combined`` card (title + authors + abstract, ord=-1) over a
    boilerplate ``pos=0`` chunk — the copyright/permissions block
    publisher PDFs routinely put first."""
    e = MockEmbedder(dim=1024)
    rid = _seed(
        store,
        slug="vaswani17c",
        title=_TITLE,
        text=(
            "Google hereby grants permission to reproduce the tables "
            "and figures for personal use only"
        ),
        embedder=e,
    )
    card_text = combined_card_text(
        _TITLE,
        ["Ashish Vaswani"],
        "We propose the Transformer, a novel network architecture "
        "based solely on attention mechanisms, dispensing with "
        "recurrence and convolutions entirely.",
        [],
    )
    store.chunks.upsert_card_combined(rid, card_text)

    block = _representative_block_for_ref(store, rid)
    assert block is not None
    assert block.chunk_kind == "card_combined"
    assert "convolutions" in block.text.lower()
    assert "grants permission" not in block.text.lower()


def test_representative_block_falls_back_to_pos0_without_card(store: Store) -> None:
    """No regression: a paper with no card variant still selects its
    ``pos=0`` block as the representative (pre-existing behavior)."""
    e = MockEmbedder(dim=1024)
    rid = _seed(
        store,
        slug="vaswani17d",
        title=_TITLE,
        text=(
            "We present a new simple network architecture, the "
            "Transformer, based solely on attention mechanisms, "
            "dispensing with recurrence and convolutions entirely."
        ),
        embedder=e,
    )

    block = _representative_block_for_ref(store, rid)
    assert block is not None
    assert block.ord == 0
    assert "convolutions entirely" in block.text.lower()


def test_title_match_row_renders_paper_record_not_chunk_soup(store: Store) -> None:
    """gr244679 fix bar item 2: the promoted row itself — not just the
    callout above it — must be legible. A row injected by the title
    introducer now renders the paper's own (``pa``) handle and a
    one-line citation, rather than a chunk handle (``pc<id>``) plus
    that chunk's keywords — even when the representative block is the
    paper's boilerplate ``pos=0`` permissions chunk (no card seeded), a
    title-shaped query must still return something recognisable as the
    paper, as a row."""
    e = MockEmbedder(dim=1024)
    rid = _seed(
        store,
        slug="vaswani17e",
        title=_TITLE,
        text=(
            "Google hereby grants permission to reproduce the tables "
            "and figures for personal use only"
        ),
        embedder=e,
    )

    resp = _handler(store, e).search(q="attention is all you need", page_size=5)
    assert "Title match" in resp.body
    pa_handle = handle_registry.format_handle("paper", rid)
    assert pa_handle in resp.body
    # No chunk handle for this ref's boilerplate block leaked into the
    # response — the row addresses the paper record, not one of its
    # chunks.
    assert "google hereby grants permission" not in resp.body.lower()
    # The row reads as a citation, not raw block text.
    assert "Vaswani" in resp.body
    assert _TITLE in resp.body


def test_title_match_survives_unrelated_body(store: Store) -> None:
    """No body block echoes the query, but the record does — the
    response must still answer "yes, it's here"."""
    e = MockEmbedder(dim=1024)
    _seed(
        store,
        slug="vaswani17b",
        title=_TITLE,
        text="zzqqxx unrelated filler tokens with no overlap whatsoever",
        embedder=e,
    )
    resp = _handler(store, e).search(q="attention is all you need", page_size=5)
    assert _TITLE in resp.body
    assert "Title match" in resp.body
