"""Bundle ingest tests: parsing, slug minting, end-to-end."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput, Upstream
from precis.ingest import (
    author_strings,
    classify_density,
    fill_embeddings,
    mint_paper_slug,
    parse_bundle,
    read_bundle,
)
from precis.store import Store

# ---------------------------------------------------------------------------
# Fixture bundle factory
# ---------------------------------------------------------------------------


def _bundle_dict(
    *,
    title: str = "Nitrate reduction on copper electrodes",
    authors: Any | None = None,
    year: int | None = 2020,
    doi: str | None = "10.1/test-doi",
    abstract: str = "An abstract paragraph here.",
    journal: str = "Nature",
    arxiv_id: str | None = None,
    bundle_slug: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "header": {
            "title": title,
            "authors": authors if authors is not None else [{"name": "Wang, Q."}],
            "year": year,
            "doi": doi,
            "abstract": abstract,
            "journal": journal,
            "arxiv_id": arxiv_id,
            "slug": bundle_slug,
            "pdf_hash": "deadbeef",
            "source": "manual",
        },
        "blocks": blocks
        or [
            {"text": "Introduction. " + "x " * 30},
            {"text": "Methods section with table 1 and 95% confidence."},
            {"text": "Numbers like 1.23, 2.34, 3.45 across 6 lines."},
            {"text": "Conclusion."},
        ],
        "enrichment_meta": {"profiles": ["mock"]},
    }


def _write_bundle(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "fixture.acatome"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Bundle parsing
# ---------------------------------------------------------------------------


class TestReadBundle:
    def test_round_trip(self, tmp_path: Path) -> None:
        data = _bundle_dict()
        path = _write_bundle(tmp_path, data)
        loaded = read_bundle(path)
        assert loaded["header"]["title"] == data["header"]["title"]

    def test_missing_file_raises_upstream(self, tmp_path: Path) -> None:
        with pytest.raises(Upstream):
            read_bundle(tmp_path / "nope.acatome")

    def test_corrupt_gzip_raises_upstream(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.acatome"
        bad.write_text("not a gzip file at all")
        with pytest.raises(Upstream):
            read_bundle(bad)


class TestParseBundle:
    def test_basic(self) -> None:
        parsed = parse_bundle(_bundle_dict(), embedding_dim=1024)
        assert parsed.title == "Nitrate reduction on copper electrodes"
        assert parsed.year == 2020
        assert parsed.doi == "10.1/test-doi"
        assert parsed.journal == "Nature"
        assert len(parsed.blocks) == 4

    def test_drops_empty_blocks(self) -> None:
        data = _bundle_dict(
            blocks=[
                {"text": "x"},
                {"text": ""},
                {"text": None},
                {"text": "y"},
            ]
        )
        parsed = parse_bundle(data, embedding_dim=1024)
        assert [b.text for b in parsed.blocks] == ["x", "y"]

    def test_density_classified_per_block(self) -> None:
        data = _bundle_dict(
            blocks=[
                {"text": "short"},  # sparse (< 20 tokens)
                {"text": " ".join(["word"] * 50)},  # medium
                {"text": " ".join(["word", "5"] * 20)},  # digit-heavy → dense
            ]
        )
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.blocks[0].density == "sparse"
        assert parsed.blocks[1].density == "medium"
        assert parsed.blocks[2].density == "dense"

    def test_uses_bundled_embedding_when_dim_matches(self) -> None:
        vec = [0.5] * 1024
        data = _bundle_dict(blocks=[{"text": "x", "embeddings": {"mock": vec}}])
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.blocks[0].embedding == vec

    def test_drops_embedding_when_dim_mismatch(self) -> None:
        vec = [0.5] * 768
        data = _bundle_dict(blocks=[{"text": "x", "embeddings": {"old": vec}}])
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.blocks[0].embedding is None

    def test_year_string_coerced(self) -> None:
        parsed = parse_bundle(_bundle_dict(year="2024"), embedding_dim=1024)  # type: ignore[arg-type]
        assert parsed.year == 2024

    def test_year_garbage_becomes_none(self) -> None:
        parsed = parse_bundle(
            _bundle_dict(year="N/A"),  # type: ignore[arg-type]
            embedding_dim=1024,
        )
        assert parsed.year is None

    def test_empty_title_raises(self) -> None:
        data = _bundle_dict(title="")
        with pytest.raises(BadInput, match="empty title"):
            parse_bundle(data, embedding_dim=1024)

    def test_bad_shape_raises(self) -> None:
        with pytest.raises(BadInput):
            parse_bundle({"header": "not a dict", "blocks": []}, embedding_dim=1024)


class TestProviderMapping:
    def test_embedded_maps_to_manual(self) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "embedded"
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "manual"

    def test_crossref_passes_through(self) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "crossref"
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "crossref"

    def test_arxiv_passes_through(self) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "arxiv"
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "arxiv"

    def test_s2_alias(self) -> None:
        for src in ("s2", "semantic_scholar", "semantic-scholar"):
            data = _bundle_dict()
            data["header"]["source"] = src
            parsed = parse_bundle(data, embedding_dim=1024)
            assert parsed.provider == "s2", src

    def test_unknown_falls_back_to_manual(self) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "totally-made-up"
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "manual"

    def test_missing_source(self) -> None:
        data = _bundle_dict()
        data["header"].pop("source", None)
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "manual"

    def test_case_insensitive(self) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "Crossref"
        parsed = parse_bundle(data, embedding_dim=1024)
        assert parsed.provider == "crossref"

    def test_ingest_writes_mapped_provider(self, tmp_path: Path, store: Store) -> None:
        data = _bundle_dict()
        data["header"]["source"] = "crossref"
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        result = store.ingest_bundle(path, embedder=e)
        ref = store.get_ref(kind="paper", id=result.slug)
        assert ref is not None
        assert ref.provider == "crossref"


class TestAuthorStrings:
    def test_dict_form(self) -> None:
        parsed = parse_bundle(
            _bundle_dict(authors=[{"name": "Smith, J."}, {"name": "Li, X."}]),
            embedding_dim=1024,
        )
        assert author_strings(parsed.authors) == ["Smith, J.", "Li, X."]

    def test_string_form_semicolon(self) -> None:
        parsed = parse_bundle(
            _bundle_dict(authors="Smith, J.; Li, X."),
            embedding_dim=1024,
        )
        assert author_strings(parsed.authors) == ["Smith, J.", "Li, X."]


# ---------------------------------------------------------------------------
# Density classifier
# ---------------------------------------------------------------------------


class TestClassifyDensity:
    def test_short_is_sparse(self) -> None:
        assert classify_density("hello world") == "sparse"

    def test_long_prose_is_medium(self) -> None:
        assert classify_density(" ".join(["word"] * 80)) == "medium"

    def test_digit_heavy_is_dense(self) -> None:
        assert classify_density(" ".join(["x", "5"] * 30)) == "dense"

    def test_many_newlines_is_sparse(self) -> None:
        text = "\n".join(["a"] * 30)
        assert classify_density(text) == "sparse"

    def test_empty_is_sparse(self) -> None:
        assert classify_density("") == "sparse"


# ---------------------------------------------------------------------------
# Slug minting helper
# ---------------------------------------------------------------------------


class TestMintPaperSlug:
    def test_uses_bundle_slug_when_free(self) -> None:
        parsed = parse_bundle(
            _bundle_dict(bundle_slug="customslug"), embedding_dim=1024
        )
        slug = mint_paper_slug(parsed, lambda s: False)
        assert slug == "customslug"

    def test_falls_back_when_bundle_slug_taken(self) -> None:
        parsed = parse_bundle(
            _bundle_dict(bundle_slug="customslug", year=2020),
            embedding_dim=1024,
        )
        # bundle_slug is taken → mint from authors/year/title
        slug = mint_paper_slug(parsed, lambda s: s == "customslug")
        assert slug.startswith("wang2020")

    def test_collision_suffix(self) -> None:
        parsed = parse_bundle(_bundle_dict(bundle_slug=None), embedding_dim=1024)
        existing = {"wang2020nitrate"}
        slug = mint_paper_slug(parsed, lambda s: s in existing)
        assert slug == "wang2020nitrate-2"


# ---------------------------------------------------------------------------
# Embedding fill
# ---------------------------------------------------------------------------


class TestFillEmbeddings:
    def test_keeps_existing_when_dim_matches(self) -> None:
        e = MockEmbedder(dim=8)
        parsed = parse_bundle(
            _bundle_dict(blocks=[{"text": "x", "embeddings": {"m": [0.5] * 8}}]),
            embedding_dim=8,
        )
        out = fill_embeddings(parsed.blocks, embedder=e)
        assert out[0].embedding == [0.5] * 8

    def test_fills_missing(self) -> None:
        e = MockEmbedder(dim=8)
        parsed = parse_bundle(
            _bundle_dict(blocks=[{"text": "alpha"}, {"text": "beta"}]),
            embedding_dim=8,
        )
        out = fill_embeddings(parsed.blocks, embedder=e)
        assert all(b.embedding is not None for b in out)
        assert all(len(b.embedding) == 8 for b in out)  # type: ignore[arg-type]

    def test_text_unchanged(self) -> None:
        e = MockEmbedder(dim=8)
        parsed = parse_bundle(_bundle_dict(blocks=[{"text": "abc"}]), embedding_dim=8)
        out = fill_embeddings(parsed.blocks, embedder=e)
        assert out[0].text == "abc"


# ---------------------------------------------------------------------------
# End-to-end: Store.ingest_bundle
# ---------------------------------------------------------------------------


class TestIngestBundle:
    def test_end_to_end(self, tmp_path: Path, store: Store) -> None:
        path = _write_bundle(tmp_path, _bundle_dict())
        e = MockEmbedder(dim=1024)
        result = store.ingest_bundle(path, embedder=e)

        assert result.inserted is True
        assert result.block_count == 4
        assert result.slug.startswith("wang2020")

        # Ref present
        ref = store.get_ref(kind="paper", id=result.slug)
        assert ref is not None
        assert ref.title == "Nitrate reduction on copper electrodes"
        assert ref.meta["doi"] == "10.1/test-doi"
        # Provenance tag applied
        tags = store.tags_for(ref.id)
        src_tags = [t for t in tags if t.namespace == "closed" and t.prefix == "SRC"]
        assert len(src_tags) == 1
        assert src_tags[0].value == "bundle"

        # Blocks present, with embeddings + density
        blocks = store.list_blocks_for_ref(ref.id, with_embedding=True)
        assert len(blocks) == 4
        assert all(b.embedding is not None for b in blocks)
        assert all(len(b.embedding) == 1024 for b in blocks)  # type: ignore[arg-type]
        assert all(b.density in ("sparse", "medium", "dense") for b in blocks)

    def test_idempotent_on_doi(self, tmp_path: Path, store: Store) -> None:
        path = _write_bundle(tmp_path, _bundle_dict())
        e = MockEmbedder(dim=1024)
        first = store.ingest_bundle(path, embedder=e)
        second = store.ingest_bundle(path, embedder=e)
        assert first.inserted is True
        assert second.inserted is False
        assert second.ref_id == first.ref_id
        assert second.slug == first.slug
        # Block count unchanged.
        assert store.count_blocks(first.ref_id) == first.block_count

    def test_uses_bundled_vectors_when_dim_matches(
        self, tmp_path: Path, store: Store
    ) -> None:
        # Pre-fill with a deterministic vector — verify it survives.
        vec = [0.0] * 1024
        vec[0] = 1.0
        data = _bundle_dict(blocks=[{"text": "first", "embeddings": {"mock": vec}}])
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        result = store.ingest_bundle(path, embedder=e)
        block = store.get_block(result.ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is not None
        assert abs(block.embedding[0] - 1.0) < 1e-6

    def test_re_embeds_when_dim_mismatch(self, tmp_path: Path, store: Store) -> None:
        # Bundle ships a vector of dim 768; active embedder is 1024.
        wrong_dim = [0.5] * 768
        data = _bundle_dict(
            blocks=[{"text": "first", "embeddings": {"old": wrong_dim}}]
        )
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        result = store.ingest_bundle(path, embedder=e)
        block = store.get_block(result.ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is not None
        assert len(block.embedding) == 1024

    def test_paper_without_identity_inserted(
        self, tmp_path: Path, store: Store
    ) -> None:
        # No DOI, no pdf_hash, no arxiv_id → no identity key at all.
        # Each call creates a new ref (slug suffix on collision). This
        # is the degenerate path that only hits when acatome-extract
        # fails to produce any stable key.
        data = _bundle_dict(doi=None)
        data["header"]["pdf_hash"] = None
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        first = store.ingest_bundle(path, embedder=e)
        second = store.ingest_bundle(path, embedder=e)
        assert first.inserted is True
        assert second.inserted is True
        assert first.slug != second.slug

    def test_idempotent_on_pdf_hash(self, tmp_path: Path, store: Store) -> None:
        # DOI-less bundles (text_rescue path) still carry pdf_hash,
        # which must dedupe on second ingest.
        data = _bundle_dict(doi=None)
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        first = store.ingest_bundle(path, embedder=e)
        second = store.ingest_bundle(path, embedder=e)
        assert first.inserted is True
        assert second.inserted is False
        assert second.ref_id == first.ref_id
        assert second.slug == first.slug

    def test_idempotent_on_arxiv_id(self, tmp_path: Path, store: Store) -> None:
        # Preprints without DOI still dedup on arxiv_id.
        data = _bundle_dict(doi=None, arxiv_id="2401.12345")
        data["header"]["pdf_hash"] = None
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        first = store.ingest_bundle(path, embedder=e)
        second = store.ingest_bundle(path, embedder=e)
        assert first.inserted is True
        assert second.inserted is False
        assert second.ref_id == first.ref_id

    def test_slug_collision_resolves(self, tmp_path: Path, store: Store) -> None:
        # Pre-create a ref with the slug we'd mint — unrelated to the
        # bundle, so neither pdf_hash nor DOI can dedup. Must suffix.
        cid = store.ensure_corpus("default")
        store.insert_ref(
            corpus_id=cid,
            kind="paper",
            slug="wang2020nitrate",
            title="Existing",
            meta={},
        )
        data = _bundle_dict(doi=None)
        data["header"]["pdf_hash"] = None
        path = _write_bundle(tmp_path, data)
        e = MockEmbedder(dim=1024)
        result = store.ingest_bundle(path, embedder=e)
        assert result.inserted is True
        assert result.slug == "wang2020nitrate-2"
