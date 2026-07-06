"""Parse SEC submissions JSON + filing HTML into a ``ParsedFiling``.

Two upstream artefacts feed the ``edgar`` ingest:

* the **submissions API** slice
  (``https://data.sec.gov/submissions/CIK##########.json``) — company
  name, tickers, and a per-filing index (form, accession, filed date,
  period of report, primary document, 8-K item codes);
* the **primary filing document** (HTML) — the actual disclosure text.

This module turns them into structured Python:

* :func:`parse_submissions` → :class:`Submissions` (company + a list of
  :class:`SubmissionFiling` rows);
* :func:`find_filing` → the row matching a given accession;
* :func:`parse_filing_html` → the primary document split into
  :class:`FilingBlock` s, each labelled with its :class:`Section` via
  ``_edgar_sections.classify_heading``;
* :func:`assemble_filing` → the combined :class:`ParsedFiling`.

We use the stdlib ``html.parser`` for text extraction so the parser has
**zero hard dependencies** (``lxml`` stays an optional accelerator). The
extractor is defensive: malformed markup degrades to best-effort text
rather than crashing the ingest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from precis.handlers._edgar_sections import BODY, Section, classify_heading

# ---------------------------------------------------------------------------
# Submissions index
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmissionFiling:
    """One row from the submissions ``filings.recent`` index."""

    accession: str  # canonical dashed
    form: str
    filed_date: str  # YYYY-MM-DD
    report_date: str | None  # period of report, YYYY-MM-DD
    primary_doc: str
    items: list[str] = field(default_factory=list)  # 8-K item codes


@dataclass(frozen=True, slots=True)
class Submissions:
    """Parsed submissions API slice for one filer."""

    cik: str
    company: str
    tickers: list[str]
    filings: list[SubmissionFiling]


def parse_submissions(raw: bytes) -> Submissions:
    """Parse a submissions-API JSON body into :class:`Submissions`.

    Only the ``filings.recent`` page is read (v1 scope — the older
    ``filings.files`` pages are a follow-up). Malformed / missing
    fields degrade to empty rather than raising.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return Submissions(cik="", company="", tickers=[], filings=[])

    cik = str(data.get("cik") or "").lstrip("0") or str(data.get("cik") or "")
    company = str(data.get("name") or "")
    tickers = [str(t) for t in (data.get("tickers") or []) if t]

    recent = ((data.get("filings") or {}).get("recent")) or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    filed = recent.get("filingDate") or []
    reports = recent.get("reportDate") or []
    primary = recent.get("primaryDocument") or []
    items = recent.get("items") or []

    filings: list[SubmissionFiling] = []
    for i, accession in enumerate(accessions):
        item_codes = _split_items(_at(items, i))
        filings.append(
            SubmissionFiling(
                accession=str(accession),
                form=str(_at(forms, i)),
                filed_date=str(_at(filed, i)),
                report_date=(str(_at(reports, i)) or None) or None,
                primary_doc=str(_at(primary, i)),
                items=item_codes,
            )
        )
    return Submissions(cik=cik, company=company, tickers=tickers, filings=filings)


def find_filing(subs: Submissions, accession: str) -> SubmissionFiling | None:
    """Return the filing row matching ``accession`` (dashed), or ``None``."""
    for f in subs.filings:
        if f.accession == accession:
            return f
    return None


def _at(seq: list, i: int) -> str:
    """Safe positional access — empty string when out of range / None."""
    if i < len(seq) and seq[i] is not None:
        return seq[i]
    return ""


def _split_items(raw: str) -> list[str]:
    """Split an 8-K ``items`` cell ('5.02,7.01') into a code list."""
    if not raw:
        return []
    return [c.strip() for c in re.split(r"[,\s]+", raw) if c.strip()]


# ---------------------------------------------------------------------------
# Filing document → blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FilingBlock:
    """One text block of the primary document, labelled with its section."""

    text: str
    section: Section


@dataclass(frozen=True, slots=True)
class ParsedFiling:
    """Structured form of one filing — meta + labelled body blocks."""

    title: str
    company: str
    cik: str
    form: str
    filed_date: str
    period_of_report: str | None
    primary_doc: str
    tickers: list[str] = field(default_factory=list)
    blocks: list[FilingBlock] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "hr",
    }
)
_SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "head"})


class _TextExtractor(HTMLParser):
    """Extract block-level text segments from filing HTML.

    Emits one string per block-level element boundary. Whitespace is
    collapsed; ``<script>`` / ``<style>`` content is dropped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        # Normalise non-breaking spaces the collapse above misses.
        text = text.replace("\xa0", " ").strip()
        if text:
            self.lines.append(text)
        self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def extract_text_lines(html_bytes: bytes) -> list[str]:
    """Return block-level text segments from filing HTML (or plain text).

    Falls back to a naive line split if the input isn't HTML-ish, so a
    plain-text filing (rare, older filings) still parses.
    """
    text = _decode(html_bytes)
    if "<" not in text:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Degrade to a crude tag-strip on pathological markup.
        stripped = re.sub(r"<[^>]+>", " ", text)
        return [
            re.sub(r"\s+", " ", ln).strip()
            for ln in stripped.splitlines()
            if ln.strip()
        ]
    return parser.lines


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# Boilerplate lines that shouldn't become their own blocks.
_BOILERPLATE_RE: tuple[re.Pattern[str], ...] = (
    re.compile(r"^table of contents$", re.IGNORECASE),
    re.compile(r"^\d+$"),  # bare page number
    re.compile(r"^page\s+\d+(\s+of\s+\d+)?$", re.IGNORECASE),
)
_MIN_BLOCK_CHARS = 3


def _is_boilerplate(line: str) -> bool:
    if len(line) < _MIN_BLOCK_CHARS:
        return True
    return any(p.match(line) for p in _BOILERPLATE_RE)


def parse_filing_html(html_bytes: bytes, *, form: str) -> list[FilingBlock]:
    """Split a primary filing document into section-labelled blocks.

    Walks the extracted text lines; a line recognised as a section
    heading (via :func:`classify_heading`) starts a new section and
    every subsequent line is stamped with it until the next heading.
    """
    lines = extract_text_lines(html_bytes)
    current: Section = BODY
    blocks: list[FilingBlock] = []
    for line in lines:
        if _is_boilerplate(line):
            continue
        section = classify_heading(line, form=form)
        if section is not None:
            current = section
        blocks.append(FilingBlock(text=line, section=current))
    return blocks


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_filing(
    *,
    filing: SubmissionFiling,
    company: str,
    cik: str,
    tickers: list[str],
    primary_html: bytes,
) -> ParsedFiling:
    """Combine a submissions row + primary-doc HTML into a ParsedFiling.

    ``items`` on the result is the union of the submissions-declared
    8-K item codes and the distinct item codes discovered by the
    section classifier — the richer set for the overview + list views.
    """
    blocks = parse_filing_html(primary_html, form=filing.form)

    discovered = {b.section.item_code for b in blocks if b.section.item_code}
    items = sorted(set(filing.items) | discovered)

    when = filing.report_date or filing.filed_date
    title = (
        f"{company} — {filing.form} ({when})"
        if company
        else (f"{filing.form} ({when})")
    )

    return ParsedFiling(
        title=title,
        company=company,
        cik=cik,
        form=filing.form,
        filed_date=filing.filed_date,
        period_of_report=filing.report_date,
        primary_doc=filing.primary_doc,
        tickers=tickers,
        blocks=blocks,
        items=items,
    )


# ---------------------------------------------------------------------------
# Full-text-search response
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EdgarHit:
    """One hit from an EDGAR full-text search response."""

    accession: str  # canonical dashed
    form: str
    filed_date: str | None
    company: str
    primary_doc: str


def parse_fts_response(raw: bytes) -> tuple[list[EdgarHit], int]:
    """Parse an EDGAR FTS JSON body into ``(hits, total)``.

    The FTS endpoint returns Elasticsearch-shaped JSON: ``hits.hits`` is
    a list whose ``_id`` is ``"<accession-dashed>:<primary-doc>"`` and
    whose ``_source`` carries ``display_names`` / ``file_type`` /
    ``file_date``. Malformed rows are skipped; ``total`` reflects the
    reported total-result count when present.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return [], 0

    hits_root = (data.get("hits") or {}) if isinstance(data, dict) else {}
    total_raw = hits_root.get("total")
    total = 0
    if isinstance(total_raw, dict):
        total = int(total_raw.get("value") or 0)
    elif isinstance(total_raw, int):
        total = total_raw

    out: list[EdgarHit] = []
    seen: set[str] = set()
    for hit in hits_root.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        _id = str(hit.get("_id") or "")
        accession_part, _, doc = _id.partition(":")
        try:
            from precis.handlers._edgar_accession import parse_accession

            accession = parse_accession(accession_part).dashed
        except Exception:
            continue
        if accession in seen:
            continue
        seen.add(accession)
        src = hit.get("_source") or {}
        names = src.get("display_names") or []
        company = str(names[0]) if names else ""
        out.append(
            EdgarHit(
                accession=accession,
                form=str(src.get("file_type") or (src.get("root_forms") or [""])[0]),
                filed_date=(str(src.get("file_date")) or None) or None,
                company=company,
                primary_doc=doc,
            )
        )

    if total == 0 and out:
        total = len(out)
    return out, total


__all__ = [
    "EdgarHit",
    "FilingBlock",
    "ParsedFiling",
    "SubmissionFiling",
    "Submissions",
    "assemble_filing",
    "extract_text_lines",
    "find_filing",
    "parse_filing_html",
    "parse_fts_response",
    "parse_submissions",
]
