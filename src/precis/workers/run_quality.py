"""Sampled quality review of lane-router runs — the expensive layer.

``run_slice.poll`` writes the cheap half on every run (``meta.outcome``,
``plans.outcome.build_outcome``) and marks the runs the sampling policy
selects (``outcome.review.sampled`` + reasons). This pass is the
expensive half (gr595545): dark until ``service_config`` lights it, it
mints one big-model judge plan per sampled run — same two-node shape
as ``transcript_audit.mint_audit``, the lane pinned AND the model
pinned to the router's FRONTIER id (:func:`_judge_params`, review
td655142) — and lands the judge's
``verdict: pass|fail`` on the judged job's ``meta.verdict``, the
eval-run-spine's second column.

The judge todo carries ``meta.judges = {job, todo}`` so the verdict
collector needs no link walking; the root↔judged ``related-to`` link
is the durable idempotency marker, same as the transcript audit's.
How often the gates lied (gate pass, review fail) rolls up per task
category through ``plans.outcome.gate_lied_rate``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.corpus_layout import host_name
from precis.plans import outcome as _outcome
from precis.plans.nodes import finish_mint
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.runner import BatchResult
from precis.workers.service_config import ALL_HOSTS, ServiceConfigResolver

log = logging.getLogger(__name__)
SERVICE_NAME = "run_quality"
_MAX_PER_TICK = 1

#: Lane the judge task is pinned to while it is live — the same
#: subscription write-MCP lane the transcript audit rides (it must read
#: the run's records and file its verdict through the precis MCP). A
#: dead pin falls back to ``pick_lane`` at mint
#: (:func:`precis.workers.factory_rescue._resolve_lane_pin`, gr604579);
#: ``params.lane_capability: "mcp"`` holds the contract when it does
#: (td610544).
JUDGE_LANE = "claude-sub"

_VERDICT_RE = re.compile(r"verdict:\s*(pass|fail)\b\s*[—\-:]?\s*(.*)", re.IGNORECASE)


def _judge_model() -> str:
    """The judge's model id — the llm router's FRONTIER pin,
    env-overridable like every cloud rung. Selected EXPLICITLY
    (review td655142): a lane's ``default_model`` is a mid-tier pick,
    and the sampled review is the expensive layer — a small-model
    judge silently degrades the calibration it exists for."""
    from precis.utils.llm.router import Tier, resolve_model

    return resolve_model(Tier.FRONTIER)


def _judge_params(store: Store) -> tuple[dict[str, str], str]:
    """The judge task's op params: ``_resolve_lane_pin``'s
    pin-or-fallback for the lane, PLUS ``params.model`` — the big
    model the review is for (review td655142: a lane pin alone ran
    the lane default). The pin is validated against
    ``Lane.accepts_model``: a pinned lane that cannot run the judge
    model falls back to a ``pick_lane`` over the MCP lanes that can,
    and a fleet with none still mints bound — submit's model-pin gate
    then refuses the launch, a visible config fault, never a silent
    lane-default judge."""
    from precis.plans import lanes as _lanes
    from precis.workers.factory_rescue import _resolve_lane_pin
    from precis.workers.job_types._slice_adapters import ADAPTERS

    judge = _judge_model()
    params, note = _resolve_lane_pin(store, JUDGE_LANE, "review")
    table = _lanes.load_lanes(store)
    pinned = next((ln for ln in table if ln.name == params.get("lane")), None)
    if pinned is not None and pinned.accepts_model(judge):
        return {**params, "model": judge}, note
    fit = tuple(
        ln
        for ln in table
        if (a := ADAPTERS.get(ln.adapter)) is not None
        and a.mcp
        and ln.accepts_model(judge)
    )
    picked = _lanes.pick_lane(store, "review", lanes=fit)
    where = params.get("lane")
    if picked is not None:
        why = (
            f"lane {where!r} runs no {judge!r}; the judge routes to {picked.name!r}"
            if where
            else f"the judge routes to {picked.name!r} (accepts {judge!r})"
        )
        return {
            "lane": picked.name,
            "lane_capability": "mcp",
            "model": judge,
        }, (f"{note}; {why}" if note else why)
    # Nothing fit launches the pinned model: mint bound on the
    # capability + the model pin alone — submit's accepts_model gate
    # refuses the launch, which is the visible fault a wrong-vendor
    # lane default would have hidden.
    params = {k: v for k, v in params.items() if k != "lane"}
    why = (
        f"no enabled MCP lane accepts judge model {judge!r}; minted "
        "bound — submit refuses the launch until one does"
    )
    return {**params, "model": judge}, (f"{note}; {why}" if note else why)


def run_quality_enabled(
    store: Store, *, conn: Any = None, host: str | None = None
) -> bool:
    """Exact-host configuration wins; absent, zero, or failed lookup is dark."""
    h = host or host_name()
    try:
        if conn is None:
            return ServiceConfigResolver(store, h).enabled(
                SERVICE_NAME, default_on=False
            )
        row = conn.execute(
            "SELECT prio FROM service_config "
            "WHERE service = %s AND host IN (%s, %s) "
            "ORDER BY (host = %s) DESC LIMIT 1",
            (SERVICE_NAME, h, ALL_HOSTS, h),
        ).fetchone()
        return bool(row) and int(row[0]) > 0
    except Exception:
        log.warning("run_quality: service_config lookup failed", exc_info=True)
        return False


def candidates(conn: Any, limit: int) -> list[dict[str, Any]]:
    """Sampled run_slice jobs whose review is not yet minted, oldest
    first. A judge task's own run carries ``meta.judges`` on its todo —
    excluded, or judges would judge judges forever."""
    rows = conn.execute(
        """
        SELECT r.ref_id, r.meta
          FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = 'run_slice'
           AND (r.meta->'outcome'->'review'->>'sampled')::boolean
           AND NOT COALESCE(
                   (r.meta->'outcome'->'review'->>'minted')::boolean, false)
           AND NOT EXISTS (
                 SELECT 1 FROM refs j
                  WHERE j.kind = 'todo' AND j.retired_at IS NULL
                    AND (j.meta->'judges'->>'job')::bigint = r.ref_id)
           AND NOT EXISTS (
                 SELECT 1 FROM refs t
                  WHERE t.ref_id = (r.meta->'params'->>'todo_id')::bigint
                    AND t.meta ? 'judges')
         ORDER BY r.ref_id
         LIMIT %s
        """,
        (int(limit),),
    ).fetchall()
    return [{"id": int(r[0]), "meta": r[1] or {}} for r in rows]


def render_brief(job: dict[str, Any], todo: Any) -> str:
    """The judge's brief: the run's mechanical record inlined (a lane
    may have no MCP); the rest fetched through it."""
    meta = job["meta"]
    outcome = meta.get("outcome") or {}
    review = outcome.get("review") or {}
    reasons = ", ".join(review.get("reasons") or ["sampled"])
    title = getattr(todo, "title", "") if todo is not None else ""
    return (
        "## Brief\n"
        f"Judge run jo{job['id']} — a lane-routed `run_slice` session the "
        f"sampling policy flagged ({reasons}). Its todo is "
        f"td{int((meta.get('params') or {}).get('todo_id') or 0)}: {title}.\n\n"
        "The mechanical record (already computed, do not recompute): "
        f"outcome `{outcome.get('outcome', '?')}`, gate_pass "
        f"{outcome.get('gate_pass')}, gates {outcome.get('gates')}, "
        f"lane {meta.get('lane') or '?'}, model {meta.get('model') or '?'}, "
        f"cost ${outcome.get('cost_usd')}, wall {outcome.get('wall_s')}s.\n\n"
        "Judge the WORK, not the mechanics: did the session do good work "
        "for what it was asked — the diff against the brief, the handoff's "
        "claims against the record, tests where they were owed. Read "
        "get(kind='job', id=<id>, view='events') and the todo's comments "
        "for the record; read the landed diff when there is one.\n\n"
        "Verdict: make `verdict: pass — <why>` or `verdict: fail — <why>` "
        "the FIRST LINE of your final message (it lands as the handoff "
        "comment the collector reads). Fail a run whose gates passed but "
        "whose work did not — the gates-lying rate is the calibration "
        "this review exists for. Commit nothing. End the session after "
        "the verdict."
    )


def mint_review(store: Store, job: dict[str, Any]) -> int:
    """Create the two-node judge plan, stamp ``outcome.review.minted``
    and the idempotency link atomically."""
    meta = job["meta"]
    todo_id = int((meta.get("params") or {}).get("todo_id") or 0)
    judged = store.get_ref(kind="todo", id=todo_id) if todo_id else None
    lane_params, lane_note = _judge_params(store)
    with store.tx() as conn:
        root = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Judge run jo{job['id']} quality (td{todo_id})",
            parent_id=None,
            meta={"auto_close": True},
            conn=conn,
        )
        task = store.insert_ref(
            kind="todo",
            slug=None,
            title="Judge the flagged run and post a verdict",
            parent_id=root.id,
            meta={
                "op": {"type": "run_slice", "params": lane_params},
                "task_class": "review",
                "judges": {"job": int(job["id"]), "todo": todo_id},
            },
            conn=conn,
        )
        for ref, tags, body in (
            (
                root,
                ["thread:factory", "audit:run-quality"],
                f"Judge the sampled run jo{job['id']} (td{todo_id}).",
            ),
            (task, [], render_brief(job, judged)),
        ):
            conn.execute(
                "UPDATE refs SET meta = meta - 'llm_tier' WHERE ref_id = %s",
                (ref.id,),
            )
            for tag in ["STATUS:open", f"plan:{root.id}", *tags]:
                store.add_tag(
                    ref.id,
                    Tag.parse_strict(tag, kind="todo"),
                    set_by="agent",
                    replace_prefix=tag.startswith("STATUS:"),
                    conn=conn,
                )
            store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "todo_body"})],
                conn=conn,
            )
        if lane_note:
            store.chunks.insert_chunks(
                task.id,
                [
                    ChunkInsert(
                        ord=1,
                        text=f"lane: {lane_note}",
                        meta={"chunk_kind": "todo_comment"},
                    )
                ],
                conn=conn,
            )
        if judged is not None:
            store.add_link(
                src_ref_id=root.id,
                dst_ref_id=int(judged.id),
                relation="related-to",
                conn=conn,
            )
        conn.execute(
            """
            UPDATE refs SET meta = jsonb_set(
                meta, '{outcome,review,minted}', 'true'::jsonb)
             WHERE ref_id = %s
            """,
            (int(job["id"]),),
        )
        for minted in (root.id, task.id):
            finish_mint(store, minted, conn=conn)
    return int(root.id)


def _verdict_comments(store: Store, todo_id: int) -> tuple[str, str] | None:
    """The newest ``verdict: pass|fail — <why>`` a judge session left on
    its task todo (the handoff line lands there), else ``None``."""
    try:
        chunks = sorted(
            (
                c
                for c in store.chunks.list_chunks_for_ref(int(todo_id))
                if c.chunk_kind == "todo_comment" and c.text
            ),
            key=lambda c: (int(c.ord), int(c.id)),
            reverse=True,
        )
    except Exception:
        return None
    for chunk in chunks:
        for line in str(chunk.text or "").splitlines():
            m = _VERDICT_RE.search(line)
            if m:
                return m.group(1).lower(), m.group(2).strip()[:300]
    return None


def collect_verdicts(store: Store) -> int:
    """Land judge verdicts on the judged jobs: for every judge todo
    whose ``meta.judges.job`` still lacks ``meta.verdict``, read the
    newest ``verdict:`` comment on the judge todo and write it through
    :func:`plans.outcome.record_verdict`. Returns the count landed."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT t.ref_id,
                   (t.meta->'judges'->>'job')::bigint AS job_id
              FROM refs t
             WHERE t.kind = 'todo' AND t.retired_at IS NULL
               AND t.meta ? 'judges'
               AND NOT EXISTS (
                     SELECT 1 FROM refs j
                      WHERE j.ref_id = (t.meta->'judges'->>'job')::bigint
                        AND j.meta ? 'verdict')
            """
        ).fetchall()
    landed = 0
    for task_id, job_id in rows:
        found = _verdict_comments(store, int(task_id))
        if found is None:
            continue
        verdict, note = found
        try:
            _outcome.record_verdict(
                store,
                int(job_id),
                verdict,
                by=f"run-quality td{int(task_id)}",
                note=note,
            )
            landed += 1
        except Exception:
            log.warning(
                "run_quality: verdict write for jo%d failed",
                int(job_id),
                exc_info=True,
            )
    return landed


def run_run_quality_pass(store: Store, *, host: str | None = None) -> BatchResult:
    """Mint at most one judge and collect verdicts; never call a model."""
    if not run_quality_enabled(store, host=host):
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    with store.pool.connection() as conn:
        picks = candidates(conn, _MAX_PER_TICK)
        lied = _outcome.gate_lied_rate(conn)
    minted: list[dict[str, int]] = []
    failed = 0
    for job in picks:
        try:
            root = mint_review(store, job)
            minted.append({"job": int(job["id"]), "root": root})
        except Exception:
            failed += 1
            log.warning(
                "run_quality: mint for jo%d failed", int(job["id"]), exc_info=True
            )
    verdicts = collect_verdicts(store)
    return BatchResult(
        handler=SERVICE_NAME,
        claimed=len(picks),
        ok=len(minted),
        failed=failed,
        extra={"minted": minted, "verdicts": verdicts, "gate_lies": lied},
    )
