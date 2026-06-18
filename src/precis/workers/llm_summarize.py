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


def claim_chunks_without_summary(
    conn: Any, *, summarizer: str, limit: int
) -> list[_Claimed]:
    """Return up to ``limit`` chunks missing the ``summarizer`` summary.

    Ordered ``ref_id, ord`` so a document's chunks arrive contiguously —
    the prompt's shared doc-header prefix then hits the llama.cpp prefix
    cache turn after turn. ``FOR UPDATE OF c SKIP LOCKED`` lets workers
    on multiple nodes fan out over the corpus without double-processing.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    sql = """
        SELECT c.chunk_id, c.ref_id, c.ord, c.chunk_kind, c.text,
               c.section_path, c.keywords, c.numerics,
               r.kind, r.title
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
          LEFT JOIN chunk_summaries cs
            ON cs.chunk_id = c.chunk_id
           AND cs.summarizer = %s
         WHERE cs.chunk_id IS NULL
           AND c.chunk_kind <> ALL(%s)
           AND length(c.text) >= %s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
         ORDER BY c.ref_id, c.ord
         LIMIT %s
           FOR UPDATE OF c SKIP LOCKED
    """
    rows = conn.execute(
        sql, (summarizer, list(SKIP_KINDS), MIN_CHUNK_CHARS, limit)
    ).fetchall()
    out: list[_Claimed] = []
    for r in rows:
        out.append(
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
        )
    return out


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

    The ``system`` message carries the fixed instruction **and** the
    document header — both identical across every chunk of a document,
    so they form the cache-hot prefix. The per-chunk material (section
    path, keywords, numerics, the passage itself) is the volatile
    ``user`` turn that follows.
    """
    noun = _kind_noun(claim.ref_kind)
    header = doc_card.strip() or f"Title: {claim.title}".strip()
    system = (
        f"You summarize a single passage from a {noun}, for a search index.\n"
        f"Output EXACTLY two lines and nothing else:\n"
        f"BRIEF: <the gist in one clause, at most {_BRIEF_MAX_WORDS} words>\n"
        f"DETAIL: <1-3 sentences adding the specifics: method, finding, "
        f"named entities, and any quantities>\n"
        f"Be faithful to the passage — never invent facts. Write plainly; "
        f"no preamble, no markdown.\n\n"
        f"--- Document for context (do not summarize this header) ---\n"
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
    """Upsert the summary row (same shape as ``RakeLemmaHandler``)."""
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


def _mark_failed(conn: Any, chunk_id: int, *, summarizer: str, error: str) -> None:
    """Failure marker (ADR 0007) — de-claims the chunk for this summarizer."""
    err = (error or "").strip()[:1000]
    conn.execute(
        """
        INSERT INTO chunk_summaries
            (chunk_id, summarizer, status, last_error)
        VALUES (%s, %s, 'failed', %s)
        ON CONFLICT (chunk_id, summarizer) DO UPDATE
           SET status = 'failed',
               last_error = EXCLUDED.last_error,
               attempts = chunk_summaries.attempts + 1
        """,
        (chunk_id, summarizer, err),
    )


# ---------------------------------------------------------------------------
# Pass
# ---------------------------------------------------------------------------


def run_llm_summarize_pass(
    store: Any,
    *,
    client: LlmClient,
    summarizer: str = SUMMARIZER_NAME,
    batch_size: int = 16,
) -> dict[str, int]:
    """One pass over the LLM-summary queue.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. Each chunk is its
    own transaction-of-record via the per-row write; a poison-pill chunk
    is marked failed and the batch continues (ADR 0007). The doc card is
    cached per ref so a document's chunks share both the DB read and the
    cache-hot prompt prefix.
    """
    claimed = ok = failed = 0
    card_cache: dict[int, str] = {}
    with store.pool.connection() as conn:
        rows = claim_chunks_without_summary(
            conn, summarizer=summarizer, limit=batch_size
        )
        claimed = len(rows)
        for claim in rows:
            try:
                if claim.ref_id not in card_cache:
                    card_cache[claim.ref_id] = fetch_doc_card(conn, claim.ref_id)
                messages = build_messages(claim, doc_card=card_cache[claim.ref_id])
                prompt_hash = hashlib.sha256(
                    json.dumps(messages, sort_keys=True).encode("utf-8")
                ).hexdigest()
                result = client.complete(messages)
                summary = parse_summary(result.text)
                write_chunk_summary(
                    conn,
                    claim.chunk_id,
                    summarizer=summarizer,
                    text=summary,
                    prompt_hash=prompt_hash,
                    token_count=result.total_tokens,
                )
                ok += 1
            except Exception as exc:
                log.exception("llm_summarize: chunk_id=%s failed", claim.chunk_id)
                _mark_failed(
                    conn, claim.chunk_id, summarizer=summarizer, error=str(exc)
                )
                failed += 1
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
