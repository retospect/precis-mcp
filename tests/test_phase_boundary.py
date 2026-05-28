"""Phase 4 boundary pin for the storage-v2 rewrite + permanent
API-decision guards.

Phases 1 (refs + identifiers), 2 (blocks → chunks), and 3 (tags +
block search) have all landed. The remaining stubbed surface is
Phase 4: link CRUD + cache CRUD. Each method here pins that those
stubs raise the right ``NotImplementedError`` with the phase marker
so a future Phase 4 port flips the assertions from raising-to-not-
raising, and the failure signals "delete this canary, write the
real CRUD test".

Permanent guards (NOT canaries — these stay even after Phase 4):

- ``test_has_flag_no_longer_exists`` — pins the ADR-equivalent
  decision that v1 ``has_flag`` is removed outright. Future refactors
  that accidentally re-add a method by that name fail this test.
"""

from __future__ import annotations

import re

import pytest

from precis.store import Store
from precis.store.types import BlockInsert

_PHASE_4_PATTERN = re.compile(
    r"phase 4 \(.*\) not yet implemented; see .*lively-yawning-kahn\.md",
    re.IGNORECASE,
)


@pytest.fixture
def stub_store() -> Store:
    """Build a Store without connecting to postgres.

    Phase 4 boundary methods raise before any pool access, so the
    pool can stay None. The permanent guard test doesn't touch the
    pool either.
    """
    store = Store.__new__(Store)
    return store


# ---------------------------------------------------------------------------
# Permanent guards (survive Phase 4)
# ---------------------------------------------------------------------------


class TestApiDecisions:
    """API-decision regression guards — keep these in place forever."""

    def test_has_flag_no_longer_exists(self, stub_store: Store) -> None:
        """v1 ``has_flag`` was removed outright. Phase 3 unified API
        is ``has_tag(ref_id, namespace, value)``. A future
        ``store.has_flag(...)`` must fail fast at attribute
        resolution rather than silently doing the wrong thing. If
        anyone tries to "helpfully" re-add the method, this test
        catches it on the next CI run.
        """
        assert not hasattr(stub_store, "has_flag")


# ---------------------------------------------------------------------------
# Phase 4 — LinksMixin (delete when Phase 4 ports the real CRUD)
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
# Phase 4 — CacheMixin (delete when Phase 4 ports the real CRUD)
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
