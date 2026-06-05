"""RandomHandler — one call, returns a random corpus block.

``get(kind='random')`` picks a single undeleted embedded block
at random and renders its canonical handle with a drill-down
hint. No DSL, no arguments — every call rolls a fresh pick.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub, InitError
from precis.errors import NotFound, Unsupported
from precis.handlers.random import RandomHandler
from precis.store import Store
from precis.store.types import BlockInsert


@pytest.fixture
def handler(hub: Hub) -> RandomHandler:
    """Store-backed handler — the ``hub`` fixture wires a fresh
    store + MockEmbedder at the right dim."""
    return RandomHandler(hub=hub)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_oracle_with_embeddings(
    store: Store, hub: Hub, slug: str, texts: list[str]
) -> int:
    """Insert an oracle ref + ``len(texts)`` embedded blocks.

    Uses the hub's embedder so the blocks have the right dim.
    Returns the ref_id.
    """
    embedder = hub.embedder
    assert embedder is not None
    ref = store.insert_ref(kind="oracle", slug=slug, title=f"Oracle {slug}")
    embs = embedder.embed(texts)
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(
                pos=i,
                slug=None,
                text=text,
                token_count=len(text.split()),
                embedding=emb,
                density="sparse",
                meta={},
            )
            for i, (text, emb) in enumerate(zip(texts, embs))
        ],
    )
    return ref.id


def _seed_memory(store: Store, hub: Hub, text: str) -> int:
    """Insert a numeric-kind (memory) ref with one embedded body
    block. Returns the ref_id."""
    embedder = hub.embedder
    assert embedder is not None
    ref = store.insert_ref(kind="memory", slug=None, title=text[:40])
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(
                pos=0,
                slug=None,
                text=text,
                token_count=len(text.split()),
                embedding=embedder.embed_one(text),
                density="sparse",
                meta={},
            )
        ],
    )
    return ref.id


# ---------------------------------------------------------------------------
# KindSpec / verb surface
# ---------------------------------------------------------------------------


def test_kindspec_declares_only_get() -> None:
    spec = RandomHandler.spec
    assert spec.kind == "random"
    assert spec.supports_get is True
    assert spec.supports_search is False
    assert spec.supports_put is False
    assert spec.supports_edit is False
    assert spec.supports_delete is False
    assert spec.supports_tag is False
    assert spec.supports_link is False
    # ``id=`` is optional — the one call takes no arguments.
    assert spec.id_required is False


def test_other_verbs_unsupported(handler: RandomHandler) -> None:
    with pytest.raises(Unsupported):
        handler.search(q="x")
    with pytest.raises(Unsupported):
        handler.put(text="x")
    with pytest.raises(Unsupported):
        handler.edit(id=1)
    with pytest.raises(Unsupported):
        handler.delete(id=1)
    with pytest.raises(Unsupported):
        handler.tag(id=1, add=["x"])
    with pytest.raises(Unsupported):
        handler.link(id=1, target="x:y")


# ---------------------------------------------------------------------------
# Store-backed construction
# ---------------------------------------------------------------------------


def test_construct_without_store_raises_init_error() -> None:
    """``random`` is store-backed — a Hub without a store is an
    InitError (same pattern as OracleHandler / PaperHandler)."""
    with pytest.raises(InitError, match="random: store required"):
        RandomHandler(hub=Hub())


# ---------------------------------------------------------------------------
# Empty corpus
# ---------------------------------------------------------------------------


def test_empty_corpus_raises_notfound(handler: RandomHandler) -> None:
    """Freshly migrated DB has no blocks — random can't draw
    anything, raises NotFound with an "ingest first" hint."""
    with pytest.raises(NotFound, match="no embedded blocks") as exc:
        handler.get()
    assert exc.value.next is not None
    assert "ingest" in exc.value.next.lower()


# ---------------------------------------------------------------------------
# Happy path: slug kind (oracle)
# ---------------------------------------------------------------------------


def test_slug_kind_handle_and_drill_down(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """Slug-kind picks render ``kind:slug~pos`` handles and a
    drill-down hint pointing at ``get(kind=…, id='slug~pos')``."""
    _seed_oracle_with_embeddings(
        store, hub, "test-trad", ["the mountain teaches stillness"]
    )
    r = handler.get()
    # Canonical handle in the body, backtick-wrapped.
    assert "`oracle:test-trad~0`" in r.body
    # Next: trailer teaches the drill-down call.
    assert "Next:" in r.body
    assert "get(kind='oracle', id='test-trad~0')" in r.body
    # And the "pick again" self-reference.
    assert "get(kind='random')" in r.body


def test_slug_kind_preview_shows_first_line(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """The response carries a short preview of the block text so
    the caller sees what they got before deciding to drill in."""
    _seed_oracle_with_embeddings(
        store, hub, "test-trad", ["the mountain teaches stillness"]
    )
    r = handler.get()
    assert "the mountain teaches stillness" in r.body


def test_slug_kind_long_line_truncated_in_preview(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """A block whose first line is longer than the preview cap is
    clipped with an ellipsis — keeps the random response tight."""
    long_line = "a" * 500
    _seed_oracle_with_embeddings(store, hub, "test-long", [long_line])
    r = handler.get()
    # Preview clipped; the full content lives one get() away.
    assert "aaaa" in r.body
    assert "…" in r.body
    # The full 500-char run must not be in the body.
    assert long_line not in r.body


# ---------------------------------------------------------------------------
# Happy path: numeric kind (memory)
# ---------------------------------------------------------------------------


def test_numeric_kind_handle_is_ref_id(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """Numeric kinds (memory / todo / …) have no slug — the
    handle falls back to ``kind:<int>~0`` and the drill-down hint
    uses the int id without quotes."""
    ref_id = _seed_memory(store, hub, "remember this")
    r = handler.get()
    assert f"`memory:{ref_id}~0`" in r.body
    # Drill-down is ``id=<int>`` (no quotes — it's a literal int).
    assert f"get(kind='memory', id={ref_id})" in r.body


# ---------------------------------------------------------------------------
# Filtering behaviour
# ---------------------------------------------------------------------------


def test_deleted_refs_excluded(store: Store, hub: Hub, handler: RandomHandler) -> None:
    """Soft-deleted refs must not be pickable — the pool excludes
    ``deleted_at IS NOT NULL`` rows."""
    live_id = _seed_oracle_with_embeddings(store, hub, "live", ["live block"])
    tombstone_id = _seed_oracle_with_embeddings(store, hub, "dead", ["dead block"])
    # Soft-delete the second ref.
    store.soft_delete_ref(tombstone_id)

    # Draw 30 times — we must only ever see the live ref.
    seen_kinds_ids: set[str] = set()
    for _ in range(30):
        r = handler.get()
        m = re.search(r"`oracle:(\S+)~\d+`", r.body)
        assert m is not None
        seen_kinds_ids.add(m.group(1))
    assert seen_kinds_ids == {"live"}
    _ = live_id  # silence unused warning; the id is informational


def test_blocks_without_embeddings_excluded(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """Blocks with ``embedding IS NULL`` must not be pickable —
    same universe as semantic search. Re-ingests use this gate
    too."""
    ref = store.insert_ref(kind="oracle", slug="mixed", title="Mixed")
    embedder = hub.embedder
    assert embedder is not None
    store.insert_blocks(
        ref.id,
        [
            # pos=0 has no embedding → must be excluded.
            BlockInsert(
                pos=0,
                slug=None,
                text="no embedding here",
                token_count=3,
                embedding=None,
                density="sparse",
                meta={},
            ),
            # pos=1 has a real embedding → the only legal pick.
            BlockInsert(
                pos=1,
                slug=None,
                text="has embedding",
                token_count=2,
                embedding=embedder.embed_one("has embedding"),
                density="sparse",
                meta={},
            ),
        ],
    )
    # Draw many times — every hit must be pos=1.
    seen_positions: set[str] = set()
    for _ in range(30):
        r = handler.get()
        m = re.search(r"`oracle:mixed~(\d+)`", r.body)
        assert m is not None
        seen_positions.add(m.group(1))
    assert seen_positions == {"1"}


def test_distribution_covers_every_pickable_block(
    store: Store, hub: Hub, handler: RandomHandler
) -> None:
    """Over enough draws the picker must land on every legal
    block — confirms we aren't silently pinned to any single
    row. Three blocks, 90 draws; P(missing one) ≈ (2/3)^90
    ≈ 1e-16."""
    _seed_oracle_with_embeddings(
        store,
        hub,
        "distrib",
        ["alpha block", "beta block", "gamma block"],
    )
    seen: set[str] = set()
    for _ in range(90):
        r = handler.get()
        m = re.search(r"`oracle:distrib~(\d+)`", r.body)
        assert m is not None
        seen.add(m.group(1))
    assert seen == {"0", "1", "2"}


# ---------------------------------------------------------------------------
# Kwarg tolerance
# ---------------------------------------------------------------------------


def test_ignores_unknown_kwargs(store: Store, hub: Hub, handler: RandomHandler) -> None:
    """Agents that pass defaults through every call (``id=None``,
    ``view=None``, ``q=None``) must not trip over ``random``'s
    no-argument surface. Extra kwargs are silently ignored."""
    _seed_oracle_with_embeddings(store, hub, "kw", ["only block"])
    # Pass every conventional kwarg; none of them mean anything
    # to random, but none should raise either.
    r = handler.get(id=None, q=None, view=None, page_size=None)
    assert "`oracle:kw~0`" in r.body


# ---------------------------------------------------------------------------
# view='slug' — random short identifier minting
# ---------------------------------------------------------------------------


def test_slug_view_default_4_chars_crockford(handler: RandomHandler) -> None:
    """``view='slug'`` mints a 4-char Crockford-alphabet slug by default."""
    from precis.handlers.random import _CROCKFORD_ALPHABET

    r = handler.get(view="slug")
    slug = r.body.splitlines()[0]
    assert len(slug) == 4
    assert all(c in _CROCKFORD_ALPHABET for c in slug), (
        f"slug {slug!r} contains chars outside Crockford alphabet"
    )


def test_slug_view_no_corpus_dependency(handler: RandomHandler) -> None:
    """Slug minting is stateless — works even when corpus is empty
    (the default block-pick path raises NotFound on empty corpus)."""
    # No fixtures seeded; corpus is empty.
    r = handler.get(view="slug")
    assert len(r.body.splitlines()[0]) == 4  # still works


def test_slug_view_freshly_random(handler: RandomHandler) -> None:
    """Successive calls return different slugs (with overwhelming
    probability — 4 chars in a 32-char alphabet = 1 in ~1M collision)."""
    slugs = {handler.get(view="slug").body.splitlines()[0] for _ in range(20)}
    # 20 draws from 1M space — collision probability vanishingly small.
    assert len(slugs) == 20


def test_slug_view_custom_length(handler: RandomHandler) -> None:
    """``args={'len': N}`` sets the slug length."""
    r = handler.get(view="slug", args={"len": 8})
    assert len(r.body.splitlines()[0]) == 8

    r = handler.get(view="slug", args={"len": 1})
    assert len(r.body.splitlines()[0]) == 1

    r = handler.get(view="slug", args={"len": 64})
    assert len(r.body.splitlines()[0]) == 64


def test_slug_view_length_out_of_range(handler: RandomHandler) -> None:
    """Length must be in [1, 64]; outside that range raises BadInput."""
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="must be in"):
        handler.get(view="slug", args={"len": 0})
    with pytest.raises(BadInput, match="must be in"):
        handler.get(view="slug", args={"len": 65})
    with pytest.raises(BadInput, match="must be in"):
        handler.get(view="slug", args={"len": -1})


def test_slug_view_non_integer_length(handler: RandomHandler) -> None:
    """Non-integer length is BadInput, not a TypeError."""
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="must be an integer"):
        handler.get(view="slug", args={"len": "four"})


def test_slug_view_named_alphabets(handler: RandomHandler) -> None:
    """``alphabet='lower' / 'alnum' / 'crockford'`` select the named
    alphabets."""
    r = handler.get(view="slug", args={"len": 20, "alphabet": "lower"})
    slug = r.body.splitlines()[0]
    assert all(c in "abcdefghijklmnopqrstuvwxyz" for c in slug)

    r = handler.get(view="slug", args={"len": 20, "alphabet": "alnum"})
    slug = r.body.splitlines()[0]
    assert all((c.isalnum() and c.islower()) or c.isdigit() for c in slug)


def test_slug_view_custom_alphabet_literal(handler: RandomHandler) -> None:
    """A literal alphabet string is accepted as-is."""
    r = handler.get(view="slug", args={"len": 32, "alphabet": "ab"})
    slug = r.body.splitlines()[0]
    assert set(slug) <= {"a", "b"}


def test_slug_view_alphabet_too_short(handler: RandomHandler) -> None:
    """Custom alphabet must have ≥ 2 distinct characters; otherwise
    every slug is just the same char repeated, which defeats the
    purpose."""
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="at least 2 distinct"):
        handler.get(view="slug", args={"alphabet": "aaa"})  # 3 chars but 1 distinct
    with pytest.raises(BadInput, match="length ≥ 2"):
        handler.get(view="slug", args={"alphabet": "x"})  # too short


def test_slug_view_unknown_alphabet_name(handler: RandomHandler) -> None:
    """Unknown alphabet name with length < 2 → not treated as literal,
    rejected with the named-alphabet error."""
    from precis.errors import BadInput

    # Short non-string-like alphabet arg
    with pytest.raises(BadInput, match="alphabet"):
        handler.get(view="slug", args={"alphabet": 42})


def test_unknown_view_raises_bad_input(handler: RandomHandler) -> None:
    """An unknown view= isn't silently treated as the default — it's
    BadInput so the agent learns the supported set."""
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="unknown random view"):
        handler.get(view="bogus")
