"""Keep only the target article from a multi-article page spread (gr463414).

Journal PDFs of short pieces (Nature letters and correspondence, 1980s-1990s
scans) print several articles per page, so the "PDF of one paper" carries the
tail of the previous article and the head of the next. Ingested whole, the
neighbours land in the paper's body and hubs mined from those chunks credit
the wrong paper (pa44468 / pa42559 share the thalidomide, mimicry, peapod and
xenon pieces; pa478893 Kroto 1987 carries a catalysis letter on each side).

:func:`trim_to_article` runs on Marker blocks before they become chunks, so
nothing stored is mutated (body chunks are append-only). Cutting real content
of an ordinary paper is worse than missing a spread, so every cut needs two
independent signals:

1. A candidate boundary. Either a title-like section header right after a
   closer (a references entry, "I thank ...", a "Received ... accepted" or
   "Correspondence and requests ..." line, a footnote affiliation after the
   prose), or a multi-name byline header followed by prose (the 1987 letter
   layout: authors, then title and abstract). Standard headings (Methods,
   STAR Methods, Funding, Declarations, numbered sections ...) never start
   an article.
2. Confirmation. The boundary into the kept article is confirmed by the
   kept article's own title/DOI match and the previous segment ending in a
   hard closer (references, acknowledgement, Received line). A boundary after
   the kept article is confirmed only by article evidence inside the next
   segment: a multi-name byline header, a "name + affiliation" line, or a
   DOI other than the target's.

A cut segment must also not match the target title over its full text, so a
main body is never dropped in favour of a "Supporting Information for <title>"
tail. Unconfirmed boundaries merge back into the kept article; when one
remains after a confirmed cut, the result says ``residual`` and the caller
tags the paper ``multi-article``.

The kept article carries the target DOI (outside references) or its head
matches the target title (:func:`title_body_score` >= 0.6, every other
article < 0.5). With no confident keep and at least two segments carrying
article evidence the verdict is ``ambiguous``: whole body kept, paper tagged.
Anything else is ``single``.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from precis.utils.title_body import title_body_score

#: Head match a kept article needs, and the ceiling every other article
#: must stay under (head and full text) for a title-based keep or a cut.
KEEP_SCORE = 0.6
OTHER_MAX_SCORE = 0.5
#: Head match a DOI-holding article still needs when a target title exists
#: (a metadata DOI on a spread can be the neighbour's).
DOI_TITLE_FLOOR = 0.3
#: Prose blocks an article needs before a footnote affiliation or a
#: Received line counts as its closer.
_CLOSER_MIN_PROSE = 3
#: A body block this long that is not a reference/affiliation is prose.
_PROSE_MIN_CHARS = 200

_HEADER_TYPES = frozenset({"section_header", "title"})
_BODY_TYPES = frozenset({"text", "paragraph", "list", "list_item"})

_MARKUP_RE = re.compile(r"<[^>]+>|[*_`\\#]")
_LEAD_JUNK_RE = re.compile(r"^[^A-Za-z0-9]+")
_RUNNING_HEAD_RE = re.compile(
    r"^(scientific correspondence|letters? to nature|letters|articles?|"
    r"news and views|review articles?|brief communications?|correspondence|"
    r"(?:nature|science)\W*vol\b.*)$",
    re.IGNORECASE,
)
_ENUMERATOR_RE = re.compile(
    r"^(?:(?:chapter|part|section)\s+)?(?:\d+(?:\.\d+)*|[IVX]+|[A-Z]|S\d+)[.):]?\s+",
    re.IGNORECASE,
)
_STANDARD_HEADING_RE = re.compile(
    r"^(abstract|introduction|background|results?|discussion|conclusions?|"
    r"summary|significance|methods?|online methods|star\W*methods|materials|"
    r"experimental|experiments?|key resources|resource availability|"
    r"lead contact|quantification|references|bibliography|literature cited|"
    r"notes|acknowledge?ments?|appendix|appendices|supplementa|supporting|"
    r"author|corresponding|competing|conflicts? of interest|declarations?|"
    r"credit\b|ethics|consent|data availability|code availability|"
    r"additional information|online content|extended data|funding|"
    r"abbreviations|keywords|nomenclature|tables?\b|fig|figures?\b|scheme|"
    r"associated content|reporting summary|peer review|contents|"
    r"table of contents|toc\b|graphical abstract|highlights|biograph|vitae|"
    r"about the authors?|limitations?|future work|outlook|chapter\b|part\b)",
    re.IGNORECASE,
)
_NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*|[IVX]+|S\d+)[.):]?\s")
_NAME_RE = (
    r"(?:(?:[A-Z]\.\s?|[A-Z]\.-[A-Z]\.\s?)|"
    r"(?:[A-Z][A-Za-z'’\-]+\s)|(?:(?:de|van|der|von|la|le|du|di)\s))+"
    r"[A-Z][A-Za-z'’\-]+"
)
_NAME_SEP = r"[\W\d†‡]*?\s*(?:,|&|\band\b)\s*"
_BYLINE_RE = re.compile(rf"^{_NAME_RE}(?:{_NAME_SEP}{_NAME_RE})*[\W\d†‡t!:]*$")
_MULTI_NAME_RE = re.compile(rf"{_NAME_RE}{_NAME_SEP}{_NAME_RE}")
_INITIAL_RE = re.compile(r"\b[A-Z]\.")
#: ``1. Lenz, W.`` / ``- I. Brown, W. E.`` / ``- 1 M. S. Holm, E. ...`` (RSC,
#: initials first), optionally behind a clipped "References" heading.
_REFERENCE_RE = re.compile(
    r"^\s*(?:[Rr]eferences?\s*)?(?:[-*•]\s*)?"
    r"(?:\[?(?:\d{1,3}|[Il])?\s*[.)\]]?\s*)?"
    r"(?:[A-Z][A-Za-z'’\-]+,\s+(?:[A-Z]\.\s?-?){1,3}"
    r"|(?:[A-Z]\.\s?-?){1,3}\s?[A-Z][A-Za-z'’\-]+,)"
)
#: Standard headings Marker clips at the front (``eference``,
#: ``cknowledgement``) still count as standard.
_CLIPPED_STANDARD = (
    "references",
    "acknowledgements",
    "acknowledgments",
    "introduction",
    "conclusions",
    "experimental",
    "abstract",
)
_YEAR_RE = re.compile(r"\((?:1[89]|20)\d\d\)|\b(?:1[89]|20)\d\d\b")
_AFFILIATION_RE = re.compile(
    r"\b(Department|Dept\.|University|Institute|Laborator\w*|College|"
    r"School of|Centre|Center|e-mail|E-mail)\b"
)
#: A footnote line: ``*Department of ...`` / ``† Institute ...``.
_FOOTNOTE_AFFILIATION_RE = re.compile(r"^\s*[\\*†‡§¶\d]+\s*\S")
#: A "name + affiliation" line: ``Michael P. Speed Environmental ... College``.
_NAME_AFFILIATION_RE = re.compile(rf"^\s*{_NAME_RE}\b")
_BIO_RE = re.compile(
    r"\b(received (?:the|his|her)|degree|is currently|was born|is a professor)\b",
    re.IGNORECASE,
)
_ACK_RE = re.compile(r"^\s*(?:(?:I|We)\s+(?:thank|are grateful)|Acknowledge?ments?\b)")
_RECEIVED_RE = re.compile(
    r"\bReceived\b.{0,80}\b(?:accepted|revised)\b", re.IGNORECASE | re.DOTALL
)
_CORRESPONDENCE_RE = re.compile(
    r"^\s*Correspondence\s+(?:and requests|should be|to\b)|requests for materials",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")


@dataclasses.dataclass(frozen=True)
class Article:
    """One article found on the spread: blocks ``[start, end)``."""

    start: int
    end: int
    head: str
    page_first: int | None
    page_last: int | None
    dois: frozenset[str]
    score: float
    #: Full-text title match (cut guard).
    text_score: float
    #: Multi-name byline / name+affiliation line / foreign DOI inside.
    evidence: bool
    #: Ends in references, an acknowledgement or a Received line.
    hard_close: bool
    #: Opened by a multi-name byline header followed by prose.
    byline_open: bool

    def audit(self) -> dict[str, Any]:
        return {
            "blocks": [self.start, self.end],
            "pages": [self.page_first, self.page_last],
            "head": self.head[:160],
            "score": round(self.score, 2),
        }


@dataclasses.dataclass(frozen=True)
class SpreadTrim:
    """Outcome of :func:`trim_to_article`.

    ``verdict`` is ``single`` (blocks untouched), ``trimmed`` (``blocks``
    holds only the kept span; ``residual`` when an unconfirmed boundary
    stayed inside it) or ``ambiguous`` (several articles, none matched with
    confidence; blocks untouched).
    """

    blocks: list[dict[str, Any]]
    verdict: str
    articles: tuple[Article, ...] = ()
    kept: tuple[int, int] | None = None
    reason: str = ""
    residual: bool = False

    def audit(self) -> dict[str, Any] | None:
        """The ``ref_events`` payload, ``None`` for ``single``."""
        if self.verdict == "single":
            return None
        out: dict[str, Any] = {
            "verdict": self.verdict,
            "reason": self.reason,
            "articles": len(self.articles),
        }
        if self.kept is not None:
            lo, hi = self.kept
            out["kept"] = {
                "blocks": [self.articles[lo].start, self.articles[hi].end],
                "pages": [
                    _min_page(self.articles[lo : hi + 1]),
                    _max_page(self.articles[lo : hi + 1]),
                ],
                "head": self.articles[lo].head[:160],
                "score": round(max(a.score for a in self.articles[lo : hi + 1]), 2),
            }
            out["cut"] = [
                a.audit() for i, a in enumerate(self.articles) if not lo <= i <= hi
            ]
            out["residual"] = self.residual
        else:
            out["found"] = [a.audit() for a in self.articles]
        return out


def _min_page(arts: tuple[Article, ...]) -> int | None:
    pages = [a.page_first for a in arts if a.page_first is not None]
    return min(pages) if pages else None


def _max_page(arts: tuple[Article, ...]) -> int | None:
    pages = [a.page_last for a in arts if a.page_last is not None]
    return max(pages) if pages else None


def _clean(text: str) -> str:
    return " ".join(_MARKUP_RE.sub("", text or "").split())


def _header_kind(text: str) -> str:
    """``running`` | ``standard`` | ``byline`` | ``title``."""
    t = _LEAD_JUNK_RE.sub("", _clean(text))
    if not t or _RUNNING_HEAD_RE.match(t):
        return "running"
    if _STANDARD_HEADING_RE.match(_ENUMERATOR_RE.sub("", t, count=1)):
        return "standard"
    low = t.lower().rstrip(":. ")
    if len(low) >= 5 and any(w.endswith(low) for w in _CLIPPED_STANDARD):
        return "standard"
    if len(t) <= 200 and _INITIAL_RE.search(t) and _BYLINE_RE.match(t):
        return "byline"
    if _NUMBERED_HEADING_RE.match(t):
        return "standard"
    return "title"


def _is_multi_byline(text: str) -> bool:
    t = _clean(text)
    return _header_kind(t) == "byline" and _MULTI_NAME_RE.search(t) is not None


def _is_reference(block: dict[str, Any]) -> bool:
    if block.get("type") == "references":
        return True
    text = block.get("text") or ""
    return bool(_REFERENCE_RE.match(text) and _YEAR_RE.search(text))


def _is_affiliation(text: str) -> bool:
    return len(text) <= 400 and _AFFILIATION_RE.search(text) is not None


def _is_footnote_affiliation(text: str) -> bool:
    return (
        _is_affiliation(text)
        and _FOOTNOTE_AFFILIATION_RE.match(text) is not None
        and _AFFILIATION_RE.search(text[:80]) is not None
    )


def _is_name_affiliation(text: str) -> bool:
    """``Michael P. Speed Environmental ... College`` — not a biography."""
    return (
        _is_affiliation(text)
        and _INITIAL_RE.search(text[:40]) is not None
        and _NAME_AFFILIATION_RE.match(text) is not None
        and _AFFILIATION_RE.search(text[:120]) is not None
        and _BIO_RE.search(text) is None
    )


def _is_prose(block: dict[str, Any]) -> bool:
    text = (block.get("text") or "").strip()
    return (
        block.get("type") in _BODY_TYPES
        and len(text) >= _PROSE_MIN_CHARS
        and not _is_reference(block)
        and not _is_affiliation(text)
        and not _ACK_RE.match(text)
    )


def _norm_doi(doi: str) -> str:
    return doi.lower().rstrip(".,;)]")


def _byline_opens(blocks: list[dict[str, Any]], i: int) -> bool:
    """True when the byline header at ``i`` opens an article: it names
    several authors and before the next header comes prose, or a short
    title line and then prose (authors, title, abstract). An end byline is
    followed by affiliations and references; a biography names one person."""
    if not _is_multi_byline(blocks[i].get("text") or ""):
        return False
    body: list[dict[str, Any]] = []
    for b in blocks[i + 1 :]:
        if b.get("type") in _HEADER_TYPES:
            break
        if (b.get("text") or "").strip():
            body.append(b)
        if len(body) == 2:
            break
    if not body:
        return False
    if _is_prose(body[0]):
        return True
    first = (body[0].get("text") or "").strip()
    return (
        len(body) == 2
        and _is_prose(body[1])
        and not _is_reference(body[0])
        and not _is_affiliation(first)
    )


def _segment(blocks: list[dict[str, Any]]) -> list[tuple[int, int, bool]]:
    """Candidate article ranges ``(start, end, byline_open)``."""
    cuts: list[tuple[int, bool]] = []
    start = 0
    closed = False
    prose = 0
    for i, block in enumerate(blocks):
        btype = block.get("type")
        text = (block.get("text") or "").strip()
        if btype in _HEADER_TYPES:
            kind = _header_kind(text)
            if kind == "running":
                continue
            if kind == "byline":
                if _byline_opens(blocks, i):
                    if prose and i > start:
                        cuts.append((i, True))
                        start, prose = i, 0
                    closed = False
                elif prose:
                    closed = True
                continue
            if kind == "title" and closed and i > start:
                cuts.append((i, False))
                start, prose = i, 0
            closed = False
            continue
        if btype not in _BODY_TYPES and btype != "references":
            continue
        # Footnote affiliations / Received lines close only after the prose:
        # up front they are the article's own front matter.
        if (
            _is_reference(block)
            or _ACK_RE.match(text)
            or _CORRESPONDENCE_RE.search(text)
            or (
                prose >= _CLOSER_MIN_PROSE
                and (_is_footnote_affiliation(text) or _RECEIVED_RE.search(text))
            )
        ):
            closed = True
        elif _is_prose(block):
            prose += 1
            closed = False
    bounds = [(0, False), *cuts, (len(blocks), False)]
    return [
        (bounds[k][0], bounds[k + 1][0], bounds[k][1]) for k in range(len(bounds) - 1)
    ]


def _hard_close(span: list[dict[str, Any]]) -> bool:
    """The span's last body blocks hold references, an acknowledgement or a
    Received/Correspondence line (not just an affiliation)."""
    tail = [b for b in span if b.get("type") not in _HEADER_TYPES][-4:]
    for b in tail:
        text = (b.get("text") or "").strip()
        if (
            _is_reference(b)
            or _ACK_RE.match(text)
            or _RECEIVED_RE.search(text)
            or _CORRESPONDENCE_RE.search(text)
        ):
            return True
    return False


def _article(
    blocks: list[dict[str, Any]],
    start: int,
    end: int,
    byline_open: bool,
    title: str,
    target_doi: str,
) -> Article:
    span = blocks[start:end]
    head_parts: list[str] = []
    seen_body = 0
    for b in span:
        text = _clean(b.get("text") or "")
        if not text:
            continue
        if b.get("type") in _HEADER_TYPES:
            if _header_kind(text) == "running":
                continue
            head_parts.append(text)
        else:
            head_parts.append(text[:400])
            seen_body += 1
        if seen_body >= 2 or len(head_parts) >= 4:
            break
    head = " ".join(head_parts)
    full = " ".join(_clean(b.get("text") or "") for b in span)
    scored = title_body_score(title, head) if title else None
    full_scored = title_body_score(title, full) if title else None
    dois = frozenset(
        _norm_doi(m)
        for b in span
        if not _is_reference(b)
        for m in _DOI_RE.findall(b.get("text") or "")
    )
    evidence = byline_open or bool(target_doi and dois - {target_doi})
    for b in span:
        text = (b.get("text") or "").strip()
        if (b.get("type") in _HEADER_TYPES and _is_multi_byline(text)) or (
            b.get("type") in _BODY_TYPES and _is_name_affiliation(text)
        ):
            evidence = True
            break
    pages = [p for b in span if (p := b.get("page")) is not None]
    return Article(
        start=start,
        end=end,
        head=head,
        page_first=min(pages) if pages else None,
        page_last=max(pages) if pages else None,
        dois=dois,
        score=scored.score if scored is not None else 0.0,
        text_score=full_scored.score if full_scored is not None else 0.0,
        evidence=evidence,
        hard_close=_hard_close(span),
        byline_open=byline_open,
    )


def _pick(
    articles: tuple[Article, ...], title: str, target_doi: str
) -> tuple[int | None, str]:
    if target_doi:
        holders = [i for i, a in enumerate(articles) if target_doi in a.dois]
        if len(holders) == 1 and (
            not title or articles[holders[0]].score >= DOI_TITLE_FLOOR
        ):
            return holders[0], "doi"
    if title:
        best = max(range(len(articles)), key=lambda i: articles[i].score)
        others = [a.score for i, a in enumerate(articles) if i != best]
        if articles[best].score >= KEEP_SCORE and max(others) < OTHER_MAX_SCORE:
            return best, "title"
    return None, ""


def trim_to_article(
    blocks: list[dict[str, Any]], *, title: str = "", doi: str | None = None
) -> SpreadTrim:
    """Keep only the blocks of the article whose title / DOI is the target."""
    ranges = _segment(blocks)
    if len(ranges) < 2:
        return SpreadTrim(blocks=blocks, verdict="single")
    title = _clean(title)
    target_doi = _norm_doi(doi) if doi else ""
    articles = tuple(
        _article(blocks, s, e, bo, title, target_doi) for s, e, bo in ranges
    )

    kept, reason = _pick(articles, title, target_doi)
    if kept is None or not any(
        _is_prose(b) for b in blocks[articles[kept].start : articles[kept].end]
    ):
        if sum(a.evidence for a in articles) >= 2:
            why = (
                "no title or DOI to match"
                if not (title or target_doi)
                else "no confident match"
            )
            return SpreadTrim(
                blocks=blocks, verdict="ambiguous", articles=articles, reason=why
            )
        return SpreadTrim(blocks=blocks, verdict="single")

    def cuttable(a: Article) -> bool:
        # A segment opening on a reference entry is a bibliography.
        body = [
            b
            for b in blocks[a.start : a.end]
            if b.get("type") not in _HEADER_TYPES and (b.get("text") or "").strip()
        ]
        return (
            a.text_score < OTHER_MAX_SCORE
            and not (target_doi and target_doi in a.dois)
            and not (body and _is_reference(body[0]))
        )

    # Boundary into the kept article: its own match + a hard closer before.
    lead_ok = (
        kept > 0
        and (articles[kept].byline_open or articles[kept - 1].hard_close)
        and all(cuttable(a) for a in articles[:kept])
    )
    lo = kept if lead_ok else 0
    # Boundaries after it: the next segment must carry article evidence.
    hi = kept
    while hi + 1 < len(articles) and not (
        articles[hi + 1].evidence and all(cuttable(a) for a in articles[hi + 1 :])
    ):
        hi += 1
    if lo == 0 and hi == len(articles) - 1:
        return SpreadTrim(blocks=blocks, verdict="single")
    residual = lo < kept or hi > kept
    return SpreadTrim(
        blocks=blocks[articles[lo].start : articles[hi].end],
        verdict="trimmed",
        articles=articles,
        kept=(lo, hi),
        reason=reason,
        residual=residual,
    )
