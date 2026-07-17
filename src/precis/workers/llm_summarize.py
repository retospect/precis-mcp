"""LLM chunk-summarization worker pass.

Writes a model-authored two-part summary — a *very brief* gist plus a
sentence or two of *additional detail* — into ``chunk_summaries`` under
``summarizer = 'llm-v1'``. This is a distinct artifact from the lexical
``rake-lemma`` keyword row (also in ``chunk_summaries``) and from the
per-chunk KeyBERT keywords on ``chunks.keywords`` (F20). Registered by
migration ``0025_register_llm_summarizer.sql``.

Why a standalone pass and not a :class:`~precis.workers.base.WorkerHandler`
-------------------------------------------------------------------------
``WorkerHandler.process`` must be pure (no DB, no I/O). In-context
summarization needs both: DB JOINs for the document header + section
path + keywords + numerics, and an outbound LLM call. So this follows
the ``chunk_keywords`` ref-pass shape (own claim query, own writes,
returns ``{claimed, ok, failed}``) rather than the handler base.

Transport
---------
A tiny stdlib ``urllib`` OpenAI ``/v1/chat/completions`` client,
identical in shape to ``RemoteEmbedder`` (ADR 0020). It points at the
cluster's litellm proxy (the ``summarizer`` alias → Qwen3-Next-80B-A3B
on llama.cpp). The :class:`Transport` seam keeps the pass
offline-testable — tests inject a fake that returns canned completions.

Default-off
-----------
The pass runs only via ``precis worker --only llm_summarize`` or
``PRECIS_SUMMARIZE_LLM=1`` — never in the default system/agent profile.
A 1M-chunk backfill is a deliberate, node-targeted batch, not something
every system worker should pick up.

Prefix-cache discipline
-----------------------
llama.cpp reuses the KV cache of the longest matching prompt *prefix*.
So the stable content (system instructions + the document header card)
is the FIRST message and is byte-identical across every chunk of a
document; the per-chunk specifics go LAST. Claims are ordered
``ref_id, ord`` and the doc card is cached per ref, so consecutive
chunks of one document reuse the cached prefix and only the short tail
is re-evaluated. NOTE: pin the litellm ``summarizer`` alias to a single
backend — least-busy routing across nodes destroys this locality.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from precis.utils.prompt import (
    AssemblyContext,
    Layer,
    LiteLLMAdapter,
    Module,
    Profile,
    assemble,
)

log = logging.getLogger(__name__)

#: Summarizer name written to ``chunk_summaries.summarizer``. Bump to
#: ``llm-v2`` (and ship a new ``0026`` registry migration) to
#: re-summarize the corpus without destroying v1 rows.
SUMMARIZER_NAME = "llm-v1"

#: Chunk kinds whose text is non-prose — summarizing them yields noise.
#: Mirrors ``chunk_keywords._SKIP_KINDS`` plus the card front-matter
#: (cards are themselves derived headers, not body content).
SKIP_KINDS: frozenset[str] = frozenset(
    {
        "card_authors",
        "card_combined",
        "card_title",
        "table",
        "equation",
        "figure",
        "references",
    }
)

#: Below this length there is too little to summarize usefully.
MIN_CHUNK_CHARS = 200

#: Above this length a chunk is almost certainly a mis-chunked dump — the
#: 99th percentile of real passages is ~830 chars. Summarizing it wastes a
#: slot and can overflow the per-slot context (--ctx-size / --parallel), so
#: skip it; the reader falls back to keywords / a text peek.
MAX_CHUNK_CHARS = 16000

#: Fraction of digit characters above which a chunk is a data table or
#: coordinate dump, not prose. The model can only hallucinate meaning from
#: these (it grabs a random cell and calls it a "time"), so the pass tags them
#: with ``NUMERIC_DUMP_TAG`` instead of calling the LLM. Checked in Python
#: (``_is_numeric_dump``) after the claim — doing it as a ``regexp_replace`` in
#: the claim SQL made the claim ~74s/batch (a regexp over ~1M un-summarized
#: rows that can't be indexed).
MAX_DIGIT_FRACTION = 0.5

#: Gloss written for a numeric/coordinate dump (mirrors the prompt's non-prose
#: tag rule, but skips the LLM call entirely).
NUMERIC_DUMP_TAG = "(tabular data)"

#: Retry budget for a failing chunk, tracked on its ``chunk_claims`` lease.
#: Each failure bumps ``chunk_claims.attempts`` and keeps the lease (its
#: refreshed ``claimed_at`` is the retry backoff via the cooldown reaper) — so
#: transient failures (e.g. the 80B returning empty during a cold-load) get
#: retried. Once ``attempts`` reaches this cap the failure is terminal: a
#: ``status='failed'`` marker row is written to ``chunk_summaries`` and the
#: lease is deleted, so a poison chunk can't re-bill the backend every pass.
#: ``attempts`` starts at 0 on claim and increments per failure, so this
#: allows 3 attempts total.
MAX_SUMMARIZE_ATTEMPTS = 3

#: Immediate in-process retries when the backend hands back an EMPTY completion
#: (``EmptySummaryError``). The blank is a *transient* backend condition — the
#: shared 80B slot returns "" in bursts under contention / model-swap, and the
#: *same* chunk summarizes fine on replay (verified: 17/17 prod-failing chunks
#: replayed clean, real prompt + doc_card). A short in-process retry recovers
#: the chunk within the same pass instead of deferring it a whole 20-min
#: cooldown, so a momentary blip costs neither a scarce cross-pass attempt nor a
#: wasted claim. Only EMPTY misses retry here; a genuine exception falls
#: straight through to the failure path (no point re-billing a real error).
EMPTY_RETRY_ATTEMPTS = 3
EMPTY_RETRY_BACKOFF_S = 1.0

#: Cross-pass cap for EMPTY misses specifically — deliberately far above
#: ``MAX_SUMMARIZE_ATTEMPTS``. An empty completion is a transient backend
#: outage, not a poison chunk, so it must never *permanently strand* a
#: summarizable chunk after a few unlucky windows (the disease behind the ~92k
#: terminally-failed embeds). With the 20-min cooldown, 12 attempts spans ~4 h
#: of recurring bad windows before a genuinely-always-empty chunk (rare, e.g.
#: adversarial input) is finally retired. Real errors keep the tight cap of 3.
MAX_SUMMARIZE_EMPTY_ATTEMPTS = 12

#: A ``chunk_claims`` row older than this many minutes is treated as abandoned
#: (the worker crashed or stalled) and re-claimed oldest-first; it is also the
#: retry backoff for failures (which keep their claim). A *live* worker
#: re-stamps ``claimed_at`` on its whole batch after every per-chunk LLM
#: completion (``_heartbeat_leases``), so this only needs to comfortably
#: exceed ONE per-chunk LLM call (the 120 s client timeout) — not the whole
#: batch, whose worst case (batch_size × timeout = 16 × 120 s = 32 min) would
#: otherwise outrun it. The occasional double-process is a no-op (idempotent
#: upsert on (chunk_id, summarizer)) and this reproducible background work
#: tolerates rework.
SUMMARY_LEASE_COOLDOWN_MIN = 20

#: GC horizon for orphaned leases: a claim row this many minutes old whose
#: chunk no longer *qualifies* (deleted / retired / filtered out by the
#: kind/length/no_index predicates) can never be selected by ``_RECLAIM_SQL``
#: (it inner-joins ``chunks`` re-applying those filters) and would otherwise
#: sit in ``chunk_claims`` forever. A generous multiple of the cooldown so a
#: merely-slow lease is never confused with an orphan; still-qualifying rows
#: are protected by the GC's ``NOT EXISTS`` regardless of age.
SUMMARY_LEASE_GC_MIN = SUMMARY_LEASE_COOLDOWN_MIN * 24  # 8 h

#: Per-pass cap on GC candidates — keeps the sweep a bounded index range scan
#: (``chunk_claims_reap_idx``) even after a mass chunk retirement.
_LEASE_GC_LIMIT = 100

#: How many words/sentences the two parts should target. Enforced by
#: the prompt, not the parser (the model occasionally overshoots; we
#: keep what it returns rather than truncating mid-sentence).
_BRIEF_MAX_WORDS = 15


# ---------------------------------------------------------------------------
# Transport seam + OpenAI chat client (RemoteEmbedder shape, ADR 0020)
# ---------------------------------------------------------------------------


class Transport(Protocol):
    """Minimal HTTP-POST seam so the pass is offline-testable."""

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]: ...


class _UrllibTransport:
    """Default stdlib transport — one POST, JSON in / JSON out."""

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        result: dict[str, Any] = json.loads(raw)
        return result


@dataclass(frozen=True)
class LlmConfig:
    """Connection + sampling config, resolved from the environment.

    Defaults target the loopback litellm proxy and its ``summarizer``
    alias. ``enabled`` gates the whole pass (the ``--only`` selector
    also enables it; see ``cli/worker.py``).
    """

    enabled: bool = False
    url: str = "http://127.0.0.1:4000/v1"
    model: str = "summarizer"
    api_key: str = "dummy"  # loopback litellm has no master_key
    max_tokens: int = 220
    timeout: float = 120.0
    #: How many chunks of a batch to summarize concurrently. The HTTP
    #: completion is the only slow part, so a thread pool of this width
    #: keeps that many llama-server slots busy from a single worker
    #: process. Set it to the backend's ``--parallel`` slot count — fewer
    #: underfills the slots, more just queues on the server (no gain).
    #: Default 1 = the original sequential behaviour.
    concurrency: int = 1

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LlmConfig:
        e = env if env is not None else dict(os.environ)
        return cls(
            enabled=_truthy(e.get("PRECIS_SUMMARIZE_LLM")),
            url=e.get("PRECIS_SUMMARIZE_LLM_URL") or cls.url,
            model=e.get("PRECIS_SUMMARIZE_MODEL") or cls.model,
            api_key=e.get("PRECIS_SUMMARIZE_LLM_KEY") or cls.api_key,
            max_tokens=int(e.get("PRECIS_SUMMARIZE_MAX_TOKENS") or cls.max_tokens),
            timeout=float(e.get("PRECIS_SUMMARIZE_TIMEOUT") or cls.timeout),
            concurrency=max(
                1, int(e.get("PRECIS_SUMMARIZE_CONCURRENCY") or cls.concurrency)
            ),
        )


@dataclass
class LlmResult:
    """A completion plus its token accounting.

    ``prompt_tokens`` / ``completion_tokens`` carry the OpenAI ``usage`` split
    so the router can price an OSS/OpenRouter call (which reports tokens, not
    dollars) via :mod:`precis.budget.pricing`. ``None`` when the backend omits
    the field. ``cost_usd`` carries a provider-returned dollar cost when one is
    present (OpenRouter reports ``usage.cost``); the router prefers it over the
    token-table estimate.
    """

    text: str
    total_tokens: int | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


class LlmClient:
    """OpenAI ``/v1/chat/completions`` client for the summarizer alias."""

    def __init__(
        self, config: LlmConfig, *, transport: Transport | None = None
    ) -> None:
        self._config = config
        self._transport: Transport = transport or _UrllibTransport()

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        """POST ``messages`` and return the assistant text + usage.

        ``extra_body`` is merged into the request payload — the seam the router
        uses to pin an OpenRouter variant (``provider:{order,quantizations,…}`` +
        ``reasoning:{effort}``, gripe 162624) without this client knowing about
        booking. Absent on the loopback / summarizer path.

        Raises on transport error or a malformed response so the pass
        marks the chunk failed (ADR 0007) and moves on.
        """
        url = self._config.url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": self._config.max_tokens,
            "temperature": 0,
        }
        if extra_body:
            payload.update(extra_body)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        body = self._transport.post_json(
            url, payload, headers=headers, timeout=self._config.timeout
        )
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"summarizer returned no completion: {body!r}") from exc
        usage = body.get("usage") or {}
        total = usage.get("total_tokens")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        # OpenRouter returns a real dollar cost in ``usage.cost`` (some
        # deployments at the top level); prefer it over the token-table
        # estimate. Absent on the loopback proxy / plain OpenAI wire.
        cost = usage.get("cost")
        if cost is None:
            cost = body.get("cost")
        return LlmResult(
            text=str(text),
            total_tokens=int(total) if total is not None else None,
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=int(completion) if completion is not None else None,
            cost_usd=float(cost) if cost is not None else None,
        )


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    chunk_id: int
    ref_id: int
    ord: int
    chunk_kind: str
    text: str
    section_path: list[str]
    keywords: list[str] | None
    numerics: list[str]
    ref_kind: str
    title: str


# Lease claim via the shared ``chunk_claims`` table. Each pass selects eligible
# chunks with ``FOR UPDATE SKIP LOCKED`` *and* writes/refreshes the claim row in
# the same statement (data-modifying CTE). The caller commits immediately
# (releasing the lock) and does the LLM work with no open transaction — so the
# xmin horizon is never pinned across an LLM call (the old long-batch
# transaction is what starved autovacuum). A crashed worker leaves its claim
# row; once ``claimed_at`` ages past the cooldown it is re-claimed. The cooldown
# is the reaper.
#
# Two sources, claimed in order until the batch is full:
#   1. FRESH    — chunks with no summary AND no claim (NOT EXISTS x2). Written
#                 as NOT EXISTS so the planner index-walks chunks and stops at
#                 LIMIT instead of seq-scanning + sorting ~1.5M rows. Split
#                 priority (conv/draft) vs rest so the queue order needs no
#                 cross-join ``CASE`` (which would force the sort).
#   2. RECLAIM  — claim rows past the cooldown (crashed in-flight + retrying
#                 failures, which keep their claim), oldest-first via
#                 ``chunk_claims_reap_idx``. Tops up only when fresh runs dry.
#
# ``artifact`` is the chunk_claims discriminator and equals the summarizer name.
_FRESH_CLAIM_SQL = """
    WITH cand AS (
        SELECT c.chunk_id, c.ref_id, c.ord, c.chunk_kind, c.text,
               c.section_path, c.keywords, c.numerics,
               r.kind AS ref_kind, r.title
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
         WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_summaries cs
                    WHERE cs.chunk_id = c.chunk_id AND cs.summarizer = %(artifact)s
               )
           AND NOT EXISTS (
                   SELECT 1 FROM chunk_claims cl
                    WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(artifact)s
               )
           AND {kind_pred}
           AND {extra_pred}
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND length(c.text) <= %(max_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
         ORDER BY {order_by}
         LIMIT %(limit)s
           FOR UPDATE OF c SKIP LOCKED
    ),
    claimed AS (
        INSERT INTO chunk_claims (chunk_id, artifact)
        SELECT chunk_id, %(artifact)s FROM cand
        ON CONFLICT (chunk_id, artifact) DO NOTHING
        RETURNING chunk_id
    )
    SELECT cand.chunk_id, cand.ref_id, cand.ord, cand.chunk_kind, cand.text,
           cand.section_path, cand.keywords, cand.numerics,
           cand.ref_kind, cand.title
      FROM cand JOIN claimed USING (chunk_id)
"""

_RECLAIM_SQL = """
    WITH cand AS (
        SELECT cl.chunk_id, c.ref_id, c.ord, c.chunk_kind, c.text,
               c.section_path, c.keywords, c.numerics,
               r.kind AS ref_kind, r.title
          FROM chunk_claims cl
          JOIN chunks c ON c.chunk_id = cl.chunk_id
          JOIN refs r ON r.ref_id = c.ref_id
         WHERE cl.artifact = %(artifact)s
           AND cl.claimed_at < now() - (%(cooldown_min)s * interval '1 minute')
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND length(c.text) <= %(max_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
         ORDER BY cl.claimed_at
         LIMIT %(limit)s
           FOR UPDATE OF cl SKIP LOCKED
    ),
    reclaimed AS (
        UPDATE chunk_claims cl SET claimed_at = now()
          FROM cand
         WHERE cl.chunk_id = cand.chunk_id AND cl.artifact = %(artifact)s
        RETURNING cl.chunk_id
    )
    SELECT cand.chunk_id, cand.ref_id, cand.ord, cand.chunk_kind, cand.text,
           cand.section_path, cand.keywords, cand.numerics,
           cand.ref_kind, cand.title
      FROM cand JOIN reclaimed USING (chunk_id)
"""

# Orphaned-lease GC. ``_RECLAIM_SQL`` inner-joins ``chunks`` re-applying the
# kind/length/no_index filters, so a lease whose chunk was deleted / retired /
# filtered out is never selected again — and, being terminal-less, never
# removed. Sweep such rows once they are far past the cooldown
# (``SUMMARY_LEASE_GC_MIN``). Bounded: the candidate subselect is an index
# range scan on ``chunk_claims_reap_idx`` capped at ``_LEASE_GC_LIMIT``;
# ``SKIP LOCKED`` keeps it from colliding with sibling nodes' reclaims. The
# ``NOT EXISTS`` mirrors ``_RECLAIM_SQL``'s chunk predicates exactly, so any
# lease reclaim could still pick survives regardless of age.
_LEASE_GC_SQL = """
    DELETE FROM chunk_claims cl
     USING (
         SELECT chunk_id
           FROM chunk_claims
          WHERE artifact = %(artifact)s
            AND claimed_at < now() - (%(gc_min)s * interval '1 minute')
          ORDER BY claimed_at
          LIMIT %(gc_limit)s
            FOR UPDATE SKIP LOCKED
     ) old
     WHERE cl.artifact = %(artifact)s
       AND cl.chunk_id = old.chunk_id
       AND NOT EXISTS (
               SELECT 1 FROM chunks c
                WHERE c.chunk_id = cl.chunk_id
                  AND c.chunk_kind <> ALL(%(skip_kinds)s)
                  AND length(c.text) >= %(min_chars)s
                  AND length(c.text) <= %(max_chars)s
                  AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           )
"""

#: Refs whose chunks jump the queue (see ``claim_chunks_without_summary``).
_PRIORITY_KINDS = ("conv", "draft")

#: Recency window for the "hot" fresh tier: an un-summarised chunk whose
#: ``last_seen`` is newer than this is a document a human recently opened
#: in the reader (``bump_salience_for_ref``), so it jumps ahead of the
#: cold backlog. Wide enough that a paper opened at the start of a reading
#: session is still hot when the trickle reaches it; env-overridable.
HOT_WINDOW_MIN = int(os.environ.get("PRECIS_SUMMARIZE_HOT_WINDOW_MIN", "1440"))

#: The non-priority kind predicate (everything that isn't conv/draft,
#: NULL kind included) — shared by the hot + rest tiers so they partition
#: the same population the two priority tiers leave behind.
_REST_KIND_PRED = "(r.kind <> ALL(ARRAY['conv', 'draft']) OR r.kind IS NULL)"

#: Fresh-claim tiers, in queue order: draft > conv > hot > rest (ADR: the
#: reader-salience priority). Each is ``(kind_pred, extra_pred, order_by)``
#: spliced into ``_FRESH_CLAIM_SQL``. The hot tier reorders the rest by
#: ``last_seen DESC`` inside a recency window; the others keep the
#: ``ref_id, ord`` contiguity that hits the llama.cpp prefix cache.
_FRESH_TIERS: dict[str, tuple[str, str, str]] = {
    "draft": ("r.kind = 'draft'", "TRUE", "c.ref_id, c.ord"),
    "conv": ("r.kind = 'conv'", "TRUE", "c.ref_id, c.ord"),
    "hot": (
        _REST_KIND_PRED,
        "c.last_seen > now() - (%(hot_window_min)s * interval '1 minute')",
        "c.last_seen DESC",
    ),
    "rest": (_REST_KIND_PRED, "TRUE", "c.ref_id, c.ord"),
}


def _rows_to_claims(rows: list[tuple[Any, ...]]) -> list[_Claimed]:
    return [
        _Claimed(
            chunk_id=int(r[0]),
            ref_id=int(r[1]),
            ord=int(r[2]),
            chunk_kind=str(r[3]),
            text=str(r[4]),
            section_path=list(r[5] or []),
            keywords=list(r[6]) if r[6] is not None else None,
            numerics=list(r[7] or []),
            ref_kind=str(r[8]),
            title=str(r[9]),
        )
        for r in rows
    ]


def _claim_fresh(
    conn: Any, *, summarizer: str, limit: int, tier: str
) -> list[_Claimed]:
    """Claim never-seen chunks (no summary, no claim) for one queue tier.

    ``tier`` is one of :data:`_FRESH_TIERS` (draft / conv / hot / rest) — its
    ``(kind_pred, extra_pred, order_by)`` is spliced into the claim. The four
    tiers partition (draft, conv) then re-order the remainder (hot before
    rest); a chunk claimed by an earlier tier is excluded from a later one by
    the fresh ``NOT EXISTS chunk_claims``. Writes the ``chunk_claims`` row in
    the same statement (the data-modifying CTE) — the digit-fraction
    numeric-dump filter is applied in Python after the claim, not here (a
    ``regexp_replace`` over ~1M rows made the claim ~74s/batch; cheap
    length/kind filters stay SQL).
    """
    kind_pred, extra_pred, order_by = _FRESH_TIERS[tier]
    rows = conn.execute(
        _FRESH_CLAIM_SQL.format(
            kind_pred=kind_pred, extra_pred=extra_pred, order_by=order_by
        ),
        {
            "artifact": summarizer,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
            "max_chars": MAX_CHUNK_CHARS,
            "hot_window_min": HOT_WINDOW_MIN,
            "limit": limit,
        },
    ).fetchall()
    return _rows_to_claims(rows)


def _gc_orphaned_leases(conn: Any, *, summarizer: str) -> None:
    """Drop leases whose chunk can no longer qualify (see ``_LEASE_GC_SQL``).

    Runs once per claim inside the same short transaction; with no orphans the
    candidate scan is empty and the statement is ~free.
    """
    conn.execute(
        _LEASE_GC_SQL,
        {
            "artifact": summarizer,
            "gc_min": SUMMARY_LEASE_GC_MIN,
            "gc_limit": _LEASE_GC_LIMIT,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
            "max_chars": MAX_CHUNK_CHARS,
        },
    )


def _claim_reclaim(conn: Any, *, summarizer: str, limit: int) -> list[_Claimed]:
    """Re-claim stale claim rows (crashed in-flight + backing-off failures)."""
    rows = conn.execute(
        _RECLAIM_SQL,
        {
            "artifact": summarizer,
            "cooldown_min": SUMMARY_LEASE_COOLDOWN_MIN,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
            "max_chars": MAX_CHUNK_CHARS,
            "limit": limit,
        },
    ).fetchall()
    return _rows_to_claims(rows)


def claim_chunks_without_summary(
    conn: Any, *, summarizer: str, limit: int
) -> list[_Claimed]:
    """Lease up to ``limit`` chunks needing the ``summarizer`` summary.

    Writes a ``chunk_claims`` row for each claimed chunk and returns its data;
    the caller must commit promptly (releasing the lock) and do the LLM work
    with no open transaction. Also GCs orphaned leases (``_LEASE_GC_SQL``)
    first, in the same short transaction. Sources, in order:

    1. **Reclaim (reserved slice)** — a small slice (``min(2, limit // 8)``)
       goes to claim rows past the cooldown (crashed in-flight + retrying
       failures + migration-seeded backfill), oldest-first. Reserving it keeps
       a deep fresh backlog (~1M chunks) from starving reclaims for months —
       previously reclaim ran only when fresh work ran dry.
    2. **Fresh, draft** then **3. Fresh, conv** — actively-edited writes with
       no summary and no claim. These jump the queue: draft/conv refs have the
       highest ref_ids (most recent), so a plain ``ref_id, ord`` order would
       bury an open write-up behind the ~1M-chunk paper backlog and it would
       never summarise. Split draft-before-conv per the queue order.
    4. **Fresh, hot** — every other never-seen chunk whose ``last_seen`` is
       inside :data:`HOT_WINDOW_MIN` (a document a human just opened in the
       reader, salience-heated via ``bump_salience_for_ref``), ``last_seen
       DESC``. Reuses the dreamer's heat signal so a just-viewed paper is
       summarised first — bounded naturally: a fully-summarised paper has no
       un-summarised chunks to claim, so it can't starve the rest.
    5. **Fresh, rest** — every remaining never-seen chunk, ``ref_id, ord``
       order (a document's chunks stay contiguous so the prompt's shared
       doc-header prefix keeps hitting the llama.cpp prefix cache).
    6. **Reclaim (top-up)** — remaining stale claim rows when fresh runs dry.
       (A row reclaimed in step 1 got ``claimed_at = now()`` so it cannot be
       double-claimed here; a fresh chunk with a claim row is excluded by the
       fresh ``NOT EXISTS``.)
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    _gc_orphaned_leases(conn, summarizer=summarizer)
    reserve = min(2, limit // 8)
    claimed: list[_Claimed] = []
    if reserve:
        claimed += _claim_reclaim(conn, summarizer=summarizer, limit=reserve)
    # Fresh tiers in queue order: draft > conv > hot > rest. The hot tier
    # (a paper a human just opened, salience-heated) jumps ahead of the
    # cold backlog but sits behind the actively-edited draft/conv writes.
    for tier in ("draft", "conv", "hot", "rest"):
        if len(claimed) >= limit:
            break
        claimed += _claim_fresh(
            conn, summarizer=summarizer, limit=limit - len(claimed), tier=tier
        )
    if len(claimed) < limit:
        claimed += _claim_reclaim(
            conn, summarizer=summarizer, limit=limit - len(claimed)
        )
    return claimed


def fetch_doc_card(conn: Any, ref_id: int) -> str:
    """Return the document's header card text (title+authors+abstract+…).

    Reads the ``card_combined`` chunk (one per ref, ``ord < 0``). Empty
    string when the ref has none — the prompt then falls back to the
    bare title. Cache the result per ref in the pass loop; it is the
    shared, cache-hot prefix across all of a document's chunks.
    """
    row = conn.execute(
        """
        SELECT text FROM chunks
         WHERE ref_id = %s AND chunk_kind = 'card_combined'
         ORDER BY ord
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


# ---------------------------------------------------------------------------
# Prompt — stable content first (cache-hot), per-chunk specifics last
# ---------------------------------------------------------------------------


def _kind_noun(ref_kind: str) -> str:
    return {
        "paper": "scientific paper",
        "patent": "patent",
        "conv": "conversation",
    }.get(ref_kind, "document")


#: Layer 1 — the fixed instruction block. Byte-identical for *every* chunk
#: in the corpus (no per-doc/per-chunk interpolation beyond the compile-time
#: ``_BRIEF_MAX_WORDS``), so it stays KV-cache-hot on a llama.cpp slot even
#: across document switches. The document *kind* is deliberately NOT here (it
#: lives in the doc-header, layer 2) so this prefix never changes between a
#: paper, a patent and a conversation. A CACHED module (ADR 0038 §4 helper).
_INSTRUCTION_BLOCK = (
    "You summarize a single passage from a larger document, "
    "as a navigation gloss.\n"
    "Output EXACTLY two lines and nothing else:\n"
    f"BRIEF: <a self-contained gist in one clause, at most {_BRIEF_MAX_WORDS} words>\n"
    "DETAIL: <1-3 terse fragments adding specifics NOT already in BRIEF — "
    "quantities, named entities, method, caveats>\n"
    "DETAIL is always shown appended to BRIEF, never on its own, so it "
    "must read as a continuation and never repeat anything in BRIEF.\n"
    "Be faithful — never invent facts. Write both lines telegraphically: "
    "plain, no preamble, no markdown, and drop leading articles and "
    "pronouns. Spell out abbreviations when standard and unambiguous (keep "
    "unit/element symbols, DNA, pH); never reuse source-only labels. Put a "
    "space between a number and its unit and reproduce quantities verbatim.\n"
    "If the passage is not prose — a data table, coordinate dump, reference "
    "list, or copyright/masthead boilerplate — set BRIEF to a short "
    "parenthetical tag naming it (e.g. (tabular data), (atomic coordinates), "
    "(copyright notice), (publication metadata), (reference list)) and leave "
    "DETAIL empty."
)

#: Layer 1 — the seven few-shot BRIEF/DETAIL pairs (style only). A CACHED
#: module (ADR 0038 §4, "examples as a cached module"); kept verbatim.
_EXAMPLES_BLOCK = (
    "Seven examples (style only — do NOT summarize these):\n"
    "PASSAGE: We synthesized a cobalt complex bearing pendant amine groups "
    "and tested it for proton reduction in acidic acetonitrile. Cyclic "
    "voltammetry and controlled-potential electrolysis gave a turnover "
    "frequency of 12,000 h⁻¹ at 80 °C, roughly threefold the Pd benchmark "
    "under identical conditions, with full activity retained over 200 cycles.\n"
    "BRIEF: cobalt catalyst triples proton-reduction turnover over palladium, "
    "stable to 200 cycles\n"
    "DETAIL: 12,000 h⁻¹ at 80 °C in acidic acetonitrile; rate credited to "
    "pendant-amine proton relays.\n\n"
    "PASSAGE: Reviewing the quarter, we argue the budget shortfall stems "
    "from the Q3 hiring freeze rather than weaker sales. Revenue held flat "
    "against forecast — the top line in Table 2 is essentially unchanged — "
    "so the gap must originate on the cost side.\n"
    "BRIEF: attributes the budget shortfall to the Q3 hiring freeze, not "
    "weaker sales\n"
    "DETAIL: flat revenue vs forecast (unchanged top line, Table 2); gap is "
    "cost-side.\n\n"
    "PASSAGE: Contrary to our hypothesis, daily supplementation produced no "
    "significant change in composite cognitive scores relative to placebo "
    "(p = 0.42). We caution that the trial was underpowered, enrolling only "
    "38 participants, and ran for just eight weeks.\n"
    "BRIEF: supplementation gave no cognitive benefit over placebo, against "
    "the hypothesis\n"
    "DETAIL: non-significant (p = 0.42); underpowered at 38 participants, "
    "eight-week trial.\n\n"
    "PASSAGE: Immediately after collection, samples were flash-frozen in "
    "liquid nitrogen within 30 s to halt metabolic activity, then moved to "
    "long-term storage at −80 °C. Aliquots were thawed on ice only once, "
    "just before analysis, to avoid freeze–thaw degradation.\n"
    "BRIEF: samples flash-frozen then cold-stored to preserve them until "
    "analysis\n"
    "DETAIL: liquid nitrogen within 30 s of collection; stored at −80 °C; "
    "thawed on ice once.\n\n"
    "PASSAGE: Throughout this paper we define resilience as the capacity of "
    "a system to absorb disturbance and reorganize while undergoing change, "
    "so as to still retain essentially the same function, structure, "
    "identity, and feedbacks — departing from engineering notions of return "
    "time to a single equilibrium.\n"
    "BRIEF: defines resilience as absorbing disturbance while keeping core "
    "function\n"
    "DETAIL: also reorganizes yet retains structure, identity, feedbacks; "
    "rejects single-equilibrium view.\n\n"
    "PASSAGE: 4822.296 273.86 10489.05 295511.5 [8,8] 54514 241665 491010 "
    "41.07 354.3621 4309522 6228.624 352.84 13601.7 384269.5 [9,9] 68598 "
    "304587 618714 51.14 443.5323 5437062\n"
    "BRIEF: (tabular data)\n"
    "DETAIL:\n\n"
    "PASSAGE: Nature Energy February 2022 Copyright 2022 The Author(s), "
    "under exclusive licence to Springer Nature Limited. All Rights "
    "Reserved. Section: Pg. 130-143; Vol. 7; No. 2; ISSN: 2058-7546\n"
    "BRIEF: (publication metadata)\n"
    "DETAIL:"
)


def _doc_header_block(ctx: AssemblyContext) -> str:
    """Layer 2 — the per-document header (kind noun + card).

    Constant within a ref, varies between refs. It rides the CACHED
    (``system``) group with the instruction/examples because it is stable
    across a document's chunk-ticks and forms the KV-cache prefix llama.cpp
    reuses; the kind lives *here* (not in layer 1) so layer 1 never
    changes between paper/patent/conv (ADR 0038 §5, Shot 2)."""
    claim: _Claimed = ctx.extras["claim"]
    doc_card: str = ctx.extras["doc_card"]
    noun = _kind_noun(claim.ref_kind)
    header = doc_card.strip() or f"Title: {claim.title}".strip()
    return (
        f"--- Document for context (a {noun}; do not summarize this header) ---\n"
        f"{header}"
    )


def _passage_block(ctx: AssemblyContext) -> str:
    """Layer 3 — the volatile per-chunk material (the ``user`` turn).

    Section path + keywords + quantities + the passage itself. A VARIABLE
    module — the only part that changes chunk-to-chunk within a document."""
    claim: _Claimed = ctx.extras["claim"]
    parts: list[str] = []
    if claim.section_path:
        parts.append("Section: " + " › ".join(claim.section_path))
    if claim.keywords:
        parts.append("Keywords: " + ", ".join(claim.keywords))
    if claim.numerics:
        parts.append("Quantities: " + ", ".join(claim.numerics[:20]))
    prefix = ("\n".join(parts) + "\n\n") if parts else ""
    return f"{prefix}Passage to summarize:\n{claim.text}"


#: The summarizer prompt as an ordered module list (ADR 0038 §2/§4, Shot 2).
#: CACHED (→ ``system``): instruction + examples + doc-header, most-stable
#: first for llama.cpp longest-prefix reuse. VARIABLE (→ ``user``): the
#: per-chunk passage block. The :class:`LiteLLMAdapter` packages them.
_SUMMARIZER_MODULES: list[Module] = [
    Module(
        id="summarizer.instruction",
        layer=Layer.CACHED,
        build=lambda _ctx: _INSTRUCTION_BLOCK,
    ),
    Module(
        id="summarizer.examples",
        layer=Layer.CACHED,
        build=lambda _ctx: _EXAMPLES_BLOCK,
    ),
    Module(
        id="summarizer.doc_header",
        layer=Layer.CACHED,
        build=_doc_header_block,
    ),
    Module(
        id="summarizer.passage",
        layer=Layer.VARIABLE,
        build=_passage_block,
    ),
]


def build_messages(claim: _Claimed, *, doc_card: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one chunk (ADR 0038 step 2).

    Delegates to the shared prompt assembler + :class:`LiteLLMAdapter`:
    :data:`_SUMMARIZER_MODULES` is resolved into layer-tagged blocks and
    the adapter packages them into the ``[system, user]`` OpenAI shape the
    :class:`LlmClient` posts. This reproduces — byte-for-byte — the
    hand-rolled prompt it replaces:

    1. **instruction** (CACHED) — byte-identical for *every* chunk;
    2. **examples** (CACHED) — the seven few-shot pairs;
    3. **doc-header** (CACHED) — kind + card, constant within a ref;
    4. **passage** (VARIABLE) — the per-chunk ``user`` tail.

    The three CACHED blocks form the stable ``system`` prefix (llama.cpp
    KV-cache reuse across a document's chunks); only the VARIABLE passage
    changes between chunks. The document *kind* lives in the doc-header,
    not the instruction, so the layer-1 prefix never changes between a
    paper, a patent and a conversation.
    """
    ctx = AssemblyContext(
        store=None,
        ref_id=claim.ref_id,
        model="summarizer",
        profile=Profile.HELPER,
        extras={"claim": claim, "doc_card": doc_card},
    )
    return LiteLLMAdapter.render(assemble(_SUMMARIZER_MODULES, ctx))


def _sanitize_model_text(text: str) -> str:
    """Strip characters Postgres rejects from model output.

    psycopg refuses NUL (``\\x00``) in text parameters, and a lone UTF-16
    surrogate (a backend truncating mid-astral-char can emit one) fails UTF-8
    encoding at send time. Either used to abort the write — and, pre
    per-chunk transactions, roll back the whole batch. Cheap fast-path: a
    clean string costs one substring scan + one encode.
    """
    if "\x00" in text:
        text = text.replace("\x00", "")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text


class EmptySummaryError(ValueError):
    """The model returned no parseable summary text.

    A *model miss*, not a code bug: the free local ``summarizer`` returns a
    blank/unparseable completion for a sizable fraction of chunks. Raised as a
    distinct type so the pass can record the miss (for the retry cap) without
    logging a per-chunk ERROR traceback — that floods the log surface (7k/day
    on melchior) and reads as "on fire" on /status when the pass is in fact
    working (successes land alongside the misses).
    """


def parse_summary(text: str) -> str:
    """Normalize the model output to ``"<brief>\\n\\n<detail>"``.

    Tolerant of casing and of the model omitting one label. If neither
    label is present we keep the whole thing as the brief (better than
    dropping a faithful-but-unlabelled summary). Raises ``EmptySummaryError``
    on empty output so the pass marks it failed rather than storing a blank.
    Output is sanitized (``_sanitize_model_text``) so it is always safely
    writable.
    """
    raw = _sanitize_model_text(text or "").strip()
    if not raw:
        raise EmptySummaryError("empty summary")
    brief = ""
    detail = ""
    for line in raw.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("brief:"):
            brief = stripped[len("brief:") :].strip()
        elif low.startswith("detail:"):
            detail = stripped[len("detail:") :].strip()
        elif detail:
            detail = f"{detail} {stripped}".strip()
    if not brief and not detail:
        return raw
    if not brief:  # label drift — promote first sentence of detail
        brief = detail.split(". ", 1)[0]
    return f"{brief}\n\n{detail}".strip()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_chunk_summary(
    conn: Any,
    chunk_id: int,
    *,
    summarizer: str,
    text: str,
    prompt_hash: str,
    token_count: int | None,
) -> None:
    """Write the terminal ``ok`` summary and release the chunk's claim."""
    conn.execute(
        """
        INSERT INTO chunk_summaries
            (chunk_id, summarizer, text, prompt_hash, token_count, status)
        VALUES (%s, %s, %s, %s, %s, 'ok')
        ON CONFLICT (chunk_id, summarizer) DO UPDATE
           SET text = EXCLUDED.text,
               prompt_hash = EXCLUDED.prompt_hash,
               token_count = EXCLUDED.token_count,
               status = 'ok',
               last_error = NULL,
               attempts = chunk_summaries.attempts + 1
        """,
        (chunk_id, summarizer, text, prompt_hash, token_count),
    )
    conn.execute(
        "DELETE FROM chunk_claims WHERE chunk_id = %s AND artifact = %s",
        (chunk_id, summarizer),
    )


def _mark_failed(
    conn: Any, chunk_id: int, *, summarizer: str, error: str, transient: bool = False
) -> None:
    """Record one failure (ADR 0007). Bumps the lease's ``attempts`` and keeps
    the claim row (``claimed_at = now()`` = backoff via the cooldown reaper) so
    a transient failure retries. Once ``attempts`` reaches the cap the failure
    is terminal: write a ``failed`` marker to chunk_summaries and DELETE the
    claim, so the poison chunk leaves the claims table and is never re-claimed.

    ``transient=True`` (an empty-completion backend miss) applies the far
    looser ``MAX_SUMMARIZE_EMPTY_ATTEMPTS`` cap instead of the tight
    ``MAX_SUMMARIZE_ATTEMPTS``: a transient backend outage must never drive a
    *summarizable* chunk to a terminal ``failed`` marker after a few unlucky
    windows — that silently drops corpus coverage (the 92k-embeds disease).

    A GONE lease means another worker reaped it (this worker outlived the
    cooldown) and already drove the chunk to a terminal state — usually a
    fresh ``status='ok'`` summary. Write **nothing** then: this used to upsert
    a terminal ``failed`` marker, clobbering that fresh ``ok`` row, and
    nothing ever repaired it (the fresh claim's ``NOT EXISTS`` excludes any
    existing chunk_summaries row regardless of status). Belt-and-braces, the
    terminal upsert below also refuses to overwrite an ``ok`` row."""
    err = _sanitize_model_text((error or "").strip()[:1000])
    row = conn.execute(
        """
        UPDATE chunk_claims SET attempts = attempts + 1, claimed_at = now()
         WHERE chunk_id = %s AND artifact = %s
        RETURNING attempts
        """,
        (chunk_id, summarizer),
    ).fetchone()
    if row is None:
        # Lease reaped by a sibling → the chunk reached a terminal state
        # elsewhere; a stale failure report must not clobber it.
        return
    attempts = int(row[0])
    cap = MAX_SUMMARIZE_EMPTY_ATTEMPTS if transient else MAX_SUMMARIZE_ATTEMPTS
    if attempts < cap:
        return
    conn.execute(
        """
        INSERT INTO chunk_summaries
            (chunk_id, summarizer, status, last_error, attempts)
        VALUES (%s, %s, 'failed', %s, %s)
        ON CONFLICT (chunk_id, summarizer) DO UPDATE
           SET status = 'failed',
               last_error = EXCLUDED.last_error,
               attempts = EXCLUDED.attempts
         WHERE chunk_summaries.status <> 'ok'
        """,
        (chunk_id, summarizer, err, attempts),
    )
    conn.execute(
        "DELETE FROM chunk_claims WHERE chunk_id = %s AND artifact = %s",
        (chunk_id, summarizer),
    )


# ---------------------------------------------------------------------------
# Pass
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    """Per-chunk result of the (parallelisable) completion phase."""

    claim: _Claimed
    prompt_hash: str
    summary: str | None
    token_count: int | None
    error: Exception | None


def _heartbeat_leases(store: Any, chunk_ids: list[int], *, summarizer: str) -> None:
    """Re-stamp ``claimed_at`` on the batch's still-held leases.

    Called from the *main* thread after each per-chunk LLM completion, in its
    own tiny autocommit-style transaction (no lock is ever held across an LLM
    call). This keeps ``SUMMARY_LEASE_COOLDOWN_MIN`` honest regardless of
    batch size: without it the LLM phase's worst case (batch_size × per-call
    timeout) outruns the cooldown and a sibling node reaps — and re-pays —
    chunks a live worker is still processing (and, pre fix, could then have
    its late failure report clobber the sibling's fresh summary). Best-effort:
    a missed heartbeat only risks a benign double-process, so failures are
    logged, never raised.
    """
    if not chunk_ids:
        return
    try:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunk_claims SET claimed_at = now() "
                "WHERE artifact = %s AND chunk_id = ANY(%s)",
                (summarizer, chunk_ids),
            )
    except Exception:  # pragma: no cover - defensive
        log.warning("llm_summarize: lease heartbeat failed", exc_info=True)


def _is_numeric_dump(text: str) -> bool:
    """True if the chunk is mostly digits — a data table or coordinate dump.

    The claim filters cheaply (length/kind) in SQL; this catches the
    numeric/coordinate dumps that aren't worth an LLM call (they only invite
    hallucinated meaning). Cheap pure-Python char count, run on the ≤batch_size
    claimed rows — not on the whole table.
    """
    if not text:
        return False
    return sum(c.isdigit() for c in text) / len(text) > MAX_DIGIT_FRACTION


def run_llm_summarize_pass(
    store: Any,
    *,
    client: LlmClient,
    summarizer: str = SUMMARIZER_NAME,
    batch_size: int = 16,
    concurrency: int = 1,
) -> dict[str, int]:
    """One pass over the LLM-summary queue.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. A poison-pill chunk is
    marked failed and the batch continues (ADR 0007).

    Three phases, each with its own short transaction(s) so **no DB lock or
    xmin snapshot is held across an LLM call** — the long batch-spanning
    transaction was what starved autovacuum on the hot tables:

    1. **Claim** (short txn): lease the batch (``chunk_claims`` rows) and
       prefetch each distinct ref's doc card — the shared, cache-hot prompt
       prefix — then COMMIT, releasing the locks.
    2. **Complete** (no long txn): build prompts and run the LLM calls via a
       thread pool of width ``concurrency``. The worker threads never touch a
       DB connection (psycopg connections are not thread-safe); the main
       thread heartbeats the batch's leases after each completion
       (``_heartbeat_leases``, a tiny txn of its own) so the cooldown reaper
       measures worker *liveness*, not batch length. Width should match the
       backend's ``--parallel`` slot count.
    3. **Write** (one tiny txn *per chunk*): write outcomes in claim order; a
       success writes the summary and DELETEs the lease, a failure bumps the
       lease's attempts (``claimed_at`` is the retry-backoff clock) or goes
       terminal at the cap. Per-chunk transactions on purpose: one poison row
       (e.g. a NUL byte psycopg rejects) used to roll back the whole batch —
       siblings' summaries, failure markers, attempt bumps and lease deletes —
       so the poison chunk retried forever (its attempts never committed, the
       cap never engaged) and the good summaries were re-paid every cycle. A
       write that still fails goes through the normal ``_mark_failed`` path.
       A crashed worker between phases simply leaves leases that the cooldown
       re-claims.
    """
    # Phase 1 — claim + prefetch cards in one short transaction, then commit.
    card_cache: dict[int, str] = {}
    with store.pool.connection() as conn:
        rows = claim_chunks_without_summary(
            conn, summarizer=summarizer, limit=batch_size
        )
        for claim in rows:
            if not _is_numeric_dump(claim.text) and claim.ref_id not in card_cache:
                card_cache[claim.ref_id] = fetch_doc_card(conn, claim.ref_id)
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}
    claimed = len(rows)
    ok = failed = 0

    # Phase 2 — build prompts + LLM completion. No DB transaction is held.
    # Numeric/coordinate dumps skip the LLM entirely (tagged in the write phase).
    numeric_dumps: list[_Claimed] = []
    prepared: list[tuple[_Claimed, list[dict[str, str]], str]] = []
    for claim in rows:
        if _is_numeric_dump(claim.text):
            numeric_dumps.append(claim)
            continue
        messages = build_messages(claim, doc_card=card_cache.get(claim.ref_id, ""))
        prompt_hash = hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode("utf-8")
        ).hexdigest()
        prepared.append((claim, messages, prompt_hash))

    def _complete(item: tuple[_Claimed, list[dict[str, str]], str]) -> _Outcome:
        claim, messages, prompt_hash = item
        empty_exc: EmptySummaryError | None = None
        # Retry an EMPTY completion in-process: it is a transient backend blank
        # (the shared 80B returns "" under contention), and the same input
        # summarizes on replay — so a momentary blip is recovered here instead
        # of deferring a whole cooldown. Runs in a pool worker thread, so the
        # short backoff never blocks the main-thread lease heartbeat.
        for attempt in range(EMPTY_RETRY_ATTEMPTS + 1):
            try:
                result = client.complete(messages)
                summary = parse_summary(result.text)
                return _Outcome(claim, prompt_hash, summary, result.total_tokens, None)
            except EmptySummaryError as exc:
                # A model/backend miss, not a bug — no per-chunk ERROR traceback
                # (it floods the log surface). Recorded below only if *every*
                # retry also comes back empty (a sustained window); the caller
                # emits one aggregated WARNING per batch.
                empty_exc = exc
                if attempt < EMPTY_RETRY_ATTEMPTS:
                    time.sleep(EMPTY_RETRY_BACKOFF_S * (attempt + 1))
            except Exception as exc:  # a genuine error — no retry, recorded below
                log.exception("llm_summarize: chunk_id=%s failed", claim.chunk_id)
                return _Outcome(claim, prompt_hash, None, None, exc)
        return _Outcome(claim, prompt_hash, None, None, empty_exc)

    # The slow phase. ex.map preserves order, so writes stay deterministic.
    # After each completion the MAIN thread heartbeats the batch's leases so
    # the cooldown can stay far below batch_size × timeout. (ex.map yields in
    # submission order, so the gap between heartbeats is bounded by one
    # per-call timeout — well inside the cooldown.)
    batch_ids = [c.chunk_id for c in rows]
    outcomes: list[_Outcome] = []
    if concurrency <= 1:
        for item in prepared:
            outcomes.append(_complete(item))
            _heartbeat_leases(store, batch_ids, summarizer=summarizer)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            for outcome in ex.map(_complete, prepared):
                outcomes.append(outcome)
                _heartbeat_leases(store, batch_ids, summarizer=summarizer)

    # Phase 3 — write outcomes back, one tiny transaction per chunk so a
    # poison row cannot discard its siblings' writes (see the docstring).
    def _record_failure(chunk_id: int, error: str, *, transient: bool = False) -> None:
        try:
            with store.pool.connection() as fconn:
                _mark_failed(
                    fconn,
                    chunk_id,
                    summarizer=summarizer,
                    error=error,
                    transient=transient,
                )
        except Exception:  # keep the batch going; the lease reaper retries it
            log.exception(
                "llm_summarize: failure-marker write failed chunk_id=%s", chunk_id
            )

    def _record_summary(
        chunk_id: int, *, text: str, prompt_hash: str, token_count: int | None
    ) -> bool:
        try:
            with store.pool.connection() as wconn:
                write_chunk_summary(
                    wconn,
                    chunk_id,
                    summarizer=summarizer,
                    text=text,
                    prompt_hash=prompt_hash,
                    token_count=token_count,
                )
            return True
        except Exception as exc:
            log.exception("llm_summarize: summary write failed chunk_id=%s", chunk_id)
            _record_failure(chunk_id, str(exc))
            return False

    for claim in numeric_dumps:
        wrote = _record_summary(
            claim.chunk_id,
            text=NUMERIC_DUMP_TAG,
            prompt_hash=hashlib.sha256(NUMERIC_DUMP_TAG.encode()).hexdigest(),
            token_count=None,
        )
        ok += 1 if wrote else 0
        failed += 0 if wrote else 1
    empty = 0
    for o in outcomes:
        if o.error is not None or o.summary is None:
            is_empty = isinstance(o.error, EmptySummaryError)
            _record_failure(o.claim.chunk_id, str(o.error), transient=is_empty)
            failed += 1
            if is_empty:
                empty += 1
        elif _record_summary(
            o.claim.chunk_id,
            text=o.summary,
            prompt_hash=o.prompt_hash,
            token_count=o.token_count,
        ):
            ok += 1
        else:
            failed += 1
    if empty:
        # One aggregated line per batch instead of a per-chunk ERROR traceback
        # (the free local model misses on a sizable fraction; ~7k/day of ERROR
        # tracebacks on melchior otherwise, which reads as an outage on /status
        # even though summaries are landing). Recorded per chunk above for the
        # retry cap; surfaced here only as a rate.
        log.warning(
            "llm_summarize: %d/%d chunks returned an empty summary "
            "(model miss; recorded for retry)",
            empty,
            len(outcomes),
        )
    return {"claimed": claimed, "ok": ok, "failed": failed}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "SUMMARIZER_NAME",
    "EmptySummaryError",
    "LlmClient",
    "LlmConfig",
    "LlmResult",
    "Transport",
    "build_messages",
    "claim_chunks_without_summary",
    "fetch_doc_card",
    "parse_summary",
    "run_llm_summarize_pass",
    "write_chunk_summary",
]
