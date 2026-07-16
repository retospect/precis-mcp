"""docx patent-mode export: prior art in-text, no References section
(slice 6 docx mirror). See ``docs/design/patent-authoring-loop.md``.

Uses a fake paragraph (records ``add_run`` text) + fake store so we test
the citation branching without building a real Document.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.export.docx as dx


class _Font:
    superscript = False


class _Run:
    def __init__(self) -> None:
        self.font = _Font()


class _Para:
    def __init__(self) -> None:
        self.runs: list[str] = []

    def add_run(self, text: str = "") -> _Run:
        self.runs.append(text)
        return _Run()


class _Ref:
    def __init__(
        self,
        meta: dict[str, Any] | None = None,
        title: str | None = None,
        slug: str | None = None,
    ) -> None:
        self.meta = meta or {}
        self.title = title
        self.slug = slug


class _Store:
    def __init__(self, refs: dict[tuple[str, str], _Ref]) -> None:
        self._refs = refs

    def get_ref(self, *, kind: str, id: str) -> _Ref | None:
        return self._refs.get((kind, id))


def _ctx(store: _Store) -> dx._Ctx:
    return dx._Ctx(store=store, known_handles=set(), doc_type="patent")


def test_paper_cite_renders_inline_not_numbered() -> None:
    store = _Store(
        {
            ("paper", "smith2015"): _Ref(
                meta={"authors": [{"name": "Smith, Jane"}], "publication_date": "2015"}
            )
        }
    )
    ctx = _ctx(store)
    para = _Para()
    dx._cite("smith2015", ctx, para)
    assert para.runs == ["(Smith et al., 2015)"]
    assert ctx.cited == []  # never registered → no References entry


def test_patent_display_link_is_wysiwyg() -> None:
    ctx = _ctx(_Store({}))
    para = _Para()
    dx._inline_source_cite("pt5", "patent", "U.S. Patent No. 2,943,737", ctx, para)
    assert para.runs == ["U.S. Patent No. 2,943,737"]


def test_patent_handle_formats_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(
        {
            ("patent", "us6368648b1"): _Ref(
                meta={"country": "us", "doc_number": "6368648", "kind_code": "B1"}
            )
        }
    )
    ctx = _ctx(store)
    monkeypatch.setattr(dx, "_handle_cite_key", lambda tgt, ctx: ("us6368648b1", None))
    para = _Para()
    dx._inline_source_cite("pt5", "patent", None, ctx, para)
    assert para.runs == ["U.S. Patent No. 6,368,648"]


def test_default_mode_still_numbers_citations() -> None:
    store = _Store({})
    ctx = dx._Ctx(store=store, known_handles=set())  # doc_type="" (default)
    assert ctx.patent_mode is False
    para = _Para()
    dx._cite("smith2015", ctx, para)
    assert para.runs == ["[1]"]  # numbered marker, registered for References
    assert ctx.cited == ["smith2015"]
