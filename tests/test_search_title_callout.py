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
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
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


def test_title_match_row_renders_card_over_boilerplate(store: Store) -> None:
    """gr244679: the promoted row itself, not just the callout, must be
    legible. ``pos=0`` is publisher boilerplate; the paper's
    ``card_combined`` card (title + authors + abstract, ord=-1) is the
    representative block a promoted row renders, so its keywords name
    the paper's actual content instead of the copyright notice."""
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

    resp = _handler(store, e).search(q="attention is all you need", page_size=5)
    assert "Title match" in resp.body
    assert handle_registry.format_handle("paper", rid) in resp.body
    # The row's chunk_keywords cell reflects the card, not pos=0.
    assert "convolutions" in resp.body.lower()
    assert "grants permission" not in resp.body.lower()


def test_title_match_row_falls_back_to_pos0_without_card(store: Store) -> None:
    """No regression: a paper with no card variant still renders its
    ``pos=0`` block as the representative row (pre-existing behavior)."""
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

    resp = _handler(store, e).search(q="attention is all you need", page_size=5)
    assert "Title match" in resp.body
    assert handle_registry.format_handle("paper", rid) in resp.body
    assert "convolutions entirely" in resp.body.lower()


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
