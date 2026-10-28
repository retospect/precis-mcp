"""gr520915: the ref-level ``JUNK:junk`` verdict on a paper.

A human stamps ``JUNK:junk`` when a paper is furniture-only — the
chunk-level junk gate's verdict lifted to the whole record via
``ref_kinds:`` in ``data/axes/junk.yaml``. Default paper read paths
then drop it: block search, the ``tags=``-only browse, the stamp
cohort listing, ``view='count'``. An explicit ``JUNK:`` pin opts out
(the audit view — ``_junk_exclude`` mirrors the STATUS default's
explicit-pin convention).
"""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store, Tag
from precis.utils import handle_registry


def _pa(ref_id: int) -> str:
    return handle_registry.format_handle("paper", ref_id)


def _seed(store: Store, *, slug: str, embedder: MockEmbedder) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    text = f"{slug} nitrate reduction body text"
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, embedding=embedder.embed_one(text))],
    )
    return ref.id


def _handler(store: Store, embedder: MockEmbedder) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=embedder))


def _ref_ids_in_search(store: Store, body: str) -> set[int]:
    """Ref ids named by the rendered ``pc<N>`` chunk handles (the
    TOON table — before the ``Next:`` trailer, which re-echoes the
    top handle)."""
    out: set[int] = set()
    for handle in re.findall(r"\bpc\d+\b", body.split("\nNext:", 1)[0]):
        resolved = store.resolve_handle(handle)
        if resolved is not None:
            out.add(resolved.ref_id)
    return out


def test_junk_verdict_drops_paper_from_block_search(store: Store) -> None:
    emb = MockEmbedder(dim=1024)
    junk = _seed(store, slug="junkblk2024", embedder=emb)
    keep = _seed(store, slug="keepblk2024", embedder=emb)
    store.add_tag(junk, Tag.closed("JUNK", "junk"))
    body = _handler(store, emb).search(q="nitrate reduction", page_size=10).body
    ids = _ref_ids_in_search(store, body)
    assert keep in ids
    assert junk not in ids


def test_tags_only_browse_excludes_junk(store: Store) -> None:
    emb = MockEmbedder(dim=1024)
    junk = _seed(store, slug="junkbrw2024", embedder=emb)
    keep = _seed(store, slug="keepbrw2024", embedder=emb)
    store.add_tag(junk, Tag.closed("JUNK", "junk"))
    store.add_tag(junk, Tag.open("topic-nitrate"))
    store.add_tag(keep, Tag.open("topic-nitrate"))
    body = _handler(store, emb).search(tags=["topic-nitrate"]).body
    assert _pa(keep) in body
    assert _pa(junk) not in body


def test_junk_pin_opts_into_the_bucket(store: Store) -> None:
    """``tags=['JUNK:junk']`` is the audit view — the pinned axis
    disables the default exclusion instead of AND-ing into zero
    hits."""
    emb = MockEmbedder(dim=1024)
    junk = _seed(store, slug="junkpin2024", embedder=emb)
    keep = _seed(store, slug="keeppin2024", embedder=emb)
    store.add_tag(junk, Tag.closed("JUNK", "junk"))
    body = _handler(store, emb).search(tags=["JUNK:junk"]).body
    assert _pa(junk) in body
    assert _pa(keep) not in body


def test_count_view_excludes_junk(store: Store) -> None:
    emb = MockEmbedder(dim=1024)
    junk = _seed(store, slug="junkcnt2024", embedder=emb)
    _seed(store, slug="keepcnt2024", embedder=emb)
    store.add_tag(junk, Tag.closed("JUNK", "junk"))
    body = _handler(store, emb).search(view="count").body
    assert body.startswith("# 1 paper entry")


def test_stamp_search_excludes_junk(store: Store) -> None:
    emb = MockEmbedder(dim=1024)
    junk = _seed(store, slug="junkstmp2024", embedder=emb)
    keep = _seed(store, slug="keepstmp2024", embedder=emb)
    store.add_tag(junk, Tag.closed("JUNK", "junk"))
    h = _handler(store, emb)
    for rid in (junk, keep):
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                ('{"probe_stamp": "yes"}', rid),
            )
    body = h.search(stamp="probe_stamp").body
    assert _pa(keep) in body
    assert _pa(junk) not in body
