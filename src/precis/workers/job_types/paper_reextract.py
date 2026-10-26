"""``paper_reextract`` job_type — re-extract one paper's body from its
stored PDF.

The enqueued half of ``edit(kind='paper', id=<pa>, args={'reextract':
True})`` (gr517423 slice 4). The edit door only validates (live ref,
reason, an attached pdf some node holds) and queues this job — it never
touches the body. The *swap* lives here, per the Opus ruling: a failed
re-extraction must never leave the ref bodiless, so the old body goes
only in the same transaction that lands the new one.

Order: resolve the target sha (``params.pdf_sha256``, else the ref's
current pointer — the named file must still be *attached* to the ref)
→ find the file on this node (``resolve_local_pdf``; held only
elsewhere → ``deferred`` — the record names the ``pdf_locations``
holders, and placement re-parks count under their own meta key with a
~2-day cap so the job keeps re-queuing until a holder claims it or
fails terminally; swept-and-gone everywhere → ``missing_file``
failure; never-swept → ``deferred`` until the ledger rules) → cheap identity
gate on the leading pages (the same ``pdf_identity`` check
``paper_pull`` runs — a merge can leave another paper's pdf attached;
junk titles read ``unchecked``, never a veto) → extract → in ONE
transaction under the ref-row lock: DELETE the old ``ord >= 0`` chunks
+ INSERT the new ones (the append-only DELETE+INSERT replace rule,
same as ``markup_backfill``), re-point ``refs.pdf_sha256``/``pdf_pages``
when the named sha differs, swap the ``content_hash`` identifier row
for the new extraction's, refresh body-owned meta, journal
``body_replaced`` (with the chunk-anchored links the DELETE cascaded).
Any failure before the swap leaves the old body untouched.

Runs under ``job_inproc``. Outcomes land on the paper's
``meta.reextract``; ``failed`` also lands a ``reextract_failed``
ref_event (and the job itself fails — a quiet ``done`` read as success,
gr517423 finding 5), ``deferred`` parks the job for a retry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from precis.ingest.paper_hygiene import _BodilessCandidate
    from precis.ingest.pipeline import BodyExtraction
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "paper_reextract"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {"type": "integer", "minimum": 1},
        "pdf_sha256": {"type": "string", "minLength": 8},
        "reason": {"type": "string"},
    },
    "required": ["ref_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

#: Placement re-park cap for a ``deferred`` run — parks land under
#: ``meta.placement_parks`` (NOT ``pause_parks``, which the deferred
#: branch resets so the shared cap never fires). At the executor's
#: default ~2h park backoff, 24 parks is ~2 days of a file staying
#: held-but-never-claimed; past it the job fails terminally naming the
#: ledger's holders (gr517423 r3 review finding A).
_PLACEMENT_PARK_CAP = 24

DESCRIPTION = (
    "Re-extract one paper's body chunks from its stored PDF (the "
    "edit(reextract) door's enqueued half; the body swap is atomic)."
)


def idem_key(ref_id: int, sha: str) -> str:
    return f"reextract:{ref_id}:{sha}"


def _ref_state(store: Store, ref_id: int) -> dict[str, Any] | None:
    """The ref's reextract-relevant state in one read: identity fields
    (title/doi for the gate), the pdf pointer + every attached sha,
    cite_keys (file resolution), last fetch source (preview check) and
    whether a body exists. ``None`` when the ref does not exist."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.kind, r.title, r.pdf_sha256, r.retired_at, "
            "       r.meta->>'superseded_by' AS tombstone, "
            "       array_remove(array_agg(DISTINCT ck.id_value), NULL) "
            "         AS cite_keys, "
            "       array_remove(array_agg(DISTINCT ps.id_value), NULL) "
            "         AS pdf_shas, "
            "       (SELECT min(d.id_value) FROM ref_identifiers d "
            "         WHERE d.ref_id = r.ref_id AND d.id_kind = 'doi') AS doi, "
            "       (SELECT p.id_value FROM ref_identifiers p "
            "         WHERE p.ref_id = r.ref_id AND p.id_kind = 'paper_id' "
            "         LIMIT 1) AS paper_id, "
            "       lf.source AS last_fetch_source "
            "  FROM refs r "
            "  LEFT JOIN ref_identifiers ck "
            "    ON ck.ref_id = r.ref_id AND ck.id_kind = 'cite_key' "
            "  LEFT JOIN ref_identifiers ps "
            "    ON ps.ref_id = r.ref_id AND ps.id_kind = 'pdf_sha256' "
            "  LEFT JOIN LATERAL ("
            "    SELECT e.source FROM ref_events e "
            "     WHERE e.ref_id = r.ref_id AND e.event = 'fetch_ok' "
            "       AND starts_with(e.source, 'fetcher:') "
            "     ORDER BY e.ts DESC LIMIT 1"
            "  ) lf ON TRUE "
            " WHERE r.ref_id = %s "
            " GROUP BY r.ref_id, lf.source",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    (
        kind,
        title,
        pointer,
        retired_at,
        tombstone,
        cite_keys,
        pdf_shas,
        doi,
        paper_id,
        last_src,
    ) = row
    pointer = str(pointer).strip() if pointer else None
    return {
        "kind": str(kind),
        "title": str(title) if title else "",
        "pointer": pointer,
        "retired_at": retired_at,
        "tombstone": tombstone,
        "cite_keys": tuple(sorted(k for k in (cite_keys or ()) if k)),
        "pdf_shas": sorted(str(s).strip() for s in (pdf_shas or ()) if s),
        "attached": sorted(
            {str(s).strip() for s in (pdf_shas or ()) if s}
            | ({pointer} if pointer else set())
        ),
        "doi": str(doi) if doi else None,
        "paper_id": str(paper_id) if paper_id else f"ref{ref_id}",
        "last_fetch_source": last_src,
    }


def _record(store: Store, ref_id: int, status: str, **fields: Any) -> dict[str, Any]:
    """Stamp ``meta.reextract`` (+ a ``reextract_failed`` event on
    ``failed``) and return the record."""
    record: dict[str, Any] = {"status": status, **fields, "at": now_iso()}
    store.update_ref(ref_id, meta_patch={"reextract": record})
    if status == "failed":
        store.append_event(
            ref_id,
            source=JOB_TYPE,
            event="reextract_failed",
            payload={
                "reason": record.get("reason"),
                "pdf_sha256": record.get("pdf_sha256"),
            },
        )
    return record


def reextract(
    store: Store,
    ref_id: int,
    *,
    pdf_sha256: str | None = None,
    corpus_dirs: tuple[Path, ...] | None = None,
    extractor: Callable[[Path, _BodilessCandidate], BodyExtraction] | None = None,
    reason: str | None = None,
    marker_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Re-extract ``ref_id``'s body from an attached, stored PDF.

    Validates → resolves the file → identity-gates → extracts → swaps
    the body in one locked transaction. Returns the ``meta.reextract``
    record (``extracted`` / ``failed`` / ``skipped`` / ``deferred``).
    """
    from precis.corpus_layout import corpus_roots_from_env, resolve_local_pdf
    from precis.ingest import body_ops, db_writer
    from precis.ingest.paper_hygiene import (
        _PREVIEW_FETCH_SOURCES,
        BODILESS_MARKER_TIMEOUT_S,
        NEEDS_OCR_TAG,
        _BodilessCandidate,
        _default_body_extractor,
        _identity_title,
        _probe_pdf,
    )
    from precis.ingest.pdf_identity import identity_verdict, read_leading_pages
    from precis.store.types import Tag

    state = _ref_state(store, ref_id)
    if state is None:
        return {"status": "skipped", "reason": "no such ref", "at": now_iso()}
    if state["kind"] != "paper":
        return _record(store, ref_id, "skipped", reason="not a paper ref")
    if state["retired_at"] is not None or state["tombstone"]:
        return _record(
            store, ref_id, "skipped", reason="ref retired or merged since enqueue"
        )
    target = (pdf_sha256 or "").strip().lower() or state["pointer"]
    if target is None:
        return _record(store, ref_id, "failed", reason="no attached pdf")
    if target not in state["attached"]:
        # The state moved since enqueue — another drop/move detached the
        # file the caller named. Not a failure, nothing to re-extract.
        return _record(
            store,
            ref_id,
            "skipped",
            reason=f"pdf {target} is no longer attached",
            pdf_sha256=target,
        )
    if state["last_fetch_source"] in _PREVIEW_FETCH_SOURCES:
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"stored file is an entitlement preview "
            f"({state['last_fetch_source']}), not the full text",
            pdf_sha256=target,
        )

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s", (target,)
        ).fetchone()
    storage_path = str(row[0]) if row and row[0] else ""
    dirs = corpus_dirs if corpus_dirs is not None else corpus_roots_from_env()
    path = resolve_local_pdf(dirs, storage_path, state["cite_keys"])
    if path is None:
        holders = store.pdf_holders(target)
        if holders:
            # A holder exists — retry elsewhere, never a failure. The
            # record names the ledger's holder(s) so an operator can
            # see where the bytes sit.
            return _record(
                store,
                ref_id,
                "deferred",
                reason="file held on node(s): " + ", ".join(holders),
                pdf_sha256=target,
                holders=holders,
            )
        if store.pdf_missing(target):
            return _record(
                store,
                ref_id,
                "failed",
                reason=f"missing_file: not on disk "
                f"(storage_path={storage_path or '-'})",
                pdf_sha256=target,
            )
        # No ledger verdict at all — the sweep hasn't judged this sha
        # yet. Defer rather than fail on a file a node may hold
        # unchecked (gr517423 r3): the next sweep writes a verdict and
        # the retried job lands held / missing correctly.
        return _record(
            store,
            ref_id,
            "deferred",
            reason="file not on this node and no pdf_locations verdict "
            "yet — waiting for the ledger sweep",
            pdf_sha256=target,
            holders=[],
        )

    try:
        pages, with_text = _probe_pdf(path)
    except Exception as exc:
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"unreadable: corrupt: {str(exc)[:160]}",
            pdf_sha256=target,
        )
    if pages == 0:
        return _record(
            store,
            ref_id,
            "failed",
            reason="unreadable: empty: 0 pages",
            pdf_sha256=target,
        )
    if with_text == 0:
        with store.pool.connection() as conn:
            store.add_tag(ref_id, Tag.open(NEEDS_OCR_TAG), set_by="system", conn=conn)
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"unreadable: scanned: {pages} page(s), no text layer",
            pdf_sha256=target,
        )

    # Identity gate before any bytes become the body: the leading pages
    # must look like the paper the ref describes (same check paper_pull
    # runs on fetched files). ``unchecked`` proceeds — nothing to
    # compare against.
    _, texts = read_leading_pages(path)
    identity, detail = identity_verdict(
        texts,
        title=_identity_title(state["title"]),
        doi=state["doi"],
    )
    if identity == "mismatch":
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"identity_mismatch: pdf text is not this paper: {detail or '-'}",
            pdf_sha256=target,
        )

    cand = _BodilessCandidate(
        ref_id=ref_id,
        pdf_sha256=target,
        title=state["title"],
        storage_path=storage_path,
        cite_keys=state["cite_keys"],
        last_fetch_source=state["last_fetch_source"],
        paper_id=state["paper_id"],
        doi=state["doi"],
    )
    extract = extractor or _default_body_extractor(
        BODILESS_MARKER_TIMEOUT_S if marker_timeout_s is None else marker_timeout_s
    )
    try:
        body = extract(path, cand)
    except Exception as exc:
        # extract_blocks_marker already absorbs a Marker failure into
        # the fitz fallback, so a raise is infrastructure, not a verdict
        # — but the caller asked for this run, so it fails loudly rather
        # than deferring (gr517423: a failed run ends the job failed).
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"extraction raised: {type(exc).__name__}: {str(exc)[:140]}",
            pdf_sha256=target,
        )
    if not body.chunks:
        reason_txt = "extractor returned no body chunks"
        fb = body.meta.get("extract_fallback_reason")
        if fb:
            reason_txt += f" (fallback: {str(fb)[:120]})"
        return _record(
            store,
            ref_id,
            "failed",
            reason=f"unreadable: {reason_txt}",
            pdf_sha256=target,
        )

    # The swap: one transaction, ref row locked — DELETE the old body
    # and INSERT the new one (the append-only replace, same shape as
    # markup_backfill), re-point the pdf when the named sha differs.
    # Any concurrent retire/detach is caught by the in-tx re-check;
    # the old body survives every failure above this line.
    pdf_pages = (
        f"[{body.page_first},{body.page_last}]"
        if body.page_first and body.page_last
        else None
    )
    with store.tx() as conn:
        row = conn.execute(
            "SELECT pdf_sha256, retired_at, meta->>'superseded_by' "
            "FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
            (ref_id,),
        ).fetchone()
        if row is None or row[1] is not None or row[2] is not None:
            return _record(
                store,
                ref_id,
                "skipped",
                reason="ref retired or merged mid-run — body untouched",
                pdf_sha256=target,
            )
        pointer_now = str(row[0]).strip() if row[0] else None
        id_rows = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'pdf_sha256'",
            (ref_id,),
        ).fetchall()
        attached_now = {str(r[0]).strip() for r in id_rows} | (
            {pointer_now} if pointer_now else set()
        )
        if target not in attached_now:
            return _record(
                store,
                ref_id,
                "skipped",
                reason=f"pdf {target} detached mid-run — body untouched",
                pdf_sha256=target,
            )
        old = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0",
            (ref_id,),
        ).fetchone()
        old_chunks = int(old[0]) if old else 0
        # Links anchored on the outgoing chunks cascade away with the
        # DELETE — count them for the event/record, the same number
        # drop_body reports.
        links_removed = body_ops.probe_body(conn, ref_id).chunk_links_total
        conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (ref_id,))
        chunk_rows = [
            (
                ref_id,
                "system",
                c.ord,
                c.chunk_kind,
                c.text,
                c.section_path,
                c.page_first,
                c.page_last,
                c.token_count,
                Jsonb(c.meta or {}),
                c.numerics or [],
            )
            for c in body.chunks
        ]
        if chunk_rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks "
                    "(ref_id, set_by, ord, chunk_kind, text, section_path, "
                    " page_first, page_last, token_count, meta, numerics) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (ref_id, ord) DO NOTHING",
                    chunk_rows,
                )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, pdf_pages = %s::int4range, "
            "pdf_role = COALESCE(pdf_role, 'main'), updated_at = now() "
            "WHERE ref_id = %s",
            (target, pdf_pages, ref_id),
        )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, %s) "
            "ON CONFLICT (id_kind, id_value) DO NOTHING",
            (target, ref_id, JOB_TYPE),
        )
        # The old extraction's content_hash describes the dropped body —
        # swap it for the new one in the same tx, as detach_body does.
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'content_hash'",
            (ref_id,),
        )
        if body.content_hash:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, "
                "source) VALUES ('content_hash', %s, %s, %s) "
                "ON CONFLICT (id_kind, id_value) DO NOTHING",
                (body.content_hash, ref_id, JOB_TYPE),
            )
        db_writer._merge_ingest_meta(ref_id, body.meta, conn=conn)
        repointed = pointer_now != target
        store.append_event(
            ref_id,
            source=JOB_TYPE,
            event="body_replaced",
            payload={
                "reason": reason,
                "old_chunks": old_chunks,
                "new_chunks": len(chunk_rows),
                "chunk_links_removed": links_removed,
                "pdf_sha256": target,
                "pdf_repointed": repointed,
                "pointer_from": pointer_now if repointed else None,
                "identity": identity,
            },
            conn=conn,
        )
        if body.spread:
            from precis.ingest.paper_hygiene import _journal_spread

            _journal_spread(store, ref_id, body.spread, path, conn=conn)
    log.info(
        "paper_reextract: ref_id=%s body swapped %d -> %d chunk(s) from %s",
        ref_id,
        old_chunks,
        len(chunk_rows),
        path,
    )
    return _record(
        store,
        ref_id,
        "extracted",
        pdf_sha256=target,
        chunks=len(chunk_rows),
        chunk_links_removed=links_removed,
        pdf_repointed=repointed,
        identity=identity,
    )


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = ctx.meta.get("params") or {}
    ref_id = int(params["ref_id"])
    record = reextract(
        ctx.store,
        ref_id,
        pdf_sha256=params.get("pdf_sha256"),
        reason=params.get("reason"),
    )
    status = str(record["status"])
    ctx.append_chunk(
        "job_summary",
        f"paper_reextract ref {ref_id}: {status}"
        + (f" ({record['reason']})" if record.get("reason") else ""),
    )
    if status == "failed":
        # A quiet 'done' reads as success to whoever watches the job —
        # failures go through the executor's terminal-failure path so
        # the bubble/alert machinery sees them (gr517423 finding 5).
        ctx.record_failure(
            f"paper_reextract ref {ref_id}: {record.get('reason') or 'failed'}"
        )
        return
    if status == "deferred":
        # Placement retry, not failure: the file lives on another node
        # or the ledger hasn't judged it yet — re-queue until a holder
        # claims it. Placement parks don't burn MAX_PAUSE_PARKS (the
        # claim gate's ``target_node`` speaks the PRECIS_NODE namespace,
        # not the heartbeat namespace ``pdf_locations.host`` uses, so a
        # pin would strand the job on most workers — deferred re-queue
        # is the routing mechanism, gr517423 r3); they count under their
        # own ``placement_parks`` key with a ~2-day cap, so a holder that
        # stays online but never claims can't park the job forever.
        holders = [str(h) for h in (record.get("holders") or [])]
        parks = int(ctx.meta.get("placement_parks") or 0) + 1
        if parks > _PLACEMENT_PARK_CAP:
            reason = (
                f"placement retry cap ({_PLACEMENT_PARK_CAP}) reached — "
                f"{record.get('reason') or 'retry'}"
            )
            _record(
                ctx.store,
                ref_id,
                "failed",
                reason=reason,
                pdf_sha256=record.get("pdf_sha256"),
            )
            ctx.record_failure(
                f"paper_reextract ref {ref_id}: {reason}; "
                f"next= copy the pdf into this node's corpus, or "
                f"retarget the job to a holder "
                f"({', '.join(holders) or 'none swept yet'})"
            )
            return
        ctx.set_meta(pause_parks=0, placement_parks=parks)
        defer = getattr(ctx, "defer", None)
        note = (
            f"paper_reextract ref {ref_id}: {record.get('reason') or 'retry'} "
            "— re-queued"
        )
        if defer is None:
            ctx.record_failure(
                f"paper_reextract ref {ref_id}: deferred but this lane "
                f"has no park — {record.get('reason')}"
            )
            return
        if defer(None, note):
            return
        ctx.record_failure(
            f"paper_reextract ref {ref_id}: the executor refused the "
            f"park — {record.get('reason')}"
        )
        return
    # Not ``ref_id=``: the executor's set_meta binds the job's own ref_id
    # positionally, so that key collides (TypeError on prod, r24).
    ctx.set_meta(
        paper_ref_id=ref_id,
        reextract_status=status,
        reextract_reason=record.get("reason"),
    )


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


__all__ = [
    "SPEC",
    "idem_key",
    "load",
    "reextract",
]
