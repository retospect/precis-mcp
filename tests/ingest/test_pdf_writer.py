"""Smoke tests for ``precis.ingest.pdf_writer``.

Covers the four observable shapes of :func:`patch_pdf_metadata`:

* happy path — fields get written, pre/post hashes differ;
* idempotency — second patch with the same target is a no-op;
* no-op on empty input — ``PatchInfo()`` with no fields skips early;
* env off-switch — ``PRECIS_PATCH_PDFS=0`` returns ``"disabled"``.

We don't test signed-PDF skip (the feature is intentionally not
implemented yet — see ADR 0014) or DRM detection (constructing an
encrypted fixture is more setup than the test is worth at this
stage).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.ingest.pdf_writer import (
    PatchInfo,
    PatchOutcome,
    patch_pdf_metadata,
)

fitz = pytest.importorskip("fitz")


def _make_pdf(path: Path, *, title: str = "", author: str = "") -> None:
    """Create a 1-page PDF at ``path`` with optional starting metadata."""
    doc = fitz.open()
    doc.new_page()
    if title or author:
        doc.set_metadata({"title": title, "author": author})
    doc.save(str(path))
    doc.close()


def _read_meta(path: Path) -> dict[str, str]:
    doc = fitz.open(str(path))
    try:
        return dict(doc.metadata or {})
    finally:
        doc.close()


class TestHappyPath:
    def test_writes_title_author_doi_and_returns_both_hashes(
        self, tmp_path: Path
    ) -> None:
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf)

        outcome = patch_pdf_metadata(
            pdf,
            PatchInfo(
                title="Attention Is All You Need",
                authors=["Vaswani", "Shazeer", "Parmar"],
                doi="10.48550/arXiv.1706.03762",
                arxiv_id="1706.03762",
            ),
        )

        assert isinstance(outcome, PatchOutcome)
        assert outcome.skipped_reason is None
        assert outcome.post_hash is not None
        assert outcome.post_hash != outcome.pre_hash
        assert outcome.post_size is not None
        assert outcome.post_size == pdf.stat().st_size

        meta = _read_meta(pdf)
        assert meta.get("title") == "Attention Is All You Need"
        assert "Vaswani" in (meta.get("author") or "")
        assert "doi:10.48550/arXiv.1706.03762" in (meta.get("keywords") or "")
        assert "10.48550/arXiv.1706.03762" in (meta.get("subject") or "")


class TestIdempotency:
    def test_second_patch_with_same_target_is_noop(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf)
        info = PatchInfo(title="Some Paper", authors=["Smith"], doi="10.1/abc")

        first = patch_pdf_metadata(pdf, info)
        assert first.skipped_reason is None
        assert first.post_hash is not None
        first_post = first.post_hash

        second = patch_pdf_metadata(pdf, info)
        assert second.skipped_reason == "noop"
        assert second.post_hash is None
        # The on-disk file is untouched by the second call.
        assert second.pre_hash == first_post


class TestNoopPaths:
    def test_empty_patchinfo_is_noop(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf, title="Stays", author="Same")

        outcome = patch_pdf_metadata(pdf, PatchInfo())
        assert outcome.skipped_reason == "noop"
        assert outcome.post_hash is None

        meta = _read_meta(pdf)
        assert meta.get("title") == "Stays"
        assert meta.get("author") == "Same"


class TestOffSwitch:
    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_disabled_via_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv("PRECIS_PATCH_PDFS", val)
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf)

        outcome = patch_pdf_metadata(pdf, PatchInfo(title="Would-be patched"))
        assert outcome.skipped_reason == "disabled"
        assert outcome.post_hash is None

        # File is untouched.
        meta = _read_meta(pdf)
        assert meta.get("title") in ("", None)

    def test_enabled_by_default_when_var_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_PATCH_PDFS", raising=False)
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf)

        outcome = patch_pdf_metadata(pdf, PatchInfo(title="Patched"))
        assert outcome.skipped_reason is None
        assert outcome.post_hash is not None


class TestPreHashReuse:
    def test_supplied_pre_hash_is_returned_verbatim(self, tmp_path: Path) -> None:
        pdf = tmp_path / "paper.pdf"
        _make_pdf(pdf)
        sentinel = "deadbeef" * 8  # 64 hex chars; never the real sha
        outcome = patch_pdf_metadata(pdf, PatchInfo(title="t"), pre_hash=sentinel)
        assert outcome.pre_hash == sentinel
