"""Parse OPS published-data XML into a ``ParsedPatent`` structure.

EPO OPS returns variants of the WIPO ST.36 schema (with OPS-specific
``ops:`` extensions). Three endpoints feed the handler:

* ``biblio`` — bibliographic metadata: title, abstract, applicants,
  inventors, dates, classifications.
* ``description`` — patent body, paragraphs in ``<p>`` elements
  inside ``<description>``.
* ``claims`` — claim text in ``<claim>`` and ``<claim-text>``
  elements inside ``<claims>``.

We use ``xml.etree.ElementTree`` rather than ``lxml`` here so the
parser has zero hard dependencies. (``lxml`` is in the optional
``patent`` extra mostly for the OPS client; if it's installed we
could swap in a faster parser later.)

ST.36 namespaces vary by endpoint but the element names we care
about are stable across versions. We strip namespaces wherever they
appear and match on local-name — defensive parsing wins long-term
forgiveness when EPO bumps a minor schema version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET


@dataclass(frozen=True, slots=True)
class ParsedPatent:
    """Structured form of one OPS published-data record.

    All fields are optional / defaultable — we never crash on a
    sparse record. ``description_paragraphs`` and ``claim_texts``
    are the body sections the ingest pipeline lifts into ``blocks``.
    """

    title: str
    abstract: str | None = None
    publication_date: str | None = None  # YYYY-MM-DD
    application_date: str | None = None
    family_id: str | None = None
    applicants: list[dict[str, str]] = field(default_factory=list)
    inventors: list[dict[str, str]] = field(default_factory=list)
    cpc_classes: list[str] = field(default_factory=list)
    ipc_classes: list[str] = field(default_factory=list)
    description_paragraphs: list[str] = field(default_factory=list)
    claim_texts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers — namespace-blind XML traversal
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    """Return ``tag`` without an XML namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def _findall(node: ET.Element, name: str) -> list[ET.Element]:
    """Find every descendant whose local-name matches ``name``."""
    return [el for el in node.iter() if _local(el.tag) == name]


def _find(node: ET.Element, name: str) -> ET.Element | None:
    """First descendant whose local-name matches, or None."""
    for el in node.iter():
        if _local(el.tag) == name:
            return el
    return None


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    """Direct children whose local-name matches."""
    return [el for el in node if _local(el.tag) == name]


def _text(el: ET.Element | None) -> str:
    """Concatenated text content, whitespace-collapsed."""
    if el is None:
        return ""
    raw = "".join(el.itertext())
    return re.sub(r"\s+", " ", raw).strip()


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def parse_patent(
    *,
    biblio_xml: bytes | None = None,
    description_xml: bytes | None = None,
    claims_xml: bytes | None = None,
) -> ParsedPatent:
    """Combine biblio + description + claims XML into a ``ParsedPatent``.

    Any of the three may be None; the corresponding fields stay
    empty. Title is mandatory — if biblio is missing or has no
    title we synthesise the placeholder ``"(untitled patent)"``
    rather than failing, so the ingest pipeline still produces a
    queryable ref.
    """
    title = "(untitled patent)"
    abstract: str | None = None
    publication_date: str | None = None
    application_date: str | None = None
    family_id: str | None = None
    applicants: list[dict[str, str]] = []
    inventors: list[dict[str, str]] = []
    cpc_classes: list[str] = []
    ipc_classes: list[str] = []
    description_paragraphs: list[str] = []
    claim_texts: list[str] = []

    if biblio_xml:
        root = _safe_root(biblio_xml)
        if root is not None:
            title = _extract_title(root) or title
            abstract = _extract_abstract(root)
            publication_date = _extract_publication_date(root)
            application_date = _extract_application_date(root)
            family_id = _extract_family_id(root)
            applicants = _extract_parties(root, party_kind="applicant")
            inventors = _extract_parties(root, party_kind="inventor")
            cpc_classes = _extract_classifications(root, scheme="cpc")
            ipc_classes = _extract_classifications(root, scheme="ipc")

    if description_xml:
        root = _safe_root(description_xml)
        if root is not None:
            description_paragraphs = _extract_paragraphs(root)

    if claims_xml:
        root = _safe_root(claims_xml)
        if root is not None:
            claim_texts = _extract_claims(root)

    return ParsedPatent(
        title=title,
        abstract=abstract,
        publication_date=publication_date,
        application_date=application_date,
        family_id=family_id,
        applicants=applicants,
        inventors=inventors,
        cpc_classes=cpc_classes,
        ipc_classes=ipc_classes,
        description_paragraphs=description_paragraphs,
        claim_texts=claim_texts,
    )


# ---------------------------------------------------------------------------
# Internals — section extractors
# ---------------------------------------------------------------------------


def _safe_root(xml_bytes: bytes) -> ET.Element | None:
    """Parse XML, return root, or None on malformed input.

    OPS occasionally returns truncated XML on rate-limit edges; we'd
    rather degrade to "empty section" than crash the whole ingest.
    """
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None


def _extract_title(root: ET.Element) -> str | None:
    """Pick the English title if present, else any title."""
    titles = _findall(root, "invention-title")
    if not titles:
        return None
    # Prefer en-language title.
    for t in titles:
        lang = t.attrib.get("lang") or t.attrib.get(
            "{http://www.w3.org/XML/1998/namespace}lang"
        )
        if lang and lang.lower().startswith("en"):
            return _text(t) or None
    return _text(titles[0]) or None


def _extract_abstract(root: ET.Element) -> str | None:
    abstracts = _findall(root, "abstract")
    if not abstracts:
        return None
    for a in abstracts:
        lang = a.attrib.get("lang") or a.attrib.get(
            "{http://www.w3.org/XML/1998/namespace}lang"
        )
        if lang and lang.lower().startswith("en"):
            txt = _text(a)
            if txt:
                return txt
    txt = _text(abstracts[0])
    return txt or None


def _extract_publication_date(root: ET.Element) -> str | None:
    """Find publication date, return ISO YYYY-MM-DD."""
    for ref in _findall(root, "publication-reference"):
        for date_el in _findall(ref, "date"):
            iso = _format_date(_text(date_el))
            if iso:
                return iso
    # Fallback: any <date> at the top of the doc.
    for date_el in _findall(root, "date"):
        iso = _format_date(_text(date_el))
        if iso:
            return iso
    return None


def _extract_application_date(root: ET.Element) -> str | None:
    for ref in _findall(root, "application-reference"):
        for date_el in _findall(ref, "date"):
            iso = _format_date(_text(date_el))
            if iso:
                return iso
    return None


def _extract_family_id(root: ET.Element) -> str | None:
    """OPS surfaces ``family-id`` as an attribute on the publication ref."""
    for ref in _findall(root, "publication-reference"):
        fam = ref.attrib.get("family-id")
        if fam:
            return fam
    # Some OPS variants put it in an ops:patent-family element.
    fam_el = _find(root, "patent-family")
    if fam_el is not None:
        fid = fam_el.attrib.get("family-id")
        if fid:
            return fid
    return None


def _extract_parties(root: ET.Element, *, party_kind: str) -> list[dict[str, str]]:
    """Extract applicants or inventors as ``[{"name": ..., "country": ...}, ...]``.

    OPS structure (typical):

        <applicants>
            <applicant>
                <applicant-name>
                    <name>SIEMENS AG</name>
                </applicant-name>
                <residence>
                    <country>DE</country>
                </residence>
            </applicant>
            ...
        </applicants>

    We're permissive: any ``<<party_kind>>`` (singular) under any
    container counts; we read its first ``<name>`` text and any
    ``<country>`` text.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for party in _findall(root, party_kind):
        # Skip OPS data-format duplicates (epodoc + original) — we
        # take the first occurrence per (name, country) pair.
        name_el = _find(party, "name")
        country_el = _find(party, "country")
        name = _text(name_el)
        country = _text(country_el)
        if not name:
            continue
        key = (name.lower(), country.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "country": country})
    return out


def _extract_classifications(root: ET.Element, *, scheme: str) -> list[str]:
    """Extract CPC or IPC classifications as a deduped list of strings.

    CPC structure (OPS):

        <classifications-cpc>
            <classification-cpc>
                <text>B01J27/24</text>
            </classification-cpc>
        </classifications-cpc>

    IPC has a similar shape but with class/subclass/group split into
    sub-elements; we render them back into a compact ``B01J27/24``-
    style string.
    """
    out: list[str] = []
    seen: set[str] = set()

    if scheme == "cpc":
        for cls in _findall(root, "classification-cpc"):
            txt = _text(_find(cls, "text"))
            if not txt:
                # Fall back to compose-from-parts.
                txt = _compose_classification(cls)
            if txt and txt not in seen:
                seen.add(txt)
                out.append(txt)
    else:  # ipc
        for cls in _findall(root, "classification-ipcr"):
            txt = _text(_find(cls, "text"))
            if not txt:
                txt = _compose_classification(cls)
            if txt and txt not in seen:
                seen.add(txt)
                out.append(txt)
        # Some OPS variants use plain <classification-ipc>.
        for cls in _findall(root, "classification-ipc"):
            txt = _text(_find(cls, "text"))
            if txt and txt not in seen:
                seen.add(txt)
                out.append(txt)
    return out


def _compose_classification(cls: ET.Element) -> str:
    """Reassemble section/class/subclass/group/subgroup into a compact code."""
    parts: list[str] = []
    for tag_name in ("section", "class", "subclass"):
        el = _find(cls, tag_name)
        if el is not None:
            parts.append(_text(el))
    head = "".join(parts)
    main = _text(_find(cls, "main-group"))
    sub = _text(_find(cls, "subgroup"))
    if main and sub:
        return f"{head}{main}/{sub}"
    if main:
        return f"{head}{main}"
    return head or ""


def _extract_paragraphs(root: ET.Element) -> list[str]:
    """Description body — one entry per ``<p>`` paragraph.

    Empty and boilerplate paragraphs are dropped. Long ones are
    kept whole; the ingest layer handles further chunking if it
    ever wants to.

    Boilerplate filter: OPS description XML interleaves page-header
    fragments (``PATENT``, ``ATTORNEY DOCKET NO: …``, bare page
    numbers) between real numbered paragraphs. Without filtering
    these become full-fledged blocks, get embedded, and return as
    noise top-K hits for any unrelated query (MCP critic: searching
    ``'food'`` returns 10 ``_(PATENT)_`` rows). We strip an optional
    ``[NNNN]`` paragraph-number prefix for the pattern check,
    then drop the paragraph when the remainder matches known
    boilerplate or is too short to be meaningful content.
    """
    out: list[str] = []
    for p in _findall(root, "p"):
        txt = _text(p)
        if txt and not _is_boilerplate_paragraph(txt):
            out.append(txt)
    return out


# Paragraphs matching these patterns are page-header boilerplate,
# not real patent content. Checked after stripping the ``[NNNN]``
# paragraph-number prefix. Patterns are case-insensitive.
_BOILERPLATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^PATENT$", re.IGNORECASE),
    re.compile(r"^ATTORNEY DOCKET NO\b.*$", re.IGNORECASE),
    re.compile(r"^\d+$"),  # bare page number
    re.compile(r"^page\s+\d+(\s+of\s+\d+)?$", re.IGNORECASE),
)

# Minimum meaningful paragraph length after prefix strip.  Ten chars
# rejects isolated formula captions (``Figure 1``) but keeps short
# real paragraphs (``[0001] Described here.``).
_MIN_PARAGRAPH_CHARS = 10

_PARA_NUM_PREFIX = re.compile(r"^\s*\[\s*\d+\s*\]\s*")


def _is_boilerplate_paragraph(txt: str) -> bool:
    """Return True if ``txt`` is a recognised page-header fragment.

    Strips an optional ``[NNNN]`` prefix before matching so the
    same patterns catch both numbered and unnumbered variants.
    """
    body = _PARA_NUM_PREFIX.sub("", txt).strip()
    if not body:
        return True
    if len(body) < _MIN_PARAGRAPH_CHARS:
        return True
    for pat in _BOILERPLATE_PATTERNS:
        if pat.match(body):
            return True
    return False


def _extract_claims(root: ET.Element) -> list[str]:
    """Each independent + dependent claim → one string.

    OPS structure: ``<claims><claim><claim-text>...</claim-text></claim>...</claims>``.
    Some variants nest a single ``<claim-text>`` per ``<claim>``; others
    put multiple. **The ``<claim-text>`` elements are arbitrary fragments,
    not per-claim** — in the wild a single ``<claim>`` holds the entire
    claims section (one claim spans several ``<claim-text>``, or one
    ``<claim-text>`` holds several claims), so concatenating per ``<claim>``
    and stopping there collapses a 20-claim patent into one giant block.
    The reliable per-claim boundary is the sequential claim *number*, so
    each concatenated block is then split by :func:`_split_claim_run`.

    Leading boilerplate is stripped from each claim — OPS inlines the
    page-header ``PATENT ATTORNEY DOCKET NO: ... CLAIMS`` banner into
    the first claim's text on many US / WO filings, which otherwise
    surfaces as a search hit for unrelated queries.
    """
    blocks: list[str] = []
    for claim in _findall(root, "claim"):
        # If this claim contains nested claims (rare), only the top-level claim text.
        texts = [_text(t) for t in _children(claim, "claim-text")]
        joined = " ".join(t for t in texts if t)
        if not joined:
            joined = _text(claim)
        joined = _strip_claim_boilerplate(joined.strip())
        if joined:
            blocks.append(joined)
    if not blocks:
        # Fallback: some OPS responses skip the wrapper and put claim-text
        # children directly under <claims>.
        for ct in _findall(root, "claim-text"):
            txt = _strip_claim_boilerplate(_text(ct).strip())
            if txt:
                blocks.append(txt)
    out: list[str] = []
    for block in blocks:
        out.extend(_split_claim_run(block))
    return out


#: A claim boundary inside a concatenated claims block: a claim number at a
#: token boundary (``12.`` / ``12 .``) followed by whitespace then claim-
#: opening text (an optional quote/paren then an uppercase letter). The
#: uppercase-follow lookahead keeps out mid-claim numerals like ``step 2 of``
#: or ``0.5 mol``; the caller additionally drops a number whose preceding
#: word is a cross-reference (``claim``, ``figure``, …).
_CLAIM_BOUNDARY_RE = re.compile(r'(?:(?<=\s)|\A)(\d{1,3})\s*\.\s+(?=["(\[]?[A-Z])')

#: Words that commonly precede a bare ``<n>.`` inside a claim body — a
#: cross-reference or figure/step callout, never a new-claim boundary.
_CLAIM_REF_PRECEDERS = frozenset(
    {
        "claim",
        "claims",
        "figure",
        "figures",
        "fig",
        "figs",
        "table",
        "tables",
        "example",
        "embodiment",
        "step",
        "item",
        "formula",
        "phase",
        "aspect",
        "paragraph",
        "section",
        "no",
    }
)

#: The claims-section header OPS leaves before claim 1 (``CLAIMS``, ``What
#: is claimed is:``, ``We claim:`` …). Stripped before splitting so its
#: trailing ``claims`` word doesn't get mistaken for a claim-reference
#: preceder that would reject claim 1's own boundary.
_CLAIM_SECTION_HEADER_RE = re.compile(
    r"\A\s*(?:CLAIMS?|WHAT\s+IS\s+CLAIMED(?:\s+IS)?|WE\s+CLAIM|I\s+CLAIM)"
    r"\s*[:.]?\s+(?=\d)",
    re.IGNORECASE,
)


def _split_claim_run(text: str) -> list[str]:
    """Split a block that concatenates a run of numbered claims
    (``1. … 2. … 3. …``) into one string per claim.

    Accepts only a **monotonic 1, 2, 3, …** sequence (a patent's claims
    always start at 1 and increment), and ignores a number whose preceding
    word is a cross-reference (``the method of claim 2``, ``Figure 3.``).
    Returns the input unchanged (single-item list) when no ``1.``-anchored
    run of ≥ 2 claims is found — so a genuinely single claim, or claims OPS
    already delivered as proper per-claim ``<claim>`` elements, pass through
    intact (never re-split). The claim's own ``N.`` prefix is kept, matching
    how the google-patents fallback stores its per-claim chunks.
    """
    text = _CLAIM_SECTION_HEADER_RE.sub("", text, count=1)
    starts: list[int] = []
    expected = 1
    for m in _CLAIM_BOUNDARY_RE.finditer(text):
        if int(m.group(1)) != expected:
            continue
        window = text[max(0, m.start() - 20) : m.start()]
        last_word = re.search(r"(\w+)\W*\Z", window)
        if last_word is not None and last_word.group(1).lower() in _CLAIM_REF_PRECEDERS:
            continue
        starts.append(m.start())
        expected += 1
    if len(starts) < 2:
        return [text]
    out: list[str] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        piece = text[s:e].strip()
        if piece:
            out.append(piece)
    return out


# Page-header boilerplate that OPS glues onto the first claim's text.
# Example seen in the wild (WO2026085320A1):
#     "PATENT ATTORNEY DOCKET NO: 51198-064WO2 CLAIMS 1 . A system ..."
# We strip through the ``CLAIMS`` sentinel so the claim text begins at
# the claim number itself. Case-insensitive; tolerant of multiple
# whitespace runs and an optional docket-number line.
_CLAIM_HEADER_RE = re.compile(
    r"^\s*PATENT\s+ATTORNEY\s+DOCKET\s+NO[:\s]*\S+\s+CLAIMS\s+",
    re.IGNORECASE,
)


def _strip_claim_boilerplate(txt: str) -> str:
    """Remove the ``PATENT ATTORNEY DOCKET ... CLAIMS`` prefix."""
    return _CLAIM_HEADER_RE.sub("", txt, count=1)


def _format_date(raw: str) -> str | None:
    """Normalise a date string to ISO YYYY-MM-DD if recognisable."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 6:
        return f"{digits[0:4]}-{digits[4:6]}"
    if len(digits) == 4:
        return digits
    return None


# ---------------------------------------------------------------------------
# Search response parsing — used by the merge layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpsHit:
    """One hit from an OPS search response."""

    docdb_id: str  # canonical lowercased slug ('ep1234567b1')
    title: str
    applicants: list[str]
    publication_date: str | None
    abstract_preview: str  # first ~200 chars


def parse_search_response(xml_bytes: bytes) -> tuple[list[OpsHit], int]:
    """Parse an OPS search response into hits + total count.

    Returns ``(hits, total)``. ``total`` reflects the OPS-reported
    total-result-count, which can exceed ``len(hits)`` for paginated
    queries.
    """
    root = _safe_root(xml_bytes)
    if root is None:
        return [], 0

    # Total-results attribute lives on biblio-search element.
    total = 0
    for el in root.iter():
        if _local(el.tag) in ("biblio-search", "search-result"):
            t = el.attrib.get("total-result-count") or el.attrib.get("total-results")
            if t and t.isdigit():
                total = int(t)
                break

    # Drive iteration off ``<exchange-document>`` — one element per
    # patent record. Each carries a nested ``<publication-reference>``
    # we read for the docdb id; walking publication-references at
    # the top level would double-count (we'd pick up both the
    # search-result-level reference *and* the bibliographic-data
    # nested one inside each exchange-document).
    seen: set[str] = set()
    hits: list[OpsHit] = []
    for host in _findall(root, "exchange-document"):
        ref_el = _find(host, "publication-reference")
        if ref_el is None:
            continue
        slug = _docdb_to_slug(ref_el)
        if slug is None or slug in seen:
            continue
        seen.add(slug)

        title = _text(_find(host, "invention-title")) or "(untitled)"
        applicants_el = _find(host, "applicants")
        applicants = (
            [_text(_find(p, "name")) for p in _findall(applicants_el, "applicant")]
            if applicants_el is not None
            else []
        )
        applicants = [a for a in applicants if a]
        pub_date = _extract_publication_date(host)
        abstract_text = _text(_find(host, "abstract"))
        preview = abstract_text[:200].rstrip() + (
            "…" if len(abstract_text) > 200 else ""
        )
        hits.append(
            OpsHit(
                docdb_id=slug,
                title=title,
                applicants=applicants,
                publication_date=pub_date,
                abstract_preview=preview,
            )
        )

    # Fallback for variant OPS responses that omit the
    # ``<exchange-document>`` wrapper (rare, but observed on some
    # error-recovery paths). Walk publication-references that are
    # NOT inside an exchange-document and dedup on slug.
    if not hits:
        for ref_el in _findall(root, "publication-reference"):
            host = _find_enclosing(root, ref_el, "exchange-document")
            if host is not None:
                continue
            slug = _docdb_to_slug(ref_el)
            if slug is None or slug in seen:
                continue
            seen.add(slug)
            hits.append(
                OpsHit(
                    docdb_id=slug,
                    title="(untitled)",
                    applicants=[],
                    publication_date=None,
                    abstract_preview="",
                )
            )

    if total == 0 and hits:
        total = len(hits)
    return hits, total


def _docdb_to_slug(ref: ET.Element) -> str | None:
    """Pull ``country + doc-number + kind`` from a publication-reference."""
    for doc_id in _findall(ref, "document-id"):
        scheme = doc_id.attrib.get("document-id-type", "")
        if scheme.lower() != "docdb":
            continue
        country = _text(_find(doc_id, "country")).lower()
        number = _text(_find(doc_id, "doc-number"))
        kind = _text(_find(doc_id, "kind")).lower()
        if country and number and kind:
            return f"{country}{number}{kind}"
    # No DOCDB document-id — fall back to first available combo.
    for doc_id in _findall(ref, "document-id"):
        country = _text(_find(doc_id, "country")).lower()
        number = _text(_find(doc_id, "doc-number"))
        kind = _text(_find(doc_id, "kind")).lower()
        if country and number and kind:
            return f"{country}{number}{kind}"
    return None


def _find_enclosing(
    root: ET.Element, target: ET.Element, name: str
) -> ET.Element | None:
    """Find the nearest ancestor of ``target`` whose local-name matches.

    ElementTree doesn't track parents; we walk every candidate of
    type ``name`` and check whether ``target`` is a descendant.
    """
    for candidate in _findall(root, name):
        for descendant in candidate.iter():
            if descendant is target:
                return candidate
    return None


__all__ = [
    "OpsHit",
    "ParsedPatent",
    "parse_patent",
    "parse_search_response",
]
