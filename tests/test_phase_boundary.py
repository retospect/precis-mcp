"""Phase 3/4 boundary pin for the storage-v2 rewrite.

After Phase 1 (refs + identifiers) and Phase 2 (blocks → chunks),
six Store-mixin areas remain on stub status:

- **TagsMixin** (Phase 3): ``add_tag`` / ``remove_tag`` / ``tags_for``
  / ``has_tag`` / ``find_first_meta_for_open_tag``
- **BlocksMixin search half** (Phase 3): ``count_blocks_lexical`` /
  ``search_blocks_lexical`` / ``search_blocks_semantic`` /
  ``search_blocks_fused``
- **LinksMixin** (Phase 4): ``add_link`` / ``remove_link`` /
  ``links_for``
- **CacheMixin** (Phase 4): ``get_cache_entry`` /
  ``get_cache_entry_by_slug`` / ``update_cache_entry`` /
  ``put_cache_entry``

These tests pin the contract: each stubbed method MUST raise
``NotImplementedError`` whose message includes the right phase
marker (``phase 3 …`` or ``phase 4 …``) and the path to the plan
file. The tests serve two purposes:

1. **Boundary documentation** — anyone scanning the test suite
   sees exactly which surface remains stubbed and at what phase
   each piece lands.
2. **Phase-completion canary** — when Phase 3 lands real tag
   dispatch, every Phase-3 entry here flips from "raises
   NotImplementedError" to "doesn't"; the failure is the signal to
   delete the relevant assertions and rewrite the actual CRUD
   tests against the v2 schema.

The tests do NOT take the ``store`` fixture (no DB needed) — they
exercise the in-memory mixin classes directly. That keeps the
boundary check fast and runnable without postgres.
"""

from __future__ import annotations

import re

import pytest

from precis.store import Store
from precis.store.types import BlockInsert, Tag

_PHASE_3_PATTERN = re.compile(
    r"phase 3 \(.*\) not yet implemented; see .*lively-yawning-kahn\.md",
    re.IGNORECASE,
)
_PHASE_4_PATTERN = re.compile(
    r"phase 4 \(.*\) not yet implemented; see .*lively-yawning-kahn\.md",
    re.IGNORECASE,
)


@pytest.fixture
def stub_store() -> Store:
    """Build a Store without connecting to postgres.

    The phase-boundary methods raise before any pool access, so the
    pool can stay None. Anything that *does* try to touch the pool
    (a method that's NOT stubbed) will trip an AttributeError instead
    of NotImplementedError — and that's the signal we want.
    """
    store = Store.__new__(Store)
    return store


# ---------------------------------------------------------------------------
# Phase 3 — TagsMixin
# ---------------------------------------------------------------------------


class TestTagsBoundary:
    """Pin that every Phase 3 tags-mixin method raises with the
    right marker. Remove this class when Phase 3 ports the unified
    tag dispatch and replace with real add/remove/has_tag tests."""

    def test_add_tag_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.add_tag(1, Tag.flag("pinned"))

    def test_remove_tag_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.remove_tag(1, Tag.flag("pinned"))

    def test_tags_for_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.tags_for(1)

    def test_has_tag_raises_phase_3(self, stub_store: Store) -> None:
        """Pin the v2 unified API: ``has_tag(ref_id, namespace, value)``.

        Replaces v1 ``has_flag`` — see plan §"`has_tag` is the one
        true API; `has_flag` is removed".
        """
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.has_tag(1, "FLAG", "pinned")

    def test_has_flag_no_longer_exists(self, stub_store: Store) -> None:
        """v1 ``has_flag`` was removed outright. Phase 3 callers use
        ``has_tag(ref_id, 'FLAG', value)``. This test asserts the
        method is genuinely gone — not stubbed, not shimmed — so a
        future ``store.has_flag(...)`` call fails fast at attribute
        resolution rather than silently doing the wrong thing.
        """
        assert not hasattr(stub_store, "has_flag")

    def test_find_first_meta_for_open_tag_raises_phase_3(
        self, stub_store: Store
    ) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.find_first_meta_for_open_tag(
                kind="patent", tag="applicant:siemens-ag"
            )


# ---------------------------------------------------------------------------
# Phase 3 — BlocksMixin search methods
# ---------------------------------------------------------------------------


class TestBlockSearchBoundary:
    """Pin the four block-search methods. CRUD methods (insert/get/
    list/count/random/density+embedding) are real Phase 2 code and
    have their own tests."""

    def test_count_blocks_lexical_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.count_blocks_lexical(q="anything")

    def test_search_blocks_lexical_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.search_blocks_lexical(q="anything")

    def test_search_blocks_semantic_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.search_blocks_semantic(embedding=[0.0] * 1024)

    def test_search_blocks_fused_raises_phase_3(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_3_PATTERN):
            stub_store.search_blocks_fused(q="anything", embedding=[0.0] * 1024)


# ---------------------------------------------------------------------------
# Phase 4 — LinksMixin
# ---------------------------------------------------------------------------


class TestLinksBoundary:
    def test_add_link_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.add_link(src_ref_id=1, dst_ref_id=2)

    def test_remove_link_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.remove_link(src_ref_id=1, dst_ref_id=2)

    def test_links_for_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.links_for(1)


# ---------------------------------------------------------------------------
# Phase 4 — CacheMixin
# ---------------------------------------------------------------------------


class TestCacheBoundary:
    def test_get_cache_entry_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.get_cache_entry(provider="web", request_hash="abc")

    def test_get_cache_entry_by_slug_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.get_cache_entry_by_slug(kind="web", slug="example-com")

    def test_update_cache_entry_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.update_cache_entry(
                ref_id=1,
                title="x",
                body_blocks=[BlockInsert(pos=0, text="x")],
                ttl_seconds=3600,
            )

    def test_put_cache_entry_raises_phase_4(self, stub_store: Store) -> None:
        with pytest.raises(NotImplementedError, match=_PHASE_4_PATTERN):
            stub_store.put_cache_entry(
                kind="web",
                slug="example-com",
                title="x",
                body_blocks=[BlockInsert(pos=0, text="x")],
                provider="web",
                request_hash="abc",
                ttl_seconds=3600,
            )
