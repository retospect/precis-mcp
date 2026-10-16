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


def test_record_maps_bib_type_to_ref_type() -> None:
    """``bib_type`` picks the EndNote ref-type (Report = 27, Book = 6,
    Book Section = 5) — every paper used to default to Journal Article."""
    for bib, name, code in (
        ("article", "Journal Article", 17),
        ("book", "Book", 6),
        ("incollection", "Book Section", 5),
        ("inproceedings", "Conference Proceedings", 10),
        ("report", "Report", 27),
        ("thesis", "Thesis", 32),
    ):
        rec = build_record({"kind": "paper", "bib_type": bib, "rec_number": 1})
        assert f'<ref-type name="{name}">{code}</ref-type>' in rec, bib
    # kind fallback: patent/datasheet map directly; unknown kinds stay journal.
    assert '"Patent">25' in build_record({"kind": "patent", "rec_number": 1})
    assert '"Generic">13' in build_record({"kind": "datasheet", "rec_number": 1})
    assert '"Journal Article">17' in build_record({"kind": "paper", "rec_number": 1})


def test_record_strips_markup_from_title_and_venue() -> None:
    """Crossref HTML in titles (``C<sub>20</sub>``) prints as text, not
    literal tags in the field data."""
    rec = build_record(
        {
            "kind": "paper",
            "rec_number": 1,
            "title": "C<sub>20</sub> fullerene",
            "journal": "<i>Nature</i>",
        }
    )
    assert "<title>C20 fullerene</title>" in rec
    assert "<secondary-title>Nature</secondary-title>" in rec
    assert "sub>" not in rec


def test_record_normalizes_name_only_authors() -> None:
    """A comma-less ``{"name": "Given Family"}`` is reordered to
    ``Family, Given`` so records don't mix name orders; stored
    ``Family, Given`` passes through."""
    rec = build_record(
        {
            "kind": "paper",
            "rec_number": 1,
            "authors": [{"name": "Sandip Niyogi"}, {"name": "Smith, Jane"}, "WHO"],
        }
    )
    assert "<author>Niyogi, Sandip</author>" in rec
    assert "<author>Smith, Jane</author>" in rec
    assert "<author>WHO</author>" in rec
