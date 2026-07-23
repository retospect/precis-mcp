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


def test_source_format_defaults_to_pdf(tmp_path: Path) -> None:
    # A PDF-only fetch: source_format defaults to 'pdf', no companion.
    pdf = tmp_path / "p.pdf"
    write_sidecar(pdf, ref_id=1, identifiers={"doi": "10.1/p"}, source="fetcher:arxiv")
    got = read_sidecar(pdf)
    assert got is not None
    assert got.source_format == "pdf"
    assert got.companion_pdf is None


def test_markup_source_format_and_companion_roundtrip(tmp_path: Path) -> None:
    markup = tmp_path / "foo.xml"
    write_sidecar(
        markup,
        ref_id=42,
        identifiers={"doi": "10.1/foo"},
        source="fetcher:europepmc_jats",
        source_format="jats",
        companion_pdf="foo.pdf",
    )
    got = read_sidecar(markup)
    assert got is not None
    assert got.source_format == "jats"
    assert got.companion_pdf == "foo.pdf"


def test_unknown_source_format_falls_back_to_pdf(tmp_path: Path) -> None:
    markup = tmp_path / "q.xml"
    write_sidecar(
        markup,
        ref_id=2,
        identifiers={"doi": "10.1/q"},
        source="fetcher:x",
        source_format="not-a-format",
    )
    got = read_sidecar(markup)
    assert got is not None
    assert got.source_format == "pdf"


def test_legacy_sidecar_without_format_decodes_as_pdf(tmp_path: Path) -> None:
    # A sidecar written before the markup-first work has neither key.
    pdf = tmp_path / "legacy.pdf"
    sidecar_path(pdf).write_text(
        '{"ref_id": 5, "identifiers": {"doi": "10.1/l"}, "source": "fetcher:s2"}',
        encoding="utf-8",
    )
    got = read_sidecar(pdf)
    assert got is not None
    assert got.ref_id == 5
    assert got.source_format == "pdf"
    assert got.companion_pdf is None


def test_printable_only_defaults_false(tmp_path: Path) -> None:
    pdf = tmp_path / "plain.pdf"
    write_sidecar(pdf, ref_id=1, identifiers={"doi": "10.1/p"}, source="fetcher:arxiv")
    got = read_sidecar(pdf)
    assert got is not None
    assert got.printable_only is False


def test_printable_only_roundtrips(tmp_path: Path) -> None:
    # gr161905: a PDF fetched alongside a markup trigger is tagged
    # printable_only so the watcher never runs Marker on it.
    pdf = tmp_path / "companion.pdf"
    write_sidecar(
        pdf,
        ref_id=42,
        identifiers={"doi": "10.1/foo"},
        source="fetcher:elsevier",
        printable_only=True,
    )
    got = read_sidecar(pdf)
    assert got is not None
    assert got.printable_only is True


def test_legacy_sidecar_without_printable_only_decodes_as_false(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "legacy2.pdf"
    sidecar_path(pdf).write_text(
        '{"ref_id": 5, "identifiers": {}, "source": "fetcher:s2"}',
        encoding="utf-8",
    )
    got = read_sidecar(pdf)
    assert got is not None
    assert got.printable_only is False
