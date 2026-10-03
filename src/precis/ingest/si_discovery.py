"""Supplementary-information (SI) discovery for one paper.

Pure parsing functions plus :func:`discover`, which takes an injected
``fetch`` callable (``url -> HttpResult``) so tests feed fixtures and the
worker (:mod:`precis.workers.si_fetch`) passes :func:`default_fetch`
(``safe_get`` only — never a raw client call).

Sources, in order; a candidate whose URL or filename is already collected
is not added twice:

1. **Figshare** — ACS mirrors its SI there. ``articles?resource_doi=<doi>``
   lists the SI article(s); the per-article ``/files`` endpoint carries
   ``download_url`` (the list search items carry no ``files``).
2. **Crossref** ``relation`` entries — any relation whose id is a DOI under
   the parent DOI with an ``.s<digits>`` suffix, or whose type mentions
   supplement/component. A component DOI resolves through the handle API.
3. **Component-DOI probe** — ``<doi>.s001`` .. ``.s005`` through the doi.org
   handle API (JSON, no redirect followed, so a Cloudflare-gated publisher
   is never touched); stops at the first that does not resolve.
4. **Landing page** — publisher link patterns (ACS ``suppl_file``,
   Elsevier ``mmc<n>``, RSC ``suppdata``, Wiley ``downloadSupplement``),
   minted only from a link whose URL or anchor text says
   supplement/supporting/ESI/suppl/mmc.

A blocked source (403, ``cf-mitigated: challenge``) is a recorded miss with
its URL — never something to get around.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

log = logging.getLogger(__name__)

SOURCE_FIGSHARE = "figshare"
SOURCE_CROSSREF = "crossref"
SOURCE_COMPONENT = "component_doi"
SOURCE_LANDING = "landing_page"

_FIGSHARE_API = "https://api.figshare.com/v2"
_CROSSREF_API = "https://api.crossref.org/works"
_HANDLE_API = "https://doi.org/api/handles"
#: Component DOIs probed per parent (``.s001`` .. ``.s005``).
MAX_COMPONENT_PROBES = 5
_API_TIMEOUT_S = 20.0

_COMPONENT_SUFFIX_RE = re.compile(r"\.s\d+$", re.IGNORECASE)
#: Words that say "supplement". A bare ``si`` is too common (SI units, the
#: element) — it counts only as ``_si_`` / ``-si-`` inside a filename.
_SUPP_WORDS_RE = re.compile(
    r"suppl|supporting|\besi\b|mmc\d+|suppdata|[_-]si[_-]", re.IGNORECASE
)
#: Publisher SI link shapes (matched against the absolute link URL).
_PUBLISHER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/doi/suppl/.+/suppl_file/", re.IGNORECASE),  # ACS
    re.compile(r"mmc\d+", re.IGNORECASE),  # Elsevier
    re.compile(r"suppdata", re.IGNORECASE),  # RSC
    re.compile(r"downloadSupplement", re.IGNORECASE),  # Wiley
)
_NON_PDF_EXTS = (
    ".zip",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".csv",
    ".txt",
    ".cif",
    ".mp4",
    ".gz",
    ".tar",
    ".7z",
    ".pptx",
    ".json",
    ".xyz",
)


@dataclass(frozen=True)
class SiCandidate:
    """One SI file a discovery source pointed at."""

    url: str
    source: str
    filename: str
    component_doi: str | None = None
    mimetype: str | None = None

    @property
    def is_pdf(self) -> bool:
        """True when this should be tried as a PDF download.

        A mimetype wins; else the filename extension. An extensionless
        name is tried (the ``%PDF-`` check in the downloader decides).
        """
        mt = (self.mimetype or "").lower()
        if mt:
            return "pdf" in mt
        name = self.filename.lower()
        return not name.endswith(_NON_PDF_EXTS)


@dataclass(frozen=True)
class HttpResult:
    """What a discovery fetch hands back (decoupled from httpx for tests)."""

    status: int
    text: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


FetchFn = Callable[[str], HttpResult]


@dataclass
class DiscoveryResult:
    candidates: list[SiCandidate] = field(default_factory=list)
    #: ``{url, source, reason}`` for a source that was blocked or errored.
    misses: list[dict[str, str]] = field(default_factory=list)


# ── pure parsers ───────────────────────────────────────────────────


def filename_from_url(url: str) -> str:
    """Best filename for ``url``: a ``file=`` query value (Wiley), else the
    last path segment."""
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    for key in ("file", "filename"):
        if q.get(key):
            return unquote(q[key][0])
    return unquote(parsed.path.rsplit("/", 1)[-1])


def parse_figshare_articles(text: str, parent_doi: str) -> list[tuple[int, str | None]]:
    """``(article_id, doi)`` for list items that are SI of ``parent_doi``.

    ``resource_doi=`` is a search, so keep only articles whose own DOI is
    a component (``<parent>.s<digits>``) of the parent.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[tuple[int, str | None]] = []
    parent = parent_doi.lower()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        doi = str(item.get("doi") or "").lower() or None
        if doi and not (
            doi.startswith(parent) and _COMPONENT_SUFFIX_RE.search(doi[len(parent) :])
        ):
            continue
        out.append((int(item["id"]), doi))
    return out


def parse_figshare_files(text: str, component_doi: str | None) -> list[SiCandidate]:
    """Candidates from a Figshare ``/articles/<id>/files`` payload."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[SiCandidate] = []
    for f in data if isinstance(data, list) else []:
        if not isinstance(f, dict):
            continue
        url = f.get("download_url")
        name = f.get("name")
        if not isinstance(url, str) or not url or not isinstance(name, str):
            continue
        mt = f.get("mimetype")
        out.append(
            SiCandidate(
                url=url,
                source=SOURCE_FIGSHARE,
                filename=name,
                component_doi=component_doi,
                mimetype=mt if isinstance(mt, str) and mt else None,
            )
        )
    return out


def parse_crossref_components(text: str, parent_doi: str) -> list[str]:
    """Component DOIs named by a Crossref works payload's ``relation``.

    Generic: any relation entry whose id is a DOI under the parent with an
    ``.s<digits>`` suffix, or whose relation type mentions
    supplement/component. Empty ``relation`` -> ``[]``.
    """
    try:
        msg = (json.loads(text) or {}).get("message") or {}
    except (ValueError, AttributeError):
        return []
    relation = msg.get("relation") or {}
    parent = parent_doi.lower()
    out: list[str] = []
    for rtype, entries in relation.items() if isinstance(relation, dict) else []:
        type_hit = bool(re.search(r"supplement|component", str(rtype), re.IGNORECASE))
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or str(e.get("id-type", "doi")).lower() != "doi":
                continue
            rid = str(e.get("id") or "").strip().lower()
            if not rid:
                continue
            under_parent = rid.startswith(parent) and bool(
                _COMPONENT_SUFFIX_RE.search(rid[len(parent) :])
            )
            if (under_parent or type_hit) and rid not in out:
                out.append(rid)
    return out


def parse_handle_url(text: str) -> str | None:
    """The ``URL`` value from a doi.org handle-API payload, else ``None``."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    for v in data.get("values", []) if isinstance(data, dict) else []:
        if isinstance(v, dict) and v.get("type") == "URL":
            val = (v.get("data") or {}).get("value")
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                return val
    return None


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def parse_landing_links(html: str, base_url: str) -> list[SiCandidate]:
    """SI candidates from a landing page's anchors.

    A link is minted only when its absolute URL matches a publisher SI
    pattern, or its anchor text says supplement/supporting/ESI/suppl/mmc
    AND the URL looks like a file (so a "Supporting Information" tab link
    to an HTML page does not mint). A plain article-PDF link
    (``/doi/pdf/...``, "Download PDF") matches neither.
    """
    parser = _AnchorParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        log.debug("si_discovery: landing page HTML parse failed", exc_info=True)
    out: list[SiCandidate] = []
    seen: set[str] = set()
    for href, text in parser.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        name = filename_from_url(url)
        pattern_hit = any(p.search(url) for p in _PUBLISHER_PATTERNS)
        text_hit = bool(_SUPP_WORDS_RE.search(text)) and bool(
            re.search(r"\.[a-z0-9]{2,4}$", name, re.IGNORECASE)
        )
        if not (pattern_hit or text_hit):
            continue
        # Guard: even a pattern hit must carry a supplement word somewhere.
        if not (_SUPP_WORDS_RE.search(url) or _SUPP_WORDS_RE.search(text)):
            continue
        seen.add(url)
        out.append(SiCandidate(url=url, source=SOURCE_LANDING, filename=name))
    return out


# ── orchestration ──────────────────────────────────────────────────


def _miss_reason(resp: HttpResult) -> str:
    if resp.status == 403 and "challenge" in (
        resp.headers.get("cf-mitigated", "").lower()
    ):
        return "cloudflare_403"
    return f"http_{resp.status}"


def _add(result: DiscoveryResult, cand: SiCandidate) -> None:
    have_url = {c.url for c in result.candidates}
    have_name = {c.filename.lower() for c in result.candidates if c.filename}
    if cand.url in have_url or (cand.filename and cand.filename.lower() in have_name):
        return
    result.candidates.append(cand)


def _safe_fetch(
    fetch: FetchFn, url: str, source: str, result: DiscoveryResult
) -> HttpResult | None:
    """Run ``fetch``; record a miss and return ``None`` unless 200."""
    try:
        resp = fetch(url)
    except Exception as exc:
        result.misses.append(
            {"url": url, "source": source, "reason": f"error:{type(exc).__name__}"}
        )
        return None
    if resp.status != 200:
        result.misses.append(
            {"url": url, "source": source, "reason": _miss_reason(resp)}
        )
        return None
    return resp


def _resolve_component(
    fetch: FetchFn, comp: str, result: DiscoveryResult, *, record_miss: bool = True
) -> str | None:
    """Resolve a component DOI to its target URL via the handle API."""
    api = f"{_HANDLE_API}/{comp}"
    try:
        resp = fetch(api)
    except Exception as exc:
        if record_miss:
            result.misses.append(
                {
                    "url": api,
                    "source": SOURCE_COMPONENT,
                    "reason": f"error:{type(exc).__name__}",
                }
            )
        return None
    url = parse_handle_url(resp.text) if resp.status == 200 else None
    if url is None and record_miss:
        result.misses.append(
            {"url": api, "source": SOURCE_COMPONENT, "reason": _miss_reason(resp)}
        )
    return url


def discover(doi: str, fetch: FetchFn) -> DiscoveryResult:
    """Find the SI files of the paper with ``doi`` (see module docstring)."""
    doi = doi.strip().lower()
    result = DiscoveryResult()

    # a. Figshare
    r = _safe_fetch(
        fetch, f"{_FIGSHARE_API}/articles?resource_doi={doi}", SOURCE_FIGSHARE, result
    )
    if r is not None:
        for art_id, art_doi in parse_figshare_articles(r.text, doi):
            fr = _safe_fetch(
                fetch,
                f"{_FIGSHARE_API}/articles/{art_id}/files",
                SOURCE_FIGSHARE,
                result,
            )
            if fr is not None:
                for cand in parse_figshare_files(fr.text, art_doi):
                    _add(result, cand)

    # b. Crossref relation
    component_dois: list[str] = []
    cr = _safe_fetch(fetch, f"{_CROSSREF_API}/{doi}", SOURCE_CROSSREF, result)
    if cr is not None:
        component_dois = parse_crossref_components(cr.text, doi)

    # c. component-DOI probe (``.s001`` ..) — handle API, no redirect followed
    resolved: dict[str, str] = {}
    for comp in component_dois:
        url = _resolve_component(fetch, comp, result)
        if url:
            resolved[comp] = url
    for n in range(1, MAX_COMPONENT_PROBES + 1):
        comp = f"{doi}.s{n:03d}"
        if comp in resolved:
            continue
        url = _resolve_component(fetch, comp, result, record_miss=False)
        if url is None:
            break  # first non-resolving component ends the probe
        resolved[comp] = url
    for comp, url in resolved.items():
        _add(
            result,
            SiCandidate(
                url=url,
                source=SOURCE_CROSSREF if comp in component_dois else SOURCE_COMPONENT,
                filename=filename_from_url(url),
                component_doi=comp,
            ),
        )

    # d. landing page
    lp = _safe_fetch(fetch, f"https://doi.org/{doi}", SOURCE_LANDING, result)
    if lp is not None:
        for cand in parse_landing_links(lp.text, lp.url or f"https://doi.org/{doi}"):
            _add(result, cand)
    return result


# ── default transport ──────────────────────────────────────────────


class _HostThrottle:
    """>= ``min_gap_s`` between requests to one host (no shared limiter row)."""

    def __init__(self, min_gap_s: float = 1.0) -> None:
        self.min_gap_s = min_gap_s
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        nxt = self._last.get(host, 0.0) + self.min_gap_s
        if now < nxt:
            time.sleep(nxt - now)
        self._last[host] = time.monotonic()


def default_fetch(email: str = "") -> FetchFn:
    """The production ``fetch``: ``safe_get`` through a pinned client, >=1 s
    between requests to one host. A 403 comes back as a status, never raises."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    throttle = _HostThrottle(1.0)
    ua = f"precis-mcp/8.0 (mailto:{email or 'noreply@example.com'})"

    def _fetch(url: str) -> HttpResult:
        throttle.wait(urlparse(url).netloc)
        with http_client(
            timeout=_API_TIMEOUT_S,
            headers={"Accept": "application/json,text/html;q=0.9,*/*;q=0.5"},
            user_agent=ua,
        ) as client:
            resp = safe_get(client, url)
            return HttpResult(
                status=resp.status_code,
                text=resp.text if resp.status_code == 200 else "",
                url=str(resp.url),
                headers={k.lower(): v for k, v in resp.headers.items()},
            )

    return _fetch


__all__ = [
    "DiscoveryResult",
    "HttpResult",
    "SiCandidate",
    "default_fetch",
    "discover",
    "filename_from_url",
    "parse_crossref_components",
    "parse_figshare_articles",
    "parse_figshare_files",
    "parse_handle_url",
    "parse_landing_links",
]
