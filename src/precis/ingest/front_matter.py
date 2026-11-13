"""Mine a paper's chunk-0 front matter for the byline and the date.

The title-repair cascade
(:func:`precis.ingest.paper_hygiene.repair_junk_title_papers`) reads the
title off chunk 0, but a paper it could not resolve through a registry —
or one it repaired before the metadata legs existed — kept whatever
``year``/``authors`` ingest guessed: the ingestion year and a blank or
placeholder byline (gr658886). Cover pages and tech reports state both
right next to the title: a ``May, 1984`` line and an author line.
:func:`parse_front_matter` reads them back, conservatively — no
confident byline or explicit month-year means ``None``, never a guess.

What counts as evidence:

* **Year** — an explicit month-year (``May, 1984``, ``15 March 1987``;
  never a bare year) that either sits inside the title block — where
  the residue around it must still be cover-shaped (a report code or a
  city, so ``Experiments began in May 1984`` is a body sentence, not a
  cover date) — or dominates its own line (``Volume 5, Number 3, May
  1984`` counts). Editorial-history lines —
  ``received``/``revised``/``accepted``/… — are skipped outright, so a
  journal's submission history can't masquerade as the publication
  date, and the first section heading ends the front matter: nothing
  below it is cover evidence.
* **Authors** — a run of name-list lines anchored on the stored title
  when it matches (the cover may wrap the title across lines or append
  a subtitle after punctuation), else on the publication-date line
  itself — the byline hugs it from above, which is the only anchor left
  when the stored title is still the junk one — else an explicit
  ``By …`` line. A byline may itself wrap across lines; affiliation,
  series and heading words disqualify a line. Where the complete
  title/subtitle ends must be reliable, not shape-based: a name-shaped
  line under the stored title's match can be the wrapped title's tail
  (``Parsing Systems`` / ``Efficient Algorithms`` / ``Jane Doe`` reads
  ``Efficient Algorithms`` as an author, the corruption this pass
  exists to remove), so a run directly under the title match counts
  only from its first person-evidenced line — an initial, a name
  particle or suffix, or a multi-name join a title tail cannot fake —
  and a run with none yields nothing. The same requirement binds the
  backward date scan: a lone subtitle the forward scan rejected must
  not reenter as a byline above the date. Without a title anchor the
  boundary is stricter still: a name-shaped line directly under a
  masthead (``MIT Laboratory``) is the cover title, and only a single
  byline line is trusted past that — a longer run yields nothing
  rather than a guess.

Pure text in, values out; the caller decides what to overwrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from precis.utils.authors import is_junk_author_name

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
#: ``May, 1984`` / ``June 1986`` / ``Sept. 1983``.
_MONTH_YEAR_RE = re.compile(
    rf"\b(?:{_MONTHS})\b\.?,?\s+(?:\d{{1,2}},?\s+)?((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
#: ``15 March 1987``.
_DAY_MONTH_YEAR_RE = re.compile(
    rf"\b\d{{1,2}}\s+(?:{_MONTHS})\b\.?,?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE
)
#: Lines whose dates are submission history, not the publication date.
_EDITORIAL_RE = re.compile(
    r"\b(?:received|revised|resubmitted|accepted|rejected|submitted|"
    r"communicated|corrected|retracted|in\s+press|available\s+online)\b",
    re.IGNORECASE,
)
#: A line that is only an optional section number plus a section-heading
#: word — where the body begins and the front matter ends. Deeper text
#: (an abstract sentence, a section body) can hold month-years and
#: name-shaped lines that are not cover evidence.
_BODY_HEADING_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*[.)]?|[ivxlcdm]+[.)])\s+)?"
    r"(?:abstract|introduction|contents|keywords?|index\s+terms|"
    r"references|bibliography|acknowledg(?:e)?ments?|methods?|"
    r"methodology|results?|discussion|conclusions?|appendix|appendices|"
    r"summary|background|preface|foreword|overview|preliminaries|"
    r"related\s+work)\b\.?\s*:?\s*$",
    re.IGNORECASE,
)
#: Institution words marking a cover masthead — the line directly under
#: one is the paper's title or another masthead line, never part of the
#: byline. Kept narrower than :data:`_NOT_NAME`: a title's own stop
#: words (``of``, ``the``) must not make it a masthead.
_MASTHEAD_WORDS = frozenset(
    [
        "university",
        "institute",
        "laboratory",
        "laboratories",
        "department",
        "school",
        "college",
        "center",
        "centre",
        "corporation",
        "company",
        "inc",
        "ltd",
        "press",
        "division",
        "foundation",
        "association",
        "society",
        "academy",
        "consortium",
        "faculty",
        "hospital",
        "office",
        "national",
    ]
)
#: Words allowed around a standalone cover date — report/series boilerplate,
#: not sentence words (that's how ``Volume 5, Number 3, May 1984`` still
#: counts while "as shown in May 1984" does not).
_DATE_FILLER = frozenset(
    [
        "published",
        "pub",
        "issued",
        "dated",
        "date",
        "copyright",
        "volume",
        "vol",
        "number",
        "no",
        "nos",
        "issue",
        "iss",
        "pages",
        "page",
        "pp",
        "part",
        "pt",
        "series",
        "report",
        "technical",
        "tech",
        "memo",
        "memorandum",
        "preprint",
        "edition",
        "version",
        "release",
        "working",
        "discussion",
        "paper",
        "of",
        "the",
        "a",
        "an",
        "and",
        "for",
        "in",
        "on",
        "at",
        "by",
    ]
)
_MIN_YEAR = 1900
#: Only the leading lines carry the cover byline/date; deeper text is body.
_SCAN_LINES = 40
#: A wrapped title spills at most this many lines.
_TITLE_WRAP_LINES = 3
#: The byline sits within this many lines after the title's last line.
_BYLINE_WINDOW = 7
#: The cover date sits within this many lines of the title block.
_DATE_WINDOW = 8
#: Non-boilerplate words allowed around an in-window month-year — a
#: report number or a city (proper-noun-shaped), not a sentence.
_IN_WINDOW_DATE_RESIDUE = 3
#: How far above a publication date a byline may start, and how many
#: non-name lines (affiliation, series) may sit between them.
_ABOVE_DATE_SCAN = 5
_ABOVE_DATE_GAP = 2
_MAX_BYLINE_AUTHORS = 8
_BY_RE = re.compile(r"^\s*by\s+", re.IGNORECASE)
_NAME_WORD_RE = re.compile(
    r"^(?:[A-Z]\.?|[A-Z][a-z]+(?:[-'][A-Za-z]+)*\.?|"
    r"(?:de|der|den|di|da|del|della|le|la|du|dos|das|bin|ibn|al|ten|ter|"
    r"van|von)\.?|(?:II|III|IV|Jr|Sr|MD|PhD|Esq)\.?)$"
)
_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\band\b)\s*")
_SQUASH_RE = re.compile(r"[^a-z0-9]+")
#: Words that mark an affiliation, series line or heading — not a person.
_NOT_NAME = frozenset(
    [
        "university",
        "institute",
        "laboratory",
        "laboratories",
        "department",
        "school",
        "college",
        "center",
        "centre",
        "corporation",
        "company",
        "inc",
        "ltd",
        "report",
        "technical",
        "memo",
        "memorandum",
        "abstract",
        "contents",
        "introduction",
        "journal",
        "press",
        "division",
        "group",
        "research",
        "science",
        "sciences",
        "engineering",
        "artificial",
        "intelligence",
        "project",
        "office",
        "national",
        "foundation",
        "association",
        "society",
        "proceedings",
        "volume",
        "number",
        "the",
        "of",
        "for",
        "on",
        "in",
        "a",
        "an",
    ]
)


@dataclass
class FrontMatter:
    """What chunk 0 states about year and authors; each ``None``/empty if
    the text did not state it confidently."""

    year: int | None = None
    authors: list[dict[str, str]] = field(default_factory=list)


def _squash(s: str) -> str:
    """Lowercase alnum words, space-joined — punctuation-insensitive."""
    return " ".join(w for w in _SQUASH_RE.split(s.lower()) if w)


def _find_title_span(lines: list[str], title: str | None) -> tuple[int, int] | None:
    """``(first, after_last)`` line span of *title*, or ``None``.

    The stored title may wrap across up to :data:`_TITLE_WRAP_LINES`
    lines, or head a line that continues into a subtitle/series after a
    word boundary (``Title: a subtitle`` counts, ``Titlex`` does not).
    """
    key = _squash(title or "")
    if not key:
        return None
    for i in range(len(lines)):
        buf = ""
        for j in range(i, min(i + _TITLE_WRAP_LINES, len(lines))):
            buf = f"{buf} {_squash(lines[j])}".strip()
            if buf == key or (
                buf.startswith(key) and len(buf) > len(key) and buf[len(key)] == " "
            ):
                return (i, j + 1)
            if len(buf) >= len(key):
                break
    return None


def _date_hit(line: str) -> tuple[int, int, bool] | None:
    """``(year, non_filler_word_count, sentence_residue)`` for the first
    month-year on *line*, else ``None``. Editorial-history lines never
    hit. The residue counts tell the caller what surrounds the date: a
    standalone cover date (``0`` — only :data:`_DATE_FILLER`
    boilerplate), short cover noise (a report code or a city — residue
    words are proper-noun-shaped), or a real sentence — which
    ``sentence_residue`` marks the moment any non-filler residue word
    starts lower-case (``Experiments began in May 1984`` is grammar,
    not boilerplate)."""
    if _EDITORIAL_RE.search(line):
        return None
    m = _MONTH_YEAR_RE.search(line) or _DAY_MONTH_YEAR_RE.search(line)
    if not m:
        return None
    year = int(m.group(1))
    if not _MIN_YEAR <= year <= datetime.now(UTC).year:
        return None
    residual = f"{line[: m.start()]} {line[m.end() :]}"
    words = re.findall(r"[A-Za-z]+", residual)
    non_filler = [w for w in words if w.lower().strip(".,") not in _DATE_FILLER]
    return year, len(non_filler), any(w[:1].islower() for w in non_filler)


def _find_date(
    lines: list[str], title_span: tuple[int, int] | None
) -> tuple[int | None, int | None]:
    """``(line_index, year)`` of publication-date evidence, else ``(None,
    None)``. A month-year inside the title block wins — there it may sit
    inside a short cover line (``AIM-800 May 1984``, ``Cambridge, Mass.,
    May 1984``) but not inside a real sentence: a lower-case
    non-boilerplate word in the residue is grammar, not boilerplate, so
    ``Experiments began in May 1984`` near the title is body text, not
    the publication date. Away from the title the date must dominate its
    own line (``May, 1984``, ``Volume 5, Number 3, May 1984``).
    Everything else — editorial history, a date buried in body text — is
    not publication evidence."""
    lo = max(0, title_span[0] - 4) if title_span else None
    hi = min(len(lines), title_span[1] + _DATE_WINDOW) if title_span else None
    dominated: tuple[int, int] | None = None
    for i, line in enumerate(lines):
        hit = _date_hit(line)
        if hit is None:
            continue
        year, non_filler, sentence = hit
        if lo is not None and hi is not None and lo <= i < hi:
            if non_filler <= _IN_WINDOW_DATE_RESIDUE and not sentence:
                return i, year
            continue
        if non_filler == 0 and dominated is None:
            dominated = (i, year)
    return dominated or (None, None)


def _byline_names(line: str) -> list[str] | None:
    """Names in *line* if it reads as a byline, else ``None``."""
    s = _BY_RE.sub("", line).strip().rstrip(".")
    if not s or any(c.isdigit() for c in s) or len(s) > 160:
        return None
    parts = [p.strip() for p in _SPLIT_RE.split(s) if p.strip()]
    if not parts or len(parts) > _MAX_BYLINE_AUTHORS:
        return None
    for p in parts:
        words = p.split()
        if not 2 <= len(words) <= 4:
            return None
        if not all(_NAME_WORD_RE.match(w) for w in words):
            return None
        if any(w.lower().strip(".") in _NOT_NAME for w in words):
            return None
        if is_junk_author_name(p):
            return None
    return parts


def _is_masthead(line: str) -> bool:
    """*line* names an institution — a cover masthead, not prose (a
    masthead is short; a sentence mentioning a university is not one)."""
    words = {w.lower().strip(".,") for w in line.split()}
    return len(words) <= 10 and bool(words & _MASTHEAD_WORDS)


#: A dotted (``J.``/``A.-K.``) or bare (``J``) initial — a wrapped title
#: tail virtually never carries one, so it bounds the byline where the
#: title block ends. The bare form excludes ``A``/``I``: a leading
#: English article is not an initial.
_DOTTED_INITIAL_RE = re.compile(r"^[A-Z]\.(?:-[A-Z]\.)*$")
_BARE_INITIAL_RE = re.compile(r"^[A-Z]$")
_ARTICLE_INITIALS = frozenset({"A", "I"})
#: Lowercase surname particles and trailing name suffixes — the same
#: evidence class as an initial. The Latin articles (``de``, ``la``…)
#: are left out: they appear inside real titles too often to bound.
_PERSON_WORDS = frozenset(
    [
        "van",
        "von",
        "der",
        "den",
        "ten",
        "ter",
        "bin",
        "ibn",
        "jr",
        "sr",
        "ii",
        "iii",
        "iv",
        "md",
        "phd",
        "esq",
    ]
)


def _has_person_marker(line: str, names: list[str]) -> bool:
    """Evidence *line* is a person, not a wrapped title tail: an
    explicit ``By``, a multi-name join (``X and Y``), a dotted or bare
    non-article initial, a name particle, or a name suffix."""
    if _BY_RE.match(line) or len(names) > 1:
        return True
    for name in names:
        for w in name.split():
            if _DOTTED_INITIAL_RE.match(w):
                return True
            if _BARE_INITIAL_RE.match(w) and w not in _ARTICLE_INITIALS:
                return True
            if w.lower().strip(".") in _PERSON_WORDS:
                return True
    return False


def _strip_title_tail(
    lines: list[str], run_idx: list[int], run_names: list[list[str]]
) -> None:
    """Drop a wrapped title's tail off the head of a byline run that
    sits directly under the title match. A marker-free name line there
    cannot be told from the title's continuation (``Efficient
    Algorithms`` under ``Parsing Systems``), so the run counts only
    from its first person-evidenced line — the reliable boundary a
    title tail cannot fake. A run with none is emptied: no boundary,
    no guess."""
    while run_names and not _has_person_marker(lines[run_idx[0]], run_names[0]):
        run_idx.pop(0)
        run_names.pop(0)


def _find_authors(
    lines: list[str],
    title_span: tuple[int, int] | None,
    date_idx: int | None,
) -> list[str]:
    """The byline's names — a run of name-list lines anchored on the
    title, else hugging the publication date from above, else an
    explicit ``By …`` line. A multi-line run directly under the title
    match can hide a wrapped title tail in its head (so can a lone
    line followed by anything but the date), so it must open on a
    person-evidenced line (:func:`_strip_title_tail`) — and the
    backward date-anchored scan applies the same requirement when its
    run reaches back up to the title match, so a lone subtitle rejected
    forward cannot reenter above the date. Without a
    title anchor the date-anchored run needs a structural boundary: a
    name-shaped line directly under a masthead is the cover title, and
    whatever is left past it is trusted only as a single byline line —
    a longer run is a wrapped title's tail the scan cannot split from
    the byline, so none are taken."""
    if title_span:
        run_idx: list[int] = []
        run_names: list[list[str]] = []
        for off, line in enumerate(
            lines[title_span[1] : title_span[1] + _BYLINE_WINDOW]
        ):
            got = _byline_names(line)
            if got is None:
                if run_names:
                    break  # a byline is one run — done
                continue
            run_idx.append(title_span[1] + off)
            run_names.append(got)
            if sum(len(r) for r in run_names) > _MAX_BYLINE_AUTHORS:
                return []
        if run_names:
            after = run_idx[-1] + 1
            # A lone line is safe only when the date follows it (or the
            # text ends): any other line after it (an affiliation) leaves
            # it indistinguishable from a wrapped title tail.
            ends_clean = after >= len(lines) or _date_hit(lines[after]) is not None
            if run_idx[0] == title_span[1] and (len(run_names) > 1 or not ends_clean):
                _strip_title_tail(lines, run_idx, run_names)
            names = [n for run in run_names for n in run]
            if names:
                return names
    if date_idx is not None:
        # No usable title anchor (or none found near it): the cover
        # byline sits directly above the date, behind at most an
        # affiliation/series line or two. Never cross back up into the
        # title span.
        floor = title_span[1] if title_span else 0
        runs: list[list[str]] = []
        back_idx: list[int] = []
        run_top: int | None = None
        skipped = 0
        j = date_idx - 1
        while j >= floor and date_idx - j <= _ABOVE_DATE_SCAN:
            if not title_span and j == 0:
                break  # a top-of-chunk line is the title block, not a byline
            got = _byline_names(lines[j])
            if got is None:
                if runs or skipped >= _ABOVE_DATE_GAP:
                    break
                skipped += 1
            else:
                runs.insert(0, got)
                back_idx.insert(0, j)
                run_top = j
                if sum(len(r) for r in runs) > _MAX_BYLINE_AUTHORS:
                    return []
            j -= 1
        if title_span:
            # A run reaching back up to the title match has the same
            # ambiguous head as the forward scan's — strip it the same
            # way, lone line included: a subtitle the forward scan
            # rejected must not reenter through the date scan. Anything
            # else sits wholly below a bounded title.
            if run_top == title_span[1]:
                _strip_title_tail(lines, back_idx, runs)
            names = [n for run in runs for n in run]
        else:
            # No title anchor — the byline boundary must be structural.
            # A name-shaped line directly under a masthead (``MIT
            # Laboratory``) is the cover title: drop exactly that line.
            # Anything longer than one byline line left after that is a
            # wrapped title's tail the scan cannot split from the byline
            # (``Parsing Systems`` / ``Efficient Algorithms`` / ``Jane
            # Doe`` has no masthead either) — storing it would write a
            # title fragment into the byline, exactly the corruption
            # this pass exists to remove. No boundary, no guess.
            if run_top and _is_masthead(lines[run_top - 1]):
                runs.pop(0)
            names = [n for run in runs for n in run] if len(runs) == 1 else []
        if names:
            return names
    for line in lines:
        if _BY_RE.match(line):
            got = _byline_names(line)
            if got:
                return got
    return []


def parse_front_matter(text: str, title: str | None = None) -> FrontMatter:
    """Year and authors stated in chunk-0 *text*, next to *title*."""
    lines = [ln.strip(" \t#*_|>") for ln in text.splitlines()]
    lines = [ln for ln in lines if ln][:_SCAN_LINES]
    # The first section heading ends the front matter: nothing below it
    # is cover evidence, so a sentence date or a name-shaped heading
    # deeper in the text can't pose as the byline or the date.
    body = next(
        (i for i, ln in enumerate(lines) if _BODY_HEADING_RE.match(ln)),
        len(lines),
    )
    lines = lines[:body]
    title_span = _find_title_span(lines, title)
    date_idx, year = _find_date(lines, title_span)
    authors = _find_authors(lines, title_span, date_idx)
    return FrontMatter(year=year, authors=[{"name": n} for n in authors])
