"""Tests for figure handling in paper handler and store."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from acatome_store.store import Store

# Skip entire module if store doesn't have figure methods (unreleased)
pytestmark = pytest.mark.skipif(
    not hasattr(Store, "get_figures"),
    reason="acatome-store does not yet have figure methods",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bundle(tmp_path: Path, slug: str, figures: list[dict]) -> Path:
    """Create a minimal .acatome bundle with figure blocks."""
    blocks = []
    for i, fig in enumerate(figures):
        block = {
            "node_id": f"test:{slug}-p00-{i:03d}",
            "page": fig.get("page", 0),
            "type": "figure",
            "text": fig.get("caption", ""),
            "section_path": [],
            "bbox": None,
            "embeddings": {},
            "summaries": {},
        }
        if fig.get("image_bytes"):
            block["image_base64"] = base64.b64encode(fig["image_bytes"]).decode("ascii")
            block["image_mime"] = fig.get("mime", "image/png")
        blocks.append(block)

    data = {
        "header": {
            "paper_id": f"doi:10.0000/{slug}",
            "slug": slug,
            "title": f"Test Paper {slug}",
            "authors": [{"name": "Test, Author"}],
            "year": 2024,
            "doi": f"10.0000/{slug}",
            "pdf_hash": slug.ljust(64, "0"),
            "page_count": 5,
            "source": "test",
            "verified": True,
            "verify_warnings": [],
            "extracted_at": "2024-01-01T00:00:00+00:00",
        },
        "blocks": blocks,
        "enrichment_meta": None,
    }
    bundle_path = tmp_path / f"{slug}.acatome"
    with gzip.open(bundle_path, "wt") as f:
        json.dump(data, f)
    return bundle_path


@pytest.fixture
def store(tmp_path):
    """Create a SQLite store for testing."""
    from acatome_store.config import StoreConfig

    cfg = StoreConfig(store_path=tmp_path / "store")
    return Store(config=cfg)


@pytest.fixture
def store_with_figures(store, tmp_path):
    """Store with a paper that has labelled figures + images."""
    bundle = _make_bundle(
        tmp_path,
        "smith2024figs",
        [
            {
                "caption": "Figure 1. Schematic of the reaction mechanism.",
                "page": 1,
                "image_bytes": b"\x89PNG fake image 1",
            },
            {
                "caption": "Fig. 2: XRD patterns of the catalyst.",
                "page": 2,
                "image_bytes": b"\x89PNG fake image 2",
            },
            {
                "caption": "Scheme 3. Proposed catalytic cycle.",
                "page": 3,
                "image_bytes": b"\x89PNG fake image 3",
            },
            {
                "caption": "",  # No caption
                "page": 4,
                "image_bytes": b"\x89PNG unlabelled image",
            },
        ],
    )
    store.ingest(bundle)
    return store


# ---------------------------------------------------------------------------
# Store.get_figures
# ---------------------------------------------------------------------------


class TestGetFigures:
    def test_figure_numbers_from_captions(self, store_with_figures):
        figs = store_with_figures.get_figures("smith2024figs")
        assert len(figs) == 4
        nums = [f["fig_num"] for f in figs]
        # Fig 1, Fig 2, Scheme 3 parsed; unlabelled gets auto-assigned 4
        assert nums == [1, 2, 3, 4]

    def test_captions_preserved(self, store_with_figures):
        figs = store_with_figures.get_figures("smith2024figs")
        assert "Schematic" in figs[0]["caption"]
        assert "XRD" in figs[1]["caption"]
        assert "catalytic cycle" in figs[2]["caption"]
        assert figs[3]["caption"] == ""

    def test_no_figures(self, store, tmp_path):
        bundle = _make_bundle(tmp_path, "nofigs2024test", [])
        store.ingest(bundle)
        # Bundle has no figure blocks, but header still creates blocks
        figs = store.get_figures("nofigs2024test")
        assert figs == []

    def test_auto_numbering_skips_used(self, store, tmp_path):
        """Auto-numbering should skip numbers already used by labelled figs."""
        bundle = _make_bundle(
            tmp_path,
            "autonums2024x",
            [
                {"caption": "Figure 2. Something.", "page": 0, "image_bytes": b"img"},
                {"caption": "", "page": 1, "image_bytes": b"img"},
                {"caption": "", "page": 2, "image_bytes": b"img"},
            ],
        )
        store.ingest(bundle)
        figs = store.get_figures("autonums2024x")
        nums = [f["fig_num"] for f in figs]
        # Fig 2 is explicit; unlabelled get 1 and 3
        assert nums == [2, 1, 3]


# ---------------------------------------------------------------------------
# Store.get_figure_image
# ---------------------------------------------------------------------------


class TestGetFigureImage:
    def test_returns_image_bytes(self, store_with_figures):
        result = store_with_figures.get_figure_image("smith2024figs", 1)
        assert result is not None
        assert result["image_bytes"] == b"\x89PNG fake image 1"
        assert result["image_ext"] == ".png"
        assert result["fig_num"] == 1

    def test_different_figure(self, store_with_figures):
        result = store_with_figures.get_figure_image("smith2024figs", 2)
        assert result is not None
        assert b"fake image 2" in result["image_bytes"]

    def test_nonexistent_figure(self, store_with_figures):
        result = store_with_figures.get_figure_image("smith2024figs", 99)
        assert result is None

    def test_nonexistent_paper(self, store_with_figures):
        result = store_with_figures.get_figure_image("nonexistent", 1)
        assert result is None


# ---------------------------------------------------------------------------
# Paper handler figure dispatch
# ---------------------------------------------------------------------------


class TestPaperHandlerFigures:
    """Test the precis paper handler figure views (mocked store)."""

    def _make_handler(self):
        from precis.handlers.paper import PaperHandler

        return PaperHandler()

    def _mock_store(self, figures=None, image_result=None):
        store = MagicMock()
        store.get.return_value = {
            "slug": "test2024paper",
            "title": "Test Paper",
            "ref_id": 1,
        }
        store.get_figures.return_value = figures or []
        store.get_figure_image.return_value = image_result
        return store

    def test_list_figures(self):
        handler = self._make_handler()
        store = self._mock_store(
            figures=[
                {
                    "fig_num": 1,
                    "caption": "Figure 1. Foo",
                    "page": 1,
                    "block_index": 0,
                    "node_id": "n1",
                },
                {
                    "fig_num": 2,
                    "caption": "Fig. 2: Bar",
                    "page": 3,
                    "block_index": 5,
                    "node_id": "n2",
                },
            ]
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview=None,
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "2 figure(s)" in result
        assert "fig 1" in result
        assert "fig 2" in result

    def test_figure_overview(self):
        handler = self._make_handler()
        store = self._mock_store(
            figures=[
                {
                    "fig_num": 3,
                    "caption": "Figure 3. Important result",
                    "page": 5,
                    "block_index": 10,
                    "node_id": "n3",
                },
            ]
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview="3",
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "fig 3" in result
        assert "Important result" in result
        assert "/fig/3/image" in result

    def test_figure_legend(self):
        handler = self._make_handler()
        store = self._mock_store(
            figures=[
                {
                    "fig_num": 1,
                    "caption": "Figure 1. The catalyst structure",
                    "page": 1,
                    "block_index": 0,
                    "node_id": "n1",
                },
            ]
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview="1/legend",
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "catalyst structure" in result

    def test_figure_image(self):
        handler = self._make_handler()
        fake_bytes = b"\x89PNG test"
        store = self._mock_store(
            image_result={
                "fig_num": 1,
                "caption": "Fig 1.",
                "page": 1,
                "image_bytes": fake_bytes,
                "image_ext": ".png",
            }
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview="1/image",
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "data:image/png;base64," in result
        assert str(len(fake_bytes)) in result

    def test_figure_export(self, tmp_path):
        handler = self._make_handler()
        handler._FIGURES_DIR = str(tmp_path / "figures")
        fake_bytes = b"\x89PNG export test"
        store = self._mock_store(
            image_result={
                "fig_num": 2,
                "caption": "Fig 2. Exported",
                "page": 2,
                "image_bytes": fake_bytes,
                "image_ext": ".png",
            }
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview="2/image/export",
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "✓ Exported" in result
        out_file = Path(handler._FIGURES_DIR) / "test2024paper_fig2.png"
        assert out_file.exists()
        assert out_file.read_bytes() == fake_bytes

    def test_figure_not_found(self):
        handler = self._make_handler()
        store = self._mock_store(
            figures=[
                {
                    "fig_num": 1,
                    "caption": "Fig 1.",
                    "page": 1,
                    "block_index": 0,
                    "node_id": "n1",
                },
            ]
        )
        with patch("precis.handlers._ref_base._get_store", return_value=store):
            result = handler.read(
                path="test2024paper",
                selector=None,
                view="fig",
                subview="99",
                query="",
                summarize=False,
                depth=0,
                page=1,
            )
        assert "not found" in result
        assert "Available: 1" in result


# ---------------------------------------------------------------------------
# URI parser: deep subview
# ---------------------------------------------------------------------------


class TestURIDeepSubview:
    def test_fig_image_export(self):
        from precis.uri import parse

        p = parse("paper:slug/fig/3/image/export")
        assert p.view == "fig"
        assert p.subview == "3/image/export"

    def test_fig_legend(self):
        from precis.uri import parse

        p = parse("paper:slug/fig/1/legend")
        assert p.view == "fig"
        assert p.subview == "1/legend"

    def test_fig_image(self):
        from precis.uri import parse

        p = parse("paper:slug/fig/5/image")
        assert p.view == "fig"
        assert p.subview == "5/image"

    def test_fig_number_only(self):
        from precis.uri import parse

        p = parse("paper:slug/fig/3")
        assert p.view == "fig"
        assert p.subview == "3"

    def test_two_level_subview_unchanged(self):
        from precis.uri import parse

        p = parse("paper:slug/cite/bib")
        assert p.view == "cite"
        assert p.subview == "bib"
