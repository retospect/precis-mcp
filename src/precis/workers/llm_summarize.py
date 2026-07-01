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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

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

#: Re-claim a ``status='failed'`` summary while its ``attempts`` is below
#: this — so transient failures (e.g. the 80B returning empty during a
#: cold-load) get retried — but stop once it's clearly a poison chunk, so a
#: permanently-failing one can't re-bill the backend every pass. ``attempts``
#: starts at 1 and increments per write, so this allows ~2 retries.
MAX_SUMMARIZE_ATTEMPTS = 3

#: A ``chunk_claims`` row older than this many minutes is treated as abandoned
#: (the worker crashed or stalled) and re-claimed oldest-first; it is also the
#: retry backoff for failures (which keep their claim). Must comfortably exceed
#: the worst-case batch wall-time (the LLM calls), so set it generously — the
#: occasional double-process is a no-op (idempotent upsert on
#: (chunk_id, summarizer)) and this reproducible background work tolerates rework.
SUMMARY_LEASE_COOLDOWN_MIN = 20

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
    """A completion plus its token accounting."""

    text: str
    total_tokens: int | None


class LlmClient:
    """OpenAI ``/v1/chat/completions`` client for the summarizer alias."""

    def __init__(
        self, config: LlmConfig, *, transport: Transport | None = None
    ) -> None:
        self._config = config
        self._transport: Transport = transport or _UrllibTransport()

    def complete(self, messages: list[dict[str, str]]) -> LlmResult:
        """POST ``messages`` and return the assistant text + usage.

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
        return LlmResult(
            text=str(text), total_tokens=int(total) if total is not None else None
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
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND length(c.text) <= %(max_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
         ORDER BY c.ref_id, c.ord
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

#: Refs whose chunks jump the queue (see ``claim_chunks_without_summary``).
_PRIORITY_KINDS = ("conv", "draft")


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
    conn: Any, *, summarizer: str, limit: int, priority: bool
) -> list[_Claimed]:
    """Claim never-seen chunks (no summary, no claim) for one queue bucket.

    ``priority=False`` covers all other kinds including a NULL ``r.kind`` so the
    two buckets are an exact partition. Writes the ``chunk_claims`` row in the
    same statement (the data-modifying CTE) — the digit-fraction numeric-dump
    filter is applied in Python after the claim, not here (a ``regexp_replace``
    over ~1M rows made the claim ~74s/batch; cheap length/kind filters stay SQL).
    """
    kind_pred = (
        "r.kind IN ('conv', 'draft')"
        if priority
        else "(r.kind <> ALL(ARRAY['conv', 'draft']) OR r.kind IS NULL)"
    )
    rows = conn.execute(
        _FRESH_CLAIM_SQL.format(kind_pred=kind_pred),
        {
            "artifact": summarizer,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
            "max_chars": MAX_CHUNK_CHARS,
            "limit": limit,
        },
    ).fetchall()
    return _rows_to_claims(rows)


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
    with no open transaction. Sources, in order:

    1. **Fresh, priority** — ``conv``/``draft`` chunks with no summary and no
       claim. These jump the queue: draft refs have the highest ref_ids (most
       recent), so a plain ``ref_id, ord`` order would bury an actively-edited
       write-up behind the ~1M-chunk paper backlog and it would never summarise.
    2. **Fresh, rest** — every other never-seen chunk, ``ref_id, ord`` order
       (a document's chunks stay contiguous so the prompt's shared doc-header
       prefix keeps hitting the llama.cpp prefix cache).
    3. **Reclaim** — claim rows past the cooldown (crashed in-flight + retrying
       failures), oldest-first. Tops up only when fresh work runs dry.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    claimed = _claim_fresh(conn, summarizer=summarizer, limit=limit, priority=True)
    if len(claimed) < limit:
        claimed += _claim_fresh(
            conn, summarizer=summarizer, limit=limit - len(claimed), priority=False
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


def build_messages(claim: _Claimed, *, doc_card: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one chunk.

    Three cache layers, ordered most-stable first so llama.cpp's
    longest-matching-prefix reuse pays off:

    1. The **instruction block** — byte-identical for *every* chunk in
       the corpus (no per-doc/per-chunk interpolation), so it stays
       cache-hot on a slot even across document switches.
    2. The **document header** (kind + card) — constant within a ref,
       varies between refs; placed after the instructions.
    3. The **per-chunk** material (section path, keywords, numerics,
       passage) — the volatile ``user`` turn.

    The document *kind* lives in layer 2 (the header line), not in the
    instruction's first line, precisely so layer 1 never changes between a
    paper, a patent and a conversation.
    """
    noun = _kind_noun(claim.ref_kind)
    header = doc_card.strip() or f"Title: {claim.title}".strip()
    system = (
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
        "DETAIL empty.\n\n"
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
        "DETAIL:\n\n"
        f"--- Document for context (a {noun}; do not summarize this header) ---\n"
        f"{header}"
    )

    parts: list[str] = []
    if claim.section_path:
        parts.append("Section: " + " › ".join(claim.section_path))
    if claim.keywords:
        parts.append("Keywords: " + ", ".join(claim.keywords))
    if claim.numerics:
        parts.append("Quantities: " + ", ".join(claim.numerics[:20]))
    prefix = ("\n".join(parts) + "\n\n") if parts else ""
    user = f"{prefix}Passage to summarize:\n{claim.text}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_summary(text: str) -> str:
    """Normalize the model output to ``"<brief>\\n\\n<detail>"``.

    Tolerant of casing and of the model omitting one label. If neither
    label is present we keep the whole thing as the brief (better than
    dropping a faithful-but-unlabelled summary). Raises on empty output
    so the pass marks it failed rather than storing a blank.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty summary")
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


def _mark_failed(conn: Any, chunk_id: int, *, summarizer: str, error: str) -> None:
    """Record one failure (ADR 0007). Bumps the claim's ``attempts`` and keeps
    the claim row (``claimed_at = now()`` = backoff via the cooldown reaper) so
    a transient failure retries. Once ``attempts`` reaches the cap the failure
    is terminal: write a ``failed`` marker to chunk_summaries and DELETE the
    claim, so the poison chunk leaves the claims table and is never re-claimed."""
    err = (error or "").strip()[:1000]
    row = conn.execute(
        """
        UPDATE chunk_claims SET attempts = attempts + 1, claimed_at = now()
         WHERE chunk_id = %s AND artifact = %s
        RETURNING attempts
        """,
        (chunk_id, summarizer),
    ).fetchone()
    # No claim row (already reaped/deleted concurrently) → treat as terminal.
    attempts = int(row[0]) if row else MAX_SUMMARIZE_ATTEMPTS
    if attempts < MAX_SUMMARIZE_ATTEMPTS:
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

    Three phases, each with its own short transaction so **no DB lock or xmin
    snapshot is held across an LLM call** — the long batch-spanning transaction
    was what starved autovacuum on the hot tables:

    1. **Claim** (short txn): lease the batch (stamp ``running``) and prefetch
       each distinct ref's doc card — the shared, cache-hot prompt prefix —
       then COMMIT, releasing the locks.
    2. **Complete** (no txn): build prompts and run the LLM calls via a thread
       pool of width ``concurrency``. Nothing touches a DB connection (psycopg
       connections are not thread-safe). Width should match the backend's
       ``--parallel`` slot count.
    3. **Write** (short txn): write outcomes in claim order; on success the
       ``running`` lease flips to ``ok``, on failure to ``failed`` (its
       ``claimed_at`` is the retry-backoff clock). A crashed worker between
       phases simply leaves a ``running`` lease that the cooldown re-claims.
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
        try:
            result = client.complete(messages)
            summary = parse_summary(result.text)
            return _Outcome(claim, prompt_hash, summary, result.total_tokens, None)
        except Exception as exc:  # recorded per chunk, written below
            log.exception("llm_summarize: chunk_id=%s failed", claim.chunk_id)
            return _Outcome(claim, prompt_hash, None, None, exc)

    # The slow phase. ex.map preserves order, so writes stay deterministic.
    if concurrency <= 1:
        outcomes = [_complete(it) for it in prepared]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            outcomes = list(ex.map(_complete, prepared))

    # Phase 3 — write outcomes back in a fresh short transaction.
    with store.pool.connection() as conn:
        for claim in numeric_dumps:
            write_chunk_summary(
                conn,
                claim.chunk_id,
                summarizer=summarizer,
                text=NUMERIC_DUMP_TAG,
                prompt_hash=hashlib.sha256(NUMERIC_DUMP_TAG.encode()).hexdigest(),
                token_count=None,
            )
            ok += 1
        for o in outcomes:
            if o.error is not None or o.summary is None:
                _mark_failed(
                    conn, o.claim.chunk_id, summarizer=summarizer, error=str(o.error)
                )
                failed += 1
            else:
                write_chunk_summary(
                    conn,
                    o.claim.chunk_id,
                    summarizer=summarizer,
                    text=o.summary,
                    prompt_hash=o.prompt_hash,
                    token_count=o.token_count,
                )
                ok += 1
    return {"claimed": claimed, "ok": ok, "failed": failed}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "SUMMARIZER_NAME",
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
