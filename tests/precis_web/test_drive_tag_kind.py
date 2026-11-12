"""``k=tag`` in the drive lists tags — they are not refs (gr636493)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.drive import _tag_rows


class _TagStore:
    def list_all_tags(self, *, page: int = 1, page_size: int = 50):
        assert page == 1
        return [("STATUS", "done", 3), ("OPEN", "carbon", 1)][:page_size]

    def suggest_tags(self, q: str, *, limit: int = 10):
        return [("OPEN", "carbon", 1)]


def test_tag_rows_browse_lists_tags_linking_to_filtered_drive() -> None:
    rows, has_next = _tag_rows(_TagStore(), "", 1)
    assert not has_next
    assert [r["title"] for r in rows] == ["STATUS:done", "OPEN:carbon"]
    assert rows[0]["kind"] == "tag"
    assert rows[0]["id"] is None
    assert rows[0]["preview"] == "3 refs"
    assert rows[0]["open_url"] == "/drive?tag=STATUS%3Adone"
    assert rows[1]["preview"] == "1 ref"
    assert rows[1]["open_url"] == "/drive?tag=carbon"


def test_tag_rows_query_uses_substring_match() -> None:
    rows, _ = _tag_rows(_TagStore(), "carb", 1)
    assert [r["title"] for r in rows] == ["OPEN:carbon"]
