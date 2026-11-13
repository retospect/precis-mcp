"""Search-row keyword fallback drops markup/citation residue (gr664574)."""

from __future__ import annotations

from precis.handlers._paper_text import _chunk_keywords_or_caption


def test_table_caption_has_no_pipe_or_comma() -> None:
    text = "| model | r, adj | ρ |\n|---|---|---|\n| a | 1 | 2 |"
    out = _chunk_keywords_or_caption(text)
    assert out == "[table] model / r; adj / ρ"
    assert "|" not in out and "," not in out


def test_table_caption_strips_markup_from_cells() -> None:
    """gr664574 review: a header cell like ``vinyl<br/>alcohol`` must
    not echo literal markup into ``chunk_keywords``."""
    text = "| vinyl<br/>alcohol | r | ρ |\n|---|---|---|\n| a | 1 | 2 |"
    out = _chunk_keywords_or_caption(text)
    assert out == "[table] vinyl alcohol / r / ρ"
    assert "<" not in out and ">" not in out


def test_table_caption_drops_residue_only_cells() -> None:
    """A header cell that is only an image marker, entity or URL
    cleans to empty and leaves no slot."""
    text = (
        "| model | ![](_page_1_Figure_1.jpeg) | https://doi.org/x | r |\n"
        "|---|---|---|---|\n"
        "| a | b | c | 1 |"
    )
    out = _chunk_keywords_or_caption(text)
    assert out == "[table] model / r"
    assert "http" not in out and "jpeg" not in out


def test_prose_fallback_strips_tags_and_citations() -> None:
    text = (
        "The vinyl<br/>alcohol membranes of the study. "
        "Rahman et al and the polyethylene crystallinity of the film. " * 3
    )
    out = _chunk_keywords_or_caption(text)
    assert out
    for kw in out.split(", "):
        assert "br" not in kw.split()
        assert "et al" not in kw
