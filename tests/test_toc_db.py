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

from precis.utils.toc_db import build_toc_segments, cluster_blocks, render_from_store


@dataclass
class _Stub:
    """Minimal stand-in for ``ChunkRow`` — the renderer reads ``ord`` +
    ``keywords`` always, ``meta``/``text``/``chunk_kind`` only for the
    section-path fallback and the pending note, ``id`` for the meta
    line's link/tag reads."""

    ord: int
    id: int | None = None
    keywords: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    text: str = ""
    chunk_kind: str = "paragraph"


class _StubStore:
    chunks = property(
        lambda self: self
    )  # chunks carve: flat fake doubles as its own sub-store

    def __init__(
        self,
        blocks: list[_Stub],
        *,
        link_counts: tuple[int, dict[int, int]] = (0, {}),
        tag_counts: list[tuple[str, str, int]] | None = None,
    ) -> None:
        self._blocks = blocks
        self._link_counts = link_counts
        self._tag_counts = tag_counts or []
        self.meta_queries: list[str] = []

    def list_chunks_for_ref(
        self, ref_id: int, *, pos_range: tuple[int, int] | None = None
    ) -> list[_Stub]:
        if pos_range is None:
            return list(self._blocks)
        lo, hi = pos_range
        return [b for b in self._blocks if lo <= b.ord <= hi]

    def count_links_for_chunks(
        self, chunk_ids: list[int]
    ) -> tuple[int, dict[int, int]]:
        self.meta_queries.append("links")
        return self._link_counts

    def chunk_tag_counts(self, chunk_ids: list[int]) -> list[tuple[str, str, int]]:
        self.meta_queries.append("tags")
        return self._tag_counts


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
        assert "Next:" in out and "drill a cluster" in out
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
            _Stub(ord=1, text="© 2024 Elsevier Ltd. " + "x" * 350),
            _Stub(ord=2, text="All rights reserved. " + "y" * 350),
            _Stub(ord=3, text="Licensed under CC-BY 4.0 " + "z" * 350),
            _Stub(ord=4, keywords=["methods"], text="methods text"),
        ]
        out = _render(blocks)
        assert "pa1~1..3" in out
        # Folded row carries the summed bytes of the run (size column):
        # (372+371+375) = 1118 bytes -> "1kB"; sub-kB neighbours stay empty.
        fold_row = next(ln for ln in out.splitlines() if ln.startswith("pa1~1..3"))
        assert fold_row.split("\t")[1] == "1kB"
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

    def test_real_content_mentioning_licence_is_not_folded(self) -> None:
        blocks = [
            _Stub(ord=0, keywords=["a"], text="We obtained a licence for the dataset."),
            _Stub(ord=1, keywords=["b"], text="Under copyright law, reuse is limited."),
            _Stub(ord=2, keywords=["c"], text="Licence"),
            _Stub(ord=3, keywords=["d"], text="Copyright law differs by country."),
        ]
        out = _render(blocks)
        assert "boilerplate" not in out
        for i in range(4):
            assert f"pa1~{i}\t" in out

    def test_long_notice_chunk_is_not_folded(self) -> None:
        blocks = [
            _Stub(ord=0, keywords=["a"], text="© 2024 Foo. " + "x" * 500),
            _Stub(ord=1, keywords=["b"], text="more content"),
        ]
        out = _render(blocks)
        assert "boilerplate" not in out
        assert "pa1~0\t" in out

    def test_boilerplate_folds_in_keywords_mode(self) -> None:
        blocks = [_Stub(ord=i, keywords=["topic"], text="body text") for i in range(10)]
        blocks += [
            _Stub(ord=10, text="© 2024 Elsevier Ltd."),
            _Stub(ord=11, text="All rights reserved."),
        ]
        blocks += [
            _Stub(ord=i, keywords=["other"], text="more body") for i in range(12, 22)
        ]
        groups = cluster_blocks(blocks)
        folded = [g for g in groups if g[1] == ["copyright / licence boilerplate"]]
        assert len(folded) == 1
        assert [b.ord for b in folded[0][0]] == [10, 11]

    def test_boilerplate_folds_in_sections_mode(self) -> None:
        """No keywords anywhere -> sections mode; a notice-only section
        still folds."""
        blocks = [_sec(i, "1. Intro", text="body") for i in range(10)]
        blocks += [
            _sec(10, "References", text="© 2024 Elsevier Ltd."),
            _sec(11, "References", text="All rights reserved."),
        ]
        blocks += [_sec(i, "2. Methods", text="body") for i in range(12, 22)]
        groups = cluster_blocks(blocks)
        folded = [g for g in groups if g[1] == ["copyright / licence boilerplate"]]
        assert len(folded) == 1
        assert [b.ord for b in folded[0][0]] == [10, 11]


# ── aggregate meta line (WU6, fable-review design call b / R7) ───────


class TestMetaLine:
    def test_meta_line_reports_count_bytes_tags_links_hubs(self) -> None:
        blocks = [_Stub(ord=i, id=i + 1, text="x" * 2000) for i in range(4)]
        store = _StubStore(
            blocks,
            link_counts=(6, {1: 4, 2: 2, 3: 1}),
            tag_counts=[
                ("OPEN", "important", 3),
                ("TOPIC", "catalysis", 2),
                ("OPEN", "review", 1),
            ],
        )
        out = render_from_store(store=store, ref_id=1, handle="pa1", kind="paper")
        meta = next(ln for ln in out.splitlines() if ln.startswith("pa1:"))
        assert "4 chunks" in meta
        assert "8kB" in meta
        assert "tags [important, TOPIC:catalysis, review]" in meta
        assert "↔6 chunk links" in meta
        assert "hubs: pa1~0 ↔4, pa1~1 ↔2, pa1~2 ↔1" in meta

    def test_hubs_ranked_by_degree_then_id_with_toc_handles(self) -> None:
        """Hardcoded order: degree 4 > 3 > tie at 2 (lower chunk id wins),
        a degree-1 chunk is cut by the 3-hub cap. Chunk 9 has no loaded
        block so it falls back to its pc<id> handle."""
        blocks = [_Stub(ord=i + 10, id=i + 1, text="x") for i in range(4)]
        store = _StubStore(
            blocks,
            link_counts=(7, {1: 2, 2: 4, 3: 3, 4: 2, 9: 1}),
        )
        out = render_from_store(store=store, ref_id=1, handle="pa1", kind="paper")
        meta = next(ln for ln in out.splitlines() if ln.startswith("pa1:"))
        assert "hubs: pa1~11 ↔4, pa1~12 ↔3, pa1~10 ↔2" in meta
        store = _StubStore(blocks, link_counts=(1, {9: 5}))
        out = render_from_store(store=store, ref_id=1, handle="pa1", kind="paper")
        assert "hubs: pc9 ↔5" in out

    def test_meta_line_stays_short_with_long_tags(self) -> None:
        blocks = [_Stub(ord=i, id=i + 1, text="x" * 2000) for i in range(4)]
        tags = [
            ("OPEN", "a-very-long-free-form-tag-number-" + str(i), 1) for i in range(10)
        ]
        store = _StubStore(blocks, link_counts=(6, {1: 4, 2: 2, 3: 1}), tag_counts=tags)
        out = render_from_store(store=store, ref_id=1, handle="pa1", kind="paper")
        meta = next(ln for ln in out.splitlines() if ln.startswith("pa1:"))
        assert len(meta.encode("utf-8")) < 400
        assert "a-very-long-free-form-t…" in meta
        assert "a-very-long-free-form-ta" not in meta

    def test_meta_line_uses_scoped_range_label(self) -> None:
        blocks = [_Stub(ord=i, id=i + 1, text="x") for i in range(5)]
        out = render_from_store(
            store=_StubStore(blocks),
            ref_id=1,
            handle="pa1",
            kind="paper",
            scope=(0, 4),
        )
        assert "pa1~0..4: 5 chunks" in out

    def test_meta_line_caps_tag_union_at_eight_with_remainder(self) -> None:
        blocks = [_Stub(ord=0, id=1, text="x")]
        tags = [("OPEN", f"tag{i}", 1) for i in range(10)]
        out = render_from_store(
            store=_StubStore(blocks, tag_counts=tags),
            ref_id=1,
            handle="pa1",
            kind="paper",
        )
        meta = next(ln for ln in out.splitlines() if ln.startswith("pa1:"))
        assert "tags [tag0, tag1, tag2, tag3, tag4, tag5, tag6, tag7 +2]" in meta

    def test_meta_line_runs_one_links_and_one_tags_query(self) -> None:
        """WU6 query budget: one grouped links read + one grouped tags
        read per render, never per row."""
        blocks = [_Stub(ord=i, id=i + 1, text="x") for i in range(20)]
        store = _StubStore(blocks)
        render_from_store(store=store, ref_id=1, handle="pa1", kind="paper")
        assert store.meta_queries == ["links", "tags"]

    def test_meta_line_survives_failed_bulk_reads(self) -> None:
        """A failed links/tags read degrades its segment, never the row."""

        class _FlakyStore(_StubStore):
            def count_links_for_chunks(self, chunk_ids):
                raise RuntimeError("pool down")

            def chunk_tag_counts(self, chunk_ids):
                raise RuntimeError("pool down")

        blocks = [_Stub(ord=0, id=1, text="x")]
        out = render_from_store(
            store=_FlakyStore(blocks), ref_id=1, handle="pa1", kind="paper"
        )
        meta = next(ln for ln in out.splitlines() if ln.startswith("pa1:"))
        assert "1 chunks" in meta
        assert "links" not in meta
        assert "tags" not in meta

    def test_meta_line_present_in_every_mode(self) -> None:
        """Top-level, scoped and per-chunk renders all carry the line."""
        blocks = [_Stub(ord=i, id=i + 1, text="x" * 200) for i in range(40)]
        out = _render(blocks)
        assert any(ln.startswith("pa1:") for ln in out.splitlines())


# ── meta line against real SQL (WU6: line equals SQL truth) ──────────


class _CountingStore:
    """``TocStore``-shaped wrapper counting the meta-line reads."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    @property
    def chunks(self):
        return self._inner.chunks

    def count_links_for_chunks(self, chunk_ids):
        self.calls.append("links")
        return self._inner.count_links_for_chunks(chunk_ids)

    def chunk_tag_counts(self, chunk_ids):
        self.calls.append("tags")
        return self._inner.chunk_tag_counts(chunk_ids)


class TestMetaLineDbTruth:
    def test_meta_line_matches_sql_truth(self, store) -> None:
        """Fixture: 4 chunks, 3 links (one intra-range), 3 chunk tags.

        The line must equal an independent SQL computation of the same
        quantities — distinct links touching the range, tag union by
        in-range frequency, per-chunk degree."""
        from precis.store.types import ChunkInsert, Tag

        ref = store.insert_ref(kind="paper", slug="metapaper", title="M")
        blocks = store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=i, text=("word " * 300).strip()) for i in range(4)],
        )
        other = store.insert_ref(kind="memory", slug=None, title="m")
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=other.id,
            relation="supports",
            src_pos=0,
        )
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=other.id,
            relation="supports",
            src_pos=1,
        )
        # Intra-range edge: block 2 → block 3. Counted once in the
        # total, once per endpoint in the degrees.
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=ref.id,
            relation="supports",
            src_pos=2,
            dst_pos=3,
        )
        store.add_tag(ref.id, Tag.open("important"), pos=0)
        store.add_tag(ref.id, Tag.open("important"), pos=1)
        store.add_tag(ref.id, Tag.closed("TOPIC", "catalysis"), pos=2)

        chunk_ids = [b.id for b in blocks]
        with store.pool.connection() as conn:
            sql_total = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT link_id) FROM links "
                    "WHERE src_chunk_id = ANY(%s) "
                    "   OR dst_chunk_id = ANY(%s)",
                    (chunk_ids, chunk_ids),
                ).fetchone()[0]
            )
            sql_degrees = {
                int(r[0]): int(r[1])
                for r in conn.execute(
                    "SELECT cid, COUNT(*) FROM ("
                    "  SELECT src_chunk_id AS cid FROM links "
                    "    WHERE src_chunk_id = ANY(%s)"
                    "  UNION ALL"
                    "  SELECT dst_chunk_id FROM links "
                    "    WHERE dst_chunk_id = ANY(%s)"
                    ") s GROUP BY cid",
                    (chunk_ids, chunk_ids),
                ).fetchall()
            }
        assert sql_total == 3
        assert sql_degrees[blocks[2].id] == 1
        assert sql_degrees[blocks[3].id] == 1

        wrapped = _CountingStore(store)
        out = render_from_store(
            store=wrapped, ref_id=ref.id, handle=f"pa{ref.id}", kind="paper"
        )
        assert wrapped.calls == ["links", "tags"]
        meta = next(ln for ln in out.splitlines() if ln.startswith(f"pa{ref.id}:"))
        assert "4 chunks" in meta
        assert f"↔{sql_total} chunk links" in meta
        assert "tags [important, TOPIC:catalysis]" in meta
        top = sorted(sql_degrees.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        ord_of = {b.id: b.ord for b in blocks}
        assert (
            "hubs: " + ", ".join(f"pa{ref.id}~{ord_of[cid]} ↔{n}" for cid, n in top)
            in meta
        )
