"""``pdf_attach`` job_type — identity-gate a stored PDF and re-home it
onto a bodiless target ref.

The enqueued half of ``edit(kind='paper', args={'move_pdf_to': …})`` /
``{'attach_pdf': …}`` (gr517423 slice 5). The edit doors only validate
— the identity gate needs the file's leading pages and the MCP layer
never reads PDF bytes, so the verdict is read here on a corpus host
with ``read_leading_pages`` (the same gate ``paper_pull`` runs — same
``unreadable``/``missing_file`` reasons), and the move is applied only
on a ``doi``/``title`` verdict through
:func:`precis.ingest.pdf_migrate.apply_pdf_move`. ``mismatch`` fails
the job with the detail line; ``unchecked`` (the target has neither
title words nor a DOI) fails naming the metadata fix.

File placement mirrors ``paper_reextract``: a file held only on another
node parks the job for a retry (``meta.placement_parks``, ~2-day cap —
``target_node`` speaks ``PRECIS_NODE``, not the heartbeat namespace
``pdf_locations.host`` uses, so a pin would strand it); a sha the
ledger swept and found absent everywhere fails ``missing_file``; a
never-swept sha defers until a verdict exists.

On a successful apply the job enqueues ``paper_reextract`` for the
target — the moved file becomes the body — and stamps
``meta.pdf_attach`` on it; ``pdf_moved`` ref_events land on target and
donor/former-owner inside the apply transaction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "pdf_attach"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_ref_id": {"type": "integer", "minimum": 1},
        "donor_ref_id": {"type": ["integer", "null"], "minimum": 1},
        "pdf_sha256": {"type": "string", "minLength": 8},
        "reason": {"type": "string"},
    },
    "required": ["target_ref_id", "pdf_sha256"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

#: Placement re-park cap — the same bound paper_reextract uses (its
#: comment explains the ~2-day arithmetic).
_PLACEMENT_PARK_CAP = 24

DESCRIPTION = (
    "Read a stored PDF's leading pages, identity-gate it against a "
    "bodiless target, then move/attach the file and queue re-extract "
    "(the edit(move_pdf_to|attach_pdf) door's enqueued half)."
)


def idem_key(target_ref_id: int, sha: str) -> str:
    return f"pdf_attach:{target_ref_id}:{sha}"


def _state(store: Store, ref_id: int) -> dict[str, Any] | None:
    """One ref's attach-relevant state: identity fields, pdf pointer +
    attached shas, cite_keys (file resolution), live body count, and
    liveness. ``None`` when the ref does not exist."""
    from precis.ingest import pdf_migrate

    with store.pool.connection() as conn:
        title, doi = pdf_migrate.ref_identity(conn, ref_id)
        row = conn.execute(
            "SELECT r.kind, r.pdf_sha256, r.retired_at, "
            "       meta->>'superseded_by' AS tombstone, "
            "       (SELECT count(*) FROM chunks c WHERE c.ref_id = r.ref_id "
            "         AND c.ord >= 0 AND c.retired_at IS NULL) AS live_chunks, "
            "       array_remove(array_agg(DISTINCT ck.id_value), NULL) "
            "         AS cite_keys, "
            "       array_remove(array_agg(DISTINCT ps.id_value), NULL) "
            "         AS pdf_shas "
            "  FROM refs r "
            "  LEFT JOIN ref_identifiers ck "
            "    ON ck.ref_id = r.ref_id AND ck.id_kind = 'cite_key' "
            "  LEFT JOIN ref_identifiers ps "
            "    ON ps.ref_id = r.ref_id AND ps.id_kind = 'pdf_sha256' "
            " WHERE r.ref_id = %s GROUP BY r.ref_id",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    kind, pointer, retired_at, tombstone, live_chunks, cite_keys, pdf_shas = row
    pointer_s = str(pointer).strip() if pointer else None
    return {
        "kind": str(kind),
        "title": title,
        "doi": doi,
        "pointer": pointer_s,
        "retired_at": retired_at,
        "tombstone": tombstone,
        "live_chunks": int(live_chunks or 0),
        "cite_keys": tuple(sorted(k for k in (cite_keys or ()) if k)),
        "attached": sorted(
            {str(s).strip() for s in (pdf_shas or ()) if s}
            | ({pointer_s} if pointer_s else set())
        ),
    }


def _record(store: Store, ref_id: int, status: str, **fields: Any) -> dict[str, Any]:
    """Stamp ``meta.pdf_attach`` (+ a ``pdf_attach_failed`` event on
    ``failed``) on the target and return the record."""
    record: dict[str, Any] = {"status": status, **fields, "at": now_iso()}
    store.update_ref(ref_id, meta_patch={"pdf_attach": record})
    if status == "failed":
        store.append_event(
            ref_id,
            source=JOB_TYPE,
            event="pdf_attach_failed",
            payload={
                "reason": record.get("reason"),
                "pdf_sha256": record.get("pdf_sha256"),
                "donor_ref_id": record.get("donor_ref_id"),
            },
        )
    return record


def _resolve_path(
    store: Store,
    sha: str,
    *,
    cite_keys: tuple[str, ...],
    corpus_dirs: tuple[Path, ...] | None,
) -> tuple[Path | None, str]:
    """``(local path, recorded storage_path)`` for ``sha`` — the same
    resolution order ``paper_reextract`` uses."""
    from precis.corpus_layout import corpus_roots_from_env, resolve_local_pdf

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s", (sha,)
        ).fetchone()
    storage_path = str(row[0]) if row and row[0] else ""
    dirs = corpus_dirs if corpus_dirs is not None else corpus_roots_from_env()
    return resolve_local_pdf(dirs, storage_path, cite_keys), storage_path


def attach(
    store: Store,
    *,
    target_ref_id: int,
    donor_ref_id: int | None,
    pdf_sha256: str,
    reason: str | None = None,
    corpus_dirs: tuple[Path, ...] | None = None,
    reader: Callable[[Path], tuple[int, list[str]]] | None = None,
    enqueue: Callable[[int, str], int | None] | None = None,
) -> dict[str, Any]:
    """Identity-gate ``pdf_sha256`` against the target and apply the
    move. Returns the ``meta.pdf_attach`` record (``applied`` /
    ``failed`` / ``skipped`` / ``deferred``).

    ``reader``/``enqueue`` are the test seams — production defaults are
    ``pdf_identity.read_leading_pages`` and the ``paper_reextract``
    mint.
    """
    from precis.ingest import pdf_migrate
    from precis.ingest.paper_hygiene import _identity_title
    from precis.ingest.pdf_identity import read_leading_pages

    sha = pdf_sha256.strip().lower()
    tgt = _state(store, target_ref_id)
    if tgt is None:
        return {"status": "skipped", "reason": "no such target ref", "at": now_iso()}
    if tgt["retired_at"] is not None or tgt["tombstone"]:
        # meta.pdf_attach can't stamp a dead ref (update_ref filters
        # retired_at IS NULL) — same plain-record shape as the
        # no-such-target return above.
        return {
            "status": "skipped",
            "reason": "target retired or merged since enqueue",
            "pdf_sha256": sha,
            "donor_ref_id": donor_ref_id,
            "at": now_iso(),
        }
    if tgt["live_chunks"] or tgt["pointer"]:
        return _record(
            store,
            target_ref_id,
            "skipped",
            reason="target gained a body or pdf since enqueue",
            pdf_sha256=sha,
            donor_ref_id=donor_ref_id,
        )

    donor: dict[str, Any] | None = None
    if donor_ref_id is not None:
        donor = _state(store, donor_ref_id)
        if donor is None:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason="no such donor ref",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
        if donor["retired_at"] is not None or donor["tombstone"]:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason="donor retired or merged since enqueue",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
        if donor["live_chunks"]:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason="donor gained a body since enqueue — body and "
                "pdf move together via move_body_to",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
        if sha not in donor["attached"]:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason=f"pdf {sha} is no longer attached to the donor",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
    else:
        with store.pool.connection() as conn:
            owner = pdf_migrate.live_pdf_sha_owner(conn, sha)
            has_pdfs_row = (
                conn.execute(
                    "SELECT 1 FROM pdfs WHERE pdf_sha256 = %s", (sha,)
                ).fetchone()
                is not None
            )
        if owner is not None:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason=f"pdf {sha} is owned by live ref {owner} — "
                "move_pdf_to is the door for a re-home",
                pdf_sha256=sha,
            )
        if not has_pdfs_row:
            return _record(
                store,
                target_ref_id,
                "failed",
                reason=f"corpus holds no pdfs row for {sha}",
                pdf_sha256=sha,
            )

    # File resolution: the cite_key aliases of whoever the file last
    # belonged to (donor) plus the target's own, in case it was already
    # filed under the target's name.
    cite_keys = tuple(
        sorted(set(tgt["cite_keys"]) | set((donor or {}).get("cite_keys") or ()))
    )
    path, storage_path = _resolve_path(
        store, sha, cite_keys=cite_keys, corpus_dirs=corpus_dirs
    )
    if path is None:
        holders = store.pdf_holders(sha)
        if holders:
            return _record(
                store,
                target_ref_id,
                "deferred",
                reason="file held on node(s): " + ", ".join(holders),
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
                holders=holders,
            )
        if store.pdf_missing(sha):
            return _record(
                store,
                target_ref_id,
                "failed",
                reason=f"missing_file: not on disk "
                f"(storage_path={storage_path or '-'})",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
        return _record(
            store,
            target_ref_id,
            "deferred",
            reason="file not on this node and no pdf_locations verdict "
            "yet — waiting for the ledger sweep",
            pdf_sha256=sha,
            donor_ref_id=donor_ref_id,
            holders=[],
        )

    read = reader or read_leading_pages
    try:
        _, texts = read(path)
    except Exception as exc:
        return _record(
            store,
            target_ref_id,
            "failed",
            reason=f"unreadable: {type(exc).__name__}: {str(exc)[:140]}",
            pdf_sha256=sha,
            donor_ref_id=donor_ref_id,
        )
    verdict, detail = pdf_migrate.target_gate_verdict(
        texts, title=_identity_title(tgt["title"]), doi=tgt["doi"]
    )
    if verdict == "mismatch":
        return _record(
            store,
            target_ref_id,
            "failed",
            reason=f"identity_mismatch: pdf text is not this paper: {detail or '-'}",
            pdf_sha256=sha,
            donor_ref_id=donor_ref_id,
        )
    if verdict == "unchecked":
        return _record(
            store,
            target_ref_id,
            "failed",
            reason="unchecked: the target has neither title words nor a "
            "DOI to judge against",
            pdf_sha256=sha,
            donor_ref_id=donor_ref_id,
            next=f"edit(kind='{tgt['kind']}', id={target_ref_id}, "
            "title=…|doi=…) first, then re-run the attach",
        )

    with store.tx() as conn:
        # TOCTOU, same shape as the handler doors: lock both ref rows
        # in ref_id order and re-verify everything the reads above
        # proved.
        ids = [i for i in {target_ref_id, donor_ref_id} if i is not None]
        locked = {
            int(r[0]): r
            for r in conn.execute(
                "SELECT ref_id, pdf_sha256, retired_at, "
                "       meta->>'superseded_by' "
                "FROM refs WHERE ref_id = ANY(%s) "
                "ORDER BY ref_id FOR NO KEY UPDATE",
                (ids,),
            )
        }
        for rid in ids:
            row = locked.get(rid)
            if row is None or row[2] is not None or row[3] is not None:
                reason = f"ref {rid} retired or merged mid-run — nothing moved"
                if rid == target_ref_id:
                    return {
                        "status": "skipped",
                        "reason": reason,
                        "pdf_sha256": sha,
                        "donor_ref_id": donor_ref_id,
                        "at": now_iso(),
                    }
                return _record(
                    store,
                    target_ref_id,
                    "skipped",
                    reason=reason,
                    pdf_sha256=sha,
                    donor_ref_id=donor_ref_id,
                )
        if (
            locked[target_ref_id][1] is not None
            or conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL LIMIT 1",
                (target_ref_id,),
            ).fetchone()
        ):
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason="target gained a body or pdf mid-run — nothing moved",
                pdf_sha256=sha,
                donor_ref_id=donor_ref_id,
            )
        if donor_ref_id is not None:
            if conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL LIMIT 1",
                (donor_ref_id,),
            ).fetchone():
                return _record(
                    store,
                    target_ref_id,
                    "skipped",
                    reason="donor gained a body mid-run — nothing moved",
                    pdf_sha256=sha,
                    donor_ref_id=donor_ref_id,
                )
        elif pdf_migrate.live_pdf_sha_owner(conn, sha) is not None:
            return _record(
                store,
                target_ref_id,
                "skipped",
                reason="sha gained a live owner mid-run — nothing moved",
                pdf_sha256=sha,
            )
        moved = pdf_migrate.apply_pdf_move(
            conn,
            store=store,
            sha=sha,
            target_id=target_ref_id,
            donor_id=donor_ref_id,
            verdict=verdict,
            reason=reason or "",
            source=JOB_TYPE,
        )
    log.info(
        "pdf_attach: pdf %s moved %s -> %s (verdict %s)",
        sha[:16],
        moved["from"],
        moved["to"],
        verdict,
    )

    reextract_job = (
        enqueue(target_ref_id, sha)
        if enqueue is not None
        else _enqueue_reextract(store, target_ref_id, sha)
    )
    return _record(
        store,
        target_ref_id,
        "applied",
        pdf_sha256=sha,
        donor_ref_id=donor_ref_id,
        verdict=verdict,
        reextract_job=reextract_job,
    )


def _enqueue_reextract(store: Store, target_ref_id: int, sha: str) -> int | None:
    """Queue the slice-4 ``paper_reextract`` so the moved file becomes
    the target's body. Returns the job ref_id (a repeat reuses the
    queued job via idem_key).

    Mints through :func:`precis.workers._mint.mint_job_once` — a worker
    never reaches into ``precis.handlers.job`` (the lint-imports
    layering contract)."""
    from precis.workers._mint import mint_job_once
    from precis.workers.job_types import paper_reextract

    with store.tx() as conn:
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type=paper_reextract.JOB_TYPE,
            executor="job_inproc",
            title=f"paper_reextract (paper:{target_ref_id})",
            params={"ref_id": target_ref_id, "pdf_sha256": sha},
            parent_id=target_ref_id,
            idem_key=paper_reextract.idem_key(target_ref_id, sha),
        )
    return job_id


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = ctx.meta.get("params") or {}
    target_ref_id = int(params["target_ref_id"])
    sha = str(params.get("pdf_sha256") or "").strip().lower()
    record = attach(
        ctx.store,
        target_ref_id=target_ref_id,
        donor_ref_id=params.get("donor_ref_id"),
        pdf_sha256=sha,
        reason=params.get("reason"),
        enqueue=lambda tid, s: _enqueue_reextract(ctx.store, tid, s),
    )
    status = str(record["status"])
    ctx.append_chunk(
        "job_summary",
        f"pdf_attach ref {target_ref_id}: {status}"
        + (f" ({record['reason']})" if record.get("reason") else ""),
    )
    if status == "failed":
        # A quiet 'done' reads as success — failures go through the
        # terminal-failure path (same ruling as paper_reextract).
        ctx.record_failure(
            f"pdf_attach ref {target_ref_id}: {record.get('reason') or 'failed'}"
        )
        return
    if status == "deferred":
        # Placement retry — the same park shape paper_reextract uses.
        holders = [str(h) for h in (record.get("holders") or [])]
        parks = int(ctx.meta.get("placement_parks") or 0) + 1
        if parks > _PLACEMENT_PARK_CAP:
            reason = (
                f"placement retry cap ({_PLACEMENT_PARK_CAP}) reached — "
                f"{record.get('reason') or 'retry'}"
            )
            _record(
                ctx.store,
                target_ref_id,
                "failed",
                reason=reason,
                pdf_sha256=sha,
                donor_ref_id=params.get("donor_ref_id"),
            )
            ctx.record_failure(
                f"pdf_attach ref {target_ref_id}: {reason}; "
                f"next= copy the pdf into this node's corpus, or "
                f"retarget the job to a holder "
                f"({', '.join(holders) or 'none swept yet'})"
            )
            return
        ctx.set_meta(pause_parks=0, placement_parks=parks)
        defer = getattr(ctx, "defer", None)
        note = (
            f"pdf_attach ref {target_ref_id}: "
            f"{record.get('reason') or 'retry'} — re-queued"
        )
        if defer is None:
            ctx.record_failure(
                f"pdf_attach ref {target_ref_id}: deferred but this lane "
                f"has no park — {record.get('reason')}"
            )
            return
        if defer(None, note):
            return
        ctx.record_failure(
            f"pdf_attach ref {target_ref_id}: the executor refused the "
            f"park — {record.get('reason')}"
        )
        return
    ctx.set_meta(
        paper_ref_id=target_ref_id,
        pdf_attach_status=status,
        pdf_attach_reason=record.get("reason"),
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
    "attach",
    "idem_key",
    "load",
]
