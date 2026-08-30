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
