"""Unit tests for the OA-fetch acquisition sidecar (no DB)."""

from __future__ import annotations

from pathlib import Path

from precis.ingest.fetch_sidecar import (
    SIDECAR_SUFFIX,
    clear_sidecar,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)


def test_roundtrip(tmp_path: Path) -> None:
    pdf = tmp_path / "continuous83.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    write_sidecar(
        pdf,
        ref_id=50698,
        identifiers={"doi": "10.1016/0022-3093(83)90424-6", "cite_key": "continuous83"},
        source="fetcher:elsevier",
    )
    got = read_sidecar(pdf)
    assert got is not None
    assert got.ref_id == 50698
    assert got.identifiers["doi"] == "10.1016/0022-3093(83)90424-6"
    assert got.identifiers["cite_key"] == "continuous83"
    assert got.source == "fetcher:elsevier"


def test_sidecar_suffix_is_not_pdf(tmp_path: Path) -> None:
    # Critical: the watcher's ``_is_pdf`` / backfill ``*.pdf`` glob must
    # never treat a sidecar as a droppable file.
    p = sidecar_path(tmp_path / "foo.pdf")
    assert p.name == "foo.pdf" + SIDECAR_SUFFIX
    assert p.suffix == ".json"
    assert not p.name.endswith(".pdf")


def test_empty_identifiers_are_dropped(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    write_sidecar(
        pdf,
        ref_id=1,
        identifiers={"doi": "10.1/x", "arxiv": "", "s2": "", "cite_key": "x24"},
        source="fetcher:s2",
    )
    got = read_sidecar(pdf)
    assert got is not None
    assert got.identifiers == {"doi": "10.1/x", "cite_key": "x24"}


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path / "nope.pdf") is None


def test_read_malformed_returns_none(tmp_path: Path) -> None:
    pdf = tmp_path / "bad.pdf"
    sidecar_path(pdf).write_text("{not json", encoding="utf-8")
    assert read_sidecar(pdf) is None

    # Valid JSON but missing the required ref_id → also treated as absent.
    sidecar_path(pdf).write_text('{"source": "fetcher:s2"}', encoding="utf-8")
    assert read_sidecar(pdf) is None


def test_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    write_sidecar(pdf, ref_id=7, identifiers={"doi": "10.1/a"}, source="fetcher:s2")
    # Only the final sidecar exists — no ``.tmp`` litter.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_clear_is_idempotent(tmp_path: Path) -> None:
    pdf = tmp_path / "c.pdf"
    write_sidecar(pdf, ref_id=9, identifiers={"doi": "10.1/c"}, source="fetcher:s2")
    assert sidecar_path(pdf).exists()
    clear_sidecar(pdf)
    assert not sidecar_path(pdf).exists()
    clear_sidecar(pdf)  # no-op, no raise
