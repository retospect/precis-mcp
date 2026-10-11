"""EndNote record: volume / number / pages ride along when the source has them
(the same ``meta.volume`` / ``number`` / ``pages`` keys the LaTeX ``.bib`` reads)."""

from __future__ import annotations

from precis.export.endnote import build_record


def _source(**extra: object) -> dict[str, object]:
    return {
        "kind": "paper",
        "tag": "smith2024",
        "rec_number": 1,
        "authors": [{"given": "J", "family": "Smith"}],
        "title": "T",
        "year": 2024,
        "journal": "J. Test",
        **extra,
    }


def test_locators_emitted_when_present() -> None:
    rec = build_record(_source(volume="15", number="7", pages="12-19"))
    assert "<volume>15</volume>" in rec
    assert "<number>7</number>" in rec
    assert "<pages>12-19</pages>" in rec


def test_locators_omitted_when_absent() -> None:
    rec = build_record(_source(volume=None, number=None, pages=None))
    assert "<volume>" not in rec and "<number>" not in rec and "<pages>" not in rec


def test_build_record_venue_precedence_matches_bib_exporters() -> None:
    from precis.export.endnote import build_record

    base = {"kind": "paper", "rec_number": 1, "title": "T"}
    rec = build_record({**base, "container_title": "Nature"})
    assert "<secondary-title>Nature</secondary-title>" in rec
    rec = build_record({**base, "venue": "V", "journal": "J"})
    assert "<secondary-title>V</secondary-title>" in rec
    assert "secondary-title" not in build_record(base)
