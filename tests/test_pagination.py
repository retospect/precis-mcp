"""Tests for the MCP-frame pagination cache + body chunking.

Covers the boundary-respecting split (section → paragraph → hard),
the TTL pruning, cursor eviction under load, and the recursive
cursor path when a tail is itself oversized.

End-to-end tests through ``dispatch_with_status`` live alongside
the runtime suite; those need a live runtime fixture. The unit
tests here exercise the pagination module directly.
"""

from __future__ import annotations

import pytest

from precis._pagination import (
    _FOOTER_RESERVE_BYTES,
    DEFAULT_MAX_BODY_BYTES,
    PaginationCache,
)

#: Caps that leave a fixed head budget after the footer reserve —
#: enough for one section but not two, so a multi-section body
#: splits at an H2 boundary. Sized off the reserve so they survive
#: footer-wording changes rather than hard-coding a number that
#: assumes the old terse footer. ``_ONE_SECTION_CAP`` fits ~272-byte
#: sections; ``_WIDE_SECTION_CAP`` fits ~407-byte sections. (The
#: reserve is a few hundred bytes, so test sections must be larger
#: than it to split at a boundary rather than a hard byte cut.)
_ONE_SECTION_CAP = str(_FOOTER_RESERVE_BYTES + 340)
_WIDE_SECTION_CAP = str(_FOOTER_RESERVE_BYTES + 410)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pagination reads env at call time. Wipe the knobs so test
    cases get the documented defaults unless they override."""
    monkeypatch.delenv("PRECIS_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("PRECIS_PAGINATION_TTL_S", raising=False)


# ── Sized bodies pass through unchanged ────────────────────────────


class TestPassthrough:
    def test_small_body_unchanged(self) -> None:
        cache = PaginationCache()
        body = "## hello\n\nfits.\n"
        out, cursor = cache.split(body)
        assert out == body
        assert cursor is None
        assert len(cache) == 0

    def test_empty_body_unchanged(self) -> None:
        cache = PaginationCache()
        out, cursor = cache.split("")
        assert out == ""
        assert cursor is None

    def test_at_cap_boundary_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A body sized exactly at the cap is not chunked."""
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "100")
        cache = PaginationCache()
        body = "x" * 100  # 100 bytes ASCII, at the cap
        out, cursor = cache.split(body)
        assert out == body
        assert cursor is None


# ── Oversized bodies split on H2 sections ──────────────────────────


class TestSectionSplit:
    def test_splits_on_h2_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Cap chosen so the head holds at most one full section
        # after the footer reserve, so section three's content
        # lives in the tail.
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", _ONE_SECTION_CAP)
        cache = PaginationCache()
        body = (
            "# heading\n"
            "intro paragraph\n"
            "## section one\n" + ("a" * 260) + "\n"
            "## section two\n" + ("b" * 260) + "\n"
            "## section three\n" + ("c" * 260) + "\n"
        )
        head, cursor = cache.split(body)
        assert cursor is not None
        # The head must include at least section one and end with
        # the ``Next:`` footer. Section three's content lives in
        # the tail.
        assert "section one" in head
        assert ("c" * 50) not in head
        assert f"more(cursor='{cursor}')" in head

    def test_tail_starts_with_next_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", _ONE_SECTION_CAP)
        cache = PaginationCache()
        body = (
            "# heading\n"
            "intro paragraph\n"
            "## section one\n" + ("a" * 260) + "\n"
            "## section two\n" + ("b" * 260) + "\n"
            "## section three\n" + ("c" * 260) + "\n"
        )
        _head, cursor = cache.split(body)
        assert cursor is not None
        tail = cache.pop(cursor)
        assert tail is not None
        # The tail must start with an H2 header so it stitches
        # cleanly with the previous chunk.
        assert tail.startswith("## ")


# ── The footer is loud enough to not be mistaken for a full result ──


class TestFooter:
    def test_footer_states_incomplete_with_size_and_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunked head must carry a loud, actionable footer: it
        says the body is incomplete, roughly how much remains, and
        the exact ``more(cursor=...)`` call to continue. A terse hint
        was being read as trailing noise and consumers acted on a
        partial body."""
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", _ONE_SECTION_CAP)
        cache = PaginationCache()
        body = (
            "# heading\n"
            "## section one\n" + ("a" * 260) + "\n"
            "## section two\n" + ("b" * 260) + "\n"
            "## section three\n" + ("c" * 260) + "\n"
        )
        head, cursor = cache.split(body)
        assert cursor is not None
        # Incompleteness is stated, not merely implied.
        assert "NOT the complete result" in head
        # A remaining-size readout is present (bytes rendered as B/KB/MB).
        assert "more follows" in head
        assert any(unit in head for unit in (" B", " KB", " MB"))
        # The exact continuation call — kept stable for the more() tool.
        assert f"more(cursor='{cursor}')" in head
        # head + footer stays under the frame cap.
        assert len(head.encode("utf-8")) <= int(_ONE_SECTION_CAP)


# ── Pop semantics ──────────────────────────────────────────────────


class TestPop:
    def test_pop_returns_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", _WIDE_SECTION_CAP)
        cache = PaginationCache()
        body = "## one\n" + ("a" * 400) + "\n## two\n" + ("b" * 400) + "\n"
        head, cursor = cache.split(body)
        assert cursor is not None
        assert "## two" not in head

        tail = cache.pop(cursor)
        assert tail is not None
        assert "## two" in tail

    def test_pop_unknown_cursor_returns_none(self) -> None:
        cache = PaginationCache()
        assert cache.pop("definitely-not-a-real-cursor") is None

    def test_pop_is_single_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "500")
        cache = PaginationCache()
        body = "## one\n" + ("a" * 350) + "\n## two\n" + ("b" * 350) + "\n"
        _head, cursor = cache.split(body)
        assert cursor is not None

        first = cache.pop(cursor)
        second = cache.pop(cursor)
        assert first is not None
        assert second is None, "cursor must not be reusable"


# ── TTL pruning ────────────────────────────────────────────────────


class TestTTL:
    def test_expired_cursor_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "150")
        # Negative TTL: every put expires immediately.
        monkeypatch.setenv("PRECIS_PAGINATION_TTL_S", "-1")
        cache = PaginationCache()
        body = "## one\n" + ("a" * 100) + "\n## two\n" + ("b" * 100) + "\n"
        _head, cursor = cache.split(body)
        # Default kicks in for negative; assert pop still works
        # (negative TTL falls through to the default), so this
        # case actually tests the env-fallback path. A direct
        # expiry check uses monkey-patching of monotonic.
        assert cursor is not None
        assert cache.pop(cursor) is not None

    def test_explicit_expiry_drop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock ``_now`` to fast-forward past TTL and verify the
        cached entry is dropped on prune."""
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "150")
        monkeypatch.setenv("PRECIS_PAGINATION_TTL_S", "1")
        cache = PaginationCache()
        body = "## a\n" + ("x" * 100) + "\n## b\n" + ("y" * 100) + "\n"
        _head, cursor = cache.split(body)

        # Fast-forward the cache's clock past the TTL.
        original_now = cache._now
        cache._now = lambda: (
            original_now() + 10_000.0
        )  # 10ks later  # type: ignore[method-assign]
        assert cursor is not None
        assert cache.pop(cursor) is None


# ── Cursor-count eviction ──────────────────────────────────────────


class TestEviction:
    def test_oldest_cursor_evicted_when_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "150")
        cache = PaginationCache(max_cursors=3)
        body = "## a\n" + ("x" * 100) + "\n## b\n" + ("y" * 100) + "\n"

        cursors = []
        for _ in range(5):
            _head, cursor = cache.split(body)
            assert cursor is not None
            cursors.append(cursor)

        # Only the most recent 3 should still be retrievable; the
        # first two were evicted.
        misses = sum(1 for c in cursors if cache.pop(c) is None)
        assert misses == 2


# ── Defaults & env handling ────────────────────────────────────────


class TestDefaults:
    def test_default_max_body_bytes(self) -> None:
        assert DEFAULT_MAX_BODY_BYTES == 24576

    def test_garbage_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A nonsense env var value falls back to the default."""
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "not-a-number")
        cache = PaginationCache()
        # No exception; body smaller than default cap passes through.
        out, cursor = cache.split("ok")
        assert out == "ok"
        assert cursor is None
