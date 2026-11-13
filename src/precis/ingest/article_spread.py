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

**Foreign-prefix marker (gr515439).** A trim only fires when the spread
segments cleanly; a shared page can also leave just the *tail* of the
previous article ahead of the target's first page (pa39683: chunks 0-7 are
the Engel & Macko 14C tail + references before Rumelhart's title;
pa35223, pa35334 the same shape). Then nothing is cut and chunk 0 holds an
unrelated paper — title extraction and front-matter readers see the
foreign text. :func:`leading_foreign_ord` detects that case on stored
chunks — a hard closer in the prefix plus a confident own-title/DOI match
after it — and :func:`mark_article_start` stamps
``refs.meta['article_start_ord']``. Body chunks are append-only, so the
marker shifts readers' ``pos_range`` windows instead of deleting:
:func:`article_start_ord` is the one read helper.

**Spliced-issue detector (gr658690).** A whole scanned journal issue can
also arrive as "the PDF of one paper": several unrelated Letters/articles
concatenated into a single record whose title is junk (pa453299, pa453293
— no single correct title exists, so neither the block-level trim nor the
junk-title repair cascade can resolve it). :func:`detect_issue_splice`
recognizes that signature on stored body chunks — repeated acknowledgement
openers (one per spliced article), an abrupt mid-stream title+byline
restart, an incoherent mid-stream contents cluster — and requires at
least two of the three to fire, the same conservatism as the trim: a
wrong flag costs a human a split of a healthy paper. The signals are
measured from the article boundary — ``article_start_ord`` when stamped,
else :func:`foreign_tail_ord`'s structural derivation — so the mild
variant (one dominant article behind a stray foreign prefix, resolvable
by a normal title fix) never flags. :func:`note_issue_splice`
journals the verdict under ``ingest:spread`` / ``issue_splice`` and tags
the paper ``multi-article`` so the owner queue routes it to a split todo
instead of a title-fix todo.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from precis.utils.title_body import is_placeholder_title, title_body_score
from precis.utils.title_views import title_plain

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

#: ``ref_events.event`` of the spliced-issue journal row (source
#: ``ingest:spread``, same source as the trim's spread journal so the one
#: monitoring query covers both).
ISSUE_SPLICE_EVENT = "issue_splice"

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


# ---- Foreign-prefix marker (gr515439) --------------------------------
#
# ``trim_to_article`` runs on Marker blocks before chunking; a leftover
# case it cannot see is the previous article's *tail alone* ahead of the
# target's first page — no second full article to trim against, so the
# foreign head lands in the stored body. These run on ChunkRow-shaped
# rows (``.ord``/``.text``/``.chunk_kind``), post-ingest or at repair
# time, and stamp ``refs.meta['article_start_ord']`` so readers shift
# their windows rather than mutating the append-only body.

#: ``refs.meta`` key naming the first ord of the paper's own article
#: when the leading body chunks are the previous article's tail.
ARTICLE_START_ORD_META_KEY = "article_start_ord"

#: The prefix is *leading* — only this many ords count as its boundary.
_LEADING_ORD_CAP = 32


def _chunk_head_score(title: str, text: str) -> float:
    """``title_body_score`` against a chunk's text, 0 when unjudgeable."""
    if not title or not text.strip():
        return 0.0
    scored = title_body_score(title, text)
    return scored.score if scored is not None else 0.0


def _is_hard_close_chunk(chunk: Any) -> bool:
    """This chunk ends a foreign article's tail: its reference list, an
    acknowledgement, or a Received/Correspondence line."""
    if getattr(chunk, "chunk_kind", "") == "references":
        return True
    text = (getattr(chunk, "text", "") or "").strip()
    return bool(
        _ACK_RE.match(text)
        or _RECEIVED_RE.search(text)
        or _CORRESPONDENCE_RE.search(text)
    )


def leading_foreign_ord(
    chunks: Sequence[Any], *, title: str = "", doi: str | None = None
) -> int | None:
    """The ord where the paper's own article starts, when the leading
    body chunks are the previous article's tail — else ``None``.

    Same conservatism as :func:`trim_to_article`: a wrong mark hides real
    content, so all three signals must fire —

    * a *candidate*: the first non-``references`` chunk holding the
      target DOI or matching ``title`` at
      :func:`title_body_score` ``>= KEEP_SCORE``;
    * a *hard closer* in the chunks before it — the previous article's
      tail. A *title* candidate accepts any closer (``chunk_kind ==
      'references'``, a Received/accepted, an acknowledgement or a
      Correspondence line); a *DOI-only* candidate needs a
      ``references`` chunk, since a Received/acknowledgement/
      Correspondence line alone can also be the paper's own front
      matter ahead of its DOI line;
    * a prefix title score < ``OTHER_MAX_SCORE`` — when the chunks before
      the candidate already match the title they ARE the paper's own
      front matter, not foreign text.

    Only the leading :data:`_LEADING_ORD_CAP` ords are examined; a
    candidate at ord 0 (no prefix) or no candidate at all returns None.
    """
    title = _clean(title)
    if is_placeholder_title(title):
        title = ""
    target_doi = _norm_doi(doi) if doi else ""
    if not (title or target_doi):
        return None
    prefix: list[Any] = []
    for chunk in chunks:
        ord_ = int(getattr(chunk, "ord", 0) or 0)
        if ord_ >= _LEADING_ORD_CAP:
            break
        text = getattr(chunk, "text", "") or ""
        title_hit = _chunk_head_score(title, text) >= KEEP_SCORE
        doi_hit = bool(
            target_doi and target_doi in {_norm_doi(m) for m in _DOI_RE.findall(text)}
        )
        if (
            getattr(chunk, "chunk_kind", "") != "references"
            and prefix
            and (title_hit or doi_hit)
        ):
            closer_ok = (
                any(_is_hard_close_chunk(c) for c in prefix)
                if title_hit
                else any(getattr(c, "chunk_kind", "") == "references" for c in prefix)
            )
            if not closer_ok:
                return None
            joined = " ".join(getattr(c, "text", "") or "" for c in prefix)
            return ord_ if _chunk_head_score(title, joined) < OTHER_MAX_SCORE else None
        prefix.append(chunk)
    return None


def article_start_ord(
    store: Store, ref_id: int, *, conn: Connection | None = None
) -> int:
    """``refs.meta['article_start_ord']`` — the first ord of the paper's
    own article when a foreign prefix was marked (gr515439); ``0`` when
    unmarked or malformed. Readers add it to their ``pos_range`` windows
    so the foreign tail stays out of title extraction and front-matter
    checks."""

    def _do(c: Connection) -> int:
        row = c.execute(
            "SELECT meta->>%s FROM refs WHERE ref_id = %s",
            (ARTICLE_START_ORD_META_KEY, ref_id),
        ).fetchone()
        if not row or row[0] is None:
            return 0
        try:
            return max(0, int(row[0]))
        except (TypeError, ValueError):
            return 0

    if conn is not None:
        return _do(conn)
    with store.pool.connection() as c:
        return _do(c)


def mark_article_start(
    store: Store, ref_id: int, *, conn: Connection | None = None
) -> int | None:
    """Stamp ``refs.meta['article_start_ord']`` when the ref's leading
    body chunks are the previous article's tail (gr515439). Returns the
    marked ord, or ``None`` when no mark holds (the common case — no
    confident boundary is no mark, never a guess).

    Called at ingest (after the spread journal) and by the junk-title
    repair pass before it re-reads first-page chunks. Journals an
    ``ingest:spread`` / ``foreign_prefix`` ref_event so the mark is
    auditable; the chunks themselves are untouched (append-only).

    Everything runs on the caller's ``conn``: at ingest the ref and its
    identifiers are still uncommitted, so pool-side reads would see
    nothing and the mark would silently skip. The marker is kept in
    step with the chunks — re-deriving the same ord writes nothing (no
    duplicate event), a moved boundary overwrites, and a marker whose
    boundary no longer detects (re-ingested front matter) is dropped
    with a ``foreign_prefix_cleared`` event.
    """
    from precis.ingest.add import ARTICLE_SPREAD_SOURCE

    def _do(c: Connection) -> int | None:
        row = c.execute(
            "SELECT title, "
            "  (SELECT min(id_value) FROM ref_identifiers "
            "   WHERE ref_id = r.ref_id AND id_kind = 'doi'), "
            "  meta->>%s "
            "FROM refs r WHERE ref_id = %s AND retired_at IS NULL",
            (ARTICLE_START_ORD_META_KEY, ref_id),
        ).fetchone()
        if row is None:
            return None
        title, doi, stamped = row[0] or "", row[1], row[2]
        current: int | None = None
        if stamped is not None:
            try:
                n = int(stamped)
                current = n if n > 0 else None
            except (TypeError, ValueError):
                current = None

        chunks = store.chunks.list_chunks_for_ref(ref_id, conn=c)
        start = leading_foreign_ord(chunks, title=title, doi=doi)
        if start is None:
            if current is None:
                return None
            c.execute(
                "UPDATE refs SET meta = meta - %s::text, updated_at = now() "
                "WHERE ref_id = %s",
                (ARTICLE_START_ORD_META_KEY, ref_id),
            )
            store.append_event(
                ref_id,
                source=ARTICLE_SPREAD_SOURCE,
                event="foreign_prefix_cleared",
                payload={"was": current},
                conn=c,
            )
            return None
        if start == current:
            return start
        store.update_paper_fields(
            ref_id,
            meta_patch={ARTICLE_START_ORD_META_KEY: start},
            source=ARTICLE_SPREAD_SOURCE,
            conn=c,
        )
        payload: dict[str, Any] = {
            ARTICLE_START_ORD_META_KEY: start,
            "foreign_ords": [int(c2.ord) for c2 in chunks if int(c2.ord) < start],
        }
        if current is not None:
            payload["was"] = current
        store.append_event(
            ref_id,
            source=ARTICLE_SPREAD_SOURCE,
            event="foreign_prefix",
            payload=payload,
            conn=c,
        )
        return start

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


# ---- Spliced-issue detector (gr658690) --------------------------------
#
# ``trim_to_article`` runs on Marker blocks before chunking and keeps the
# target article; a whole scanned issue (several unrelated articles, no
# single title) defeats it at the identity-matching step. These run on
# stored body chunks instead, so they cover both fresh ingests (wired into
# ``precis.ingest.add`` next to ``mark_article_start``) and legacy records
# swept by the reconcile pass.

#: Minimum stored body chunks before the splice signature may fire — a
#: spliced scan is an issue's worth of text; a handful of chunks is never
#: an issue.
_SPLICE_MIN_BODY_CHUNKS = 10

#: A title/byline restart or a contents cluster only counts this many ords
#: into the article — the paper's own title block sits at the first ords of
#: its span, so an article-boundary signature there is the paper's own front
#: matter. Measured from ``start_ord``, the established article boundary
#: (gr515439): ords below it are the previous article's tail, not the paper's.
_SPLICE_MIDSTREAM_MIN_ORD = 3

#: A mid-stream contents cluster: an issue-level "Contents" page lands
#: between articles, never inside one.
_TOC_HEAD_RE = re.compile(r"^(?:contents|table of contents)\b", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class IssueSplice:
    """The spliced-issue signature over a paper's stored body chunks.

    ``ack_ords`` holds the ords of every acknowledgement opener; a genuine
    single article has at most one. ``restart_ord`` is the first
    mid-stream chunk opening on a non-standard title header immediately
    followed by a multi-name byline — an article boundary mid-body.
    ``toc_ord`` is the first mid-stream contents-heading chunk.
    ``start_ord`` is the established article boundary the signals were
    measured from (``0`` when none was established — stamped or
    structural; see :func:`detect_issue_splice`).
    """

    ack_ords: tuple[int, ...]
    restart_ord: int | None
    toc_ord: int | None
    body_chunks: int
    start_ord: int = 0

    @property
    def signal_count(self) -> int:
        """How many of the three independent splice signals fired."""
        return (
            (len(self.ack_ords) >= 2)
            + (self.restart_ord is not None)
            + (self.toc_ord is not None)
        )

    def audit(self) -> dict[str, Any]:
        """The ``ref_events`` payload."""
        out: dict[str, Any] = {
            "body_chunks": self.body_chunks,
            "ack_ords": list(self.ack_ords),
        }
        if self.start_ord:
            out["start_ord"] = self.start_ord
        if self.restart_ord is not None:
            out["restart_ord"] = self.restart_ord
        if self.toc_ord is not None:
            out["toc_ord"] = self.toc_ord
        return out


def _head_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _opens_article(body: Sequence[Any], idx: int) -> bool:
    """Chunk ``idx`` opens an article: its first line is a non-standard
    title head (:func:`_header_kind` == ``title``) and its own next line —
    or, when it has none, the next chunk's first line — is a multi-name
    byline. The one opening signature the restart signal and the
    structural boundary (:func:`foreign_tail_ord`) both test."""
    lines = _head_lines(getattr(body[idx], "text", "") or "")
    if not lines or _header_kind(lines[0]) != "title":
        return False
    nxt = lines[1] if len(lines) > 1 else ""
    if not _is_multi_byline(nxt) and idx + 1 < len(body):
        nxt_lines = _head_lines(getattr(body[idx + 1], "text", "") or "")
        nxt = nxt_lines[0] if nxt_lines else ""
    return _is_multi_byline(nxt)


def detect_issue_splice(
    chunks: Sequence[Any], *, start_ord: int = 0
) -> IssueSplice | None:
    """Detect a scanned-journal-issue splice over stored body chunks.

    The signature is two or more of three independent signals, each the
    fingerprint of an article boundary that a single paper does not have
    mid-body:

    * **repeated acknowledgements** — acknowledgement openers
      (:data:`_ACK_RE` on a chunk's first line) at two or more ords; a
      genuine article has exactly one;
    * **mid-stream restart** — a chunk this far past the front matter
      opening on a non-standard title header (:func:`_header_kind` ==
      ``title``) whose own next line, or the next non-empty chunk's first
      line, is a multi-name byline;
    * **contents cluster** — a mid-stream chunk opening on a contents /
      table-of-contents heading.

    ``start_ord`` is the established article boundary
    (``refs.meta['article_start_ord']``, gr515439 — or, when a junk title
    and no DOI leave the identity anchor empty, the structural boundary
    callers derive with :func:`foreign_tail_ord`): ords below it are the
    previous article's tail, not the paper's, so they feed no signal, and
    the front-matter exemption covers the first
    :data:`_SPLICE_MIDSTREAM_MIN_ORD` ords *of the paper's own span*. That
    matters for the mild variant (pa478920): a stray foreign
    acknowledgement/reference prefix ahead of one dominant article reads
    as repeated acks plus a "restart" at the article's own title —
    resolvable by a normal title fix, never a split.

    Also requires at least :data:`_SPLICE_MIN_BODY_CHUNKS` non-empty body
    chunks at or past ``start_ord``. Pure — never raises; returns ``None``
    (no confident splice) unless two signals fire, the same conservatism as
    :func:`trim_to_article`: a wrong flag costs a human a split of a
    healthy paper.
    """
    start_ord = max(0, int(start_ord or 0))
    body = [
        c
        for c in chunks
        if int(getattr(c, "ord", 0) or 0) >= start_ord
        and (getattr(c, "text", "") or "").strip()
    ]
    if len(body) < _SPLICE_MIN_BODY_CHUNKS:
        return None
    ack_ords: list[int] = []
    restart_ord: int | None = None
    toc_ord: int | None = None
    for idx, chunk in enumerate(body):
        ord_ = int(getattr(chunk, "ord", 0) or 0)
        lines = _head_lines(getattr(chunk, "text", "") or "")
        head = lines[0] if lines else ""
        if _ACK_RE.match(head):
            ack_ords.append(ord_)
        if ord_ < start_ord + _SPLICE_MIDSTREAM_MIN_ORD:
            continue
        if toc_ord is None and _TOC_HEAD_RE.match(head):
            toc_ord = ord_
        if restart_ord is None and _opens_article(body, idx):
            restart_ord = ord_
    splice = IssueSplice(
        ack_ords=tuple(ack_ords),
        restart_ord=restart_ord,
        toc_ord=toc_ord,
        body_chunks=len(body),
        start_ord=start_ord,
    )
    return splice if splice.signal_count >= 2 else None


def _is_references_chunk(chunk: Any) -> bool:
    """A stored references chunk (by kind or by text shape) — a
    bibliography in the leading ords is never the paper's own front
    matter."""
    if getattr(chunk, "chunk_kind", "") == "references":
        return True
    text = (getattr(chunk, "text", "") or "").strip()
    return bool(_REFERENCE_RE.match(text) and _YEAR_RE.search(text))


def foreign_tail_ord(chunks: Sequence[Any]) -> int | None:
    """The ord where the paper's own article starts, when the leading
    body chunks are a foreign article's tail — derived structurally for
    the junk-titled, DOI-less records :func:`leading_foreign_ord` has no
    anchor for (the gr658690 mild variant, pa478920: a few stray
    reference-list chunks plus the previous article's acknowledgement
    ahead of the record's one dominant article).

    The boundary is the body's *first* article opening — the same
    title-head + multi-name-byline signature the restart signal tests
    (:func:`_opens_article`) — provided the chunks before it are a proven
    tail: they hold a references chunk. The stricter closer is the
    DOI-only rule of :func:`leading_foreign_ord`: a Received/
    acknowledgement/Correspondence line alone can be the paper's own
    front matter, but a bibliography in the leading ords is not. A first
    opening with no proven tail behind it is the paper's own head (or an
    unprovable one) — no boundary; later openings are the splice
    detector's restart signal, never a *leading* boundary. Only the
    leading :data:`_LEADING_ORD_CAP` ords count, the same cap as
    :func:`leading_foreign_ord`.

    Deliberately detection-local: without an identity anchor it is never
    stamped on ``refs.meta['article_start_ord']`` — the gr515439 marker
    stays title/DOI-anchored.
    """
    body = [
        c
        for c in chunks
        if int(getattr(c, "ord", 0) or 0) >= 0
        and (getattr(c, "text", "") or "").strip()
    ]
    prefix: list[Any] = []
    for idx, chunk in enumerate(body):
        ord_ = int(getattr(chunk, "ord", 0) or 0)
        if ord_ >= _LEADING_ORD_CAP:
            break
        if _opens_article(body, idx):
            if prefix and any(_is_references_chunk(c) for c in prefix):
                return ord_
            return None
        prefix.append(chunk)
    return None


#: A whole-line emphasis wrapper — ``**Title**`` / ``_Title_`` front-matter
#: layout emphasis, never part of the title text itself.
_WRAP_EMPH_RE = re.compile(r"(\*{1,2}|_{1,2})\s*(\S.*?)\s*\1", re.S)

#: One ``<sup>`` span on a title head — a footnote/affiliation marker or a
#: real superscript, decided per-span by :func:`_drop_marker_sup`.
_SUP_SPAN_RE = re.compile(r"<sup\b[^>]*>(.*?)</sup>", re.I | re.S)

#: One footnote/affiliation mark: a digit run, a footnote symbol or a
#: single lowercase affiliation letter — ``1``, ``12``, ``†``, ``*``, ``a``.
_MARK = r"(?:\d+|[†‡§¶*#‖]|[a-z])"

#: A mark *list* — ``1,2``, ``1–3``, ``a,b``, ``*,†``, ``1 a`` — is always
#: furniture: an exponent, a charge or an oxidation state is one token,
#: never a separator-joined list. Hyphen-minus stays OUT of the
#: separator class: it is the math minus, not a list join, so
#: ``x<sup>n-1</sup>`` (review td660456) and any other hyphen-joined
#: exponent expression survive as the title's own text. (A leading
#: ``-`` can't start a mark either, so a minus exponent like
#: ``<sup>-1</sup>`` never matches.)
_MARK_LIST_RE = re.compile(rf"{_MARK}(?:[\s,;–—]+{_MARK})+")

#: A trailing singleton of ``‡§¶#‖`` is furniture — none of these has a
#: scientific-superscript reading in a paper title. ``†`` is deliberately
#: absent: it doubles as the Hermitian-conjugate/adjoint dagger, so
#: ``Properties of H<sup>†</sup>`` is the title's own text (review
#: td660456). A lone ``†`` survives like any other ambiguous lone mark
#: and drops only inside a list or with furniture proof.
_FOOTNOTE_SYMBOL_RE = re.compile(r"[‡§¶#‖]+")

#: A lone mark (``1``, ``a``, ``*``, ``†``) is ambiguous —
#: ``x<sup>2</sup>`` is an exponent, ``K<sup>*</sup>`` a star and
#: ``H<sup>†</sup>`` the Hermitian conjugate, so it drops only with
#: extra furniture proof: bold/strong styling a scientific superscript
#: never carries (``<sup><b>1</b></sup>`` — italic doesn't count,
#: ``<i>n</i>`` is how a variable exponent arrives), or a whitespace
#: gap before the span, which a superscript's own base never leaves.
_LONE_MARK_RE = re.compile(_MARK)
_SUP_BOLD_RE = re.compile(r"</?(?:b|strong)\b[^>]*>", re.I)

#: Tags nested inside a marker span are stripped before the content
#: tests — though ``<b>``/``<strong>`` is itself furniture proof
#: (:data:`_SUP_BOLD_RE`), and the footnote symbols ``*``/``†`` are
#: content, never noise.
_SUP_INNER_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

#: A footnote marker *trails* the title head ("Title<sup>1,†</sup>"):
#: after it only whitespace, terminal punctuation and emphasis closers
#: remain. A ``<sup>`` with title text still after it sat mid-title, where
#: a superscript is the formula's own ("the x<sup>2</sup> term"), never a
#: dangling footnote digit. Necessary but not sufficient — the content
#: must also be something a superscript cannot be (see
#: :func:`_drop_marker_sup`).
_SUP_TAIL_NOISE_RE = re.compile(r"[\s*_~^`.,;:!?()\[\]'\"–—-]*")


def _drop_marker_sup(m: re.Match[str]) -> str:
    """``<sup>`` → ``""`` for a proven footnote/affiliation marker; any
    other span verbatim so :func:`title_plain` renders its content.

    Proven needs BOTH legs (review td660456): the span trails the line
    AND its content is something a superscript cannot be — a mark list
    (:data:`_MARK_LIST_RE` — hyphen-minus never separates), an
    always-furniture symbol (:data:`_FOOTNOTE_SYMBOL_RE`, ``‡§¶#‖`` —
    ``†`` is not one, see there) or a lone mark carrying furniture proof
    (:data:`_SUP_BOLD_RE` or a whitespace gap before the span).
    Marker-shaped plus trailing alone is not proof: a lone
    ``<sup>2</sup>``/``<sup>*</sup>``/``<sup>†</sup>`` at line end can be
    the title's own ``x²``/``K*``/``H†``, so ambiguous marks stay for
    ``title_plain``."""
    inner = _SUP_INNER_TAG_RE.sub("", m.group(1)).strip()
    tail = _SUP_INNER_TAG_RE.sub("", m.string[m.end() :])
    if not _SUP_TAIL_NOISE_RE.fullmatch(tail):
        return m.group(0)
    if _MARK_LIST_RE.fullmatch(inner) or _FOOTNOTE_SYMBOL_RE.fullmatch(inner):
        return ""
    if _LONE_MARK_RE.fullmatch(inner) and (
        _SUP_BOLD_RE.search(m.group(1))
        or (m.start() > 0 and m.string[m.start() - 1].isspace())
    ):
        return ""
    return m.group(0)


def _storable_title(line: str) -> str:
    """A chunk head line → a storable title: markdown heading marks and
    wrapping emphasis (the front-matter layout, not the text) off, proven
    footnote/affiliation ``<sup>`` markers dropped whole
    (:func:`_drop_marker_sup` — real superscripts stay), then
    :func:`~precis.utils.title_views.title_plain` flattens the residual
    inline markup (``mml:math``, ``<sub>``, kept ``<sup>``, ``$…$``)."""
    s = _SUP_SPAN_RE.sub(_drop_marker_sup, line.strip())
    s = re.sub(r"^#{1,6}\s+", "", s)
    for _ in range(2):
        m = _WRAP_EMPH_RE.fullmatch(s)
        if not m:
            break
        s = m.group(2).strip()
    return title_plain(s)


def front_matter_title(chunks: Sequence[Any], *, start_ord: int = 0) -> str | None:
    """The title a paper's own front matter states, or ``None`` (gr658887).

    The test is the article-opening signature the restart signal and the
    structural boundary share (:func:`_opens_article`): the first body
    chunk at or past ``start_ord`` must open on a non-standard title head
    immediately followed by a multi-name byline. Only that pairing makes
    the head line a *title* — alone it could be a running head, a
    furniture line or a section label.

    ``start_ord`` is the established article boundary
    (``refs.meta['article_start_ord']``, or the structural
    :func:`foreign_tail_ord` derivation when no anchor exists to stamp
    one) — ords below it are the previous article's tail, so a foreign
    title head can never become this paper's title. The returned string
    is store-ready: :func:`_storable_title` has already removed the
    layout emphasis and any *proven* trailing footnote/affiliation
    ``<sup>`` markers (:func:`_drop_marker_sup` — a lone ambiguous mark
    stays, being possibly the title's own superscript), and rendered
    residual inline markup to its plain view. Pure — never raises.
    """
    start_ord = max(0, int(start_ord or 0))
    body = [
        c
        for c in chunks
        if int(getattr(c, "ord", 0) or 0) >= start_ord
        and (getattr(c, "text", "") or "").strip()
    ]
    if not body or not _opens_article(body, 0):
        return None
    return _storable_title(_head_lines(getattr(body[0], "text", "") or "")[0]) or None


def note_issue_splice(
    store: Store, ref_id: int, *, conn: Connection | None = None
) -> IssueSplice | None:
    """Journal + tag a stored body that carries the spliced-issue signature.

    Journals ``ingest:spread`` / :data:`ISSUE_SPLICE_EVENT` with the
    signature audit and tags the paper ``multi-article`` (the tag's meaning
    — "the stored body still holds several articles" — is exactly right
    for a splice). Idempotent: a ref that already has the journal row is
    never re-examined, so the daily sweep touches each spliced record
    once. Returns the detected signature, or ``None`` when the body
    doesn't carry it (or it was already flagged).

    The boundary is established first (:func:`mark_article_start`, kept
    in step with the chunks): the mild variant — one dominant article
    behind a stray foreign prefix — must not flag, and its acks and
    title only stop reading as a splice once the signals are measured
    from ``article_start_ord``. When the title is junk and no DOI is
    stored, the identity anchor finds nothing, so the boundary falls
    back to :func:`foreign_tail_ord` — the structural first-opening-
    behind-a-references-tail derivation; without it that same mild
    variant reads as a splice and is wrongly excluded from title repair.

    Everything runs on the caller's ``conn`` when given: at ingest the ref
    and its chunks are still uncommitted, so pool-side reads would see
    nothing and the flag would silently skip.
    """
    from precis.ingest.add import ARTICLE_SPREAD_SOURCE, MULTI_ARTICLE_TAG
    from precis.store.types import Tag

    def _do(c: Connection) -> IssueSplice | None:
        if (
            c.execute(
                "SELECT 1 FROM ref_events WHERE ref_id = %s "
                "AND source = %s AND event = %s LIMIT 1",
                (ref_id, ARTICLE_SPREAD_SOURCE, ISSUE_SPLICE_EVENT),
            ).fetchone()
            is not None
        ):
            return None
        mark_article_start(store, ref_id, conn=c)
        chunks = store.chunks.list_chunks_for_ref(ref_id, conn=c)
        start = (
            article_start_ord(store, ref_id, conn=c) or foreign_tail_ord(chunks) or 0
        )
        splice = detect_issue_splice(chunks, start_ord=start)
        if splice is None:
            return None
        store.append_event(
            ref_id,
            source=ARTICLE_SPREAD_SOURCE,
            event=ISSUE_SPLICE_EVENT,
            payload=splice.audit(),
            conn=c,
        )
        store.add_tag(ref_id, Tag.open(MULTI_ARTICLE_TAG), set_by="system", conn=c)
        return splice

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)
