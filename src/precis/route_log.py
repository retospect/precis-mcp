"""Full LLM interaction log — Slice 1 of the adaptive-router plan.

Records every ``router.dispatch`` call's full logical request + final response +
outcome metadata to postgres (``llm_call_log`` + content-addressed ``llm_blob``,
migration 0061), so later slices can replay requests on other models and score
the difference. Operational, **not corpus** — never embedded (peer to
:mod:`precis.agentlog` / alerts).

**Dark by construction.** The writer is best-effort over a process-bound store
(bound at worker / runtime boot, mirroring :mod:`precis.secrets`); with no store
bound (DB-free callers, tests) it is a no-op, and any write failure is swallowed
so a logging problem can never break an LLM call.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Process-bound store the best-effort writer uses. ``bind_store`` sets it at
#: worker / runtime boot; unbound → every :func:`record_call` is a no-op.
_STORE: Store | None = None

#: Retention floor for the GC valve (days). Generous — the log is the training
#: asset — but bounded so it can't grow forever. Env-overridable.
RETENTION_DAYS_ENV = "PRECIS_LLM_LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90

#: Fleet-wide advisory-lock key for :func:`gc`. Every host runs a system-profile
#: sweeper, and each pass called ``gc`` with no guard — so the (table-scanning)
#: orphan-blob sweep piled up concurrently and pegged the DB host's CPU. A fixed,
#: arbitrary signed-64 constant (ascii ``"llm_gc"``) makes the sweep single-flight
#: across the fleet: one worker holds it, the rest fast-fail. Xact-scoped, so it
#: auto-releases at commit/rollback — no unlock bookkeeping.
_GC_LOCK = 0x6C6C6D5F6763


def bind_store(store: Store | None) -> None:
    """Bind (or clear) the process store the logger writes through."""
    global _STORE
    _STORE = store


def enabled() -> bool:
    """True when a store is bound — so a caller can skip building the record
    (serializing the full request) when logging would no-op anyway."""
    return _STORE is not None


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One dispatch call to log: full request/response text + metadata. The
    caller (``router.dispatch``) fills this; the writer dedups the text blobs."""

    source: str | None
    tier: str | None
    transport: str | None
    model: str | None
    tools_needed: bool | None
    request_text: str
    response_text: str
    cost_usd: float | None
    turns_used: int | None
    duration_ms: int | None
    errored: bool
    error: str | None
    data_parsed: bool | None
    features: dict[str, Any] = field(default_factory=dict)
    ref_id: int | None = None
    #: Store the full request/response text in ``llm_blob`` (the replay material),
    #: or write a **lite** metadata-only row (hashes NULL, char counts still
    #: recorded). Corpus batch passes set this ``False`` — cheap + mineable, no
    #: per-call blob. See :attr:`precis.utils.llm.router.LlmRequest.log_blobs`.
    store_blobs: bool = True


def _blob_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_call(rec: LlmCallRecord, *, store: Store | None = None) -> None:
    """Write one call record — best-effort, never raises.

    No-op when no store is bound (and none passed). Dedups the request/response
    text into ``llm_blob`` (content-addressed), then inserts the metadata row.
    A store may be passed explicitly (tests); otherwise the bound one is used.
    """
    st = store if store is not None else _STORE
    if st is None:
        return
    try:
        _write(st, rec)
    except Exception:
        log.debug("route_log: record_call failed", exc_info=True)


def _write(store: Store, rec: LlmCallRecord) -> None:
    import json

    # Char counts are recorded either way (the mineable volume signal); the blob
    # text + its hash are stored only for a full row. A lite row leaves the
    # (nullable) hashes NULL — no ~18 KB replay blob per corpus-batch call.
    req_hash = _blob_hash(rec.request_text) if rec.store_blobs else None
    resp_hash = _blob_hash(rec.response_text) if rec.store_blobs else None

    with store.pool.connection() as conn:
        if rec.store_blobs:
            for h, text in (
                (req_hash, rec.request_text),
                (resp_hash, rec.response_text),
            ):
                conn.execute(
                    "INSERT INTO llm_blob (hash, text, bytes) VALUES (%s, %s, %s) "
                    "ON CONFLICT (hash) DO NOTHING",
                    (h, text, len(text.encode("utf-8"))),
                )
        conn.execute(
            """
            INSERT INTO llm_call_log (
                source, tier, transport, model, tools_needed,
                request_hash, response_hash, request_chars, response_chars,
                cost_usd, turns_used, duration_ms, errored, error, data_parsed,
                ref_id, features
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s
            )
            """,
            (
                rec.source,
                rec.tier,
                rec.transport,
                rec.model,
                rec.tools_needed,
                req_hash,
                resp_hash,
                len(rec.request_text),
                len(rec.response_text),
                rec.cost_usd,
                rec.turns_used,
                rec.duration_ms,
                rec.errored,
                rec.error,
                rec.data_parsed,
                rec.ref_id,
                json.dumps(rec.features),
            ),
        )
        conn.commit()


@dataclass(frozen=True, slots=True)
class SpendRow:
    """One grouped row of the mining rollup — natural units kept *separate* (no
    single collapsed 'cost' number): a lane's real dollars, its char volume, and
    its wall-clock all stand on their own so a later placement decision reads the
    dimension it cares about. ``real_usd`` sums only non-null ``cost_usd`` (the
    lanes that actually bill); the OAuth / local lanes report ``$0`` here because
    they cost quota / wall-clock, not money — that is the point, not a gap."""

    key: str
    calls: int
    real_usd: float
    req_chars: int
    resp_chars: int
    wall_ms: int
    errors: int


#: The columns ``group_by`` may bucket on (allow-listed — never interpolate raw).
_GROUP_COLS = {
    "transport": "transport",
    "source": "source",
    "ref": "ref_id",
    "model": "model",
}


def spend_rollup(
    store: Store,
    *,
    days: int = 7,
    group_by: str = "transport",
    source: str | None = None,
    limit: int = 40,
) -> list[SpendRow]:
    """Mine ``llm_call_log`` for the last ``days`` — grouped by lane / pass / ref /
    model — into per-group volume + real-$ + wall-clock. Read-only.

    Covers every LLM lane that goes through ``dispatch``: the agentic / judge
    calls (full rows) **and** the corpus batch passes (``llm_summarize`` /
    ``classify`` / ``paper_glossary``), which write **lite** metadata rows — so
    local-vs-cloud volume + wall-clock is answerable here. What's *not* here:
    non-LLM compute (spark DFT / relax / fold, container jobs) never touches
    ``dispatch``, so a placement view over those still needs its own counter."""
    col = _GROUP_COLS.get(group_by)
    if col is None:
        raise ValueError(f"group_by must be one of {sorted(_GROUP_COLS)}")
    with store.pool.connection() as conn:
        cur = conn.execute(
            f"""
            SELECT COALESCE({col}::text, '∅') AS key,
                   count(*)::int AS calls,
                   COALESCE(sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL), 0)::float AS real_usd,
                   COALESCE(sum(request_chars), 0)::bigint AS req_chars,
                   COALESCE(sum(response_chars), 0)::bigint AS resp_chars,
                   COALESCE(sum(duration_ms), 0)::bigint AS wall_ms,
                   count(*) FILTER (WHERE errored)::int AS errors
            FROM llm_call_log
            WHERE ts > now() - (%s || ' days')::interval
              AND (%s::text IS NULL OR source = %s)
            GROUP BY 1 ORDER BY calls DESC LIMIT %s
            """,
            (days, source, source, limit),
        )
        return [
            SpendRow(
                key=r[0],
                calls=r[1],
                real_usd=float(r[2]),
                req_chars=int(r[3]),
                resp_chars=int(r[4]),
                wall_ms=int(r[5]),
                errors=r[6],
            )
            for r in cur.fetchall()
        ]


def gc(store: Store, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete log rows past the retention window; GC orphaned blobs. Returns the
    number of call rows deleted (0 when another worker holds the GC lock).

    Two guards keep this cheap on the per-pass sweeper hot path:

    * **Single-flight** — a fleet-wide advisory lock (:data:`_GC_LOCK`). Every
      host runs a sweeper, so without it they ran concurrent orphan-blob sweeps
      and saturated the DB host. Xact-scoped: released at commit/rollback.
    * **Skip when idle** — the orphan-blob sweep only runs when this pass
      actually deleted call rows. No deleted calls → no newly-orphaned blobs, so
      the anti-join (indexed on ``request_hash``/``response_hash``, migration
      0077) is skipped entirely on the common no-op pass. When it does run, it
      still clears *any* orphan, so a one-off backlog is reclaimed next sweep.
    """
    with store.pool.connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (_GC_LOCK,)
        ).fetchone()
        if not got or not got[0]:
            return 0  # another worker is already GC'ing — don't pile on
        cur = conn.execute(
            "DELETE FROM llm_call_log WHERE ts < now() - (%s || ' days')::interval",
            (retention_days,),
        )
        deleted = cur.rowcount
        if deleted:
            conn.execute(
                "DELETE FROM llm_blob b WHERE NOT EXISTS ("
                "  SELECT 1 FROM llm_call_log l"
                "   WHERE l.request_hash = b.hash OR l.response_hash = b.hash)"
            )
        conn.commit()
    return deleted


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "LlmCallRecord",
    "SpendRow",
    "bind_store",
    "enabled",
    "gc",
    "record_call",
    "spend_rollup",
]
