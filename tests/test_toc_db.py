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

from precis.utils.toc_db import render_from_store


@dataclass
class _Stub:
    """Minimal stand-in for ``Block`` — renderer reads pos + keywords."""

    pos: int
    keywords: list[str] = field(default_factory=list)


class _StubStore:
    def __init__(self, blocks: list[_Stub]) -> None:
        self._blocks = blocks

    def list_blocks_for_ref(
        self, ref_id: int, *, pos_range: tuple[int, int] | None = None
    ) -> list[_Stub]:
        if pos_range is None:
            return list(self._blocks)
        lo, hi = pos_range
        return [b for b in self._blocks if lo <= b.pos <= hi]


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
        blocks = [_Stub(pos=i, keywords=[f"kw{i}"]) for i in range(4)]
        out = _render(blocks)
        # Headline names chunk count, not "(per-chunk preview)".
        assert "4 chunks" in out
        assert "per-chunk preview" not in out
        # Schema is always (handle, keywords).
        assert "{handle\tkeywords}" in out
        # One row per chunk, with the chunk's own keyword as the label.
        for i in range(4):
            assert f"pa1~{i}\t" in out

    def test_short_range_keywords_never_snippets(self) -> None:
        """Regression: the dropped preview path used to leak chunk
        text into the 'preview' column. The new contract has no
        'preview' column and never inspects ``.text``."""
        blocks = [
            _Stub(pos=0, keywords=["mitochondria", "parkin"]),
            _Stub(pos=1, keywords=["retromer", "vps35"]),
        ]
        out = _render(blocks)
        assert "preview" not in out
        assert "mitochondria, parkin" in out
        assert "retromer, vps35" in out

    def test_per_chunk_path_emits_no_topics_or_next(self) -> None:
        blocks = [_Stub(pos=i, keywords=["shared"]) for i in range(5)]
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
        a = [_Stub(pos=i, keywords=["alpha", "beta", "shared"]) for i in range(half)]
        b = [
            _Stub(pos=i, keywords=["gamma", "delta", "shared"]) for i in range(half, n)
        ]
        return a + b

    def test_bucketed_schema_is_handle_keywords(self) -> None:
        out = _render(self._two_topic_body(n=60))
        assert "{handle\tkeywords}" in out
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
        a = [_Stub(pos=i, keywords=["alpha", "beta"]) for i in range(30)]
        b = [_Stub(pos=i, keywords=["gamma", "delta"]) for i in range(30, 60)]
        out = _render(a + b)
        assert "Topics:" not in out


# ── drill-in hint (Next: block) ─────────────────────────────────────


class TestDrillInHint:
    def test_next_block_fires_for_fat_cluster(self) -> None:
        """One huge same-topic cluster → Next: hint with its handle."""
        # 40 identical-keyword chunks, then 30 different ones. The
        # first 40 form a fat cluster (≥ _BUCKETING_THRESHOLD=30).
        blocks = [
            _Stub(pos=i, keywords=["alpha", "beta", "gamma"]) for i in range(40)
        ] + [_Stub(pos=i, keywords=["delta", "epsilon", "zeta"]) for i in range(40, 70)]
        out = _render(blocks)
        assert "Next: drill into fat clusters" in out
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
            _Stub(pos=i, keywords=["alpha", "beta", "gamma"]) for i in range(40)
        ] + [_Stub(pos=i, keywords=["delta", "epsilon", "zeta"]) for i in range(40, 70)]
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
            blocks.append(_Stub(pos=i, keywords=[f"topic{i // 3}", "shared"]))
        out = _render(blocks)
        assert "Next: drill into fat clusters" not in out


# ── empty / scope paths ─────────────────────────────────────────────


class TestEdges:
    def test_empty_range(self) -> None:
        out = _render([], scope=(10, 20))
        assert "no chunks in scope" in out

    def test_scope_headline(self) -> None:
        blocks = [_Stub(pos=i, keywords=[f"kw{i}"]) for i in range(5)]
        out = _render(blocks, scope=(0, 4))
        assert "sub-TOC ~0..4" in out
