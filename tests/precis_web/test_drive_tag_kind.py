"""``k=tag`` in the drive lists tags — they are not refs (gr636493)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.drive import _tag_rows
from precis_web.routes.items import _PAGE_SIZE


class _TagStore:
    """Serves a fixed roster, honouring the LIMIT-style ``page_size`` /
    ``limit`` the way ``Store.list_all_tags`` / ``suggest_tags`` do. The
    route must always ask with ``page=1`` and page by growing the limit,
    then slice — never by passing ``page`` with an over-fetched size."""

    ROSTER: list[tuple[str, str, int]] = [
        (f"NS{i}", f"v{i}", i) for i in range(2 * _PAGE_SIZE + 1)
    ]

    def list_all_tags(self, *, page: int = 1, page_size: int = 50):
        assert page == 1
        return self.ROSTER[:page_size]

    def suggest_tags(self, q: str, *, limit: int = 10):
        assert q
        return self.ROSTER[:limit]


class _SmallStore:
    def list_all_tags(self, *, page: int = 1, page_size: int = 50):
        assert page == 1
        return [("STATUS", "done", 3), ("OPEN", "carbon", 1)][:page_size]

    def suggest_tags(self, q: str, *, limit: int = 10):
        assert q
        return [("OPEN", "carbon", 1)][:limit]


def test_tag_rows_browse_lists_tags_linking_to_filtered_drive() -> None:
    rows, has_next = _tag_rows(_SmallStore(), "", 1, ["paper"])
    assert not has_next
    assert [r["title"] for r in rows] == ["STATUS:done", "OPEN:carbon"]
    assert rows[0]["kind"] == "tag"
    assert rows[0]["id"] is None
    assert rows[0]["preview"] == "3 refs"
    # The link pins the ref kinds: a bare ?tag= link would resolve a
    # saved k=tag cookie and list tags again instead of matching refs
    # (td650230 finding 2).
    assert rows[0]["open_url"] == "/drive?tag=STATUS%3Adone&k=paper"
    assert rows[1]["preview"] == "1 ref"
    assert rows[1]["open_url"] == "/drive?tag=carbon&k=paper"


def test_tag_rows_link_pins_every_ref_kind_in_order() -> None:
    rows, _ = _tag_rows(_SmallStore(), "", 1, ["paper", "draft"])
    assert rows[0]["open_url"] == "/drive?tag=STATUS%3Adone&k=paper&k=draft"


def test_tag_rows_pages_do_not_skip_or_repeat() -> None:
    # td650230 finding 1: page_size=N+1 with page-based offsets stepped
    # the offset by the over-fetched size, skipping one tag per boundary.
    store = _TagStore()
    page1, next1 = _tag_rows(store, "", 1, ["paper"])
    page2, next2 = _tag_rows(store, "", 2, ["paper"])
    page3, next3 = _tag_rows(store, "", 3, ["paper"])
    assert len(page1) == _PAGE_SIZE and next1
    assert len(page2) == _PAGE_SIZE and next2
    assert len(page3) == 1 and not next3
    seen = [r["title"] for r in [*page1, *page2, *page3]]
    assert seen == [f"NS{i}:v{i}" for i in range(2 * _PAGE_SIZE + 1)]


def test_tag_rows_query_pages_do_not_skip_or_repeat() -> None:
    store = _TagStore()
    page1, next1 = _tag_rows(store, "v", 1, ["paper"])
    page2, next2 = _tag_rows(store, "v", 2, ["paper"])
    page3, next3 = _tag_rows(store, "v", 3, ["paper"])
    assert len(page1) == _PAGE_SIZE and next1
    assert len(page2) == _PAGE_SIZE and next2
    assert len(page3) == 1 and not next3
    seen = [r["title"] for r in [*page1, *page2, *page3]]
    assert seen == [f"NS{i}:v{i}" for i in range(2 * _PAGE_SIZE + 1)]


def test_tag_rows_query_uses_substring_match() -> None:
    rows, _ = _tag_rows(_SmallStore(), "carb", 1, ["paper"])
    assert [r["title"] for r in rows] == ["OPEN:carbon"]
