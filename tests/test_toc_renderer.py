"""Generic TOC renderer contract tests.

Locks the output shape — column layout, headline format, shared-
phrases footer, abbreviation legend, sub-range / scoped rendering,
H2-vs-embedding selection — so refactors in ``precis.utils.toc``
fail loudly before silently re-shaping every TOC view in the
codebase.

Synthetic chunks + synthetic embeddings; no real bge-m3 calls.
"""

from __future__ import annotations

import math

from precis.utils.toc import ChunksForToc, cache_clear, render, render_for_ref


def _unit(*xs: float) -> list[float]:
    """Cheap L2-normalised vector for cosine math in tests."""
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


# ── trivial / boundary cases ────────────────────────────────────────


class TestTrivial:
    def test_empty_chunks_returns_empty_marker(self) -> None:
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=[],
            embeddings=[],
            h2_boundaries=None,
        )
        assert out == "# foo — empty"

    def test_single_chunk_renders_one_row(self) -> None:
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=["lithium-mediated nitrogen reduction is studied"],
            embeddings=[_unit(1.0, 0.0)],
            h2_boundaries=None,
        )
        assert "foo~0" in out
        assert "1 chunk" in out


# ── H2-mode selection ───────────────────────────────────────────────


class TestH2Mode:
    def _long_body(self, idx: int) -> str:
        """Chunks long enough to escape the boilerplate classifier's
        position-0 ``len < 1500`` head heuristic. Real body chunks
        are 2-5 KB; tests should match shape."""
        return ("substantive paragraph " * 200) + f" chunk {idx} marker."

    def test_h2_mode_when_coverage_high(self) -> None:
        """3 H2 sections covering 100 % of an 9-chunk body -> H2 mode."""
        chunks = [self._long_body(i) for i in range(9)]
        h2 = [
            (0, 2, "Introduction"),
            (3, 5, "Methods"),
            (6, 8, "Conclusion"),
        ]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=[_unit(1.0, 0.0)] * 9,
            h2_boundaries=h2,
        )
        # Headline mentions "H2 sections".
        assert "H2 sections" in out
        # Column header includes heading.
        assert "{handle\theading\tkeywords}" in out

    def test_falls_back_to_embedding_when_h2_too_few(self) -> None:
        """One H2 in a 9-chunk body -> embedding clustering."""
        chunks = [self._long_body(i) for i in range(9)]
        h2 = [(0, 2, "Only Section")]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=[_unit(math.cos(i), math.sin(i)) for i in range(9)],
            h2_boundaries=h2,
        )
        assert "embedding clustering" in out
        assert "{handle\tkeywords}" in out

    def test_no_h2_no_embeddings_flat_listing(self) -> None:
        chunks = [f"chunk body {i}" for i in range(5)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=None,
            h2_boundaries=None,
        )
        # 5 rows expected (flat). Each row has handle of form foo~N.
        assert "foo~0" in out
        assert "foo~4" in out


# ── shared-phrases footer ───────────────────────────────────────────


class TestPaperWideRow:
    """The "Shared across segments" footer was replaced by a
    paper-wide row at the top of the TOC (2026-05-31). Tests now
    check for the top-row pattern instead of the footer string.
    """

    def _long_body(self, idx: int, *, words: str = "lithium battery") -> str:
        return (f"{words} body {idx} " * 200) + f" chunk {idx} marker."

    def test_paper_wide_row_appears_first(self) -> None:
        chunks = [self._long_body(i) for i in range(6)]
        embeddings = [_unit(math.cos(i), math.sin(i)) for i in range(6)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=embeddings,
            h2_boundaries=None,
        )
        # Old footer string must not appear.
        assert "Shared across segments:" not in out
        # The paper-wide row uses the full-range handle.
        assert "foo~0..5" in out
        # And it appears before the first per-segment row.
        first_handle_lines = [
            i for i, ln in enumerate(out.splitlines()) if ln.startswith("foo~")
        ]
        assert first_handle_lines, "expected at least one foo~ row"
        # The full-range handle should be the first one rendered.
        first_row = out.splitlines()[first_handle_lines[0]]
        assert first_row.startswith("foo~0..5"), (
            f"expected paper-wide row first; got {first_row!r}"
        )


# ── abbreviation legend ─────────────────────────────────────────────


class TestAbbreviationLegend:
    def test_legend_appears_when_abbrevs_detected(self) -> None:
        chunks = [
            "Fourier Transform Infrared (FTIR) spectroscopy was performed. "
            "Density Functional Theory (DFT) calculations confirm trends.",
            "FTIR data revealed strong absorbance at 1600 cm-1 in samples.",
            "DFT calculations match the observed FTIR data within error margins.",
        ]
        embeddings = [_unit(math.cos(i), math.sin(i)) for i in range(3)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=embeddings,
            h2_boundaries=None,
        )
        # Legend line carries both abbreviations.
        assert "Abbrevs:" in out
        assert "FTIR" in out
        assert "DFT" in out

    def test_no_legend_when_no_abbrevs(self) -> None:
        chunks = [
            "Battery design overview using stable lithium chemistry components.",
            "Battery cell testing protocol description involves multiple cycles.",
            "Battery performance analysis section examines capacity fade over time.",
        ]
        embeddings = [_unit(math.cos(i), math.sin(i)) for i in range(3)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=embeddings,
            h2_boundaries=None,
        )
        assert "Abbrevs:" not in out


# ── scoped (recursive sub-segment) rendering ────────────────────────


class TestScope:
    def test_scope_restricts_range_and_emits_absolute_handles(self) -> None:
        chunks = [f"body of chunk {i}" for i in range(20)]
        embeddings = [_unit(math.cos(i), math.sin(i)) for i in range(20)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=embeddings,
            h2_boundaries=None,
            scope=(5, 14),
        )
        # Headline announces sub-TOC.
        assert "sub-TOC" in out
        assert "foo~5..14" in out
        # Handles are absolute (offset by scope.start).
        assert "foo~5" in out  # the segment-0 handle inside the scope
        # Should NOT contain chunks outside the scope.
        assert "foo~0\t" not in out
        assert "foo~15\t" not in out


# ── column shape ────────────────────────────────────────────────────


class TestColumnShape:
    def test_embedding_mode_uses_two_column_layout(self) -> None:
        chunks = [f"body {i}" for i in range(6)]
        embeddings = [_unit(math.cos(i), math.sin(i)) for i in range(6)]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=embeddings,
            h2_boundaries=None,
        )
        # TOON header is two-column: {handle\tkeywords}.
        assert "{handle\tkeywords}" in out

    def test_h2_mode_uses_three_column_layout(self) -> None:
        # Long chunks to avoid the boilerplate classifier reclassifying
        # chunk 0 as front-matter (position-0 + len<1500 rule).
        chunks = [
            ("substantive paragraph " * 200) + f" chunk {i} marker." for i in range(9)
        ]
        h2 = [
            (0, 2, "Real Section Name"),
            (3, 5, "Another Real Section"),
            (6, 8, "Third Distinct Heading"),
        ]
        out = render(
            slug="foo",
            kind="paper",
            chunks_text=chunks,
            embeddings=[_unit(1.0, 0.0)] * 9,
            h2_boundaries=h2,
        )
        # TOON header is three-column: {handle\theading\tkeywords}.
        assert "{handle\theading\tkeywords}" in out


# ── caching ──────────────────────────────────────────────────────────


class TestCache:
    def setup_method(self) -> None:
        cache_clear()

    def test_render_for_ref_returns_cached_on_repeat_call(self) -> None:
        """Second call with the same key returns the same body
        instance without re-running RAKE. The LRU is keyed on
        ``(ref_id, kind, chunker_version, embedder_name,
        SEGMENTATION_VERSION, scope)``."""
        adapter = ChunksForToc(
            chunks_text=tuple(f"chunk {i} body" for i in range(6)),
            embeddings=tuple(tuple(_unit(math.cos(i), math.sin(i))) for i in range(6)),
            h2_boundaries=(),
            chunker_version="1.0",
            embedder_name="mock",
        )
        first = render_for_ref(ref_id=42, slug="foo", kind="paper", adapter=adapter)
        second = render_for_ref(ref_id=42, slug="foo", kind="paper", adapter=adapter)
        assert first == second
        # Specifically same object reference — cache hit, not recompute.
        assert first is second

    def test_different_scope_misses_cache(self) -> None:
        adapter = ChunksForToc(
            chunks_text=tuple(f"chunk {i} body" for i in range(20)),
            embeddings=tuple(tuple(_unit(math.cos(i), math.sin(i))) for i in range(20)),
            h2_boundaries=(),
            chunker_version="1.0",
            embedder_name="mock",
        )
        full = render_for_ref(ref_id=42, slug="foo", kind="paper", adapter=adapter)
        scoped = render_for_ref(
            ref_id=42, slug="foo", kind="paper", adapter=adapter, scope=(5, 14)
        )
        assert full is not scoped
        assert "foo~5..14" in scoped

    def test_chunker_version_change_invalidates(self) -> None:
        adapter_v1 = ChunksForToc(
            chunks_text=tuple(f"chunk {i}" for i in range(6)),
            embeddings=tuple(tuple(_unit(math.cos(i), math.sin(i))) for i in range(6)),
            h2_boundaries=(),
            chunker_version="1.0",
            embedder_name="mock",
        )
        adapter_v2 = ChunksForToc(
            chunks_text=adapter_v1.chunks_text,
            embeddings=adapter_v1.embeddings,
            h2_boundaries=(),
            chunker_version="2.0",
            embedder_name="mock",
        )
        a = render_for_ref(ref_id=42, slug="foo", kind="paper", adapter=adapter_v1)
        b = render_for_ref(ref_id=42, slug="foo", kind="paper", adapter=adapter_v2)
        # Different versions → cache miss → different object refs even
        # if the body content happens to be identical.
        assert a is not b
