"""``survey_extract`` + ``measure_accept`` — the survey/review numeric
extraction pass (docs/backlog/measures-substrate.md, "Next slice").

``survey_extract`` walks one paper's chunks in slices. Each live chunk
is rendered with ``[n]`` anchors (:mod:`precis.extract.anchors`),
prompted to the local model on the served-OSS lane (``placement=
"local"``, ``Tier.BIG`` — the local reader on castor), and the reply is
turned into checked runs by :func:`precis.extract.proposals.build_runs`.
Good runs land through ``store.insert_measure`` with
``meta.extraction_run`` — the batch token the acceptance step samples
by. A re-run of the same paper re-derives the same literals on the same
spans and the store's duplicate refusal turns them into skips, so the
pass is idempotent.

When every chunk is read the job spawns one ``measure_accept`` child
and waits on it. ``measure_accept`` draws the deterministic stratified
sample (:mod:`precis.extract.accept`), has a frontier-tier model review
each sampled row against its anchored span, records the verdicts on the
``reviews`` ledger, and marks the batch accepted or failed by the
Wilson rule (upper edge of the error-rate interval ≤ the bar, default
5% at the one-sided 95% quantile). The verdict rides
``refs.meta.acceptance`` on the accept job and comes back into the
parent's summary.

``measure_accept`` is also a standalone job_type — re-accepting a batch
(more samples, a different bar, a new frontier model) never re-runs the
extraction.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.extract import accept as _accept
from precis.extract import prompt as _prompt
from precis.extract.proposals import VocabEntry, build_runs, vocab_entry
from precis.utils.env import env_int
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    resolve_model,
    route,
    tier_from_str,
)
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

_ACTOR = "survey-extract"
_ESCALATION_CAP = 200


# ── helpers shared by both job types ────────────────────────────────


def _paper_id(store: Store, raw: Any) -> int | None:
    """``paper`` param → a live paper ref_id (int or ``pa…`` slug)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (raw,),
            ).fetchone()
        return int(raw) if row else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return _paper_id(store, int(text))
        resolved = store.resolve_handle(text)
        if resolved is not None:
            return int(resolved.ref_id)
    return None


def _paper_title(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _chunks(store: Store, paper: int) -> list[dict[str, Any]]:
    """The paper's live body chunks, ord order."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, chunk_kind, text, section_path "
            "  FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "  AND retired_at IS NULL ORDER BY ord",
            (paper,),
        ).fetchall()
    out = []
    for cid, ord_, kind, text, section in rows:
        sec = None
        if isinstance(section, list) and section:
            sec = str(section[-1])
        out.append(
            {
                "chunk_id": int(cid),
                "ord": int(ord_),
                "chunk_kind": str(kind),
                "text": str(text or ""),
                "section": sec,
            }
        )
    return out


def _measurand_root(store: Store, raw: Any) -> int | None:
    """``measurand_root`` param or the 'measurand' start node's ref_id."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'slug' = 'measurand' "
            "ORDER BY ref_id LIMIT 1"
        ).fetchone()
    return int(row[0]) if row else None


def _vocab(store: Store, root: int | None) -> dict[int, VocabEntry]:
    """The measurand vocabulary the reader may name."""
    if root is None:
        return {}
    ids = [root]
    try:
        ids += [rid for rid, _depth, _axis in store.taxon_descendants(root)]
    except Exception:
        log.debug("survey_extract: taxon_descendants(%s) failed", root)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta, title FROM refs WHERE ref_id = ANY(%s) "
            "AND retired_at IS NULL",
            (ids,),
        ).fetchall()
    out: dict[int, VocabEntry] = {}
    for rid, meta, title in rows:
        m = dict(meta or {})
        if not m.get("name"):
            m["name"] = str(title or "")
        out[int(rid)] = vocab_entry(int(rid), m)
    return out


def _escalate(bucket: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    if len(bucket) < _ESCALATION_CAP:
        bucket.append(entry)


def _wake_s(seconds: int = 1) -> WakeWhen:
    return WakeWhen("at_time", {"ts": int(time.time()) + seconds})


def _retry_wake(result: Any) -> WakeWhen:
    retry_at = getattr(result, "retry_at", None)
    if retry_at is not None:
        return WakeWhen("at_time", {"ts": int(retry_at.timestamp())})
    return _wake_s(300)


# ── survey_extract ──────────────────────────────────────────────────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": ["integer", "string"]},
        "model": {"type": ["string", "null"]},
        "tier": {"type": ["string", "null"]},
        "chunks_per_slice": {"type": ["integer", "null"]},
        "min_chars": {"type": ["integer", "null"]},
        "measurand_root": {"type": ["integer", "null"]},
        "run_token": {"type": ["string", "null"]},
        "skip_accept": {"type": ["boolean", "null"]},
        "accept_model": {"type": ["string", "null"]},
        "accept_tier": {"type": ["string", "null"]},
        "sample_n": {"type": ["integer", "null"]},
        "bar": {"type": ["number", "null"]},
    },
    "required": ["paper"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Survey/review numeric extraction over one paper's chunks — local "
    "reader on the served-OSS lane, anchored measure rows, then a "
    "spawned measure_accept child samples the batch (Wilson rule)."
)


def _chunks_per_slice(params: dict[str, Any]) -> int:
    raw = params.get("chunks_per_slice")
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 200:
        return raw
    return env_int("PRECIS_SURVEY_EXTRACT_CHUNKS_PER_SLICE", 8, lo=1, hi=200)


def _min_chars(params: dict[str, Any]) -> int:
    raw = params.get("min_chars")
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 4000:
        return raw
    return 60


def _max_extract_errors() -> int:
    """Consecutive LLM-error slices tolerated before the job fails."""
    return env_int("PRECIS_SURVEY_EXTRACT_MAX_ERRORS", 10, lo=1, hi=100)


def _fresh_stats() -> dict[str, int]:
    return {
        "chunks": 0,
        "calls": 0,
        "proposals": 0,
        "written": 0,
        "skipped_dup": 0,
        "skipped_short": 0,
        "dropped": 0,
        "llm_errors": 0,
    }


def _process_chunk(
    ctx: DispatchContext,
    *,
    chunk: dict[str, Any],
    paper: int,
    paper_title: str | None,
    vocab: dict[int, VocabEntry],
    run_token: str,
    tier: Tier,
    model_pin: str | None,
    stats: dict[str, int],
    escalations: list[dict[str, Any]],
) -> str | None:
    """One chunk end to end. ``'paused'`` when the router paused (the
    caller yields for the window rather than burning through it),
    ``'failed'`` when the consecutive-error cap says the lane is down,
    ``None`` otherwise."""
    req = LlmRequest(
        tier=tier,
        prompt=_prompt.reader_prompt(
            vocab=sorted(vocab.values(), key=lambda e: e.slug),
            chunk_text=chunk["text"],
            section=chunk.get("section"),
            paper_title=paper_title,
        ),
        source="survey_extract",
        ref_id=paper,
        model=model_pin,
        placement=None if model_pin else "local",
        thinking=False,
        temperature=0.0,
        max_tokens=2048,
        log_blobs=False,
    )
    stats["calls"] += 1
    try:
        result = route(req)
    except Exception as exc:
        stats["llm_errors"] += 1
        stats["consecutive_errors"] = stats.get("consecutive_errors", 0) + 1
        _escalate(
            escalations,
            {
                "kind": "llm_exception",
                "chunk": chunk["chunk_id"],
                "detail": str(exc)[:200],
            },
        )
        return (
            "failed" if stats["consecutive_errors"] >= _max_extract_errors() else None
        )
    if getattr(result, "paused", False):
        stats["consecutive_errors"] = stats.get("consecutive_errors", 0) + 1
        return "paused"
    stats["consecutive_errors"] = 0
    if getattr(result, "error", None) or not getattr(result, "text", ""):
        stats["llm_errors"] += 1
        stats["consecutive_errors"] = stats.get("consecutive_errors", 0) + 1
        _escalate(
            escalations,
            {
                "kind": "llm_error",
                "chunk": chunk["chunk_id"],
                "detail": str(getattr(result, "error", "empty reply"))[:200],
            },
        )
        return (
            "failed" if stats["consecutive_errors"] >= _max_extract_errors() else None
        )
    stats["consecutive_errors"] = 0
    reply = _prompt.parse_reply(result.text)
    model_used = getattr(result, "model", None) or model_pin
    if reply is None:
        stats["dropped"] += 1
        _escalate(
            escalations,
            {
                "kind": "unparseable_reply",
                "chunk": chunk["chunk_id"],
                "detail": (result.text or "")[:200],
            },
        )
        return None
    outcome = build_runs(
        reply,
        chunk_id=chunk["chunk_id"],
        chunk_text=chunk["text"],
        paper_ref_id=paper,
        vocab=vocab,
        extraction_run=run_token,
        model=model_used,
    )
    for drop in outcome.dropped:
        stats["dropped"] += 1
        _escalate(
            escalations,
            {
                "kind": "dropped",
                "chunk": chunk["chunk_id"],
                "reason": drop.get("reason"),
                "location": drop.get("location"),
                "detail": str(drop.get("detail"))[:200],
            },
        )
    for oflag in outcome.flags:
        _escalate(
            escalations,
            {
                "kind": "flag",
                "chunk": chunk["chunk_id"],
                "flag": oflag.get("flag"),
                "location": oflag.get("location"),
                "detail": str(oflag.get("detail"))[:200],
            },
        )
    for proposal in outcome.proposals:
        stats["proposals"] += 1
        try:
            run = ctx.store.insert_measure(
                proposal.output,
                proposal.inputs,
                actor=_ACTOR,
                model=model_used,
            )
        except BadInput as exc:
            if "duplicate of measure" in str(exc):
                stats["skipped_dup"] += 1
            else:
                stats["dropped"] += 1
                _escalate(
                    escalations,
                    {
                        "kind": "write_refused",
                        "chunk": chunk["chunk_id"],
                        "location": proposal.location,
                        "detail": str(exc)[:200],
                    },
                )
            continue
        stats["written"] += 1
        for flag in proposal.flags:
            _escalate(
                escalations,
                {
                    "kind": "flag",
                    "chunk": chunk["chunk_id"],
                    "flag": flag,
                    "location": proposal.location,
                    "measure": run.output_id,
                },
            )
    return None


def _survey_dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Any:
    state = (ctx.meta or {}).get("coordinator_state") or {}
    phase = state.get("phase") or "extract"
    if phase == "extract":
        return _phase_extract(ctx, state)
    if phase == "await_accept":
        return _phase_await(ctx, state)
    return Done(summary=f"survey_extract: unknown phase {phase!r}", success=False)


def _phase_extract(ctx: DispatchContext, state: dict[str, Any]) -> Any:
    params = (ctx.meta or {}).get("params") or {}
    paper = _paper_id(ctx.store, params.get("paper"))
    if paper is None:
        return Done(
            summary="survey_extract: params.paper does not resolve to a live ref",
            success=False,
        )
    chunks = _chunks(ctx.store, paper)
    run_token = str(
        params.get("run_token")
        or state.get("run_token")
        or f"survey-extract:{ctx.ref_id}"
    )
    stats = dict(state.get("stats") or _fresh_stats())
    escalations = list(state.get("escalations") or [])
    idx = int(state.get("idx") or 0)
    per_slice = _chunks_per_slice(params)
    min_chars = _min_chars(params)
    tier = tier_from_str(params.get("tier") or "big", default=Tier.BIG)
    model_pin = params.get("model") if isinstance(params.get("model"), str) else None
    title = _paper_title(ctx.store, paper)
    vocab = _vocab(ctx.store, _measurand_root(ctx.store, params.get("measurand_root")))

    if ctx.is_cancel_requested():
        return Done(summary="survey_extract cancelled", success=False)

    processed = 0
    while idx < len(chunks) and processed < per_slice:
        chunk = chunks[idx]
        idx += 1
        if len(chunk["text"].strip()) < min_chars:
            stats["skipped_short"] += 1
            continue
        processed += 1
        stats["chunks"] += 1
        outcome = _process_chunk(
            ctx,
            chunk=chunk,
            paper=paper,
            paper_title=title,
            vocab=vocab,
            run_token=run_token,
            tier=tier,
            model_pin=model_pin,
            stats=stats,
            escalations=escalations,
        )
        if outcome == "failed" or (
            outcome == "paused"
            and int(stats.get("consecutive_errors") or 0) >= _max_extract_errors()
        ):
            return Done(
                summary=(
                    f"survey_extract: the reader failed "
                    f"{stats.get('consecutive_errors')} chunks running — "
                    "the local lane looks down"
                ),
                success=False,
                summary_meta={"stats": stats},
            )
        if outcome == "paused":
            return Yield(
                state={
                    "phase": "extract",
                    "idx": idx - 1,
                    "stats": stats,
                    "escalations": escalations,
                    "run_token": run_token,
                },
                wake_when=_wake_s(300),
            )

    if idx < len(chunks):
        if ctx.is_cancel_requested():
            return Done(summary="survey_extract cancelled", success=False)
        return Yield(
            state={
                "phase": "extract",
                "idx": idx,
                "stats": stats,
                "escalations": escalations,
                "run_token": run_token,
            },
            wake_when=_wake_s(1),
        )

    # Extraction done — spawn the acceptance child (unless skipped).
    if params.get("skip_accept"):
        acc_note = "acceptance skipped (params.skip_accept)"
        accept_child = None
    else:
        accept_child = ctx.spawn_child(
            "measure_accept",
            executor="coordinator",
            params={
                "paper": paper,
                "extraction_run": run_token,
                "sample_n": params.get("sample_n"),
                "bar": params.get("bar"),
                "tier": params.get("accept_tier"),
            },
            model=params.get("accept_model"),
            idem_key=f"measure-accept:{ctx.ref_id}",
        )
        acc_note = f"acceptance delegated to measure_accept job {accept_child}"

    ctx.append_chunk(
        "job_event",
        "extract done: "
        + ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        + f" · escalations={len(escalations)}",
    )
    if accept_child is not None:
        return Yield(
            state={
                "phase": "await_accept",
                "stats": stats,
                "escalations": escalations,
                "run_token": run_token,
                "accept_child": accept_child,
            },
            wake_when=WakeWhen("children_done", {"child_job_ids": [accept_child]}),
        )
    return _survey_done(ctx, stats, escalations, run_token, acc_note, None)


def _phase_await(ctx: DispatchContext, state: dict[str, Any]) -> Any:
    child = state.get("accept_child")
    if not isinstance(child, int):
        return _survey_done(
            ctx,
            dict(state.get("stats") or {}),
            list(state.get("escalations") or []),
            str(state.get("run_token") or ""),
            "no accept child recorded",
            None,
        )
    with ctx.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.meta, COALESCE("
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id"
            "   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),"
            "  'queued') "
            "FROM refs r WHERE r.ref_id = %s",
            (child,),
        ).fetchone()
    status = "missing" if row is None else str(row[1])
    if status in {"queued", "running", "claimed"} or status.startswith("waiting"):
        if ctx.is_cancel_requested():
            return Done(summary="survey_extract cancelled", success=False)
        return Yield(state=state, wake_when=_wake_s(120))
    acceptance = None
    if row is not None and isinstance(row[0], dict):
        acceptance = row[0].get("acceptance")
    note = f"accept child {child} ended {status}"
    return _survey_done(
        ctx,
        dict(state.get("stats") or {}),
        list(state.get("escalations") or []),
        str(state.get("run_token") or ""),
        note,
        acceptance if isinstance(acceptance, dict) else None,
    )


def _survey_done(
    ctx: DispatchContext,
    stats: dict[str, Any],
    escalations: list[dict[str, Any]],
    run_token: str,
    accept_note: str,
    acceptance: dict[str, Any] | None,
) -> Done:
    head = f"survey_extract done ({run_token}): " + ", ".join(
        f"{k}={v}" for k, v in sorted(stats.items())
    )
    lines = [head, f"acceptance: {accept_note}"]
    if acceptance:
        lines.append(
            "batch "
            + ("ACCEPTED" if acceptance.get("accepted") else "FAILED")
            + f" — {acceptance.get('errors')} errors in "
            + f"{acceptance.get('sampled')} sampled "
            + f"(Wilson upper {acceptance.get('wilson_hi')}"
            f" vs bar {acceptance.get('bar')})"
        )
    if escalations:
        lines.append(f"escalations ({len(escalations)}):")
        for e in escalations[:50]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in e.items()))
    ctx.append_chunk("job_summary", "\n".join(lines))
    meta: dict[str, Any] = {
        "extraction_run": run_token,
        "stats": stats,
        "escalation_count": len(escalations),
    }
    if acceptance:
        meta["acceptance"] = acceptance
    return Done(summary="\n".join(lines), success=True, summary_meta=meta)


SPEC = JobTypeSpec(
    name="survey_extract",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_survey_dispatch,
)


# ── measure_accept ──────────────────────────────────────────────────

ACCEPT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": ["integer", "string"]},
        "extraction_run": {"type": ["string", "null"]},
        "sample_n": {"type": ["integer", "null"]},
        "bar": {"type": ["number", "null"]},
        "z": {"type": ["number", "null"]},
        "tier": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "reviews_per_slice": {"type": ["integer", "null"]},
    },
    "required": ["paper"],
    "additionalProperties": True,
}

ACCEPT_DESCRIPTION = (
    "Sampling acceptance of one extraction batch — stratified sample of "
    "the batch's output rows, frontier-model verdicts on the reviews "
    "ledger, Wilson interval verdict on the batch."
)


def _reviews_per_slice(params: dict[str, Any]) -> int:
    raw = params.get("reviews_per_slice")
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 50:
        return raw
    return env_int("PRECIS_MEASURE_ACCEPT_PER_SLICE", 6, lo=1, hi=50)


def _batch_rows(store: Store, paper: int, run_token: str) -> list[dict[str, Any]]:
    """The batch's live output rows (newest-first irrelevant — the sample
    is drawn deterministically over the id set)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT m.id, m.measurand_ref_id, m.literal, m.reported_unit, "
            "       m.subject, m.span, m.anchor_scheme, m.run_key, "
            "       l.src_chunk_id, t.meta->>'slug' "
            "  FROM measures m "
            "  JOIN links l ON l.link_id = m.primary_link_id "
            "  LEFT JOIN refs t ON t.ref_id = m.measurand_ref_id "
            " WHERE m.subject_ref_id = %s AND m.direction = 'output' "
            "   AND m.superseded_by IS NULL "
            "   AND m.meta->>'extraction_run' = %s ORDER BY m.id",
            (paper, run_token),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "measurand_ref_id": r[1],
            "literal": r[2],
            "reported_unit": r[3],
            "subject": r[4],
            "span": r[5],
            "anchor_scheme": r[6],
            "run_key": r[7],
            "chunk_id": r[8],
            "measurand": r[9] or str(r[1]),
        }
        for r in rows
    ]


def _latest_run_token(store: Store, paper: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'extraction_run' AS tok, max(created_at) "
            "  FROM measures WHERE subject_ref_id = %s "
            "   AND meta ? 'extraction_run' GROUP BY tok "
            " ORDER BY max(created_at) DESC LIMIT 1",
            (paper,),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _span_text(store: Store, row: dict[str, Any]) -> str | None:
    """The row's anchored text: the span's slice for an offsets anchor,
    the whole chunk otherwise (mirrors the store's own _span_text)."""
    with store.pool.connection() as conn:
        r = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (row["chunk_id"],)
        ).fetchone()
    if r is None:
        return None
    text = str(r[0] or "")
    span = row.get("span")
    if (
        isinstance(span, list)
        and len(span) == 3
        and all(isinstance(x, int) for x in span)
    ):
        return text[span[1] : span[2]]
    return text


def _row_conditions(store: Store, run_key: str) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'condition', literal FROM measures "
            "WHERE run_key = %s AND direction = 'input' ORDER BY id",
            (run_key,),
        ).fetchall()
    return [f"{cond or '?'} = {lit}" for cond, lit in rows if isinstance(lit, str)]


_VERDICT_TO_REVIEW = {
    "correct": "approved",
    "wrong": "rejected",
    "undecidable": "proposed",
}


def _accept_dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Any:
    state = (ctx.meta or {}).get("coordinator_state") or {}
    params = (ctx.meta or {}).get("params") or {}
    paper = _paper_id(ctx.store, params.get("paper"))
    if paper is None:
        return Done(
            summary="measure_accept: params.paper does not resolve to a live ref",
            success=False,
        )
    run_token = str(
        params.get("extraction_run") or _latest_run_token(ctx.store, paper) or ""
    )
    if not run_token:
        return Done(
            summary=(
                f"measure_accept: no extraction_run given and none found on "
                f"paper {paper}"
            ),
            success=False,
        )
    rows = _batch_rows(ctx.store, paper, run_token)
    if not rows:
        return Done(
            summary=(
                f"measure_accept: no live output rows for run {run_token!r} "
                f"on paper {paper}"
            ),
            success=False,
        )

    if "sampled" not in state:
        strata: dict[str, list[int]] = {}
        for r in rows:
            strata.setdefault(str(r["measurand"]), []).append(r["id"])
        draw = _accept.stratified_sample(
            strata,
            _sample_n(params),
            seed=run_token,
        )
        state = {
            **state,
            "sampled": [mid for ids in draw.values() for mid in ids],
            "verdicts": {},
            "idx": 0,
            "total_rows": len(rows),
            "run_token": run_token,
        }
    sampled = [int(m) for m in state.get("sampled") or []]
    verdicts = dict(state.get("verdicts") or {})
    idx = int(state.get("idx") or 0)
    by_id = {r["id"]: r for r in rows}
    per_slice = _reviews_per_slice(params)
    tier = tier_from_str(params.get("tier") or "frontier", default=Tier.FRONTIER)
    model_pin = params.get("model") if isinstance(params.get("model"), str) else None

    processed = 0
    while idx < len(sampled) and processed < per_slice:
        if ctx.is_cancel_requested():
            return Done(summary="measure_accept cancelled", success=False)
        mid = sampled[idx]
        idx += 1
        measure = by_id.get(mid)
        if measure is None:
            verdicts[str(mid)] = {"verdict": "undecidable", "reason": "row gone"}
            continue
        span = _span_text(ctx.store, measure)
        if span is None:
            verdicts[str(mid)] = {
                "verdict": "undecidable",
                "reason": "anchor chunk missing",
            }
            continue
        result = route(
            LlmRequest(
                tier=tier,
                prompt=_prompt.review_prompt(
                    literal=str(measure["literal"] or ""),
                    reported_unit=measure["reported_unit"],
                    measurand=str(measure["measurand"]),
                    subject_label=measure["subject"],
                    span_text=span,
                    conditions=_row_conditions(
                        ctx.store, str(measure["run_key"] or "")
                    ),
                ),
                source="measure_accept",
                ref_id=paper,
                model=model_pin,
                thinking=False,
                temperature=0.0,
                max_tokens=256,
                log_blobs=False,
            )
        )
        if getattr(result, "paused", False):
            # Yield, not defer_on_pause: the Yield persists the verdicts
            # already earned this run, a park would lose the slice's work.
            return Yield(
                state={**state, "idx": idx - 1, "verdicts": verdicts},
                wake_when=_retry_wake(result),
            )
        if getattr(result, "error", None):
            verdicts[str(mid)] = {
                "verdict": "undecidable",
                "reason": f"llm_error: {str(result.error)[:120]}",
            }
            continue
        verdict, reason = _prompt.parse_verdict(result.text)
        model_used = getattr(result, "model", None) or model_pin or resolve_model(tier)
        try:
            ctx.store.record_target_review(
                "measure",
                mid,
                actor="measure-accept",
                verdict=_VERDICT_TO_REVIEW[verdict],
                model=model_used,
                version="1",
                note=reason[:500] if reason else None,
            )
        except Exception as exc:  # row vanished between fetch and review
            log.info("measure_accept: review of mx%s failed: %s", mid, exc)
            verdict = "undecidable"
            reason = f"review write failed: {exc}"
        verdicts[str(mid)] = {"verdict": verdict, "reason": reason}
        processed += 1

    if idx < len(sampled):
        return Yield(
            state={**state, "idx": idx, "verdicts": verdicts},
            wake_when=_wake_s(1),
        )

    errors = sum(1 for v in verdicts.values() if v.get("verdict") != "correct")
    z = params.get("z") if isinstance(params.get("z"), (int, float)) else None
    bar = params.get("bar") if isinstance(params.get("bar"), (int, float)) else None
    acc = _accept.acceptance(
        errors,
        len(sampled),
        z=float(z) if z else _accept.DEFAULT_Z,
        bar=float(bar) if bar else _accept.DEFAULT_BAR,
    )
    acc_meta = {
        "accepted": acc.accepted,
        "errors": acc.errors,
        "sampled": acc.sampled,
        "error_rate": acc.error_rate,
        "wilson_lo": acc.wilson_lo,
        "wilson_hi": acc.wilson_hi,
        "z": acc.z,
        "bar": acc.bar,
        "run_token": run_token,
        "total_rows": int(state.get("total_rows") or len(rows)),
    }
    ctx.set_meta(acceptance=acc_meta)
    verdict = "ACCEPTED" if acc.accepted else "FAILED"
    bad = [
        (mid, verdicts[str(mid)])
        for mid in sampled
        if verdicts.get(str(mid), {}).get("verdict") != "correct"
    ]
    lines = [
        f"measure_accept: batch {verdict} for run {run_token} on pa{paper}: "
        f"{acc.errors} error(s) in {acc.sampled} sampled "
        f"(Wilson upper {acc.wilson_hi:.3f} vs bar {acc.bar})",
    ]
    for mid, v in bad[:50]:
        lines.append(f"  mx{mid}: {v.get('verdict')} — {v.get('reason') or ''}")
    ctx.append_chunk("job_summary", "\n".join(lines))
    return Done(
        summary="\n".join(lines),
        success=True,
        summary_meta={"acceptance": acc_meta},
    )


def _sample_n(params: dict[str, Any]) -> int:
    raw = params.get("sample_n")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return _accept.DEFAULT_SAMPLE


ACCEPT_SPEC = JobTypeSpec(
    name="measure_accept",
    params_schema=ACCEPT_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=ACCEPT_DESCRIPTION,
    dispatch=_accept_dispatch,
)
