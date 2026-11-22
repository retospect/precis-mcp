"""``memory_triage`` job_type — the big-model pass over a twice-memory-killed
job's run logs (gr595538).

The memory-kill policy (``precis.workers.executors._common.handle_memory_kill``)
parks a job killed for memory a second time (``STATUS:parked``, reason
memory) and mints one triage job (idem ``memory_triage:<ref_id>``,
parented nowhere — its verdict is the parked job's disposition, not a
child outcome for the todo). This job_type is that pass: one BIG-tier
``route()`` call carrying the parked job's last ``job_event`` chunks and
its footprint forensics, asking for one of three verdicts:

* ``too_big`` — the workload exceeds any box the fleet can offer: the
  parked job is failed with ``failure_class="too-big"`` (the drop
  disposition; the failure bubbles to the parent like any terminal).
* ``bug`` — a bug, leak or runaway loop: a gripe is filed with the
  evidence pack (fingerprint ``memory-triage:<job_id>``, so a re-triage
  dedupes onto the open gripe), then the parked job is failed naming it.
* ``unknown`` — what the triage cannot explain: an ``ask-user`` todo is
  minted and the parked job STAYS parked — only what triage cannot
  explain reaches the user.

Lane: ``claude_inproc`` (``REQUIRES claude_bin``) — one BIG-tier read,
no tools, no MCP: the model decides from the prompt's log extracts and
cannot touch anything. A parked job the verdict race already resolved
(no longer ``STATUS:parked``) is a no-op success, not an error.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo.guards import check_ask_meta
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, route
from precis.utils.secret_scan import mask_secrets_deep
from precis.utils.timeutil import now_iso
from precis.workers.executors._common import PARKED, current_status, record_failure
from precis.workers.job_types import JobTypeSpec, defer_on_pause

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

NAME = "memory_triage"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "integer"},
    },
    "required": ["job_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
REQUIRES: frozenset[str] = frozenset({"claude_bin"})
DESCRIPTION = (
    "Triage a job killed for memory twice (STATUS:parked): one BIG-tier "
    "read of its run logs decides too-big (drop) / bug (file a gripe) / "
    "unknown (ask-user todo)."
)

#: Spend + wall-clock on the single BIG-tier call.
MAX_USD = 1.00
TIMEOUT_S = 900.0
#: How much run-log text one call may carry.
MAX_EVENTS = 10
MAX_CHARS = 20_000

_PROMPT = """You are triaging a compute job that was killed by its memory
cap TWICE — retried once with a bumped footprint estimate and killed again.
Decide what it actually is from the evidence below.

Job: {title}
Job type: {job_type}
Executor: {executor}
Footprint forensics (refs.meta):
{forensics}

Last run-log events (newest last), truncated to fit:
{events}

Return ONE JSON object (no prose, no code fence) of this shape:

{{
  "verdict": "too_big" | "bug" | "unknown",
  "explanation": "<two-to-four sentences: what the logs show, why this
                  verdict>",
  "confidence": <0..1>
}}

Verdicts:
- "too_big": the workload genuinely needs more memory than any reasonable
  box (estimates far above the cap, allocation growth matching real input
  growth, no leak signature). The job is dropped.
- "bug": a leak, runaway loop or misconfiguration — RSS growth without
  bound at constant input, repeated identical allocation failures in the
  log, an obvious config error. A gripe is filed with this evidence.
- "unknown": neither reading is supportable from the evidence. A human
  is asked.

Do not guess beyond the evidence; unknown is an honest answer.
"""


class LlmCallFailed(Exception):
    """The routed call came back with ``res.error`` — carries the
    :class:`LlmResult` so the dispatch can park on a window-scoped pause
    (``defer_on_pause``) instead of recording a defect."""

    def __init__(self, res: LlmResult) -> None:
        super().__init__(str(res.error))
        self.res = res


def _llm(prompt: str, *, ref_id: int | None) -> LlmResult:
    """The routed BIG-tier call — module-level so a test can swap it."""
    return route(
        LlmRequest(
            tier=Tier.BIG,
            source="memory_triage",
            prompt=prompt,
            max_usd=MAX_USD,
            timeout_s=TIMEOUT_S,
            ref_id=ref_id,
        )
    )


def parse_verdict(text: str) -> dict[str, Any]:
    """The model's reply → the verdict dict. Accepts a bare object or one
    inside a ```json fence; anything else is a ValueError."""
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the model reply")
        s = s[start : end + 1]
    payload = json.loads(s)
    if not isinstance(payload, dict):
        raise ValueError("model reply is not a JSON object")
    verdict = payload.get("verdict")
    if verdict not in ("too_big", "bug", "unknown"):
        raise ValueError(f"verdict must be too_big|bug|unknown, got {verdict!r}")
    explanation = payload.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("verdict must include an explanation")
    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError("verdict confidence must be between 0 and 1")
    return payload


def _evidence(store: Store, job_id: int) -> dict[str, Any] | None:
    """The parked job's forensics + run-log tail, or ``None`` when the
    row vanished. Never raises on a missing row — the dispatch treats
    that as a nothing-to-do success."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
        if row is None:
            return None
        title = str(row[0] or "")
        meta = dict(row[1] or {})
        events = [
            str(r[0] or "")
            for r in conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL ORDER BY ord DESC LIMIT %s",
                (job_id, MAX_EVENTS),
            ).fetchall()
        ]
    events.reverse()
    picked: list[str] = []
    total = 0
    for text in reversed(events):
        # Cap from the newest end: the freshest forensics matter most.
        if picked and total + len(text) > MAX_CHARS:
            break
        picked.append(text)
        total += len(text)
    picked.reverse()
    forensics = mask_secrets_deep(
        {
            "job_type": meta.get("job_type"),
            "params": meta.get("params"),
            "executor": meta.get("executor"),
            "size": meta.get("size"),
            "footprint_req": meta.get("footprint_req"),
            "killed_memory": meta.get("killed_memory"),
            "footprint": meta.get("footprint"),
            "failure_class": meta.get("failure_class"),
            "error": meta.get("error"),
            "image": meta.get("image"),
        }
    )
    return {
        "title": title,
        "job_type": meta.get("job_type"),
        "executor": meta.get("executor"),
        "forensics": json.dumps(forensics, indent=1, sort_keys=True, default=str),
        "events": "\n\n---\n\n".join(picked),
    }


def build_prompt(ev: dict[str, Any]) -> str:
    return _PROMPT.format(
        title=ev["title"],
        job_type=ev["job_type"],
        executor=ev["executor"],
        forensics=ev["forensics"],
        events=ev["events"] or "(no run-log chunks)",
    )


def _short(text: str, cap: int = 300) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _evidence_pack(job_id: int, verdict: str, explanation: str) -> str:
    return (
        f"memory-triage:{job_id}\n"
        f"verdict: {verdict}\n"
        f"explanation: {explanation}\n"
        f"triaged_at: {now_iso()}\n"
        "A compute job was killed by its memory cap twice (requeue with a "
        "bumped footprint estimate, then killed again) and parked. The "
        "triage pass classified it from the run logs — see the parked "
        "job's job_event chunks for the raw evidence."
    )


def apply_verdict(
    store: Store,
    job_id: int,
    verdict: str,
    *,
    explanation: str,
    conn: Connection | None = None,
) -> int | None:
    """Apply a triage verdict to the parked job. ``too_big`` fails the job
    (the drop — the bubble reaches the parent todo); ``bug`` files a gripe
    carrying the evidence and fails the job naming it; ``unknown`` mints
    an ``ask-user`` todo and leaves the job parked. Returns the gripe or
    todo id when one was minted, else ``None``.

    The parked-status check and disposition share a row lock and the
    caller's transaction (``conn=None`` opens a short transaction). An already-resolved job (no longer
    ``STATUS:parked``) is left alone: the verdict race lost.
    """

    def _act(c: Connection) -> int | None:
        row = c.execute(
            "SELECT meta->>'job_type' FROM refs WHERE ref_id = %s "
            "AND kind = 'job' AND retired_at IS NULL FOR UPDATE",
            (job_id,),
        ).fetchone()
        if row is None or current_status(c, job_id) != PARKED:
            return None
        jt = str(row[0] or "")
        if verdict == "too_big":
            record_failure(
                store,
                job_id,
                "memory triage: too big for any box — dropped. " + explanation,
                gripe_rollback=None,
                failure_class="too-big",
                conn=c,
            )
            return None
        if verdict == "bug":
            fingerprint = f"memory-triage:{job_id}"
            existing = c.execute(
                "SELECT ref_id FROM refs r WHERE r.kind = 'gripe' "
                "AND r.retired_at IS NULL AND r.meta->>'fingerprint' = %s "
                "AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
                "ON t.tag_id = rt.tag_id WHERE rt.ref_id = r.ref_id "
                "AND t.namespace = 'STATUS' AND t.value IN ('done', 'wontfix')) "
                "LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                gripe_id = int(existing[0])
            else:
                gripe = store.insert_ref(
                    kind="gripe",
                    slug=None,
                    title=f"jobs: {jt or 'job'} memory-killed twice — "
                    "triage says bug, leak or runaway loop",
                    meta={"fingerprint": fingerprint},
                    conn=c,
                )
                gripe_id = int(gripe.id)
                store.add_tag(
                    gripe_id,
                    Tag.closed("STATUS", "open"),
                    set_by="system",
                    replace_prefix=True,
                    conn=c,
                )
                store.chunks.insert_chunks(
                    gripe_id,
                    [
                        ChunkInsert(
                            ord=0,
                            text=_evidence_pack(job_id, verdict, explanation),
                            meta={"chunk_kind": "gripe_body"},
                        )
                    ],
                    conn=c,
                )
            record_failure(
                store,
                job_id,
                f"memory triage: bug/leak/runaway — gripe #{gripe_id} filed "
                f"with the evidence. {explanation}",
                gripe_rollback=None,
                failure_class="infra",
                conn=c,
            )
            return gripe_id
        # unknown: only what triage cannot explain reaches the user.
        existing = c.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta->>'memory_triage_job_id' = %s ORDER BY ref_id LIMIT 1",
            (str(job_id),),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        ask = {
            "kind": "ruling",
            "options": [
                "split the job into smaller pieces",
                "drop it",
                "investigate the leak or runaway loop",
            ],
            "blocks": "nothing",
            "context": _short(
                f"job {job_id} ({jt or 'unknown type'}) was memory-killed "
                "twice and parked; the automated memory triage could not "
                f"classify it: {explanation}",
                800,
            ),
        }
        check_ask_meta({"ask": ask}, question="memory-killed job: what now?")
        todo = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"memory-killed job {job_id} ({jt or 'unknown type'}): "
            "split, drop or investigate?",
            meta={"ask": ask, "memory_triage_job_id": job_id},
            conn=c,
        )
        todo_id = int(todo.id)
        store.add_tag(
            todo_id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=c,
        )
        store.add_tag(todo_id, Tag.open("ask-user"), set_by="system", conn=c)
        store.chunks.insert_chunks(
            todo_id,
            [
                ChunkInsert(
                    ord=0,
                    text=_evidence_pack(job_id, verdict, explanation),
                    meta={"chunk_kind": TODO_BODY_KIND},
                )
            ],
            conn=c,
        )
        return todo_id

    if conn is not None:
        return _act(conn)
    with store.pool.connection() as c:
        with c.transaction():
            return _act(c)


def triage(
    store: Store,
    job_id: int,
    *,
    llm: Callable[..., LlmResult] | None = None,
    event: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one triage pass over the parked job. Deterministic outcomes are
    records, not raises; an LLM failure raises :class:`LlmCallFailed` so
    the dispatch can park on a window-scoped pause."""
    ev = _evidence(store, job_id)
    if ev is None:
        return {"status": "gone", "job_id": job_id}
    with store.pool.connection() as conn:
        status = current_status(conn, job_id)
    if status != PARKED:
        # The verdict race already resolved it (operator, cancel, another
        # pass) — a nothing-to-do success, not an error.
        return {"status": "resolved", "job_id": job_id, "status_now": status}
    res = (llm or _llm)(build_prompt(ev), ref_id=job_id)
    if res.error:
        raise LlmCallFailed(res)
    try:
        payload = parse_verdict(res.text or "")
    except ValueError as exc:
        if event is not None:
            event(f"unparseable reply:\n{(res.text or '')[:2000]}")
        return {"status": "bad_payload", "job_id": job_id, "detail": str(exc)}
    verdict = str(payload["verdict"])
    explanation = _short(payload["explanation"], 2000)
    minted = apply_verdict(store, job_id, verdict, explanation=explanation)
    return {
        "status": "ok",
        "job_id": job_id,
        "verdict": verdict,
        "explanation": explanation,
        "confidence": payload.get("confidence"),
        "minted_ref_id": minted,
    }


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    params = (ctx.meta or {}).get("params") or {}
    raw = params.get("job_id")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        ctx.record_failure(
            f"memory_triage: job_id must be a positive integer, got {raw!r}",
            failure_class="infra",
        )
        return
    job_id = raw
    try:
        record = triage(
            ctx.store,
            job_id,
            event=lambda t: ctx.append_chunk("job_event", t),
        )
    except LlmCallFailed as exc:
        if defer_on_pause(ctx, exc.res, label=NAME):
            return
        record = {"status": "llm_error", "detail": str(exc.res.error)}
    if record["status"] in ("bad_payload", "llm_error"):
        reason = f"memory triage could not return a verdict: {record['detail']}"
        apply_verdict(ctx.store, job_id, "unknown", explanation=reason)
        ctx.record_failure(reason, failure_class="infra")
        return
    ctx.append_chunk("job_summary", f"memory_triage job {job_id}: {record['status']}")
    if record["status"] == "ok":
        ctx.append_chunk(
            "job_event",
            f"verdict: {record['verdict']} — {record['explanation']}",
        )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name=NAME,
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
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "apply_verdict",
    "build_prompt",
    "load",
    "parse_verdict",
    "triage",
]
