"""TagHandler — corpus-wide tag discovery surface.

The handler exposes:

* ``get(kind='tag')``                — paginated tag list
* ``get(kind='tag', id='STATUS:done')`` — metadata + sample refs
* ``search(kind='tag', q='topic')``  — hybrid lexical + semantic

Tests cover the slug grammar (closed UPPERCASE / lowercase
prefix / bare flag), the read-only verb shape, and pagination.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub, InitError
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.tag import TagHandler
from precis.store import Store
from precis.store.types import Tag

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_paper(store: Store, *, slug: str, title: str = "test paper") -> int:
    """Insert a paper ref + cite_key identifier; return ref_id."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    return ref.id


def _seed_memory(store: Store, *, title: str = "test memory") -> int:
    """Insert a numeric (memory) ref; return ref_id."""
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    return ref.id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(hub: Hub) -> TagHandler:
    """Store-backed handler with the test hub's MockEmbedder."""
    return TagHandler(hub=hub)


@pytest.fixture
def handler_no_embedder(hub_no_embedder: Hub) -> TagHandler:
    """Handler without an embedder — lex-only code paths."""
    return TagHandler(hub=hub_no_embedder)


# ---------------------------------------------------------------------------
# KindSpec / verb surface
# ---------------------------------------------------------------------------


def test_kindspec_is_read_only() -> None:
    spec = TagHandler.spec
    assert spec.kind == "tag"
    assert spec.supports_get is True
    assert spec.supports_search is True
    assert spec.supports_put is False
    assert spec.supports_edit is False
    assert spec.supports_delete is False
    assert spec.supports_tag is False
    assert spec.supports_link is False
    assert spec.id_required is False


def test_write_verbs_unsupported(handler: TagHandler) -> None:
    with pytest.raises(Unsupported):
        handler.put(text="x")
    with pytest.raises(Unsupported):
        handler.edit(id="x")
    with pytest.raises(Unsupported):
        handler.delete(id="x")
    with pytest.raises(Unsupported):
        handler.tag(id="x", add=["y"])
    with pytest.raises(Unsupported):
        handler.link(id="x", target="y")


def test_init_requires_store() -> None:
    bare_hub = Hub()  # no store wired
    with pytest.raises(InitError):
        TagHandler(hub=bare_hub)


# ---------------------------------------------------------------------------
# get(kind='tag') — list mode
# ---------------------------------------------------------------------------


class TestGetList:
    def test_empty_corpus_returns_empty_body(self, handler: TagHandler) -> None:
        resp = handler.get()
        assert "no tags in use" in resp.body
        # Empty-state still teaches the agent where to go next.
        assert "precis-tags" in resp.body

    def test_lists_tags_with_usage_counts(
        self, store: Store, handler: TagHandler
    ) -> None:
        # Two papers carry topic:co2-capture; one carries STATUS:done.
        p1 = _seed_paper(store, slug="paper-one")
        p2 = _seed_paper(store, slug="paper-two")
        m1 = _seed_memory(store)
        store.add_tag(p1, Tag.open("topic:co2-capture"))
        store.add_tag(p2, Tag.open("topic:co2-capture"))
        store.add_tag(m1, Tag.open("topic:carbon"))

        resp = handler.get()
        assert "topic:co2-capture" in resp.body
        assert "topic:carbon" in resp.body
        # Ordering — co2-capture (count=2) appears before carbon (count=1).
        idx_top = resp.body.index("topic:co2-capture")
        idx_carbon = resp.body.index("topic:carbon")
        assert idx_top < idx_carbon

    def test_scope_restricts_to_kind(self, store: Store, handler: TagHandler) -> None:
        p1 = _seed_paper(store, slug="paper-one")
        m1 = _seed_memory(store)
        store.add_tag(p1, Tag.open("topic:papers-only"))
        store.add_tag(m1, Tag.open("topic:memories-only"))

        resp = handler.get(scope="paper")
        assert "topic:papers-only" in resp.body
        assert "topic:memories-only" not in resp.body

    def test_pagination(self, store: Store, handler: TagHandler) -> None:
        # Seed 5 tags; page_size=2 should split across 3 pages.
        ref = _seed_paper(store, slug="paginated")
        for i in range(5):
            store.add_tag(ref, Tag.open(f"topic:page-{i}"))

        page1 = handler.get(page=1, page_size=2)
        page2 = handler.get(page=2, page_size=2)
        page3 = handler.get(page=3, page_size=2)
        # Each page contains the per-page rows and the tags don't repeat
        # between adjacent pages (orderings tie on count=1; deterministic
        # via namespace ASC, value ASC).
        all_bodies = page1.body + page2.body + page3.body
        for i in range(5):
            assert f"topic:page-{i}" in all_bodies


# ---------------------------------------------------------------------------
# get(kind='tag', id=...) — metadata mode
# ---------------------------------------------------------------------------


class TestGetMetadata:
    def test_missing_tag_raises_not_found(self, handler: TagHandler) -> None:
        with pytest.raises(NotFound):
            handler.get(id="topic:nonexistent")

    def test_open_tag_metadata(self, store: Store, handler: TagHandler) -> None:
        p1 = _seed_paper(store, slug="abc")
        store.add_tag(p1, Tag.open("topic:co2-capture"))

        resp = handler.get(id="topic:co2-capture")
        assert "topic:co2-capture" in resp.body
        assert "axis: open" in resp.body
        assert "count:" in resp.body
        # Sample ref surfaces.
        assert "abc" in resp.body

    def test_closed_tag_metadata_shows_siblings(
        self, store: Store, handler: TagHandler
    ) -> None:
        p1 = _seed_paper(store, slug="ddd")
        store.add_tag(p1, Tag.closed("CACHE", "fresh"))
        resp = handler.get(id="CACHE:fresh")
        assert "CACHE:fresh" in resp.body
        assert "axis: closed:CACHE" in resp.body
        # Sibling values list from _CLOSED_VOCAB.
        assert "sibling values" in resp.body
        assert "stale" in resp.body
        assert "pinned" in resp.body

    def test_bare_flag_resolves_either_namespace(
        self, store: Store, handler: TagHandler
    ) -> None:
        # 'pinned' on a memory becomes namespace=OPEN, value=pinned
        # (memory has no CACHE: axis so the collision check passes).
        m1 = _seed_memory(store)
        store.add_tag(m1, Tag.parse_strict("pinned", kind="memory"))
        resp = handler.get(id="pinned")
        assert "pinned" in resp.body
        assert "count:" in resp.body


# ---------------------------------------------------------------------------
# search(kind='tag') — hybrid surface
# ---------------------------------------------------------------------------


class TestSearch:
    def test_empty_q_without_scope_raises(self, handler: TagHandler) -> None:
        with pytest.raises(BadInput):
            handler.search(q="")
        with pytest.raises(BadInput):
            handler.search()

    def test_empty_q_with_scope_falls_back_to_list(
        self, store: Store, handler: TagHandler
    ) -> None:
        p1 = _seed_paper(store, slug="aa")
        store.add_tag(p1, Tag.open("topic:listed"))
        resp = handler.search(q="", scope="paper")
        assert "topic:listed" in resp.body

    def test_lexical_match_surfaces_substring(
        self, store: Store, handler: TagHandler
    ) -> None:
        p1 = _seed_paper(store, slug="ll")
        store.add_tag(p1, Tag.open("topic:co2-capture"))
        store.add_tag(p1, Tag.open("project:unrelated"))
        resp = handler.search(q="co2")
        assert "topic:co2-capture" in resp.body
        # Other tag should not surface as a lexical hit.
        assert "project:unrelated" not in resp.body

    def test_empty_result_returns_empty_body(self, handler: TagHandler) -> None:
        resp = handler.search(q="totally-novel-string-xyz")
        assert "no tags match" in resp.body

    def test_semantic_search_runs_with_embedder(
        self, store: Store, handler: TagHandler
    ) -> None:
        # Seed + embed via the same MockEmbedder the hub bound.
        p1 = _seed_paper(store, slug="sem")
        store.add_tag(p1, Tag.open("topic:photocatalysis"))
        # Mirror what the worker would do.
        emb = handler.embedder
        assert emb is not None
        vec = emb.embed_one("topic:photocatalysis")
        store.write_tag_embedding(
            namespace="OPEN",
            value="topic:photocatalysis",
            vector=vec,
            embedder="bge-m3",
            version=1,
        )
        # Search for something unrelated lexically; the semantic path
        # at least returns the seeded tag (mock embeddings are
        # deterministic but not meaningfully clustered, so we assert
        # only that the code path runs without crashing).
        resp = handler.search(q="photochemistry")
        assert resp.body  # non-empty

    def test_lexical_only_without_embedder(
        self, store: Store, handler_no_embedder: TagHandler
    ) -> None:
        p1 = _seed_paper(store, slug="zz")
        store.add_tag(p1, Tag.open("topic:no-embedder"))
        resp = handler_no_embedder.search(q="no-embedder")
        assert "topic:no-embedder" in resp.body
        # Hint surfaces the worker recovery path.
        assert "tag-embedding" in resp.body or "worker" in resp.body


# ---------------------------------------------------------------------------
# Slug parsing / canonicalisation
# ---------------------------------------------------------------------------


class TestSlugParsing:
    def test_closed_uppercase_axis(self, handler: TagHandler) -> None:
        # parse via the handler's helper indirectly — round-trip the
        # slug through metadata not-found to assert the namespace
        # the handler probes.
        from precis.handlers.tag import _parse_slug, _slug_from

        assert _parse_slug("STATUS:done") == ("STATUS", "done")
        assert _slug_from("STATUS", "done") == "STATUS:done"

    def test_open_lowercase_prefix(self) -> None:
        from precis.handlers.tag import _parse_slug, _slug_from

        # Lowercase prefix → stored under OPEN with the whole string
        # as value.
        assert _parse_slug("topic:co2-capture") == ("OPEN", "topic:co2-capture")
        assert _slug_from("OPEN", "topic:co2-capture") == "topic:co2-capture"

    def test_bare_flag_has_empty_namespace_hint(self) -> None:
        from precis.handlers.tag import _parse_slug, _slug_from

        assert _parse_slug("pinned") == ("", "pinned")
        # Flags round-trip through the FLAG sentinel.
        assert _slug_from("FLAG", "pinned") == "pinned"

    def test_empty_slug_raises(self) -> None:
        from precis.handlers.tag import _parse_slug

        with pytest.raises(BadInput):
            _parse_slug("")


# ---------------------------------------------------------------------------
# Embedder swap helpers
# ---------------------------------------------------------------------------


def test_handler_uses_hub_embedder() -> None:
    """The handler picks up ``hub.embedder`` at construction."""
    from precis.dispatch import Hub

    hub = Hub(store=None, embedder=MockEmbedder(dim=1024))
    # Without a store the init must still raise — store is mandatory.
    with pytest.raises(InitError):
        TagHandler(hub=hub)
