"""Renderer contract for :mod:`precis.utils.toc_db`.

Pins the F20 dynamic TOC behaviour after the 2026-06-05 rework:

* Uniform ``(handle, keywords)`` schema at every range size — no
  more "per-chunk preview" branch that emitted snippet text.
* ``Topics:`` line surfaces keywords present in ≥75% of clusters
  *without* stripping them from per-row labels (lossless redundant
  summary).
* ``Next:`` block hints recursive ``view='toc'`` on any cluster
  large enough to re-bucket on its own.

Synthetic block stubs only; no DB, no embedder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from precis.utils.toc_db import build_toc_segments, render_from_store


@dataclass
class _Stub:
    """Minimal stand-in for ``ChunkRow`` — the renderer reads ``ord`` +
    ``keywords`` always, ``meta``/``text``/``chunk_kind`` only for the
    section-path fallback and the pending note."""

    ord: int
    keywords: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    text: str = ""
    chunk_kind: str = "paragraph"


class _StubStore:
    chunks = property(
        lambda self: self
    )  # chunks carve: flat fake doubles as its own sub-store

    def __init__(self, blocks: list[_Stub]) -> None:
        self._blocks = blocks

    def list_chunks_for_ref(
        self, ref_id: int, *, pos_range: tuple[int, int] | None = None
    ) -> list[_Stub]:
        if pos_range is None:
            return list(self._blocks)
        lo, hi = pos_range
        return [b for b in self._blocks if lo <= b.ord <= hi]


def _render(blocks: list[_Stub], *, scope: tuple[int, int] | None = None) -> str:
    return render_from_store(
        store=_StubStore(blocks),
        ref_id=1,
        handle="pa1",
        kind="paper",
        scope=scope,
    )


# ── per-chunk path (n < _BUCKETING_THRESHOLD) ───────────────────────


class TestPerChunkPath:
    def test_short_range_emits_one_row_per_chunk(self) -> None:
        blocks = [_Stub(ord=i, keywords=[f"kw{i}"]) for i in range(4)]
        out = _render(blocks)
        # Headline names chunk count, not "(per-chunk preview)".
        assert "4 chunks" in out
        assert "per-chunk preview" not in out
        # Schema is always (handle, size, keywords).
        assert "{handle\tsize\tkeywords}" in out
        # One row per chunk, with the chunk's own keyword as the label.
        for i in range(4):
            assert f"pa1~{i}\t\tkw{i}" in out

    def test_short_range_keywords_never_snippets(self) -> None:
        """Regression: the dropped preview path used to leak chunk
        text into the 'preview' column. The new contract has no
        'preview' column and never inspects ``.text``."""
        blocks = [
            _Stub(ord=0, keywords=["mitochondria", "parkin"]),
            _Stub(ord=1, keywords=["retromer", "vps35"]),
        ]
        out = _render(blocks)
        assert "preview" not in out
        assert "mitochondria, parkin" in out
        assert "retromer, vps35" in out

    def test_per_chunk_path_emits_no_topics_or_next(self) -> None:
        blocks = [_Stub(ord=i, keywords=["shared"]) for i in range(5)]
        out = _render(blocks)
        assert "Topics:" not in out
        assert "Next:" not in out


# ── bucketed path (n ≥ _BUCKETING_THRESHOLD) ────────────────────────


class TestBucketedPath:
    def _two_topic_body(self, n: int = 60) -> list[_Stub]:
        """Half the chunks share keyword set A; the other half share B.

        The Jaccard signal between the halves is 1.0, so segment_dp
        cleanly separates them no matter what k it's asked for.
        """
        half = n // 2
        a = [_Stub(ord=i, keywords=["alpha", "beta", "shared"]) for i in range(half)]
        b = [
            _Stub(ord=i, keywords=["gamma", "delta", "shared"]) for i in range(half, n)
        ]
        return a + b

    def test_bucketed_schema_is_handle_keywords(self) -> None:
        out = _render(self._two_topic_body(n=60))
        assert "{handle\tsize\tkeywords}" in out
        assert "per-chunk preview" not in out
        assert "clusters" in out

    def test_topics_line_promotes_shared_keyword_losslessly(self) -> None:
        """A keyword present in every cluster's label is promoted to
        the Topics line — and remains visible on each row."""
        out = _render(self._two_topic_body(n=60))
        # Topics line carries the pervasive keyword.
        assert "Topics: " in out
        topics_line = next(
            line for line in out.splitlines() if line.startswith("Topics:")
        )
        assert "shared" in topics_line
        # Lossless: 'shared' still appears in the per-row keyword
        # columns (not stripped). Count occurrences in row lines.
        row_lines = [line for line in out.splitlines() if line.startswith("pa1~")]
        assert row_lines, "expected at least one row"
        assert all("shared" in line for line in row_lines), (
            "Topics promotion must NOT strip the keyword from row labels"
        )

    def test_no_topics_when_no_keyword_is_pervasive(self) -> None:
        """Two clusters share no keywords → no Topics line."""
        a = [_Stub(ord=i, keywords=["alpha", "beta"]) for i in range(30)]
        b = [_Stub(ord=i, keywords=["gamma", "delta"]) for i in range(30, 60)]
        out = _render(a + b)
        assert "Topics:" not in out


# ── drill-in hint (Next: block) ─────────────────────────────────────


class TestDrillInHint:
    def test_next_block_fires_for_fat_cluster(self) -> None:
        """One huge same-topic cluster → Next: hint with its handle."""
        # 40 identical-keyword chunks, then 30 different ones. The
        # first 40 form a fat cluster (≥ _BUCKETING_THRESHOLD=30).
        blocks = [
            _Stub(ord=i, keywords=["alpha", "beta", "gamma"]) for i in range(40)
        ] + [_Stub(ord=i, keywords=["delta", "epsilon", "zeta"]) for i in range(40, 70)]
        out = _render(blocks)
        assert "Next: drill" in out
        # The fat cluster's handle appears in a drill-in suggestion.
        next_lines = [
            line for line in out.splitlines() if "view='toc'" in line and "get(" in line
        ]
        assert next_lines, "expected at least one drill-in hint line"
        # At least one references a multi-chunk handle (`~lo..hi`),
        # not a singleton `~N`.
        assert any(".." in line.split("'")[3] for line in next_lines), (
            f"expected a multi-chunk fat-cluster handle in hints: {next_lines}"
        )

    def test_drill_in_hint_carries_universal_handle_no_chunk_comment(self) -> None:
        """Drill-in hints address by the universal handle (``pa<id>~lo..hi``)
        and drop the superfluous ``# N chunks`` trailing comment."""
        blocks = [
            _Stub(ord=i, keywords=["alpha", "beta", "gamma"]) for i in range(40)
        ] + [_Stub(ord=i, keywords=["delta", "epsilon", "zeta"]) for i in range(40, 70)]
        out = _render(blocks)
        next_lines = [
            line for line in out.splitlines() if "view='toc'" in line and "get(" in line
        ]
        assert next_lines
        for line in next_lines:
            # Universal handle prefix, never the legacy slug / kind:slug form.
            assert "id='pa1~" in line
            # The "# N chunks" comment is gone.
            assert "#" not in line
            assert "chunks" not in line

    def test_no_next_block_when_no_fat_cluster(self) -> None:
        """30-chunk body distributed across distinct micro-topics →
        no single cluster is fat enough to drill into."""
        blocks: list[_Stub] = []
        for i in range(30):
            # Each chunk a different "topic" so DP cuts often.
            blocks.append(_Stub(ord=i, keywords=[f"topic{i // 3}", "shared"]))
        out = _render(blocks)
        assert "Next: drill" not in out


# ── empty / scope paths ─────────────────────────────────────────────


class TestEdges:
    def test_empty_range(self) -> None:
        out = _render([], scope=(10, 20))
        assert "no chunks in scope" in out

    def test_scope_headline(self) -> None:
        blocks = [_Stub(ord=i, keywords=[f"kw{i}"]) for i in range(5)]
        out = _render(blocks, scope=(0, 4))
        assert "sub-TOC ~0..4" in out


# ── drill-down hierarchy (scope-aware sub-clustering) ────────────────


def _segments(
    blocks: list[_Stub], *, scope: tuple[int, int] | None = None
) -> list[dict]:
    return build_toc_segments(
        store=_StubStore(blocks), ref_id=1, handle="pa1", scope=scope
    )


class TestDrillHierarchy:
    """A drilled sub-range re-clusters into sub-groups instead of
    flattening to one-row-per-chunk — the huang23f ~25..53 regression
    where a 23-chunk group showed 23 undrillable singleton rows."""

    def _range(self, lo: int, hi: int) -> list[_Stub]:
        # Distinct per-chunk keyword + a shared topic; DP splits on size,
        # mimicking a coherent drilled sub-range.
        return [_Stub(ord=i, keywords=[f"kw{i}", "topic"]) for i in range(lo, hi + 1)]

    def test_drilled_subrange_subclusters_not_flattened(self) -> None:
        blocks = self._range(25, 47)  # 23 chunks, < top-level threshold 30
        assert len(blocks) == 23
        segs = _segments(blocks, scope=(25, 47))
        # Grouped: fewer rows than chunks, and at least one multi-chunk
        # sub-group the reader can drill again.
        assert 1 < len(segs) < 23
        assert any(s["hi"] > s["lo"] for s in segs)
        # Sub-groups tile the range end-to-end, no gaps/overlap.
        assert segs[0]["lo"] == 25 and segs[-1]["hi"] == 47
        for a, b in pairwise(segs):
            assert b["lo"] == a["hi"] + 1

    def test_same_range_top_level_still_per_chunk(self) -> None:
        # Without a scope, a 23-chunk body is under the top-level floor and
        # stays scannable per-chunk (top-level behaviour is unchanged).
        blocks = self._range(0, 22)
        segs = _segments(blocks)
        assert len(segs) == 23
        assert all(s["lo"] == s["hi"] for s in segs)

    def test_small_drilled_range_is_a_leaf(self) -> None:
        # A drilled range at/under 2*_DRILL_GROUP_SIZE (8) is its own leaf —
        # show its chunks directly so drilling terminates cleanly.
        blocks = self._range(10, 17)  # 8 chunks
        segs = _segments(blocks, scope=(10, 17))
        assert len(segs) == 8
        assert all(s["lo"] == s["hi"] for s in segs)

    def test_just_over_leaf_floor_clusters(self) -> None:
        blocks = self._range(10, 18)  # 9 chunks, > 8
        segs = _segments(blocks, scope=(10, 18))
        assert len(segs) < 9
        assert any(s["hi"] > s["lo"] for s in segs)

    def test_drilled_render_reports_clusters_not_flat_list(self) -> None:
        blocks = self._range(25, 47)
        out = render_from_store(
            store=_StubStore(blocks),
            ref_id=1,
            handle="pa1",
            kind="paper",
            scope=(25, 47),
        )
        # Bucketed headline names a cluster count; the per-chunk fallback
        # would omit it. This pins the fix.
        assert "clusters" in out


# ── section_path fallback (gr520232: the keyword backlog drains) ─────


def _sec(ord_: int, title: str | None, **kw) -> _Stub:
    meta = {"section_path": [title]} if title is not None else {}
    return _Stub(ord=ord_, meta=meta, **kw)


class TestSectionPathFallback:
    def _outline(self, n: int = 40) -> list[_Stub]:
        """A keyword-barren body under three numbered sections — the
        pa519908 shape (0 keyworded chunks)."""
        blocks: list[_Stub] = [_sec(0, None), _sec(1, "1. Introduction")]
        for i in range(2, 25):
            blocks.append(_sec(i, f"2.{i - 1}. Method detail {i - 1}"))
        for i in range(25, n):
            blocks.append(_sec(i, "3. Results"))
        return blocks

    def test_keyword_barren_body_groups_by_section_heading(self) -> None:
        out = _render(self._outline())
        rows = [ln for ln in out.splitlines() if ln.startswith("pa1~")]
        # 4 groups (pathless front matter, 1. Intro, section-2 run,
        # section-3 run), not 40 flat rows and not the degenerate
        # 2-unlabelled-range shape.
        assert len(rows) == 4
        assert any("Introduction" in ln for ln in rows)
        assert any("Results" in ln for ln in rows)
        # The section-2 run groups under its top-level number and is
        # labelled by a section-2 title, not left empty.
        sec2 = next(ln for ln in rows if "Method detail" in ln or "2." in ln)
        assert "~2..24" in sec2

    def test_subsections_group_under_the_top_level_number(self) -> None:
        """``2.1.`` … ``2.9.`` are ONE group (keyed on top-level ``2``),
        and a shallow ``2. Methods`` title wins as its name."""
        blocks = [
            _sec(0, "2.1. First"),
            _sec(1, "2. Methods"),
            _sec(2, "2.2. Second"),
            *[_sec(i, f"3.{i}. Sub") for i in range(3, 40)],
        ]
        segs = _segments(blocks)
        assert len(segs) == 2
        assert segs[0]["lo"] == 0 and segs[0]["hi"] == 2
        assert segs[0]["keywords"] == ["2. Methods"]

    def test_bold_titles_group_and_render_clean(self) -> None:
        blocks = [_sec(i, "**1. Introduction**") for i in range(20)] + [
            _sec(i, "2. Methods") for i in range(20, 40)
        ]
        out = _render(blocks)
        assert "Introduction" in out
        assert "**" not in out

    def test_degenerate_outline_falls_back_to_per_chunk(self) -> None:
        """No section_path anywhere → one unlabelled mega-group is worse
        than per-chunk rows; fall back."""
        blocks = [_sec(i, None) for i in range(40)]
        out = _render(blocks)
        rows = [ln for ln in out.splitlines() if ln.startswith("pa1~")]
        assert len(rows) == 40
        assert "clusters" not in out

    def test_keywords_still_win_above_the_coverage_floor(self) -> None:
        """A quarter-or-more keyworded body keeps the Jaccard clustering —
        the fallback only engages below ``_KEYWORD_COVERAGE_MIN``."""
        blocks = [
            _sec(i, "1. Section", keywords=(["alpha"] if i < 20 else ["gamma"]))
            for i in range(40)
        ]
        out = _render(blocks)
        assert "alpha" in out and "gamma" in out
        assert "Section" not in out

    def test_leading_pathless_run_gets_an_empty_label(self) -> None:
        blocks = [_sec(i, None) for i in range(3)] + [
            _sec(i, "1. Introduction") for i in range(3, 40)
        ]
        segs = _segments(blocks)
        assert segs[0]["lo"] == 0 and segs[0]["hi"] == 2
        assert segs[0]["keywords"] == []

    def test_sections_mode_announces_its_label_source(self) -> None:
        """The heading-labelled outline names its label source — without
        the note a section-label column reads as a new keyword shape
        (gr520238). Stubs carry no ``text`` → nothing pending."""
        out = _render(self._outline())
        assert "_(labels: section headings)_" in out

    def test_sections_notice_counts_pending_keyword_chunks(self) -> None:
        """Long keywordless blocks are the pending drain the notice
        names; skip-kinds and below-floor stubs never carry labels and
        don't count."""
        blocks = [_sec(i, "1. Introduction", text="x" * 200) for i in range(20)]
        blocks += [_sec(i, "2. Methods", text="x" * 200) for i in range(20, 40)]
        blocks[3] = _sec(3, "1. Introduction", text="x" * 200, chunk_kind="table")
        blocks[4] = _sec(4, "1. Introduction", text="tiny")
        out = _render(blocks)
        assert "keywords pending for 38 chunks" in out
        assert "keyword labels appear as chunk_keywords drains" in out


# ── readiness line (read-for-question loop, slice 4) ─────────────────────


class TestReadinessLine:
    def test_per_chunk_path_renders_readiness_line(self) -> None:
        blocks = [_Stub(ord=i, keywords=[f"kw{i}"]) for i in range(4)]
        out = render_from_store(
            store=_StubStore(blocks),
            ref_id=1,
            handle="pa1",
            kind="paper",
            readiness_line="readiness: embedded 3/4 · summarised 4/4",
        )
        assert "readiness: embedded 3/4 · summarised 4/4" in out

    def test_bucketed_path_renders_readiness_line(self) -> None:
        blocks = [_Stub(ord=i, keywords=[f"kw{i % 5}"]) for i in range(40)]
        out = render_from_store(
            store=_StubStore(blocks),
            ref_id=1,
            handle="pa1",
            kind="paper",
            readiness_line="readiness: embedded 40/40 · summarised 38/40",
        )
        assert "readiness: embedded 40/40 · summarised 38/40" in out

    def test_omitted_readiness_line_unchanged(self) -> None:
        """No ``readiness_line=`` -> header is byte-identical to before —
        the default keeps every existing caller (and test) unchanged."""
        blocks = [_Stub(ord=i, keywords=[f"kw{i}"]) for i in range(4)]
        out = render_from_store(
            store=_StubStore(blocks), ref_id=1, handle="pa1", kind="paper"
        )
        assert "readiness:" not in out


class TestCopyrightFold:
    def test_consecutive_copyright_chunks_fold_into_one_entry(self) -> None:
        """gr531128 (gr524226 item 2): a run of all-copyright/licence
        chunks is one TOC entry, not a row apiece."""
        blocks = [
            _Stub(ord=0, keywords=["intro"], text="introduction text"),
            _Stub(ord=1, text="© 2024 Elsevier Ltd.\n" * 40),
            _Stub(ord=2, text="All rights reserved.\n" * 40),
            _Stub(ord=3, text="Licensed under CC-BY 4.0\n" * 40),
            _Stub(ord=4, keywords=["methods"], text="methods text"),
        ]
        out = _render(blocks)
        assert "pa1~1..3" in out
        # Folded row carries the summed bytes of the run (size column):
        # 40*(22+21+25) = 2720 bytes -> "3kB"; sub-kB neighbours stay empty.
        fold_row = next(ln for ln in out.splitlines() if ln.startswith("pa1~1..3"))
        assert fold_row.split("\t")[1] == "3kB"
        assert "copyright / licence boilerplate" in out
        assert "pa1~1\t" not in out
        assert "pa1~2\t" not in out
        assert "pa1~3\t" not in out
        assert "pa1~0\t" in out
        assert "pa1~4\t" in out

    def test_mixed_chunk_is_not_folded(self) -> None:
        """A chunk carrying a notice line plus real text keeps its row."""
        blocks = [
            _Stub(ord=0, keywords=["x"], text="© 2024 Foo\n\nreal content here"),
            _Stub(ord=1, keywords=["y"], text="more content"),
        ]
        out = _render(blocks)
        assert "pa1~0\t" in out
        assert "pa1~1\t" in out
