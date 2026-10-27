"""Live jlcpcb.com website calls — the two gaps the Open API doesn't cover.

The Open API (:mod:`precis.pcb.jlc_api`) has a bulk cursor walk and a lookup
by C-number, but no keyword/free-text query. ``search(kind='part', q=...)``
falls back to a keyword search when the local catalog has no hit, and that is
the first gap this module covers, via the listing endpoint behind the
parts-library page: ``GET`` an XSRF cookie, then ``POST`` the list with the
token echoed in a header. Interactive only, one page per query; the catalog
refresh and C-number lookups all go through the Open API (gr464537). That
endpoint sits under ``/api``, which jlcpcb.com's robots.txt disallows, so it
leaves as soon as the Open API gains a keyword query.

The second gap: the listing endpoint (and the Open API's detail endpoint)
can't see whole categories — Supercapacitors answer ``total=0`` to keyword,
exact-MPN AND ``secondSortName`` (gr519923) — while the bare
``/partdetail/C<n>`` page serves every C-number's record. Its Next.js RSC
payload (``self.__next_f.push`` chunks) embeds the component record as JSON;
:func:`partdetail_lookup` fetches that page and normalizes the record into
the same row the listing produces. ``/partdetail`` is not under ``/api``.
The host is fixed, never agent-supplied. Live calls use the tight
:data:`LIVE_POLICY` (an agent is waiting).

The third gap: the Open API has no taxonomy at all. The parts nav's
first/second-sort tree (``sortAndCountVoList``) rides the same listing
envelope under ``searchType:"1"`` — :func:`category_tree` serves it,
:func:`browse_categories` walks a ``'/'``-joined sort-name path and pulls
the leaf's live listing (:func:`category_listing`) so ``view='categories'``
on the part kind can browse where keyword search is blind (gr519793).

Sort-name convention GOTCHA — the two surfaces disagree on which field
is the parent. Listing rows say ``firstSortName`` = leaf,
``secondSortName`` = parent ("Chip Resistor - Surface Mount" /
"Resistors"); the partdetail RSC record says ``firstSortName`` = parent,
``secondSortName`` = leaf ("Capacitors" / "Supercapacitors") — verified
live 2026-10-26. ``normalize_library_row`` expects the listing
convention; ``_parse_partdetail`` swaps at the boundary.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from precis.pcb import catalog
from precis.pcb._http import (
    BULK_POLICY,
    TRACE_ID_HEADER,
    Policy,
    VendorError,
    check_envelope,
    with_backoff,
)
from precis.utils.coerce import to_int
from precis.utils.rate_limit import acquire

if TYPE_CHECKING:
    import httpx

    from precis.store import Store

log = logging.getLogger(__name__)

HOST = "https://jlcpcb.com"
XSRF_PATH = "/api/overseas-pcb-order/v1/getAll"
LIST_PATH = "/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"

_SERVICE = "jlcpcb-web"

#: Live keyword lookups run inside a tool call — a tighter
#: policy than the bulk walk: fewer attempts, a small cap, and a shorter
#: breaker cooldown so an agent is not pinned for minutes on a wedged
#: endpoint.
LIVE_POLICY = Policy(attempts=3, base=0.6, cap=8.0, breaker_cooldown=120.0)

#: Page size for live keyword queries (one page per query).
_LIVE_PAGE_SIZE = 25

#: One list request body in -> decoded JSON out. Swappable in tests.
PostFn = Callable[[dict[str, Any]], dict[str, Any]]


class _WebPoster:
    """Real transport: XSRF handshake once, then POSTs via ``safe_stream``."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        policy: Policy = BULK_POLICY,
        pace: Callable[[str], object] = acquire,
    ) -> None:
        from precis.utils.http import http_client

        self._client = http_client(timeout=timeout)
        self._policy = policy
        #: Fleet-wide pacing (``external_rate_limits`` row ``jlcpcb-web``),
        #: taken before every wire attempt, retries included.
        self._pace = pace
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _xsrf(self) -> str:
        if self._token is None:
            from precis.utils.safe_fetch import safe_get

            def _handshake() -> httpx.Response:
                self._pace(_SERVICE)
                return safe_get(self._client, f"{HOST}{XSRF_PATH}")

            resp = with_backoff(
                _handshake,
                service=_SERVICE,
                policy=self._policy,
            )
            token = resp.cookies.get("XSRF-TOKEN") or self._client.cookies.get(
                "XSRF-TOKEN"
            )
            if not token:
                raise VendorError(_SERVICE, "no XSRF-TOKEN cookie from handshake")
            self._token = token
        return self._token

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        from precis.utils.safe_fetch import safe_stream

        token = self._xsrf()
        headers = {
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": token,
            "Cookie": f"XSRF-TOKEN={token}",
        }
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)

        def _thunk() -> httpx.Response:
            self._pace(_SERVICE)
            with safe_stream(
                self._client,
                "POST",
                f"{HOST}{LIST_PATH}",
                headers=headers,
                content=payload,
            ) as resp:
                resp.read()
                return resp

        resp = with_backoff(_thunk, service=_SERVICE, policy=self._policy)
        if resp.status_code != 200:
            raise VendorError(
                _SERVICE,
                f"HTTP {resp.status_code} from {LIST_PATH}",
                status=resp.status_code,
                trace_id=resp.headers.get(TRACE_ID_HEADER),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise VendorError(_SERVICE, f"non-JSON response from {LIST_PATH}") from exc
        if not isinstance(data, dict):
            return {}
        return check_envelope(_SERVICE, data, resp, LIST_PATH)


def _price(raw: Any) -> list[dict[str, Any]] | None:
    out: list[dict[str, Any]] = []
    for b in raw or []:
        try:
            end = int(b["endNumber"])
            out.append(
                {
                    "qFrom": int(b["startNumber"]),
                    "qTo": end if end >= 0 else None,
                    "price": float(b["productPrice"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            return None
    return out or None


#: Listing-row fields that carry a lifecycle / buyability signal for the
#: EOL sweep (view='availability'). All confirmed present on the live
#: ``selectSmtComponentList`` row (2026-10-14); any of them can be null.
#: ``merged`` is the strongest EOL tell — JLCPCB folded this part into
#: another C-number — ``alts`` names suggested successors, ``no_buy`` a
#: sale stop reason, ``est`` a restock estimate, ``idle`` a slow-moving
#: flag, ``loss`` write-offs.
_LIFECYCLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("mergedComponentCode", "merged"),
    ("componentAlternativesCode", "alts"),
    ("noBuyReason", "no_buy"),
    ("idleFlag", "idle"),
    ("estimateDate", "est"),
    ("lossNumber", "loss"),
)


def normalize_library_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One listing row -> the ``parts`` columns
    :func:`precis.pcb.catalog.normalize_jlcparts_v2_row` produces. ``None``
    when the row has no C-number.

    ``params`` additionally carries two ``_``-prefixed vendor keys the dump
    never supplies: ``_lcsc_url`` (the lcsc.com product page) and
    ``_lifecycle`` (EOL/buyability signals, only when at least one is
    non-empty — see :data:`_LIFECYCLE_FIELDS`). Both ride the parametric
    blob the same way ``_category`` does; a bare code+flags stub row still
    gets ``params=None``."""
    lcsc = catalog._lcsc_number(row.get("componentCode"))
    if lcsc is None:
        return None
    params: dict[str, Any] = {}
    for attr in row.get("attributes") or []:
        name = attr.get("attribute_name_en")
        if name:
            params[name] = attr.get("attribute_value_name")
    cat = " / ".join(
        s for s in (row.get("secondSortName"), row.get("firstSortName")) if s
    )
    if cat:
        params["_category"] = cat
    if url := row.get("lcscGoodsUrl"):
        params["_lcsc_url"] = url
    lifecycle = {
        key: v
        for src, key in _LIFECYCLE_FIELDS
        if (v := row.get(src)) not in (None, "", 0)
    }
    if lifecycle:
        params["_lifecycle"] = lifecycle
    return {
        "lcsc": lcsc,
        "mfr": row.get("componentBrandEn") or None,
        "mfr_part": row.get("componentModelEn") or None,
        "description": row.get("describe") or "",
        "jlcpcb_assemblable": True,
        "basic": row.get("componentLibraryType") == "base"
        or bool(row.get("preferredComponentFlag")),
        "stock": to_int(row.get("stockCount")) or 0,
        # a no-buy listing's ladder is a placeholder, not a price (gr519681)
        "price": (
            None if lifecycle.get("no_buy") else _price(row.get("componentPrices"))
        ),
        "package": row.get("componentSpecificationEn") or None,
        "height_mm": catalog._height_mm(params),
        "params": params or None,
        "datasheet_url": row.get("dataManualUrl") or None,
    }


def _live_poster(post: PostFn | None) -> tuple[PostFn, _WebPoster | None]:
    """``(send, poster-to-close)`` for a live call: the injected fake, or a
    real poster on the interactive policy + a tighter socket timeout."""
    if post is not None:
        return post, None
    poster = _WebPoster(timeout=15.0, policy=LIVE_POLICY)
    return poster, poster


def iter_keyword_rows(
    keyword: str,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
    max_pages: int = 1,
) -> Iterator[dict[str, Any]]:
    """Raw listing rows for one ``keyword`` search, paged on ``pages`` like
    :func:`iter_library_rows` (``hasNextPage`` lies there too), bounded by
    ``max_pages``. Live policy + timeout when no ``post`` is injected."""
    poster: _WebPoster | None = None
    if post is None:
        send, poster = _live_poster(post)
    else:
        send = post
    try:
        page = 1
        while True:
            data = send(
                {
                    "currentPage": page,
                    "pageSize": page_size,
                    "keyword": str(keyword),
                }
            )
            info = (data.get("data") or {}).get("componentPageInfo") or {}
            yield from info.get("list") or []
            pages = to_int(info.get("pages")) or 0
            if page >= pages or page >= max_pages:
                break
            page += 1
    finally:
        if poster is not None:
            poster.close()


def live_search(
    store: Store,
    q: str,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Keyword-search the listing endpoint, upsert every part-shaped row,
    and return the normalized rows — the live fallback for
    ``search(kind='part')`` when the local catalog has no hit. One page,
    bounded by ``page_size``."""
    rows = [
        n
        for raw in iter_keyword_rows(q, post=post, page_size=page_size, max_pages=1)
        if (n := normalize_library_row(raw)) is not None
    ]
    if rows:
        store.parts_import(rows)
    return rows


# ── partdetail fallback (gr519923) ───────────────────────────────────

#: ``GET /partdetail/<C-no>`` — a bare C-number works, no mfr/mpn slug
#: needed. Not under ``/api``, so outside the listing endpoint's robots
#: disallow.
PARTDETAIL_PATH = "/partdetail/"

#: One URL → the page HTML. Swappable in tests (a canned partdetail page);
#: the default hits the wire via :func:`_partdetail_get`.
FetchFn = Callable[[str], str]


def _partdetail_get(url: str) -> str:
    """Real transport for the partdetail fallback — one GET on the live
    policy. Module-level so the conftest deny-fixture can pin it: a test
    that forgets ``fetch=`` must never reach the wire."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=15.0) as client:

        def _thunk() -> httpx.Response:
            acquire(_SERVICE)
            return safe_get(client, url)

        resp = with_backoff(_thunk, service=_SERVICE, policy=LIVE_POLICY)
        status, text = resp.status_code, resp.text
        trace_id = resp.headers.get(TRACE_ID_HEADER)
    if status == 404:
        return ""  # unknown C-number — parses as a real miss below
    if status != 200:
        raise VendorError(
            _SERVICE,
            f"HTTP {status} from {PARTDETAIL_PATH}",
            status=status,
            trace_id=trace_id,
        )
    return text


_PUSH_ARG = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_RSC_ROW = re.compile(r"([0-9a-f]+):(.*)", re.S)
_RSC_REF = re.compile(r"\$([0-9a-f]+)\Z")

#: The site's absent markers inside the RSC payload: ``"--"`` is the
#: display placeholder, ``"$undefined"`` the RSC sentinel for a missing
#: value.
_ABSENT = (None, "", "--", "$undefined")


def _rsc_rows(html: str) -> dict[str, Any]:
    """De-escape the page's ``self.__next_f.push`` script chunks and parse
    every ``<id>:<json>`` row into ``{row-id: payload}``. RSC refs
    (``"$30"``) and sentinels survive as plain strings — the caller
    resolves them. Non-JSON payloads (module manifests, ``T`` text rows)
    are skipped."""
    rows: dict[str, Any] = {}
    chunks: list[str] = []
    for m in _PUSH_ARG.finditer(html):
        args = _try_json(m.group(1))
        if isinstance(args, list):
            chunks.extend(a for a in args if isinstance(a, str))
    # Joined before splitting: a row can straddle a push-chunk boundary.
    for line in "\n".join(chunks).split("\n"):
        rm = _RSC_ROW.match(line)
        if rm is None:
            continue
        try:
            rows[rm.group(1)] = json.loads(rm.group(2))
        except ValueError:
            continue
    return rows


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _rsc_dicts(
    node: Any, rows: dict[str, Any], _refs: frozenset[str] = frozenset()
) -> Iterator[dict[str, Any]]:
    """Every dict reachable from a parsed RSC payload, following
    ``"$id"`` chunk refs (a record's ``prices``/``attributes``/
    ``componentInfo`` values are refs, not inline values — ``"$30"``
    resolves to row ``30``). ``"$L.."`` lazy refs, ``"$undefined"`` and
    plain strings yield nothing; ``_refs`` stops a ref cycle."""
    if isinstance(node, str):
        if (m := _RSC_REF.match(node)) is not None and m.group(1) not in _refs:
            yield from _rsc_dicts(rows.get(m.group(1)), rows, _refs | {m.group(1)})
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _rsc_dicts(v, rows, _refs)
    elif isinstance(node, list):
        for v in node:
            yield from _rsc_dicts(v, rows, _refs)


def _rsc_list(v: Any, rows: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A record field that is either an inline list or a ``"$id"`` ref to
    a ref-list row → the resolved dicts."""
    if isinstance(v, str):
        if (m := _RSC_REF.match(v)) is None:
            return None
        v = rows.get(m.group(1))
    if not isinstance(v, list):
        return None
    out = [d for item in v for d in _rsc_dicts(item, rows)]
    return out or None


def _clean(v: Any) -> Any:
    """The page's absent markers (and unresolved ``"$id"`` refs on scalar
    fields) → ``None``; everything else passes through."""
    if v is None or v in ("", "--", "$undefined"):
        return None
    if isinstance(v, str) and _RSC_REF.match(v):
        return None
    return v


def _parse_partdetail(html: str, code: str) -> dict[str, Any] | None:
    """The partdetail page's RSC payload → a raw row in the listing shape
    :func:`normalize_library_row` accepts. ``None`` when no record matches
    ``code`` — the miss page (``null | null | JLCPCB``) carries only
    related-product rows for OTHER parts, which fail the
    ``componentCode``+``componentModelEn`` check here.

    The record arrives as up to three chunks: the render record
    (``describe``/``componentLibraryType``, prices inline or ``$ref``),
    the buy record (``isBuyComponent``/``overseasStockCount``/
    ``noBuyReason``) and the spec record (``firstSortName``/
    ``secondSortName``/``attributes``-ref). Fields merge across all
    matching dicts; ``"--"``/``"$undefined"``/unresolved refs count as
    absent."""
    rows = _rsc_rows(html)
    recs = [
        d
        for r in rows.values()
        for d in _rsc_dicts(r, rows)
        if str(d.get("componentCode") or "").upper() == code
        and d.get("componentModelEn") not in _ABSENT
    ]
    if not recs:
        return None
    merged: dict[str, Any] = {}
    for d in recs:
        for k, v in d.items():
            if k not in merged or merged[k] in _ABSENT:
                merged[k] = v
    stock = to_int(merged.get("overseasStockCount")) or to_int(merged.get("stockCount"))
    return {
        "componentCode": code,
        "componentBrandEn": _clean(merged.get("componentBrandEn")),
        "componentModelEn": _clean(merged.get("componentModelEn")),
        "describe": _clean(merged.get("describe"))
        or _clean(merged.get("componentName")),
        "componentSpecificationEn": _clean(merged.get("componentSpecificationEn")),
        "componentLibraryType": _clean(merged.get("componentLibraryType")),
        "preferredComponentFlag": merged.get("preferredComponentFlag") is True,
        "stockCount": stock or 0,
        "componentPrices": _rsc_list(merged.get("prices"), rows),
        "dataManualUrl": _clean(merged.get("dataManualUrl")),
        "attributes": _rsc_list(merged.get("attributes"), rows),
        # The RSC record's sort names are parent-then-leaf
        # (``firstSortName:"Capacitors", secondSortName:"Supercapacitors"``)
        # — the listing endpoint's convention is the REVERSE (leaf first:
        # ``firstSortName:"Chip Resistor - Surface Mount",
        # secondSortName:"Resistors"``, verified live 2026-10-26). Feed
        # them through in the LISTING convention so the ``_category``
        # join in ``normalize_library_row`` yields the same
        # "parent / leaf" path either way (gr519793: partdetail rows were
        # catalogued with the leaf-parent path inverted).
        "firstSortName": _clean(merged.get("secondSortName")),
        "secondSortName": _clean(merged.get("firstSortName")),
        "lcscGoodsUrl": f"{HOST}{PARTDETAIL_PATH}{code}",
        # the lifecycle sources _LIFECYCLE_FIELDS reads, under their
        # listing names
        "mergedComponentCode": _clean(merged.get("mergedComponentCode")),
        "componentAlternativesCode": _clean(merged.get("componentAlternativesCode")),
        "noBuyReason": _clean(merged.get("noBuyReason")),
        "idleFlag": _clean(merged.get("idleFlag")),
        "estimateDate": _clean(merged.get("estimateDate")),
        "lossNumber": _clean(merged.get("lossNumber")),
    }


def partdetail_lookup(
    lcsc: str, *, fetch: FetchFn | None = None
) -> dict[str, Any] | None:
    """Fetch ``jlcpcb.com/partdetail/<C-no>`` and return the part's record
    as the normalized row :meth:`JlcApiClient.component_info` produces —
    the fallback for parts the Open API's listing can't see (gr519923).
    ``None`` on a real miss (the page 200s but carries no record for the
    code); a transport failure raises :class:`VendorError` like every
    other live call here."""
    code = catalog._lcsc_number(lcsc)
    if code is None:
        return None
    fetch = fetch or _partdetail_get
    row = _parse_partdetail(fetch(f"{HOST}{PARTDETAIL_PATH}{code}"), code)
    return None if row is None else normalize_library_row(row)


# ── Category browse (gr519793) ───────────────────────────────────────
#: ``searchType:"1"`` is the tree trigger: the listing envelope then
#: carries ``sortAndCountVoList`` — the full first/second-sort taxonomy
#: the /parts nav walks (probed live 2026-10-26: 55 first-sort nodes,
#: 860 second-sort leaves, ~330 KB response). A keyword query returns
#: the same key ``null``; drilling is done client-side over this one
#: payload.
_TREE_REQUEST = {"currentPage": 1, "pageSize": 1, "searchType": "1"}


def _sort_node(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(raw.get("sortName") or ""),
        "sort_id": to_int(raw.get("componentSortKeyId")),
        "count": to_int(raw.get("componentCount")) or 0,
        "children": [
            _sort_node(c) for c in raw.get("childSortList") or [] if isinstance(c, dict)
        ],
    }


def category_tree(*, post: PostFn | None = None) -> list[dict[str, Any]]:
    """JLCPCB's first/second-sort taxonomy — one listing call that
    returns the nav tree, not rows. ``[{name, sort_id, count,
    children: [...]}]``; the empty list when the endpoint dropped the
    key (callers treat that as a shape break, not "no categories")."""
    send, poster = _live_poster(post)
    try:
        data = send(dict(_TREE_REQUEST))
        raw = (data.get("data") or {}).get("sortAndCountVoList") or []
        return [_sort_node(n) for n in raw if isinstance(n, dict)]
    finally:
        if poster is not None:
            poster.close()


def _match_segment(
    candidates: list[dict[str, Any]], remaining: str
) -> tuple[dict[str, Any], str] | None:
    """Prefix-match one path segment off ``remaining``, longest name
    wins — sort names contain '/' themselves ("Audio Products /
    Vibration Motors"), so a category path cannot just ``split('/')``.
    Returns ``(node, rest)``; ``rest`` is '' when the segment ended
    the path."""
    low = remaining.lower()
    best: tuple[dict[str, Any], str] | None = None
    for c in candidates:
        name = str(c["name"])
        nlow = name.lower()
        if low == nlow:
            rest = ""
        elif low.startswith(nlow + "/"):
            rest = remaining[len(name) + 1 :]
        else:
            continue
        if best is None or len(name) > len(best[0]["name"]):
            best = (c, rest)
    return best


def resolve_category(
    tree: list[dict[str, Any]], path: str
) -> tuple[list[dict[str, Any]], str]:
    """``'Capacitors/Supercapacitors'`` (any case, optional leading
    ``/``) → ``(matched nodes top-down, unmatched tail)``. The tail is
    ``""`` on a clean walk; whatever segment stopped the match
    otherwise (the handler turns that into the keyword/options path)."""
    matched: list[dict[str, Any]] = []
    rest = path.strip().strip("/")
    level = tree
    while rest:
        hit = _match_segment(level, rest)
        if hit is None:
            break
        node, rest = hit
        matched.append(node)
        level = node["children"]
    return matched, rest


def category_listing(
    store: Store,
    first: str,
    second: str | None = None,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    """Live-browse one sort category's parts — the listing filtered by
    sort NAME (``searchSource:"sort"`` + ``firstSortName`` /
    ``secondSortName``; sort IDs prune the tree but do NOT filter the
    rows — probed live 2026-10-26). Rows normalize + upsert through
    ``parts_import`` exactly like :func:`live_search`. ``(rows,
    total)``, one page."""
    body: dict[str, Any] = {
        "currentPage": 1,
        "pageSize": page_size,
        "searchSource": "sort",
        "firstSortName": first,
    }
    if second:
        body["secondSortName"] = second
    send, poster = _live_poster(post)
    try:
        data = send(body)
        info = (data.get("data") or {}).get("componentPageInfo") or {}
        rows = [
            n
            for raw in info.get("list") or []
            if (n := normalize_library_row(raw)) is not None
        ]
        if rows:
            store.parts_import(rows)
        return rows, to_int(info.get("total")) or 0
    finally:
        if poster is not None:
            poster.close()


def browse_categories(
    store: Store,
    path: str | None,
    *,
    post: PostFn | None = None,
    page_size: int = _LIVE_PAGE_SIZE,
) -> dict[str, Any]:
    """``view='categories'`` backend: the taxonomy tree plus, when
    ``path`` resolves cleanly to a leaf, that category's live listing —
    the parts keyword search cannot see (gr519923), catalogued on the
    way out the same way ``live_search`` catalogues. Two requests at
    most, both through the listing endpoint.

    ``{tree, path, unmatched, parts, total}``: ``path`` the resolved
    node chain, ``unmatched`` the tail segment that stopped resolution
    (``""`` on a clean walk — the handler routes that to the keyword
    path-match), ``parts``/``total`` the leaf's listing. An empty tree
    means the endpoint's envelope changed shape — a :class:`VendorError`
    like the bulk walk's empty-first-page guard (gr494906).
    """
    tree = category_tree(post=post)
    if not tree:
        raise VendorError(
            _SERVICE,
            "category browse returned an empty sortAndCountVoList — "
            "the listing endpoint's tree shape changed",
        )
    matched: list[dict[str, Any]] = []
    rest = ""
    if path:
        matched, rest = resolve_category(tree, path)
    node = matched[-1] if matched else None
    parts: list[dict[str, Any]] = []
    total = 0
    if node is not None and not rest and not node["children"]:
        parts, total = category_listing(
            store,
            matched[0]["name"],
            matched[1]["name"] if len(matched) > 1 else None,
            post=post,
            page_size=page_size,
        )
    return {
        "tree": tree,
        "path": matched,
        "unmatched": rest,
        "parts": parts,
        "total": total,
    }


__all__ = [
    "HOST",
    "LIST_PATH",
    "LIVE_POLICY",
    "PARTDETAIL_PATH",
    "XSRF_PATH",
    "FetchFn",
    "PostFn",
    "browse_categories",
    "category_listing",
    "category_tree",
    "iter_keyword_rows",
    "live_search",
    "normalize_library_row",
    "partdetail_lookup",
    "resolve_category",
]
