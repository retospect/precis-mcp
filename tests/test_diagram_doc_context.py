"""Layer-1/Layer-2 owning-document context for the draw-with-me loop
(``precis.diagram.doc_context``) — the piece that lets the drawer draw from the
draft instead of the figure title. Pure/injectable, so no DB or embedder here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from precis.diagram.doc_context import (
    build_document_context,
    document_context_for,
    entities_from_instruction,
    pick_paragraphs,
)
from precis.diagram.turn import build_prompt
from precis.figure.svg import SVG_LANG


@dataclass
class _Chunk:
    dc: str
    depth: int
    chunk_kind: str
    text: str
    chunk_id: int
    keywords: list[str] = field(default_factory=list)

    @property
    def handle(self) -> str:
        return self.dc


class _FakeStore:
    def __init__(
        self, chunks: list[_Chunk], owning: tuple[int, int] | None, title: str
    ) -> None:
        self._chunks = chunks
        self._owning = owning
        self._title = title

    def reading_order(self, ref_id: int, *, kind: str = "draft") -> list[_Chunk]:
        return self._chunks

    def figure_owning_draft(self, figure_ref_id: int) -> tuple[int, int] | None:
        return self._owning

    def get_ref(self, *, kind: str, id: int) -> SimpleNamespace:
        return SimpleNamespace(title=self._title)


_DECK_HOOK = [
    _Chunk("dc10", 0, "heading", "Detailed Description", 10),
    _Chunk(
        "dc11",
        1,
        "paragraph",
        "anchor formation widens below the neck to span decking gaps; flukes splay outward",
        11,
        keywords=["anchor formation", "flukes"],
    ),
    _Chunk(
        "dc12",
        1,
        "paragraph",
        "the neck narrows for gap entry between two boards",
        12,
        keywords=["neck"],
    ),
    _Chunk(
        "dc13", 1, "paragraph", "unrelated prose about corrosion-resistant coatings", 13
    ),
    _Chunk(
        "dc810", 0, "figure", "a perspective view of a deck hook, showing the neck", 810
    ),
]

_INSTRUCTION = (
    "a perspective view showing the planar body, the anchor formation, and the neck"
)


# ── entity extraction ────────────────────────────────────────────────


def test_entities_strip_boilerplate_keep_subjects() -> None:
    terms = entities_from_instruction(_INSTRUCTION)
    # drawing/view boilerplate is dropped …
    for junk in ("perspective", "view", "showing", "the", "and", "a"):
        assert junk not in terms
    # … the real subjects survive.
    for subject in ("planar", "body", "anchor", "formation", "neck"):
        assert subject in terms


def test_entities_keep_quoted_phrase_whole() -> None:
    terms = entities_from_instruction('draw the "anchor formation" over the neck')
    assert "anchor formation" in terms
    assert "neck" in terms


# ── paragraph selection ──────────────────────────────────────────────


def test_pick_matches_by_term_skips_anchor_and_headings() -> None:
    terms = entities_from_instruction(_INSTRUCTION)
    picks = pick_paragraphs(_DECK_HOOK, terms, anchor_chunk_id=810, limit=3)
    ids = [c.dc for c in picks]
    # anchor-formation paragraph (2 hits) ranks before the neck-only one (1 hit)
    assert ids[0] == "dc11"
    assert "dc12" in ids
    # the anchor figure chunk itself and the heading are never expansion targets
    assert "dc810" not in ids
    assert "dc10" not in ids
    # unrelated prose (0 hits) is excluded
    assert "dc13" not in ids


def test_pick_empty_without_terms() -> None:
    assert pick_paragraphs(_DECK_HOOK, [], anchor_chunk_id=810) == []


# ── full assembly ────────────────────────────────────────────────────


def test_build_document_context_outline_plus_fisheye() -> None:
    store = _FakeStore(_DECK_HOOK, owning=(5, 810), title="Deck Hook")
    out = build_document_context(
        store,
        draft_ref_id=5,
        anchor_chunk_id=810,
        instruction=_INSTRUCTION,
        expand=lambda _s, handle: f"FISHEYE[{handle}]",
    )
    assert "Deck Hook" in out
    assert "ground truth" in out  # the admonition
    # Layer 1: the whole collapsed outline, every block as a gloss row.
    assert "dc10  [heading]" in out
    assert "dc13  [paragraph]" in out
    # Layer 2: the instruction's paragraphs, fisheye-expanded in place.
    assert "FISHEYE[dc11]" in out
    assert "FISHEYE[dc12]" in out


def test_build_document_context_expand_failure_degrades_to_verbatim() -> None:
    def _boom(_s: object, _h: str) -> str:
        raise RuntimeError("no eye stack")

    store = _FakeStore(_DECK_HOOK, owning=(5, 810), title="Deck Hook")
    out = build_document_context(
        store,
        draft_ref_id=5,
        anchor_chunk_id=810,
        instruction=_INSTRUCTION,
        expand=_boom,
    )
    # A failed fisheye still surfaces the block's own text — thin beats nothing.
    assert "anchor formation widens below the neck" in out


def test_build_document_context_empty_draft() -> None:
    store = _FakeStore([], owning=(5, 810), title="Deck Hook")
    assert (
        build_document_context(
            store, draft_ref_id=5, anchor_chunk_id=810, instruction="x"
        )
        == ""
    )


# ── the single entry point + free-standing figure ────────────────────


def test_document_context_for_resolves_owning_draft() -> None:
    store = _FakeStore(_DECK_HOOK, owning=(5, 810), title="Deck Hook")
    out = document_context_for(store, figure_ref_id=99, instruction=_INSTRUCTION)
    assert "Deck Hook" in out and "dc11" in out


def test_document_context_for_free_standing_figure_is_empty() -> None:
    store = _FakeStore(_DECK_HOOK, owning=None, title="Deck Hook")
    assert document_context_for(store, figure_ref_id=99, instruction=_INSTRUCTION) == ""


def test_document_context_for_store_without_resolver_is_empty() -> None:
    bare = SimpleNamespace()  # no figure_owning_draft
    assert document_context_for(bare, figure_ref_id=99, instruction=_INSTRUCTION) == ""


# ── the prompt carries it ────────────────────────────────────────────


def test_build_prompt_carries_document_context() -> None:
    prompt = build_prompt(
        SVG_LANG,
        message="draw the neck",
        source="<svg viewBox='0 0 256 256'></svg>",
        vocab="",
        notes="",
        findings=[],
        bounds=(0.0, 0.0, 256.0, 256.0),
        document_context="## The document this figure illustrates — Deck Hook\nOUTLINE_HERE",
    )
    assert "The document this figure illustrates — Deck Hook" in prompt
    assert "OUTLINE_HERE" in prompt
