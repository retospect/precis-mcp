"""``datasheet_pull`` job_type — fetch, ingest and link one part's datasheet.

The enqueued half of the datasheet auto-pull
(``precis.pcb.datasheets.enqueue_pulls``; ``put(kind='pcb')`` mints one job
per C-number its board uses that has no datasheet yet). Steps:

1. **Resolve the URL.** With JLCPCB API credentials, ``component_info`` (its
   row is upserted into ``parts``) and its ``dataManualUrl``; a JLC API error
   falls through to an existing ``parts.datasheet_url``. No URL either way
   fails ``no_url`` (or ``jlc_api_error:<status|class>`` when the API erred).
2. **Fetch** through ``safe_stream`` (supplier URLs are external input),
   capped at 50 MB (``too_large``), ``%PDF`` magic required (``not_pdf``);
   a refused address is ``fetch_refused:<detail>``, an HTTP error
   ``http_<status>``.
3. **Dedupe by content sha**: a datasheet with that ``pdf_sha256`` already
   exists, so only the link is made (one family datasheet, many parts).
4. **Ingest** otherwise, through the same Marker pipeline the inbox's
   ``datasheets/`` drop uses (``precis_add(PdfInput(as_kind='datasheet'))``),
   then stamp ``meta.source_url`` / ``meta.part_lcsc``.
5. **Link** ``datasheet-of`` datasheet → the part's lazy ref.

The outcome is recorded on the part ref's ``meta.datasheet_pull``. A
deterministic failure is a recorded state, not a raised error, so the job
succeeds and nothing retries forever; infrastructure errors (DB, Marker)
raise for the executor's normal handling.

Runs under ``job_inproc`` (bounded in-process work, one job per pass tick —
the same lane as ``pcb_place``/``embed_batch``): one HTTP fetch of at most
50 MB plus one ingest, no detached compute, no claude.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from precis.pcb.jlc_api import JlcApiClient
from precis.utils.http import http_client, require_httpx
from precis.utils.safe_fetch import SsrfBlocked, safe_stream
from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"lcsc": {"type": "string", "minLength": 2}},
    "required": ["lcsc"],
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


def _resolve_url(store: Store, lcsc: str) -> str:
    client = _jlc_client(store)
    existing = store.part_row(lcsc)
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
        return url.strip()
    if jlc_reason is not None:
        raise _Failed(jlc_reason)
    raise _Failed(
        "no_url",
        "JLC API returned no datasheet URL for this part"
        if client.available
        else "no JLCPCB API credentials and no parts row with a datasheet_url",
    )


def _download(url: str, dest: Path) -> tuple[str, str]:
    """Stream ``url`` to ``dest``; returns ``(final_url, sha256)``."""
    httpx = require_httpx()
    sha = hashlib.sha256()
    size = 0
    head = b""
    checked = False
    deadline = time.monotonic() + _DEADLINE_S
    try:
        with http_client(
            timeout=_FETCH_TIMEOUT_S, headers={"Accept": "application/pdf,*/*"}
        ) as client:
            with safe_stream(client, "GET", url) as resp:
                if resp.status_code >= 400:
                    raise _Failed(f"http_{resp.status_code}")
                declared = resp.headers.get("Content-Length")
                if declared is not None and declared.isdigit():
                    if int(declared) > MAX_BYTES:
                        raise _Failed("too_large", f"Content-Length {declared}")
                final_url = str(resp.url)
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                        if time.monotonic() > deadline:
                            raise _Failed("fetch_timeout", f"over {_DEADLINE_S:.0f}s")
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
    except SsrfBlocked as exc:
        raise _Failed("fetch_refused", str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise _Failed("fetch_timeout", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise _Failed(f"fetch_error:{type(exc).__name__}", str(exc)) from exc
    if size == 0:
        raise _Failed("not_pdf", "empty body")
    if not checked:
        _require_pdf(head)  # a body shorter than the window
    return final_url, sha.hexdigest()


def _require_pdf(head: bytes) -> None:
    if b"%PDF" not in head[:_MAGIC_WINDOW]:
        raise _Failed("not_pdf", f"head={head[:8]!r}")


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


def _kind_of(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row is not None else ""


def _body_chunks(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def pull(store: Store, lcsc: str) -> dict[str, Any]:
    """Run one pull and record its outcome on the part ref; returns the
    record. Deterministic failures are returned, not raised."""
    lcsc = lcsc.strip().upper()
    url: str | None = None
    try:
        url = _resolve_url(store, lcsc)
        with tempfile.TemporaryDirectory(prefix="datasheet-pull-") as tmp:
            path = Path(tmp) / "datasheet.pdf"
            final_url, sha = _download(url, path)
            ds_id = _existing_by_sha(store, sha)
            if ds_id is None:
                # A name that cannot collide with a paper stub's cite_key.
                named = path.with_name(f"datasheet-{sha[:12]}.pdf")
                path.rename(named)
                ds_id, inserted = _ingest(store, named)
                kind = _kind_of(store, ds_id)
                if kind != "datasheet":
                    # the ingest's own dedupe resolved to another kind of ref
                    raise _Failed(f"ingest_failed:sha_matches_{kind}")
                if inserted:
                    store.update_paper_fields(
                        ds_id,
                        meta_patch={"source_url": final_url, "part_lcsc": lcsc},
                        source="datasheet_pull",
                    )
        part_id = store.ensure_part_ref(lcsc, uncatalogued_ok=True)
        store.add_link(src_ref_id=ds_id, dst_ref_id=part_id, relation="datasheet-of")
        record: dict[str, Any] = {
            "status": "ok",
            "url": final_url,
            "sha256": sha,
            "at": now_iso(),
        }
        if _body_chunks(store, ds_id) == 0:
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
    store.update_ref(part_id, meta_patch={"datasheet_pull": record})
    return record


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    lcsc = str((ctx.meta.get("params") or {}).get("lcsc") or "").strip().upper()
    if not re.fullmatch(r"C\d+", lcsc):
        ctx.record_failure(
            f"datasheet_pull: {lcsc!r} is not an LCSC C-number", failure_class="infra"
        )
        return
    record = pull(ctx.store, lcsc)
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
