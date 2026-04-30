"""Tests for ``precis.jobs.ingest_oracles`` — bulk seeding of oracle YAMLs.

Coverage:
- bundled_oracle_dir() finds the package-shipped data directory.
- render_chunk_body / section_path render correctly with and without
  the optional structured-tail fields.
- ingest_paper writes one oracle ref + N blocks against a fresh DB,
  with open tags applied (no closed axes).
- ingest_paper is idempotent — second run skips, ``--overwrite`` re-
  ingests in place.
- ingest_directory aggregates stats across multiple files.
- Dry-run mode never opens a connection (here covered by passing a
  literal None for `store=`).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.handlers.oracle import OracleHandler
from precis.jobs.ingest_oracles import (
    bundled_oracle_dir,
    ingest_directory,
    ingest_paper,
    render_chunk_body,
    section_path,
)
from precis.store import Store

# ---------------------------------------------------------------------------
# Pure-function tests — no DB.
# ---------------------------------------------------------------------------


def test_bundled_oracle_dir_finds_package_data() -> None:
    p = bundled_oracle_dir()
    assert p is not None, "bundled data/oracle/ should ship with the package"
    assert p.is_dir()
    yamls = sorted(x.name for x in p.glob("*.yaml"))
    # Sanity: at least the canonical traditions ship.
    assert "iching.yaml" in yamls
    assert "stoic.yaml" in yamls


def test_render_chunk_body_without_tail() -> None:
    out = render_chunk_body({"title": "A", "body": "Plain body."})
    assert out == "Plain body."


def test_render_chunk_body_with_tail_keys() -> None:
    out = render_chunk_body(
        {
            "title": "A",
            "body": "Body.",
            "original": "原文",
            "lang": "zh",
            "source": "Analects 1.1",
        }
    )
    assert out.startswith("Body.\n\n")
    assert "_original_: 原文" in out
    assert "_lang_: zh" in out
    assert "_source_: Analects 1.1" in out


def test_render_chunk_body_skips_empty_values() -> None:
    out = render_chunk_body(
        {"title": "A", "body": "Body.", "original": "", "lang": None}
    )
    assert out == "Body."


def test_section_path_basic() -> None:
    assert section_path({"title": "Head"}) == ["Head"]


def test_section_path_with_extras_and_dedup() -> None:
    out = section_path(
        {"title": "Head", "extra_section_path": ["Extra", "Head", "—", ""]}
    )
    assert out == ["Head", "Extra"]


def test_section_path_empty_title() -> None:
    assert section_path({"extra_section_path": ["X"]}) == ["X"]


# ---------------------------------------------------------------------------
# YAML fixtures.
# ---------------------------------------------------------------------------


_MINI_YAML = textwrap.dedent(
    """\
    slug: minitest
    title: Mini Test
    description: Tiny tradition for unit tests.
    tags: [mini, sample]
    entries:
      - title: First entry
        body: |
          The first lesson.
        source: test/1
      - title: Second entry
        body: |
          The second lesson.
        original: 第二
        lang: zh
    """
)


@pytest.fixture
def yaml_dir(tmp_path: Path) -> Path:
    p = tmp_path / "oracles"
    p.mkdir()
    (p / "minitest.yaml").write_text(_MINI_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# DB-backed tests.
# ---------------------------------------------------------------------------


class TestIngestPaper:
    def test_creates_ref_and_blocks(self, store: Store, yaml_dir: Path) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        stats = ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        assert stats == {
            "created": 1,
            "replaced": 0,
            "chunks": 2,
            "skipped": 0,
            "errors": 0,
        }

        ref = store.get_ref(kind="oracle", id="minitest")
        assert ref is not None
        assert ref.title == "Mini Test"
        assert ref.provider is None
        assert ref.meta["tradition"] == "minitest"
        assert "ingested_at" in ref.meta

        blocks = store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 2
        assert blocks[0].text.startswith("The first lesson.")
        assert "_source_: test/1" in blocks[0].text
        assert blocks[0].meta["section_path"] == ["First entry"]
        assert blocks[1].meta["lang"] == "zh"

    def test_applies_open_tags_only(self, store: Store, yaml_dir: Path) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        ref = store.get_ref(kind="oracle", id="minitest")
        assert ref is not None
        tags = sorted(str(t) for t in store.tags_for(ref.id))
        # YAML asks for [mini, sample]; ingest also adds 'built-in'.
        assert tags == ["built-in", "mini", "sample"]
        # Every tag must be open-namespaced (oracle disallows closed axes).
        for tag in store.tags_for(ref.id):
            assert tag.namespace == "open"

    def test_skip_existing_default(self, store: Store, yaml_dir: Path) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        first = ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        assert first["created"] == 1

        second = ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        assert second == {
            "created": 0,
            "replaced": 0,
            "chunks": 0,
            "skipped": 1,
            "errors": 0,
        }

    def test_overwrite_replaces_blocks(self, store: Store, yaml_dir: Path) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        # Mutate the YAML (drop one entry) and re-ingest with overwrite.
        (yaml_dir / "minitest.yaml").write_text(
            textwrap.dedent(
                """\
                slug: minitest
                title: Mini Test (v2)
                tags: [mini]
                entries:
                  - title: Only entry
                    body: |
                      Replaced.
                """
            ),
            encoding="utf-8",
        )

        stats = ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
            overwrite=True,
        )
        assert stats == {
            "created": 0,
            "replaced": 1,
            "chunks": 1,
            "skipped": 0,
            "errors": 0,
        }
        ref = store.get_ref(kind="oracle", id="minitest")
        assert ref is not None
        assert ref.title == "Mini Test (v2)"
        blocks = store.list_blocks_for_ref(ref.id)
        assert len(blocks) == 1
        assert blocks[0].text.startswith("Replaced.")

    def test_dry_run_writes_nothing(self, store: Store, yaml_dir: Path) -> None:
        # dry_run skips both DB writes and embedder calls; passing a
        # null embedder must therefore be safe.
        stats = ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=None,
            dry_run=True,
        )
        assert stats == {
            "created": 1,  # would-create
            "replaced": 0,
            "chunks": 2,
            "skipped": 0,
            "errors": 0,
        }
        # No ref written.
        assert store.get_ref(kind="oracle", id="minitest") is None

    def test_handler_round_trip(self, store: Store, yaml_dir: Path) -> None:
        """End-to-end: ingest then read via OracleHandler."""
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_paper(
            yaml_dir / "minitest.yaml",
            store=store,
            embedder=embedder,
        )
        h = OracleHandler(store=store)
        resp = h.get(id="minitest")
        body = resp.body
        assert "oracle minitest" in body
        assert "Mini Test" in body
        assert "The first lesson." in body
        assert "The second lesson." in body


class TestIngestDirectory:
    def test_aggregates_per_file(self, store: Store, tmp_path: Path) -> None:
        d = tmp_path / "many"
        d.mkdir()
        (d / "a.yaml").write_text(
            textwrap.dedent(
                """\
                slug: a
                title: A
                tags: [a]
                entries:
                  - title: a1
                    body: hello A
                """
            ),
            encoding="utf-8",
        )
        (d / "b.yaml").write_text(
            textwrap.dedent(
                """\
                slug: b
                title: B
                tags: [b]
                entries:
                  - title: b1
                    body: hello B
                  - title: b2
                    body: world B
                """
            ),
            encoding="utf-8",
        )
        embedder = MockEmbedder(dim=store.embedding_dim())
        agg = ingest_directory(d, store=store, embedder=embedder)
        assert agg["files"] == 2
        assert agg["created"] == 2
        assert agg["chunks"] == 3
        assert agg["errors"] == 0
        assert set(agg["per_file"].keys()) == {"a.yaml", "b.yaml"}

    def test_empty_dir_raises(self, store: Store, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            ingest_directory(empty, store=store, embedder=None)

    def test_invalid_yaml_records_error(self, store: Store, tmp_path: Path) -> None:
        d = tmp_path / "bad"
        d.mkdir()
        # Missing 'entries' key -> ValueError raised at parse time.
        (d / "broken.yaml").write_text("slug: x\ntitle: X\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required key"):
            ingest_directory(d, store=store, embedder=None)


class TestBundledIngest:
    def test_iching_round_trip(self, store: Store) -> None:
        """The shipped iching YAML must ingest cleanly end-to-end."""
        bundled = bundled_oracle_dir()
        assert bundled is not None
        embedder = MockEmbedder(dim=store.embedding_dim())
        stats = ingest_paper(
            bundled / "iching.yaml",
            store=store,
            embedder=embedder,
        )
        assert stats["errors"] == 0
        assert stats["created"] == 1
        # 64 hexagrams in the unified system.
        assert stats["chunks"] == 64
        ref = store.get_ref(kind="oracle", id="iching")
        assert ref is not None
        assert ref.title == "I-Ching"
