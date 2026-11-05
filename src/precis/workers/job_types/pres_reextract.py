"""Re-read a stored presentation PDF with fitz, without OCR or model calls.

The edit door queues work without touching slides. Extraction and SHA checks
precede an atomic, ref-locked body replacement. Identical slide rows are kept
so re-reading an image-only PDF updates its count without breaking anchors.
Missing local bytes defer to another worker, with a bounded placement retry
matching paper re-extraction. Failures leave the previous slides intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.corpus_layout import corpus_roots_from_env, resolve_local_pdf
from precis.ingest.pres import OCR_NEEDED_TAG, extract_pres, write_pres_slides
from precis.store.types import Tag
from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

JOB_TYPE = "pres_reextract"
PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {"type": "integer", "minimum": 1},
        "pdf_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
    "required": ["ref_id", "pdf_sha256"],
    "additionalProperties": False,
}
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = "Refresh presentation slides and low-text page counts with fitz; no OCR."
_PLACEMENT_PARK_CAP = 24


def idem_key(ref_id: int, sha: str) -> str:
    return f"pres_reextract:{ref_id}:{sha}"


def reextract(
    store: Store,
    ref_id: int,
    *,
    pdf_sha256: str,
    corpus_dirs: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Extract before taking the swap lock; every failure preserves the body."""
    refs = store.fetch_refs_by_ids([ref_id])
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "pres" or ref.retired_at is not None:
        return {"status": "failed", "reason": "presentation is missing or retired"}
    if not ref.pdf_sha256 or ref.pdf_sha256 != pdf_sha256:
        return {
            "status": "failed",
            "reason": "stored PDF changed; request reextract again",
        }
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s", (pdf_sha256,)
        ).fetchone()
    path = resolve_local_pdf(
        corpus_dirs if corpus_dirs is not None else corpus_roots_from_env(),
        str(row[0]) if row and row[0] else None,
        (ref.slug,) if ref.slug else (),
    )
    if path is None:
        if store.pdf_missing(pdf_sha256):
            return {
                "status": "failed",
                "reason": "stored PDF is missing; restore it into the corpus and retry",
            }
        return {
            "status": "deferred",
            "reason": "stored PDF is not on this worker; waiting for a holder",
        }
    try:
        pres = extract_pres(path, slug_hint=ref.slug, title_hint=ref.title)
    except ModuleNotFoundError as exc:
        return {
            "status": "failed",
            "reason": f"worker lacks PDF extraction dependency {exc.name}; "
            "run on a PDF-capable worker and retry",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"extraction failed ({type(exc).__name__}); repair the PDF and retry",
        }
    if pres.pdf_sha256 != pdf_sha256:
        return {
            "status": "failed",
            "reason": "stored file SHA256 mismatch; restore the matching PDF and retry",
        }

    with store.tx() as conn:
        current = conn.execute(
            "SELECT pdf_sha256 FROM refs WHERE ref_id = %s AND kind = 'pres' "
            "AND retired_at IS NULL FOR NO KEY UPDATE",
            (ref_id,),
        ).fetchone()
        if current is None or current[0] != pdf_sha256:
            return {
                "status": "failed",
                "reason": "presentation retired or source changed mid-run; inspect it and retry",
            }
        old = conn.execute(
            "SELECT ord, text, meta->>'slide_title', page_first, page_last, "
            "chunk_kind FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
        new = [
            (s.pos, s.text, s.slide_title, s.page, s.page, "pres_slide")
            for s in pres.slides
        ]
        changed = old != new
        links_removed = 0
        if changed:
            from precis.ingest.body_ops import probe_body

            links_removed = probe_body(conn, ref_id).chunk_links_total
            conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (ref_id,))
            write_pres_slides(
                pres, ref_id=ref_id, slug=ref.slug or f"pr{ref_id}", conn=conn
            )
        count = pres.meta["image_only_pages"]
        record = {
            "status": "extracted",
            "at": now_iso(),
            "pdf_sha256": pdf_sha256,
            "chunks": len(pres.slides),
            "image_only_pages": count,
            "body_changed": changed,
            "chunk_links_removed": links_removed,
        }
        store.update_ref(
            ref_id,
            meta_patch={"image_only_pages": count, "reextract": record},
            conn=conn,
        )
        tag = Tag.parse_strict(OCR_NEEDED_TAG, kind="pres")
        if count:
            store.add_tag(ref_id, tag, set_by="system", conn=conn)
        else:
            store.remove_tag(ref_id, tag, conn=conn)
        store.append_event(
            ref_id,
            source=JOB_TYPE,
            event="body_replaced" if changed else "reextract_refreshed",
            payload=record,
            conn=conn,
        )
    return record


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = ctx.meta.get("params") or {}
    ref_id = int(params["ref_id"])
    record = reextract(ctx.store, ref_id, pdf_sha256=params["pdf_sha256"])
    status = record["status"]
    ctx.append_chunk(
        "job_summary", f"pres_reextract pr{ref_id}: {status}; fitz only, no OCR"
    )
    if status == "deferred":
        parks = int(ctx.meta.get("placement_parks") or 0) + 1
        if parks <= _PLACEMENT_PARK_CAP:
            ctx.set_meta(pause_parks=0, placement_parks=parks)
            if ctx.defer is not None and ctx.defer(None, record["reason"]):
                return
        record = {
            "status": "failed",
            "reason": "placement retry exhausted or refused; restore the PDF "
            "on a worker and request reextract again",
        }
    if record["status"] == "failed":
        ctx.record_failure(f"pres_reextract pr{ref_id}: {record['reason']}")
        return
    ctx.set_meta(pres_ref_id=ref_id, reextract_status=status)


SPEC = JobTypeSpec(
    name=JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=None,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC
