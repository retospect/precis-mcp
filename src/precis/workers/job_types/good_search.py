"""``good_search`` — agentic broad-retrieval as a coordinator campaign.

Thin slice of ``docs/design/good-search-coordinator.md`` (Phasing step
1): fuse → fan out triage children → heartbeat gather → ``Done`` with a
merged verdict. No verify rung, no ``kind='citation'`` writes, no
query/HyDE self-expansion — those are phase 2.

Two job_types live here:

- ``good_search`` — the coordinator campaign (``SPEC``). Its
  ``dispatch`` is a phase machine keyed on
  ``ctx.meta['coordinator_state']['phase']``:

  * **plan** (first slice): run Tier-1 fusion via
    ``ctx.store.search_blocks_multi`` over ``q`` + caller-supplied
    ``queries``/``answers`` (lexical legs only — the worker slice has
    no embedder handle; see the module note below), build a candidate
    pool, partition into triage batches, ``ctx.spawn_child`` one
    ``good_search_triage`` child per batch, and ``Yield`` on an
    ``at_time`` heartbeat (§Liveness: a bare ``children_done`` parks
    forever on a child stuck at ``STATUS:queued``).
  * **triage** (heartbeat wakes): check the listed children's terminal
    status; all terminal → gather; past the deadline / slice cap →
    force a best-effort gather (``timed_out=True``, non-terminal
    children counted as dropped); else re-yield another heartbeat.
  * **gather**: read each succeeded child's ``job_result`` verdicts,
    merge + rank kept candidates (child relevance × fusion-rank
    signal), tolerate failed children, and ``Done`` with the result
    envelope (§Result envelope, minus citations).

- ``good_search_triage`` — the fan-out child (``TRIAGE_SPEC``), run by
  ``claude_inproc`` via its plugin ``dispatch`` (the executor calls
  ``spec.dispatch(ctx, spec)`` for registered plugin job_types; the
  bare ``run()`` path is reserved for the in-tree built-ins). It
  builds a compact judging prompt over its batch, calls the one-shot
  JSON judge (:func:`precis.utils.claude_p.call_claude_p`, stub-binary
  testable via ``PRECIS_CLAUDE_BIN``), validates the verdict list
  (clamp relevance, drop unknown handles), and writes it as the job's
  ``job_result`` chunk. Malformed output → one retry → fail the child
  (the coordinator tolerates it: a dead batch just drops its
  candidates).

**Embedding note (thin-slice simplification).** The handler's broad
path embeds ``q``/``queries``/``answers`` via a degrade-safe batch
call; the coordinator pass only receives a ``store`` (no embedder
handle), so the plan phase runs **lexical legs only** —
``search_blocks_multi(mode='lexical')`` with ``answers`` folded in as
extra lexical legs. The RRF fusion across phrasings still applies;
semantic legs ride in when the executor grows an embedder seam.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any

from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

# ── knobs (env-overridable; defaults per the design doc) ───────────

#: Terminal STATUS values (mirrors ``executors._common.TERMINAL``;
#: re-declared to keep this module import-light for the MCP path).
_TERMINAL = ("succeeded", "failed", "cancelled")

#: RRF constant for the gather-side rank signal (matches the store's
#: fusion ``k``).
_RRF_K = 60

#: Handler-equivalent broad-retrieval leg caps (``_BROAD_LEG_CAP``).
_LEG_CAP = 8

#: Per-candidate text cap in the triage child's prompt.
_CANDIDATE_TEXT_CAP = 700


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 10_000) -> int:
    try:
        n = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, n))


def _heartbeat_s() -> int:
    """Seconds between liveness wakes while children run (default 180)."""
    return _env_int("PRECIS_GOOD_SEARCH_HEARTBEAT_S", 180, lo=1, hi=3600)


def _deadline_s() -> int:
    """Campaign wall-clock budget (default 20 min)."""
    return _env_int("PRECIS_GOOD_SEARCH_DEADLINE_S", 1200, lo=10, hi=86_400)


def _max_slices() -> int:
    """Slice-count cap — the second liveness guard (default 30)."""
    return _env_int("PRECIS_GOOD_SEARCH_MAX_SLICES", 30, lo=2, hi=1000)


def _pool_size() -> int:
    """Fusion candidate-pool size (default 100)."""
    return _env_int("PRECIS_GOOD_SEARCH_POOL", 100, lo=1, hi=1000)


def _triage_batch() -> int:
    """Candidates per triage child (default 30)."""
    return _env_int("PRECIS_GOOD_SEARCH_TRIAGE_BATCH", 30, lo=1, hi=200)


def _default_max_children() -> int:
    """Fan-out ceiling when ``params.max_children`` is unset (thin
    slice default 4 — melchior's agent worker is the one queue)."""
    return _env_int("PRECIS_GOOD_SEARCH_MAX_CHILDREN", 4, lo=1, hi=64)


def _per_paper_cap() -> int:
    """Breadth knob on the fusion pool (default 3 chunks per paper)."""
    return _env_int("PRECIS_GOOD_SEARCH_PER_PAPER", 3, lo=1, hi=50)


# ── good_search (coordinator campaign) ─────────────────────────────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {"type": "string"},
        "queries": {"type": ["array", "null"]},
        "answers": {"type": ["array", "null"]},
        "context": {"type": ["string", "null"]},
        "max_children": {"type": ["integer", "null"]},
        # Stored for phase 2's budget enforcement; the thin slice
        # records it in the envelope note but doesn't enforce it.
        "budget_usd": {"type": ["number", "null"]},
        "want": {"enum": ["citations", "chunks", "papers"]},
        # Triage-child model (cheap default via claude_p's env knobs).
        "model": {"type": ["string", "null"]},
    },
    "required": ["q"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Deep paper search campaign: Tier-1 fusion → batched LLM triage "
    "children → merged ranked verdict (async; poll the job)."
)


def _clean_str_list(raw: Any, *, cap: int = _LEG_CAP) -> list[str]:
    """Coerce a params list to at most ``cap`` non-empty strings."""
    if not isinstance(raw, list):
        return []
    out = [str(s).strip() for s in raw if s and str(s).strip()]
    return out[:cap]


def _child_states(store: Any, child_ids: list[int]) -> dict[int, str]:
    """Current ``STATUS:`` value per live child job.

    Missing / soft-deleted rows are simply absent from the returned
    dict — the caller treats absence as terminal, mirroring
    ``wake_runner._wake_children_done`` (an operator soft-deleting a
    stuck child unblocks the campaign instead of parking it forever).
    """
    if not child_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id
                         AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'queued'
                   )
              FROM refs r
             WHERE r.ref_id = ANY(%s)
               AND r.kind = 'job'
               AND r.deleted_at IS NULL
            """,
            (list(child_ids),),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def _dispatch(ctx: Any, spec: Any) -> Any:
    """Coordinator phase machine. Returns ``Done`` | ``Yield``."""
    state = (ctx.meta or {}).get("coordinator_state") or {}
    phase = state.get("phase") or "plan"
    if phase == "plan":
        return _phase_plan(ctx)
    if phase == "triage":
        return _phase_triage(ctx, state)
    return Done(
        summary=f"good_search: unknown phase {phase!r} in coordinator_state",
        success=False,
    )


def _phase_plan(ctx: Any) -> Any:
    """First slice: fuse, pool, fan out triage children, yield."""
    params = (ctx.meta or {}).get("params") or {}
    q = str(params.get("q") or "").strip()
    if not q:
        return Done(summary="good_search: params.q is required", success=False)
    queries = _clean_str_list(params.get("queries"))
    answers = _clean_str_list(params.get("answers"))
    context = str(params.get("context") or "").strip()
    want = params.get("want") or "chunks"
    if want not in ("citations", "chunks", "papers"):
        want = "chunks"
    model = params.get("model")

    # Tier-1 fusion. Lexical legs only in the thin slice (no embedder
    # handle on the worker pass — see the module docstring); the HyDE
    # ``answers`` still contribute as extra lexical legs. Leg count is
    # bounded at 1 + 8 + 8 = 17, under the store's hard cap of 32.
    hits = ctx.store.search_blocks_multi(
        q_texts=[q, *queries, *answers],
        query_vecs=[],
        mode="lexical",
        kind="paper",
        limit=_pool_size(),
        per_paper=_per_paper_cap(),
        card_kinds=("card_combined",),
    )

    if not hits:
        note = "no candidates; broaden q/queries"
        return Done(
            summary=(
                f"deep search found no candidates for {q!r} — "
                "broaden q or supply more queries=/answers= phrasings."
            ),
            success=True,
            summary_meta={
                "result": {
                    "want": want,
                    "chunks": [],
                    "considered": 0,
                    "kept": 0,
                    "children": 0,
                    "children_failed": 0,
                    "timed_out": False,
                    "partial": False,
                    "note": note,
                }
            },
        )

    # Candidate pool, rank-ordered (fusion order). ``pool`` maps the
    # chunk handle onto its fusion rank + paper for the gather phase.
    candidates: list[dict[str, Any]] = []
    pool: dict[str, dict[str, Any]] = {}
    for rank, (block, ref, _score) in enumerate(hits):
        paper = ref.slug or str(ref.id)
        handle = f"{paper}~{block.pos}"
        if handle in pool:  # defensive: fusion output is unique per chunk
            continue
        pool[handle] = {"rank": rank, "paper": paper}
        candidates.append(
            {
                "handle": handle,
                "text": (block.text or "")[:_CANDIDATE_TEXT_CAP],
                "paper": paper,
            }
        )

    # Partition into triage batches, capped by max_children (the
    # lowest-ranked tail past the cap is dropped — it still counts in
    # ``considered`` so the envelope is honest about the truncation).
    batch_size = _triage_batch()
    raw_cap = params.get("max_children")
    max_children = (
        int(raw_cap)
        if isinstance(raw_cap, int) and not isinstance(raw_cap, bool) and raw_cap >= 1
        else _default_max_children()
    )
    n_children = max(1, min(math.ceil(len(candidates) / batch_size), max_children))
    child_ids: list[int] = []
    for i in range(n_children):
        batch = candidates[i * batch_size : (i + 1) * batch_size]
        if not batch:
            break
        child_ids.append(
            ctx.spawn_child(
                "good_search_triage",
                params={
                    "q": q,
                    "context": context,
                    "want": want,
                    "candidates": batch,
                },
                model=model,
                idem_key=f"good_search:{ctx.ref_id}:triage:{i}",
            )
        )

    now = time.time()
    ctx.append_chunk(
        "job_event",
        f"plan: fused {len(hits)} candidates over "
        f"{1 + len(queries) + len(answers)} lexical legs → "
        f"{len(child_ids)} triage child(ren) {child_ids}",
    )
    return Yield(
        state={
            "phase": "triage",
            "child_job_ids": child_ids,
            "started_ts": now,
            "deadline_ts": now + _deadline_s(),
            "slice_count": 1,
            "pool": pool,
            "considered": len(pool),
            "want": want,
        },
        wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
    )


def _phase_triage(ctx: Any, state: dict[str, Any]) -> Any:
    """Heartbeat wake: advance, re-yield, or force-complete."""
    slice_count = int(state.get("slice_count") or 0) + 1
    state = {**state, "slice_count": slice_count}

    if ctx.is_cancel_requested():
        return Done(
            summary="deep search cancelled by request",
            success=False,
            summary_meta={"cancelled": True},
        )

    child_ids = [int(c) for c in (state.get("child_job_ids") or [])]
    statuses = _child_states(ctx.store, child_ids)
    # Absent (hard/soft-deleted) counts terminal — wake_runner semantics.
    pending = [
        cid for cid in child_ids if cid in statuses and statuses[cid] not in _TERMINAL
    ]

    if not pending:
        return _gather(ctx, state, statuses, timed_out=False, dropped=[])

    now = time.time()
    timed_out = now >= float(state.get("deadline_ts") or 0)
    if timed_out or slice_count >= _max_slices():
        return _gather(ctx, state, statuses, timed_out=True, dropped=pending)

    return Yield(
        state=state,
        wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
    )


def _read_child_verdicts(store: Any, child_id: int) -> list[dict[str, Any]] | None:
    """Parse the child's latest ``job_result`` chunk into a verdict list.

    Returns ``None`` when the chunk is missing or malformed (the
    caller counts the child as failed).
    """
    try:
        blocks = store.list_blocks_for_ref(child_id)
    except Exception:  # pragma: no cover — defensive
        log.warning("good_search: reading child %d blocks failed", child_id)
        return None
    result_blocks = [b for b in blocks if b.chunk_kind == "job_result"]
    if not result_blocks:
        return None
    try:
        data = json.loads(result_blocks[-1].text)
    except (TypeError, json.JSONDecodeError):
        return None
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    return verdicts if isinstance(verdicts, list) else None


def _gather(
    ctx: Any,
    state: dict[str, Any],
    statuses: dict[int, str],
    *,
    timed_out: bool,
    dropped: list[int],
) -> Done:
    """Merge child verdicts into the ranked result envelope → ``Done``."""
    child_ids = [int(c) for c in (state.get("child_job_ids") or [])]
    pool: dict[str, dict[str, Any]] = state.get("pool") or {}
    want = state.get("want") or "chunks"
    considered = int(state.get("considered") or len(pool))

    children_failed = 0
    merged: dict[str, dict[str, Any]] = {}
    for cid in child_ids:
        if cid in dropped or statuses.get(cid) != "succeeded":
            children_failed += 1
            continue
        verdicts = _read_child_verdicts(ctx.store, cid)
        if verdicts is None:
            children_failed += 1
            continue
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            handle = str(v.get("candidate_handle") or "")
            info = pool.get(handle)
            if info is None:  # unknown handle — drop
                continue
            if not v.get("keep"):
                continue
            try:
                relevance = float(v.get("relevance", 0.0))
            except (TypeError, ValueError):
                relevance = 0.0
            relevance = max(0.0, min(1.0, relevance))
            # Cross-signal fusion: child relevance × the RRF-shaped
            # rank signal from the plan-phase fusion ordering.
            score = relevance * (1.0 / (_RRF_K + int(info["rank"]) + 1))
            prior = merged.get(handle)
            if prior is not None and prior["_score"] >= score:
                continue
            merged[handle] = {
                "handle": handle,
                "paper": info["paper"],
                "relevance": round(relevance, 3),
                "why": str(v.get("why") or "")[:300],
                "best_quote": (str(v.get("best_quote"))[:500])
                if v.get("best_quote")
                else None,
                "_score": score,
            }

    kept = sorted(merged.values(), key=lambda d: -d["_score"])
    for d in kept:
        d.pop("_score", None)

    wall = round(time.time() - float(state.get("started_ts") or time.time()), 1)
    all_failed = bool(child_ids) and children_failed == len(child_ids)

    notes: list[str] = []
    if timed_out:
        notes.append(
            f"timed out with {len(dropped)} child(ren) still pending "
            "(counted as dropped batches)"
        )
    if children_failed and not all_failed:
        notes.append(
            f"{children_failed} triage child(ren) failed; their candidates were dropped"
        )
    if all_failed:
        notes.append("all triage children failed — no verdicts to merge")

    result = {
        "want": want,
        "chunks": kept,
        "considered": considered,
        "kept": len(kept),
        "children": len(child_ids),
        "children_failed": children_failed,
        "timed_out": timed_out,
        "partial": timed_out or children_failed > 0,
        "note": "; ".join(notes) or None,
        "wall_seconds": wall,
    }
    if want == "papers":
        seen: list[str] = []
        for d in kept:
            if d["paper"] not in seen:
                seen.append(d["paper"])
        result["papers"] = seen

    if all_failed:
        return Done(
            summary=(
                f"deep search failed: all {len(child_ids)} triage children "
                "failed — no verdicts to merge. "
                f"child statuses: { {c: statuses.get(c, 'gone') for c in child_ids} }"
            ),
            success=False,
            summary_meta={"result": result, "timed_out": timed_out},
        )

    lines = [
        f"deep search: kept {len(kept)} of {considered} candidates "
        f"({len(child_ids)} triage child(ren), {children_failed} failed"
        f"{', timed out' if timed_out else ''})."
    ]
    for i, d in enumerate(kept[:50], start=1):
        quote = f' — "{d["best_quote"]}"' if d.get("best_quote") else ""
        lines.append(f"{i}. {d['handle']} (rel {d['relevance']}) — {d['why']}{quote}")
    if len(kept) > 50:
        lines.append(f"… and {len(kept) - 50} more in meta.result.chunks")
    if notes:
        lines.append("note: " + "; ".join(notes))

    return Done(
        summary="\n".join(lines),
        success=True,
        summary_meta={"result": result, "timed_out": timed_out},
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("good_search runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="good_search",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


# ── good_search_triage (claude_inproc fan-out child) ───────────────

TRIAGE_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {"type": "string"},
        "context": {"type": ["string", "null"]},
        "want": {"type": ["string", "null"]},
        # [{handle, text, paper}, ...] — validated loosely (the
        # hand-rolled validator has no array-item support).
        "candidates": {"type": "array"},
        "model": {"type": ["string", "null"]},
    },
    "required": ["q", "candidates"],
    "additionalProperties": True,
}

TRIAGE_COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
TRIAGE_DESCRIPTION = (
    "Batched relevance triage for a good_search campaign: judge a "
    "batch of candidate chunks against q via the one-shot JSON judge."
)


def _triage_prompt(
    q: str, context: str, want: str, candidates: list[dict[str, Any]]
) -> str:
    """Compact judging prompt over the batch (numbered candidates)."""
    lines = [
        "You are triaging literature-search candidates for relevance.",
        "",
        f"Question: {q}",
    ]
    if context:
        lines.append(f"Context (what a good source looks like): {context}")
    lines.extend(
        [
            f"Goal: pick the candidates worth keeping as {want}.",
            "",
            "Candidates (each is one chunk of a paper):",
        ]
    )
    for i, c in enumerate(candidates, start=1):
        handle = str(c.get("handle") or "")
        paper = str(c.get("paper") or "")
        text = str(c.get("text") or "")[:_CANDIDATE_TEXT_CAP]
        lines.append(f"\n[{i}] handle={handle} paper={paper}\n{text}")
    lines.extend(
        [
            "",
            "For EVERY candidate return one verdict object. Echo the",
            "handle exactly as given. relevance is 0..1 (how directly the",
            "chunk answers the question). keep=true only when the chunk",
            "materially helps answer it. best_quote is an optional short",
            "verbatim quote from the chunk text.",
            "",
            "Respond with ONLY this JSON shape:",
            '{"verdicts": [{"candidate_handle": "<handle>", "keep": true,',
            ' "relevance": 0.8, "why": "<one sentence>",',
            ' "best_quote": "<verbatim or omit>"}, ...]}',
        ]
    )
    return "\n".join(lines)


def _validate_verdicts(
    data: dict[str, Any], known_handles: set[str]
) -> list[dict[str, Any]] | None:
    """Normalise the judge's verdict list; ``None`` when malformed.

    Clamps relevance to [0, 1], coerces ``keep`` to bool, and drops
    verdicts whose handle isn't in the batch (the model hallucinated
    or mangled it). An empty *valid* list is fine — "keep nothing" is
    a legitimate verdict; only a missing/broken ``verdicts`` key is
    malformed.
    """
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        handle = str(v.get("candidate_handle") or "")
        if handle not in known_handles:
            continue
        try:
            relevance = float(v.get("relevance", 0.0))
        except (TypeError, ValueError):
            relevance = 0.0
        out.append(
            {
                "candidate_handle": handle,
                "keep": bool(v.get("keep")),
                "relevance": max(0.0, min(1.0, relevance)),
                "why": str(v.get("why") or ""),
                "best_quote": str(v.get("best_quote")) if v.get("best_quote") else None,
            }
        )
    return out


def _triage_dispatch(ctx: Any, spec: Any) -> None:
    """Judge one batch; write the verdicts as the ``job_result`` chunk.

    Malformed JSON from the model → one retry → fail the child. The
    executor (``_finalize_plugin_dispatch``) drives the happy path to
    ``STATUS:succeeded``.
    """
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    params = (ctx.meta or {}).get("params") or {}
    q = str(params.get("q") or "").strip()
    candidates = params.get("candidates")
    if not q or not isinstance(candidates, list) or not candidates:
        ctx.record_failure(
            "good_search_triage: params.q and a non-empty "
            "params.candidates are required"
        )
        return
    context = str(params.get("context") or "").strip()
    want = str(params.get("want") or "chunks")
    model = params.get("model")

    prompt = _triage_prompt(q, context, want, candidates)
    known_handles = {str(c.get("handle") or "") for c in candidates if c.get("handle")}

    verdicts: list[dict[str, Any]] | None = None
    last_err = "no parseable verdicts"
    for attempt in (1, 2):
        # Routed through the LLM seam (ADR 0046 unit 4b): CLOUD_SMALL judge, so
        # PRECIS_LLM_BACKEND can switch it. Errors fold into res.error.
        res = dispatch(
            LlmRequest(
                tier=Tier.CLOUD_SMALL,
                prompt=prompt,
                model=model,
                source="good_search:triage",
                ref_id=ctx.ref_id,  # attribute spend to the search ref (gr162130)
            )
        )
        if res.error:
            last_err = res.error
            ctx.append_chunk(
                "job_event",
                f"triage attempt {attempt}: judge call failed: {res.error}",
            )
            continue
        verdicts = _validate_verdicts(res.data or {}, known_handles)
        if verdicts is not None:
            break
        last_err = f"malformed verdicts shape: {res.data!r}"[:500]
        ctx.append_chunk(
            "job_event",
            f"triage attempt {attempt}: {last_err}",
        )
    if verdicts is None:
        ctx.record_failure(
            f"good_search_triage: judge output unusable after retry — {last_err}"
        )
        return

    kept_n = sum(1 for v in verdicts if v["keep"])
    ctx.append_chunk("job_result", json.dumps({"verdicts": verdicts}))
    ctx.append_chunk(
        "job_summary",
        f"triaged {len(candidates)} candidate(s): kept {kept_n}, "
        f"{len(verdicts)} verdict(s) returned",
    )


def _triage_run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("good_search_triage runs via dispatch(), not run()")


TRIAGE_SPEC = JobTypeSpec(
    name="good_search_triage",
    params_schema=TRIAGE_PARAMS_SCHEMA,
    compatible_executors=TRIAGE_COMPATIBLE_EXECUTORS,
    requires=frozenset(),
    description=TRIAGE_DESCRIPTION,
    run=_triage_run,
    dispatch=_triage_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


def load_triage() -> JobTypeSpec:
    return TRIAGE_SPEC


__all__ = ["SPEC", "TRIAGE_SPEC", "load", "load_triage"]
