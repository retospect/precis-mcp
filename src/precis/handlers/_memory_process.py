"""Bounded memory embedding requests through the existing worker job lane.

The memory and chunk locks serialize invalidation with native edits and worker
claims. Jobs pin append-only chunk IDs, nullable content hashes and text hashes;
they never follow a ref onto a later revision. Existing job priority promotes
only this bounded work order, without changing the scheduler or default scans.
Other enrichment passes lack the required targeted, revision-aware job contract
and are explicitly refused. Reader salience is not a processing control.

Queue map: chunk_keywords scans missing/version-stale keywords after body
embedding, with conv/draft first then newest fresh/stale chunks. RAKE inherits
WorkerHandler's oldest-chunk scan and abandoned-claim top-up. LLM summaries
use draft/conv, reader-recency, cold and reclaim tiers; derived_drain cannot
select one memory. axis:move accepts selected refs internally but stores only
ref-tag completion, without an input revision. ingest.blocks.fill_embeddings
is an in-memory helper, not a scheduling door.

An active request is shared unless recompute would reuse a promote or retry:
those jobs preserve complete artifacts, so recompute must wait for them to end.
A completed recompute receipt is reused for that revision, so replaying a
request cannot erase its result.
Retry creates a new attempt only after failure. Readiness always comes from
current artifacts, never the job's terminal status. Waits release connections
between snapshots and never execute an embedder in the MCP process.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from precis.errors import BadInput, NotFound
from precis.handlers._memory_processing import (
    _SNAPSHOT_SQL,
    _artifact,
    _categorization,
    _keywords,
)
from precis.protocol import Response
from precis.store import Store
from precis.store._chunks_ops import header_card_text
from precis.utils.next_block import render_next_section
from precis.workers._mint import (
    JOB_TERMINAL_STATUSES,
    latest_job_for_idem,
    mint_job_once,
)
from precis.workers.llm_summarize import SUMMARIZER_NAME
from precis.workers.memory_embed import MAX_TARGET_CHUNKS

_STAGES = (
    "embeddings",
    "keywords",
    "summary/summarize:rake-lemma",
    "summary/llm_summarize",
    "categorization",
)
_ACTIONS = ("promote", "retry", "recompute")
_MAX_WAIT_S = 10
_UNSUPPORTED = {
    "keywords": "chunk_keywords has no selected-memory queue control",
    "summary/summarize:rake-lemma": "summarize:rake-lemma has no selected-memory queue control",
    "summary/llm_summarize": "derived_drain cannot target this revision; reader salience also changes recall",
    "categorization": "axis:move has no revision-attested targeted job receipt",
}


@dataclass(frozen=True)
class _Receipt:
    state: str
    detail: str
    job_id: int | None = None
    target: dict[str, Any] | None = None
    next_action: str | None = None


def _inspect(ref_id: int) -> str:
    return f"get(kind='memory', id={ref_id}, view='processing')"


def _call(ref_id: int, action: str) -> str:
    return (
        f"put(kind='memory', id={ref_id}, mode='process', "
        f"args={{'stages': ['embeddings'], 'action': '{action}'}})"
    )


def _rows(conn: Connection, ref_id: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            _SNAPSHOT_SQL, {"ref_id": ref_id, "summarizer": SUMMARIZER_NAME}
        ).fetchall()


def _target(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ref_id": rows[0]["ref_id"],
        "embedder": rows[0]["embedder"],
        "chunks": [
            {
                "chunk_id": row["chunk_id"],
                "content_sha": row["content_sha"],
                "text_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest(),
            }
            for row in rows
            if row["chunk_id"] is not None
        ],
    }


def _embedding_block(rows: list[dict[str, Any]]) -> str | None:
    chunks = [row for row in rows if row["chunk_id"] is not None]
    if not rows[0]["embedder"]:
        return "no default embedder registered; ask the worker operator to configure it"
    if len(chunks) > MAX_TARGET_CHUNKS:
        return f"more than {MAX_TARGET_CHUNKS} current chunks; split the memory before processing"
    if not any(row["chunk_kind"] == "memory_body" for row in chunks):
        return "no current body chunk; repair the memory body before processing"
    header = header_card_text(rows[0]["title"], rows[0]["hook"])
    cards = [row for row in chunks if row["chunk_kind"] == "card_combined"]
    if (header and not cards) or any(row["text"] != header for row in cards):
        return (
            "header card missing or stale; ask the operator to repair the header card"
        )
    if any(row["no_index"] == "true" for row in chunks):
        return (
            "chunk meta.no_index=true; ask the operator to review indexing eligibility"
        )
    return None


def _request_embeddings(
    store: Store, conn: Connection, rows: list[dict[str, Any]], action: str
) -> _Receipt:
    blocked = _embedding_block(rows)
    if blocked:
        return _Receipt("refused", blocked)
    target = _target(rows)
    ref_id = int(target["ref_id"])
    digest = hashlib.sha256(
        json.dumps(target, sort_keys=True).encode("utf-8")
    ).hexdigest()
    key = f"memory-process:{ref_id}:{digest}"
    prior = {a: latest_job_for_idem(conn, f"{key}:{a}") for a in _ACTIONS}
    for previous_action, previous in prior.items():
        if previous is not None and previous[1] not in JOB_TERMINAL_STATUSES:
            if action == "recompute" and previous_action != "recompute":
                return _Receipt(
                    "refused",
                    f"{previous_action} is active; wait for job jo{previous[0]} "
                    "to finish, then request action='recompute' again",
                    previous[0],
                    target,
                    next_action="recompute",
                )
            return _Receipt("pending", "reused active request", previous[0], target)
    states = [_artifact(row, "embedding", "embeddings").state for row in rows]
    previous_recompute = prior["recompute"]
    if action == "recompute" and previous_recompute is not None:
        return _Receipt(
            "pending",
            "reused recompute request for this revision",
            previous_recompute[0],
            target,
        )
    if all(state == "complete" for state in states) and action != "recompute":
        return _Receipt(
            "ready", "body and header embeddings match current revision", target=target
        )
    failed_job = any(p and p[1] in {"failed", "cancelled"} for p in prior.values())
    if action == "promote" and ("failed" in states or failed_job):
        return _Receipt(
            "refused",
            "failed work requires action='retry'",
            target=target,
            next_action="retry",
        )
    if action == "retry" and "failed" not in states and not failed_job:
        return _Receipt(
            "refused",
            "no recorded failure to retry; use action='promote'",
            target=target,
            next_action="promote",
        )
    if action == "recompute" and "failed" in states:
        return _Receipt(
            "refused",
            "failed artifacts require action='retry' before recompute",
            target=target,
            next_action="retry",
        )
    if action == "recompute" and "complete" not in states:
        return _Receipt(
            "refused",
            "no completed output to recompute; use action='promote'",
            target=target,
            next_action="promote",
        )
    if any(row["embedding_claimed_at"] is not None for row in rows):
        return _Receipt(
            "refused",
            "existing chunk claim may be running or abandoned; inspect it before retrying; no claim was changed",
            target=target,
        )
    ids = [row["chunk_id"] for row in rows]
    invalidated = 0
    if action in {"retry", "recompute"}:
        # Only derived artifacts are invalidated; body chunks remain append-only.
        predicate = " AND status = 'failed'" if action == "retry" else ""
        invalidated = conn.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id = ANY(%s) AND embedder = %s"
            + predicate,
            (ids, target["embedder"]),
        ).rowcount
    job_id, reused = mint_job_once(
        store,
        conn=conn,
        job_type="embed_batch",
        executor="job_inproc",
        title=f"Process memory me{ref_id} embeddings ({action})",
        params={"memory_target": target, "limit": len(ids)},
        idem_key=f"{key}:{action}",
        prio=1,
        origin="agent",
    )
    return _Receipt(
        "queued",
        "reused request"
        if reused
        else f"queued targeted worker request; invalidated {invalidated} embedding artifacts",
        job_id,
        target,
    )


def _observe(store: Store, ref_id: int, receipt: _Receipt) -> _Receipt:
    with store.pool.connection() as conn, conn.transaction():
        rows = _rows(conn, ref_id)
        if not rows or _target(rows) != receipt.target:
            return _Receipt(
                "stale",
                "memory revision changed; request the current revision",
                receipt.job_id,
                receipt.target,
                next_action="promote",
            )
        blocked = _embedding_block(rows)
        if blocked:
            return _Receipt("refused", blocked, receipt.job_id, receipt.target)
        states = [_artifact(row, "embedding", "embeddings").state for row in rows]
        if all(state == "complete" for state in states):
            return _Receipt(
                "ready",
                "body and header artifacts match the requested current revision",
                receipt.job_id,
                receipt.target,
            )
        if "failed" in states:
            return _Receipt(
                "failed",
                "worker recorded a failed embedding; use action='retry'",
                receipt.job_id,
                receipt.target,
                next_action="retry",
            )
        job = conn.execute(
            "SELECT t.value FROM refs r JOIN ref_tags rt ON rt.ref_id=r.ref_id "
            "JOIN tags t USING(tag_id) WHERE r.ref_id=%s AND r.kind='job' "
            "AND r.retired_at IS NULL AND t.namespace='STATUS'",
            (receipt.job_id,),
        ).fetchone()
    status = str(job[0]) if job else "unknown"
    state = (
        status if status in {"queued", "running", "failed", "cancelled"} else "pending"
    )
    return _Receipt(
        state,
        f"job status={status}; requested artifacts are not ready",
        receipt.job_id,
        receipt.target,
        next_action="retry" if state in {"failed", "cancelled"} else None,
    )


def process_memory(
    store: Store,
    ref_id: int,
    *,
    stages: list[str] | None,
    action: str | None,
    wait_s: float | None,
) -> Response:
    """Queue at most one memory; unsupported stages retain explicit refusals."""
    action = "promote" if action is None else action
    if action not in _ACTIONS:
        raise BadInput("unknown processing action", options=list(_ACTIONS))
    wait = 0 if wait_s is None else wait_s
    if (
        isinstance(wait, bool)
        or not isinstance(wait, (int, float))
        or not math.isfinite(wait)
        or not 0 <= wait <= _MAX_WAIT_S
    ):
        raise BadInput(
            "wait_s must be a finite number from 0 to 10", next=_call(ref_id, action)
        )
    if stages is not None and (
        not isinstance(stages, list)
        or not stages
        or len(stages) > len(_STAGES)
        or any(not isinstance(stage, str) for stage in stages)
    ):
        raise BadInput(
            "stages must be a nonempty list of at most five stage names",
            options=list(_STAGES),
        )
    with store.tx() as conn:
        ref = conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id=%s AND kind='memory' "
            "AND retired_at IS NULL FOR NO KEY UPDATE",
            (ref_id,),
        ).fetchone()
        if ref is None:
            raise NotFound(
                "live memory not found", next="search(kind='memory', q='<topic>')"
            )
        # Workers claim chunks with FOR UPDATE; taking the same lock closes
        # the check/invalidate race without deleting their durable leases.
        conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id=%s AND retired_at IS NULL "
            "AND (chunk_kind='memory_body' OR (chunk_kind='card_combined' AND ord=-1)) "
            "ORDER BY chunk_id FOR UPDATE",
            (ref_id,),
        ).fetchall()
        rows = _rows(conn, ref_id)
        requested = list(dict.fromkeys(stages if stages is not None else _STAGES))
        requested = [
            "embeddings" if s == f"embed:{rows[0]['embedder']}" else s
            for s in requested
        ]
        if any(stage not in _STAGES for stage in requested):
            raise BadInput("unknown processing stage", options=list(_STAGES))
        requested = list(dict.fromkeys(requested))
        receipt = (
            _request_embeddings(store, conn, rows, action)
            if "embeddings" in requested
            else None
        )
    request_detail = receipt.detail if receipt is not None else ""
    if (
        receipt is not None
        and receipt.job_id is not None
        and receipt.state != "refused"
    ):
        deadline = time.monotonic() + wait
        while True:
            receipt = _observe(store, ref_id, receipt)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or receipt.state not in {"pending", "queued", "running"}:
                break
            time.sleep(min(0.25, remaining))
    lines = [
        f"memory me{ref_id} process action={action}; requested: {', '.join(requested)}"
    ]
    hints = [(_inspect(ref_id), "inspect current revision and every enrichment stage")]
    chunks = [row for row in rows if row["chunk_id"] is not None]
    bodies = [row for row in chunks if row["chunk_kind"] == "memory_body"]
    for stage in requested:
        if stage == "embeddings" and receipt is not None:
            handle = f"; job jo{receipt.job_id}" if receipt.job_id is not None else ""
            lines.append(f"embeddings: {receipt.state} — {receipt.detail}{handle}")
            if request_detail != receipt.detail:
                lines.append(f"Request effect: {request_detail}.")
            if receipt.target:
                lines.append(
                    "Requested chunks: "
                    + ", ".join(
                        f"pc{c['chunk_id']} sha256={c['text_sha256']}"
                        for c in receipt.target["chunks"]
                    )
                )
            if receipt.job_id is not None:
                hints.append(
                    (
                        f"get(kind='job', id={receipt.job_id})",
                        "inspect the worker request; job success alone is not readiness",
                    )
                )
            if receipt.next_action is not None:
                hints.append(
                    (
                        _call(ref_id, receipt.next_action),
                        "request the indicated action for current work",
                    )
                )
        else:
            evidence = (
                [_keywords(row) for row in bodies]
                if stage == "keywords"
                else [_categorization(rows[0], bodies)]
                if stage == "categorization"
                else [
                    _artifact(row, "rake" if "rake" in stage else "llm", stage)
                    for row in (chunks if "rake" in stage else bodies)
                ]
            )
            if not evidence or all(s.state == "not applicable" for s in evidence):
                lines.append(
                    f"{stage}: not applicable — "
                    + (evidence[0].detail if evidence else "no eligible body")
                )
            else:
                lines.append(
                    f"{stage}: refused — {_UNSUPPORTED[stage]}; inspect processing and ask the worker operator for targeted support"
                )
                hints.append(
                    (
                        "get(kind='skill', id='precis-job-help')",
                        "processing controls and unsupported stage next steps",
                    )
                )
    return Response(
        body="\n".join(lines) + render_next_section(list(dict.fromkeys(hints)))
    )
