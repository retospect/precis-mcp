"""Chunk-0 byline/date parsing for the title-repair pass (gr658886)."""

from __future__ import annotations

from precis.ingest.front_matter import parse_front_matter
from precis.ingest.paper_hygiene import _authors_are_placeholder


def test_tech_report_cover() -> None:
    text = (
        "MIT Artificial Intelligence Laboratory\n"
        "A Theory of Parsing\n"
        "Robert C. Berwick and Amy Weinberg\n"
        "May, 1984\n"
    )
    fm = parse_front_matter(text, "A Theory of Parsing")
    assert fm.year == 1984
    assert [a["name"] for a in fm.authors] == ["Robert C. Berwick", "Amy Weinberg"]


def test_by_line_without_title_match() -> None:
    fm = parse_front_matter("Some Title\nBy Jane Doe\nJune 1986\n", None)
    assert fm.year == 1986
    assert fm.authors == [{"name": "Jane Doe"}]


def test_no_confident_data_returns_nothing() -> None:
    fm = parse_front_matter("Title\nAbstract\nWe study things in 2009.\n", "Title")
    assert fm.year is None
    assert fm.authors == []


def test_affiliation_line_is_not_a_byline() -> None:
    fm = parse_front_matter("T\nStanford University\n", "T")
    assert fm.authors == []


def test_placeholder_authors() -> None:
    assert _authors_are_placeholder([])
    assert _authors_are_placeholder([{"name": "Nature User"}])
    assert not _authors_are_placeholder([{"name": "Jane Doe"}])


# ── regression: the review findings (wrapped titles, multiline bylines,
# junk stored titles, publication-date evidence) ─────────────────────


def test_wrapped_title_still_anchors_the_byline() -> None:
    """The cover wraps the stored title across two lines; the byline and
    date still anchor on it."""
    text = (
        "MIT Artificial Intelligence Laboratory\n"
        "A Theory of\n"
        "Parsing\n"
        "Robert C. Berwick and Amy Weinberg\n"
        "May, 1984\n"
    )
    fm = parse_front_matter(text, "A Theory of Parsing")
    assert fm.year == 1984
    assert [a["name"] for a in fm.authors] == ["Robert C. Berwick", "Amy Weinberg"]


def test_title_heading_a_subtitle_line_still_anchors() -> None:
    text = "A Theory of Parsing: an approach\nJ. Doe\nJune 1986\n"
    fm = parse_front_matter(text, "A Theory of Parsing")
    assert fm.year == 1986
    assert fm.authors == [{"name": "J. Doe"}]


def test_multiline_byline_keeps_every_author() -> None:
    text = "The Title\nJ. Doe\nK. Smith\nMay 1984\n"
    fm = parse_front_matter(text, "The Title")
    assert [a["name"] for a in fm.authors] == ["J. Doe", "K. Smith"]


def test_junk_stored_title_finds_byline_above_the_date() -> None:
    """A junk stored title never matches chunk 0's real title — the
    standalone cover date is the anchor instead."""
    text = (
        "MIT Artificial Intelligence Laboratory\n"
        "A Theory of Parsing\n"
        "Robert C. Berwick and Amy Weinberg\n"
        "May, 1984\n"
    )
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert [a["name"] for a in fm.authors] == ["Robert C. Berwick", "Amy Weinberg"]


def test_editorial_dates_are_not_publication_evidence() -> None:
    text = (
        "A Real Paper\n"
        "J. Doe\n"
        "Received 3 March 2009; revised 4 May 2009; accepted 7 June 2009\n"
    )
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year is None


def test_date_inside_a_sentence_is_not_evidence() -> None:
    text = (
        "A Real Paper\n"
        "J. Doe\n"
        "We build on the approach we first presented in May 1984 at the "
        "workshop.\n"
    )
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year is None


def test_cover_boilerplate_around_the_date_is_fine() -> None:
    text = "A Real Paper\nJ. Doe\nVolume 5, Number 3, May 1984\n"
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year == 1984


def test_report_number_beside_an_in_window_date_is_fine() -> None:
    text = "A Real Paper\nJ. Doe\nTechnical Report AIM-800, May 1984\n"
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year == 1984


def test_city_residue_around_an_in_window_date_is_fine() -> None:
    """Proper-noun-shaped residue (a city) is still cover boilerplate."""
    text = "A Real Paper\nJ. Doe\nCambridge, Mass., May 1984\n"
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year == 1984


def test_sentence_date_near_the_title_is_not_evidence() -> None:
    """gr658886 review: ``Experiments began in May 1984`` is grammar —
    a lower-case residue word — not a cover date, even inside the title
    window."""
    text = "A Real Paper\nJ. Doe\nExperiments began in May 1984.\n"
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year is None


def test_first_section_heading_ends_the_front_matter() -> None:
    """A standalone date below the body's first heading is body text,
    not a cover date."""
    text = "A Real Paper\nJ. Doe\nAbstract\nMay 1984\n"
    fm = parse_front_matter(text, "A Real Paper")
    assert fm.year is None
    # The byline above the heading is still read.
    assert fm.authors == [{"name": "J. Doe"}]


def test_name_shaped_line_under_masthead_is_the_title() -> None:
    """gr658886 review: with a junk stored title the backward date scan
    can't tell ``Parsing Systems`` from ``Jane Doe`` by shape — the
    masthead boundary decides."""
    text = "MIT Laboratory\nParsing Systems\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == [{"name": "Jane Doe"}]


def test_lone_name_line_under_masthead_is_not_a_byline() -> None:
    """A single name-shaped line directly under the masthead is more
    likely the title than the byline — no guess."""
    text = "MIT Laboratory\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == []


def test_wrapped_title_under_masthead_is_not_a_byline() -> None:
    """gr658886 review: a wrapped title of name-shaped lines under the
    masthead has no reliable byline boundary — store no authors rather
    than the second title line."""
    text = "MIT Laboratory\nParsing Systems\nEfficient Algorithms\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == []


def test_wrapped_title_without_masthead_is_not_a_byline() -> None:
    """gr658886 review (td659776): the same wrapped title with no
    masthead at all — ``Efficient Algorithms`` must not come back as an
    author alongside ``Jane Doe``."""
    text = "Parsing Systems\nEfficient Algorithms\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == []


def test_run_below_a_report_line_is_still_ambiguous() -> None:
    """A non-name line (``A.I. Memo 800``) above the run does not bound
    the title either — it can sit between the masthead and the title —
    so a multi-line run under it is still not byline evidence."""
    text = "MIT Laboratory\nA.I. Memo 800\nParsing Systems\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == []


def test_lone_byline_line_above_the_date_is_read() -> None:
    """A single name-line hugging the date is the byline on the dominant
    cover layout — the ambiguity only starts at two lines."""
    text = "Parsing Systems\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "S999_1a 1..3")
    assert fm.year == 1984
    assert fm.authors == [{"name": "Jane Doe"}]


def test_matched_title_bounds_a_multiline_byline_below_it() -> None:
    """With a real title anchor the run below the title span is already
    bounded — a multi-line byline reads whole even past the title
    window, and an affiliation line in between pops nothing."""
    text = "The Title\n" + "Stanford University\n" * 10 + "J. Doe\nK. Smith\nMay 1984\n"
    fm = parse_front_matter(text, "The Title")
    assert fm.year == 1984
    assert [a["name"] for a in fm.authors] == ["J. Doe", "K. Smith"]


def test_wrapped_subtitle_under_a_matched_title_is_not_a_byline() -> None:
    """gr658886 review (td659776): the stored title matched only the
    first line of a wrapped cover title, so the name-shaped run under
    it mixes a possible title tail with the byline — and no marker
    bounds them, so nothing is stored rather than a title fragment."""
    text = "Parsing Systems\nEfficient Algorithms\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "Parsing Systems")
    assert fm.year == 1984
    assert fm.authors == []


def test_lone_subtitle_before_an_affiliation_is_not_a_byline() -> None:
    """A single name-shaped line under the title, broken off by an
    affiliation line, is no byline boundary: it is not returned, and the
    real byline above the date still reads."""
    text = "Parsing Systems\nEfficient Algorithms\nStanford University\nJane Doe\nMay 1984\n"
    fm = parse_front_matter(text, "Parsing Systems")
    assert fm.year == 1984
    assert fm.authors == [{"name": "Jane Doe"}]


def test_lone_subtitle_rejected_forward_cannot_reenter_via_date_scan() -> None:
    """gr658886 review (td659776): the forward scan rejects a lone
    subtitle under the matched title, but the backward date scan used to
    reaccept it — the same reliable-boundary requirement applies there,
    so no authors are stored rather than the title fragment."""
    text = "Parsing Systems\nEfficient Algorithms\nStanford University\nMay 1984\n"
    fm = parse_front_matter(text, "Parsing Systems")
    assert fm.year == 1984
    assert fm.authors == []


def test_lone_byline_under_title_with_date_next_is_kept() -> None:
    fm = parse_front_matter("The Title\nJane Doe\nMay 1984\n", "The Title")
    assert fm.authors == [{"name": "Jane Doe"}]


def test_evidenced_byline_survives_a_wrapped_subtitle() -> None:
    """The same wrapped title, but the byline carries an initial — the
    boundary a title tail can't fake — so the marker-free head line is
    dropped and the real byline reads on."""
    text = "Parsing Systems\nEfficient Algorithms\nJ. Doe\nMay 1984\n"
    fm = parse_front_matter(text, "Parsing Systems")
    assert fm.year == 1984
    assert fm.authors == [{"name": "J. Doe"}]


def test_marker_free_multiline_byline_under_title_yields_nothing() -> None:
    """Two full-name lines directly under the matched title: the run's
    head can't be told from a wrapped title tail, so neither line is
    stored — the accepted cost of never writing a title fragment."""
    text = "The Title\nJane Doe\nJohn Roe\nMay 1984\n"
    fm = parse_front_matter(text, "The Title")
    assert fm.year == 1984
    assert fm.authors == []


def test_multi_name_line_bounds_the_byline_under_a_title() -> None:
    """A joined byline (``X and Y``) is person evidence too — the
    marker-free title-tail candidate above it drops and both names
    read."""
    text = "The Title\nEfficient Algorithms\nJane Doe and John Roe\nMay 1984\n"
    fm = parse_front_matter(text, "The Title")
    assert fm.year == 1984
    assert [a["name"] for a in fm.authors] == ["Jane Doe", "John Roe"]
