"""``datasheet_pull`` job_type — fetch, ingest and link one part's datasheet.

The enqueued half of the datasheet auto-pull
(``precis.pcb.datasheets.enqueue_pulls``; ``put(kind='pcb')`` mints one job
per C-number its board uses that has no datasheet yet). Steps:

1. **Resolve the URL.** With JLCPCB API credentials, ``component_info`` (its
   row is upserted into ``parts``) and its ``dataManualUrl``; a JLC API error
   falls through to an existing ``parts.datasheet_url``. No URL either way
   fails ``no_url`` (or ``jlc_api_error:<status|class>`` when the API erred).
2. **Fetch** through ``safe_stream`` (supplier URLs are external input),
   capped at 50 MB (``too_large``), ``%PDF`` magic required and a ``text/*``
   Content-Type refused (``not_pdf``, detail ``not a PDF (text/html)``; the
   catalog URL is tried once when it differs from the JLC one, else the
   part waits for an operator drop; LCSC's tiny ``location.href=`` HTML
   forward to the real PDF is followed once, same site only);
   a refused address is ``fetch_refused:<detail>``, an HTTP error
   ``http_<status>``.
3. **Dedupe by content sha**: a datasheet with that ``pdf_sha256`` already
   exists, so only the link is made (one family datasheet, many parts).
4. **Ingest** otherwise, through the same Marker pipeline the inbox's
   ``datasheets/`` drop uses (``precis_add(PdfInput(as_kind='datasheet'))``),
   then stamp ``meta.source_url`` / ``meta.part_lcsc`` — and the
   ``<mfr> <mfr_part>`` title when the PDF's own metadata doesn't name the
   part (a mirror copy's metadata is whatever the mirror shipped).
5. **Link** ``datasheet-of`` datasheet → the part's lazy ref.

The outcome is recorded on the part ref's ``meta.datasheet_pull``. A
deterministic failure is a recorded state, not a raised error, so the job
succeeds and nothing retries forever; infrastructure errors (DB, Marker)
raise for the executor's normal handling.

Runs under ``job_inproc`` (bounded in-process work, one job per pass tick —
the same lane as ``pcb_place``/``embed_batch``): one HTTP fetch of at most
50 MB plus one ingest, no detached compute, no claude.

A supplier-linked component may queue this same worker with its stored
manufacturer URL and component ref id. Reuse the SSRF-pinned bounded download
and datasheet ingest; no API parameter/price/stock is imported. The datasheet
becomes independent sourced evidence via a datasheet-of link.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import psycopg

from precis.pcb.datasheets import datasheet_state
from precis.pcb.jlc_api import JlcApiClient
from precis.utils.http import (
    POLICIES,
    RETRY_STATUSES,
    VendorError,
    call_with_backoff,
    http_client,
    is_retryable_external_error,
    netloc_provider,
    polite_user_agent,
    require_httpx,
)
from precis.utils.rate_limit import RateLimited
from precis.utils.safe_fetch import SsrfBlocked, safe_stream
from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lcsc": {"type": "string", "minLength": 2},
        "component_ref_id": {"type": "integer", "minimum": 1},
        "url": {"type": "string", "minLength": 8},
    },
    "oneOf": [
        {
            "required": ["lcsc"],
            "not": {"required": ["component_ref_id"]},
        },
        {"required": ["component_ref_id", "url"], "not": {"required": ["lcsc"]}},
    ],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Fetch, ingest and link the datasheet of one LCSC part a pcb design uses "
    "(the enqueued half of the datasheet auto-pull)."
)

#: Disk/memory guard on one datasheet download.
MAX_BYTES = 50 * 1024 * 1024
_FETCH_TIMEOUT_S = 60.0
#: Wall-clock cap on the whole download (a slow trickle never trips the
#: per-read timeout).
_DEADLINE_S = 300.0
_CHUNK = 64 * 1024
#: ``%PDF`` may follow a few bytes of junk; the spec allows 1024.
_MAGIC_WINDOW = 1024


class _Failed(Exception):
    """A pull failure recorded on the part, not raised to the executor
    (``precis.pcb.datasheets.is_transient`` says which ones a later put
    retries by itself)."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _jlc_client(store: Store) -> JlcApiClient:
    """The network seam for the URL lookup (tests swap it for a fake)."""
    return JlcApiClient(store=store)


def _resolve_urls(store: Store, lcsc: str) -> list[str]:
    """Candidate datasheet URLs, best first: the JLC API's, then the catalog's
    own when it differs (a bot-walled host may serve the other one)."""
    client = _jlc_client(store)
    existing = store.part_row(lcsc)
    catalog_url = (existing or {}).get("datasheet_url")
    jlc_reason: str | None = None
    if client.available:
        try:
            row = client.component_info(lcsc)
        except Exception as exc:  # an API outage must not become a retry loop
            status = getattr(exc, "status", None)
            jlc_reason = f"jlc_api_error:{status or type(exc).__name__}"
            log.warning("datasheet_pull: JLC lookup of %s failed: %s", lcsc, exc)
        else:
            if row is not None:
                if not row.get("datasheet_url") and existing is not None:
                    row["datasheet_url"] = existing["datasheet_url"]
                store.parts_import([row])
                existing = store.part_row(lcsc)
    url = (existing or {}).get("datasheet_url")
    if isinstance(url, str) and url.strip():
        urls = [url.strip()]
        if isinstance(catalog_url, str) and catalog_url.strip() not in urls:
            urls.append(catalog_url.strip())
        return urls
    if jlc_reason is not None:
        raise _Failed(jlc_reason)
    raise _Failed(
        "no_url",
        "JLC API returned no datasheet URL for this part"
        if client.available
        else "no JLCPCB API credentials and no parts row with a datasheet_url",
    )


def _download_any(urls: list[str], dest: Path) -> tuple[str, str]:
    """Try each candidate once; only a ``not_pdf`` answer moves on to the next
    (no retries of the same URL, no header tricks)."""
    for i, candidate in enumerate(urls):
        try:
            return _download(candidate, dest)
        except _Failed as exc:
            if exc.reason != "not_pdf" or i == len(urls) - 1:
                raise
            log.info("datasheet_pull: %s was not a PDF, trying next URL", candidate)
    raise _Failed("no_url")  # unreachable: urls is never empty


#: LCSC's file hosts answer a PDF URL with a tiny HTML page whose only job is
#: ``window.top.location.href='<real pdf url>'``; a browser follows it, httpx
#: does not.
_JS_REDIRECT = re.compile(
    r"""location(?:\.href)?\s*=\s*(['"])(?P<u>https?://[^'"\s]+)\1""", re.I
)
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh[^>]+url=(?P<u>https?://[^"'>\s]+)""", re.I
)
_REDIRECT_PAGE_MAX = 4096


def _site(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


def _html_redirect_target(page: bytes, url: str) -> str | None:
    """The same-site URL a small HTML interstitial forwards to, if it is one.

    Same registrable domain only (``wmsc.lcsc.com`` -> ``datasheet.lcsc.com``):
    a page that bounces to another site is a wall, not a hop. The target is
    fetched through ``safe_stream`` like the first URL."""
    text = page[:_REDIRECT_PAGE_MAX].decode("utf-8", "replace")
    m = _JS_REDIRECT.search(text) or _META_REFRESH.search(text)
    if m is None:
        return None
    target = m.group("u")
    if _site(urlsplit(target).hostname or "") != _site(urlsplit(url).hostname or ""):
        return None
    return target


class _RetryStatus(Exception):
    """A 429/5xx answer, raised so the backoff retries it (carries the
    response for ``Retry-After``)."""

    def __init__(self, response: Any) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def _download(url: str, dest: Path, *, hop: int = 0) -> tuple[str, str]:
    """Stream ``url`` to ``dest``; returns ``(final_url, sha256)``.

    One same-site HTML forward (``hop``) is followed; nothing deeper."""
    httpx = require_httpx()
    sha = hashlib.sha256()
    size = 0
    head = b""
    checked = False
    forward: str | None = None
    final_url = url
    deadline = time.monotonic() + _DEADLINE_S

    def attempt() -> None:
        nonlocal sha, size, head, checked, forward, final_url
        # A retry restarts the transfer from scratch.
        sha = hashlib.sha256()
        size, head, checked, forward, final_url = 0, b"", False, None, url
        with http_client(
            timeout=_FETCH_TIMEOUT_S,
            headers={"Accept": "application/pdf,*/*"},
            user_agent=polite_user_agent(),
        ) as client:
            with safe_stream(client, "GET", url) as resp:
                if resp.status_code in RETRY_STATUSES:
                    raise _RetryStatus(resp)  # retried, Retry-After honoured
                if resp.status_code >= 400:
                    raise _Failed(f"http_{resp.status_code}")
                declared = resp.headers.get("Content-Length")
                if declared is not None and declared.isdigit():
                    if int(declared) > MAX_BYTES:
                        raise _Failed("too_large", f"Content-Length {declared}")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                if ctype.lower().startswith("text/"):
                    # a bot wall / error page: never write it to disk
                    if hop == 0:
                        page = b""
                        for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                            page += chunk
                            if len(page) >= _REDIRECT_PAGE_MAX:
                                break
                        forward = _html_redirect_target(page, url)
                    if forward is None:
                        raise _Failed("not_pdf", f"not a PDF ({ctype.lower()})")
                if forward is None:
                    final_url = str(resp.url)
                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                            if time.monotonic() > deadline:
                                raise _Failed(
                                    "fetch_timeout", f"over {_DEADLINE_S:.0f}s"
                                )
                            if not checked:
                                head += chunk[: _MAGIC_WINDOW - len(head)]
                                if len(head) >= _MAGIC_WINDOW:
                                    checked = True
                                    _require_pdf(head)
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise _Failed("too_large", f"over {MAX_BYTES} bytes")
                            sha.update(chunk)
                            fh.write(chunk)

    def retryable(exc: BaseException) -> bool:
        # Only before the first body byte: a 429/5xx status or a failed connect.
        return is_retryable_external_error(exc) and isinstance(
            exc, (_RetryStatus, httpx.ConnectError, httpx.ConnectTimeout)
        )

    try:
        call_with_backoff(
            attempt,
            service=netloc_provider(url) or "datasheet-pull",
            policy=POLICIES["datasheet-pull"],
            url=url,
            retry_if=retryable,
        )
    except SsrfBlocked as exc:
        raise _Failed("fetch_refused", str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise _Failed("fetch_timeout", str(exc)) from exc
    except _RetryStatus as exc:
        raise _Failed(f"http_{exc.response.status_code}") from exc
    except (VendorError, RateLimited) as exc:
        raise _Failed("fetch_error:rate_limited", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise _Failed(f"fetch_error:{type(exc).__name__}", str(exc)) from exc
    if forward is not None:
        return _download(forward, dest, hop=1)
    if size == 0:
        raise _Failed("not_pdf", "empty body")
    if not checked:
        _require_pdf(head)  # a body shorter than the window
    return final_url, sha.hexdigest()


def _require_pdf(head: bytes) -> None:
    if b"%PDF" not in head[:_MAGIC_WINDOW]:
        if head.lstrip()[:1] == b"<":
            raise _Failed("not_pdf", "not a PDF (text/html)")
        raise _Failed("not_pdf", f"not a PDF (head={head[:8]!r})")


def _ingest(store: Store, path: Path) -> tuple[int, bool]:
    """The datasheet ingest path (what ``<inbox>/datasheets/`` and
    ``precis add --as datasheet`` run). Returns ``(ref_id, inserted)``."""
    from precis.ingest.add import PdfInput, precis_add

    try:
        result = precis_add(PdfInput(pdf_path=path, as_kind="datasheet"), store=store)
    except psycopg.Error:
        raise  # infrastructure: the executor's normal handling retries
    except Exception as exc:  # a poison PDF must not re-run Marker on every put
        log.warning("datasheet_pull: ingest failed", exc_info=True)
        raise _Failed(f"ingest_failed:{type(exc).__name__}", str(exc)) from exc
    if result is None:
        raise RuntimeError("datasheet ingest claimed by another host; retry")
    return result.ref_id, result.inserted


def _existing_by_sha(store: Store, sha: str) -> int | None:
    """A live DATASHEET with these bytes (a paper with the same sha does not
    count)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM ref_identifiers ri "
            "JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = 'pdf_sha256' AND ri.id_value = %s "
            "  AND r.kind = 'datasheet' AND r.retired_at IS NULL "
            "ORDER BY r.ref_id LIMIT 1",
            (sha,),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _kind_title(store: Store, ref_id: int) -> tuple[str, str]:
    """``(kind, title)`` of a just-minted ref."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, title FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return (str(row[0]), str(row[1] or "")) if row is not None else ("", "")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _mpn_title(store: Store, lcsc: str, minted_title: str) -> str | None:
    """``'<mfr> <mfr_part>'`` when the extracted title doesn't name the part.

    The PDF's metadata is whatever a mirror happened to ship — a junk title
    mints a junk handle (gr496320: a BSMD1206 sheet titled as a legal
    paper) — while the catalog row's identity is known-good. ``None`` when
    no MPN is catalogued or the minted title already contains it.
    """
    row = store.part_row(lcsc) or {}
    mpn = str(row.get("mfr_part") or "").strip()
    if not mpn or _squash(mpn) in _squash(minted_title):
        return None
    return " ".join(p for p in (str(row.get("mfr") or "").strip(), mpn) if p)


def _stamp_new_sheet(
    store: Store, ds_id: int, lcsc: str, source_url: str, minted_title: str
) -> None:
    """Provenance stamps on a freshly minted datasheet. A title override
    rewrites the card chunks too (the pair the paper edit path writes), so
    search matches the new title."""
    meta_patch = {"source_url": source_url, "part_lcsc": lcsc}
    title = _mpn_title(store, lcsc, minted_title)
    if title is None:
        store.update_paper_fields(ds_id, meta_patch=meta_patch, source="datasheet_pull")
        return
    from precis.ingest.cards import rewrite_cards
    from precis.utils.authors import author_names

    with store.tx() as conn:
        updated = store.update_paper_fields(
            ds_id,
            title=title,
            meta_patch=meta_patch,
            source="datasheet_pull",
            conn=conn,
        )
        meta = updated.meta or {}
        rewrite_cards(
            conn,
            ds_id,
            title=updated.title or "",
            author_names=author_names(updated.authors),
            abstract=str(meta.get("abstract") or ""),
            keywords=list(meta.get("keywords") or []),
        )


def _body_chunks(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def pull(store: Store, lcsc: str, url: str | None = None) -> dict[str, Any]:
    """Run one pull and record its outcome on the part ref; returns the
    record. Deterministic failures are returned, not raised. ``url`` is an
    agent-supplied datasheet URL (``put(kind='datasheet')``) that replaces
    the JLC/catalog lookup; the download is the same ``safe_stream`` one."""
    lcsc = lcsc.strip().upper()
    prior = _stored_ok(store, lcsc)  # read once, before this pull touches anything
    try:
        urls = [url] if url else _resolve_urls(store, lcsc)
        url = urls[0]
        with tempfile.TemporaryDirectory(prefix="datasheet-pull-") as tmp:
            path = Path(tmp) / "datasheet.pdf"
            final_url, sha = _download_any(urls, path)
            ds_id = _existing_by_sha(store, sha)
            if ds_id is None:
                # A name that cannot collide with a paper stub's cite_key.
                named = path.with_name(f"datasheet-{sha[:12]}.pdf")
                path.rename(named)
                ds_id, inserted = _ingest(store, named)
                kind, minted_title = _kind_title(store, ds_id)
                if kind != "datasheet":
                    # the ingest's own dedupe resolved to another kind of ref
                    raise _Failed(f"ingest_failed:sha_matches_{kind}")
                if inserted:
                    _stamp_new_sheet(store, ds_id, lcsc, final_url, minted_title)
        part_id = store.ensure_part_ref(lcsc, uncatalogued_ok=True)
        empty = _body_chunks(store, ds_id) == 0
        if not (empty and prior is not None):
            # an empty re-ingest never becomes a second datasheet of a part
            # that already holds a good one
            store.add_link(
                src_ref_id=ds_id, dst_ref_id=part_id, relation="datasheet-of"
            )
        record: dict[str, Any] = {
            "status": "ok",
            "url": final_url,
            "sha256": sha,
            "at": now_iso(),
        }
        if empty:
            # Marker down (or an image-only PDF) ingests "successfully" with
            # no text: keep the ref and the link, but do not call it a pull.
            record["status"] = "failed"
            record["reason"] = "ingest_empty"
            record["detail"] = "datasheet ingested with 0 body chunks"
    except _Failed as exc:
        record = {"status": "failed", "reason": exc.reason, "at": now_iso()}
        if exc.detail:
            record["detail"] = exc.detail[:300]
        if url:
            record["url"] = url
        part_id = store.ensure_part_ref(lcsc, uncatalogued_ok=True)
    if record["status"] == "failed" and prior is not None:
        # A later failed/partial pull never replaces a stored ok record: the
        # ok record stays as is and carries the failure as ``last_attempt``.
        store.update_ref(
            part_id, meta_patch={"datasheet_pull": {**prior, "last_attempt": record}}
        )
        return record
    store.update_ref(part_id, meta_patch={"datasheet_pull": record})
    return record


def _stored_ok(store: Store, lcsc: str) -> dict[str, Any] | None:
    """The part's recorded ``ok`` pull (without its ``last_attempt``), if any."""
    ref = store.get_ref(kind="part", id=lcsc)
    prior = (ref.meta or {}).get("datasheet_pull") if ref is not None else None
    if (
        isinstance(prior, dict)
        and prior.get("status") == "ok"
        # an ok record whose datasheet is gone is stale: let the failure show
        and datasheet_state(store, lcsc)["datasheet"] is not None
    ):
        return {k: v for k, v in prior.items() if k != "last_attempt"}
    return None


def pull_component(store: Store, ref_id: int, url: str) -> dict[str, Any]:
    """Acquire independent manufacturer evidence; no API parameters are used."""
    ref = store.get_ref(kind="component", id=ref_id)
    if ref is None or ref.kind != "component":
        raise ValueError("datasheet target must be a live component")
    links = (ref.meta or {}).get("supplier_links") or []
    if not any(item.get("datasheet_url") == url for item in links):
        raise ValueError("datasheet URL must match stored supplier identity link")
    try:
        with tempfile.TemporaryDirectory(prefix="component-datasheet-") as tmp:
            path = Path(tmp) / "datasheet.pdf"
            final_url, sha = _download(url, path)
            ds_id = _existing_by_sha(store, sha)
            if ds_id is None:
                named = path.with_name(f"datasheet-{sha[:12]}.pdf")
                path.rename(named)
                ds_id, inserted = _ingest(store, named)
                if _kind_title(store, ds_id)[0] != "datasheet":
                    raise _Failed("ingest_failed:wrong_kind")
                if inserted:
                    store.update_paper_fields(
                        ds_id,
                        meta_patch={"source_url": final_url},
                        source="datasheet_pull",
                    )
        store.add_link(src_ref_id=ds_id, dst_ref_id=ref_id, relation="datasheet-of")
        return {
            "status": "ok" if _body_chunks(store, ds_id) > 0 else "failed",
            "datasheet_ref_id": ds_id,
            "at": now_iso(),
        }
    except _Failed as exc:
        return {"status": "failed", "reason": exc.reason, "at": now_iso()}


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = ctx.meta.get("params") or {}
    if params.get("component_ref_id") is not None:
        record = pull_component(
            ctx.store, int(params["component_ref_id"]), str(params["url"])
        )
        ctx.append_chunk(
            "job_summary", f"component manufacturer datasheet: {record['status']}"
        )
        ctx.set_meta(
            pull_status=record["status"],
            datasheet_ref_id=record.get("datasheet_ref_id"),
            pull_reason=record.get("reason"),
        )
        return
    lcsc = str(params.get("lcsc") or "").strip().upper()
    if not re.fullmatch(r"C\d+", lcsc):
        ctx.record_failure(
            f"datasheet_pull: {lcsc!r} is not an LCSC C-number", failure_class="infra"
        )
        return
    record = pull(ctx.store, lcsc, str(params["url"]) if params.get("url") else None)
    ctx.append_chunk(
        "job_summary",
        f"datasheet_pull {lcsc}: {record['status']}"
        + (f" ({record['reason']})" if record.get("reason") else ""),
    )
    ctx.set_meta(
        lcsc=lcsc, pull_status=record["status"], pull_reason=record.get("reason")
    )


SPEC = JobTypeSpec(
    name="datasheet_pull",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=None,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["MAX_BYTES", "SPEC", "load", "pull"]
