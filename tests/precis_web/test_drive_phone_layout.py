"""gr543586: /drive phone layout — blurb, active chips and folders fold.

The three folds ship as ``<details>`` elements closed in markup (the
phone default); Alpine's ``wideScreen`` provider holds them open at md+
so the desktop page is unchanged. These tests pin the markup half: the
blurb and the folders aside sit inside closed details, and the sidebar
keeps its tour anchor.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

pytest.importorskip("fastapi")

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


class _Folds(HTMLParser):
    """Ancestor tracking for the blurb text and the folders aside."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.blurb_folded: bool | None = None
        self.aside_attrs: dict[str, str | None] | None = None
        self.aside_folded: bool | None = None
        self.active_chips_folded: bool | None = None

    def _folded(self) -> bool:
        return any(t == "details" and "open" not in a for t, a in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "aside" and a.get("data-tour") == "drive-folders":
            self.aside_attrs = a
            self.aside_folded = self._folded()
        if a.get("data-tour") == "drive-tags":
            self.active_chips_folded = self._folded()
        if tag not in _VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if tag == "aside" and a.get("data-tour") == "drive-folders":
            self.aside_attrs = a
            self.aside_folded = self._folded()

    def handle_data(self, data):
        if "Discover newly created work" in data:
            self.blurb_folded = self._folded()

    def handle_endtag(self, tag):
        while self.stack and self.stack[-1][0] != tag:
            self.stack.pop()
        if self.stack:
            self.stack.pop()


def test_drive_phone_folds(client):
    response = client.get("/drive")
    assert response.status_code == 200
    page = _Folds()
    page.feed(response.text)
    # The explanatory text under the heading sits in a closed <details>.
    assert page.blurb_folded is True
    # The folders sidebar is wrapped in a <details> without `open` and
    # keeps its tour anchor.
    assert page.aside_attrs is not None
    assert page.aside_attrs["data-tour"] == "drive-folders"
    assert page.aside_folded is True
    # The active kind/tag chips fold into a disclosure on small screens.
    assert page.active_chips_folded is True
