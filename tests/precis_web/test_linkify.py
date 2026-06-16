"""Tests for the inline ``kind:ref`` linkifier filter.

Pure Python — no Postgres, no FastAPI. The HTML routes that consume the
filter's output (``/preview/...``, ``/r/...``) are exercised in
``test_routes.py`` with the FakeStore fixture.
"""

from __future__ import annotations

from precis_web.linkify import linkify_refs


def test_no_refs_passes_through_unchanged() -> None:
    assert str(linkify_refs("just plain prose here")) == "just plain prose here"


def test_simple_paper_ref_becomes_anchor() -> None:
    out = str(linkify_refs("See paper:acheson26 for details."))
    assert 'href="/r/paper/acheson26"' in out
    assert "paper:acheson26" in out  # display preserved
    assert 'hx-get="/preview/paper/acheson26"' in out


def test_numeric_ref_strips_hash_in_url() -> None:
    out = str(linkify_refs("memory:#6184 covers this."))
    assert 'href="/r/memory/6184"' in out
    # Display preserves the user's literal text including the ``#``.
    assert "memory:#6184" in out


def test_numeric_ref_without_hash_resolves_same() -> None:
    out = str(linkify_refs("memory:6184 covers this."))
    assert 'href="/r/memory/6184"' in out


def test_paper_chunk_address_carried_through() -> None:
    out = str(linkify_refs("paper:acheson26~7 page reference"))
    assert 'href="/r/paper/acheson26?chunk=7"' in out
    # Display shows the full address.
    assert "paper:acheson26~7" in out


def test_paper_chunk_range_address() -> None:
    out = str(linkify_refs("paper:inamuddin21~5..9 for this."))
    assert "?chunk=5..9" in out


def test_paper_page_address_uses_p_prefix() -> None:
    out = str(linkify_refs("paper:inamuddin21~p23 talks about it."))
    assert "?chunk=p23" in out


def test_multiple_refs_in_one_string() -> None:
    out = str(linkify_refs("see paper:foo and memory:42 for context"))
    assert 'href="/r/paper/foo"' in out
    assert 'href="/r/memory/42"' in out


def test_ref_inside_code_block_left_alone() -> None:
    raw = "Use <code>paper:acheson26</code> in your put call."
    out = str(linkify_refs(raw))
    # The <code> block is preserved verbatim.
    assert "<code>paper:acheson26</code>" in out
    # No anchor for the bracketed mention.
    assert 'href="/r/paper/acheson26"' not in out


def test_ref_inside_pre_block_left_alone() -> None:
    raw = "<pre>get(kind='paper', id='acheson26')</pre>"
    out = str(linkify_refs(raw))
    assert "<pre>" in out
    # Filter shouldn't touch the inside (pre is verbatim).
    assert out.count('href="/r/paper/') == 0


def test_existing_anchor_not_double_wrapped() -> None:
    raw = 'click <a href="elsewhere">paper:foo</a> here'
    out = str(linkify_refs(raw))
    # The original <a> is preserved as-is.
    assert '<a href="elsewhere">paper:foo</a>' in out
    # The original anchor block is one continuous skip-zone; no nested anchor.
    assert out.count('href="/r/paper/') == 0


def test_word_boundary_prevents_runon_capture() -> None:
    """``memory:6184foobar`` should NOT capture — the regex requires that
    the id be followed by a non-word character (or end of string)."""
    out = str(linkify_refs("ids like memory:6184foobar should not link"))
    assert 'href="/r/memory/' not in out


def test_anchor_has_hover_delay_via_alpine_modifiers() -> None:
    """The hover-200ms requirement rides in via Alpine ``debounce``."""
    out = str(linkify_refs("paper:acheson26"))
    assert "@mouseenter.debounce.200ms" in out


def test_anchor_has_htmx_lazy_preview_attributes() -> None:
    """The preview fragment loads via htmx on first hover only."""
    out = str(linkify_refs("paper:acheson26"))
    assert 'hx-trigger="mouseenter delay:200ms once"' in out


def test_empty_string_returns_empty() -> None:
    assert str(linkify_refs("")) == ""


def test_none_value_returns_empty() -> None:
    assert str(linkify_refs(None)) == ""  # type: ignore[arg-type]
