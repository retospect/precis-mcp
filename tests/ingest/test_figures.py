"""Tests for figure extraction and caption matching."""

from __future__ import annotations

import base64

from precis.ingest.figures import encode_image, match_figure_captions


class TestMatchFigureCaptions:
    def test_caption_merged(self):
        blocks = [
            {"type": "figure", "text": ""},
            {"type": "text", "text": "Figure 1: Error rates for surface codes."},
            {"type": "text", "text": "Some paragraph."},
        ]
        result = match_figure_captions(blocks)
        assert len(result) == 2
        assert result[0]["text"] == "Figure 1: Error rates for surface codes."
        assert result[1]["text"] == "Some paragraph."

    def test_no_caption(self):
        blocks = [
            {"type": "figure", "text": ""},
            {"type": "text", "text": "This is not a caption."},
        ]
        result = match_figure_captions(blocks)
        assert len(result) == 2
        assert result[0]["text"] == ""

    def test_fig_abbreviation(self):
        blocks = [
            {"type": "figure", "text": ""},
            {"type": "text", "text": "Fig. 3 - Comparison of threshold values."},
        ]
        result = match_figure_captions(blocks)
        assert len(result) == 1
        assert "Comparison" in result[0]["text"]

    def test_no_figures(self):
        blocks = [
            {"type": "text", "text": "Just text."},
            {"type": "text", "text": "More text."},
        ]
        result = match_figure_captions(blocks)
        assert len(result) == 2

    def test_figure_at_end(self):
        blocks = [
            {"type": "text", "text": "Paragraph."},
            {"type": "figure", "text": ""},
        ]
        result = match_figure_captions(blocks)
        assert len(result) == 2
        assert result[1]["text"] == ""


class TestEncodeImage:
    def test_encode_png(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
        data, mime = encode_image(img)
        assert mime == "image/png"
        assert base64.b64decode(data) == b"\x89PNG\r\n\x1a\nfakedata"

    def test_encode_jpeg(self, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0fakedata")
        data, mime = encode_image(img)
        assert mime == "image/jpeg"
