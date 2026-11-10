"""Read-only evidence for the current memory revision (gr563700).

One ref-scoped SELECT joins current body/header chunks to their artifacts and
claims. It neither scans the corpus nor polls jobs: bulk jobs cannot attest
that this ref is queued. Claims survive crashes/backoff, so they are evidence
within ``pending``, never proof of execution. Artifact ``created_at`` is the
first insertion time, not the last completion time (upserts preserve it).

Native memory rewrites replace chunk IDs. An artifact must match that ID and
its nullable content_sha; NULL hashes use append-only chunk identity, not a
claim that NULL hashes certify arbitrary mutable text. The displayed SHA256
of current text makes the compared revision explicit. Header text is also
compared with the current title/hook. Move categorization only records ref
tags, with no input revision: an older marker is demonstrably stale, and a
newer one still cannot certify the current body across a concurrent edit.
Service switches are deliberately not treated as fleet-wide disable proof:
host overrides and derived_drain can run outside the standing pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import Error as DatabaseError
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from precis.errors import NotFound, Upstream
from precis.store import Store
from precis.store._chunks_ops import header_card_text
from precis.workers.chunk_keywords import KEYWORDS_VERSION
from precis.workers.chunk_keywords import MIN_CHUNK_CHARS as KEYWORD_MIN_CHARS
from precis.workers.chunk_keywords import SKIP_KINDS as KEYWORD_SKIP_KINDS
from precis.workers.embed import EmbedHandler
from precis.workers.llm_summarize import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    SUMMARIZER_NAME,
)
from precis.workers.llm_summarize import (
    SKIP_KINDS as LLM_SKIP_KINDS,
)
from precis.workers.summarize import RakeLemmaHandler

# Fixed aliases, never caller-controlled SQL. Each artifact and claim join is
# keyed by this ref's chunk IDs; the number of statements stays one.
_ARTIFACT_COLUMNS = ",\n".join(
    f"{alias}.{column} AS {alias}_{column}"
    for alias in ("embedding", "rake", "llm")
    for column in ("status", "content_sha", "created_at", "last_error", "attempts")
)
_CLAIM_COLUMNS = ",\n".join(
    f"{alias}_claim.{column} AS {alias}_{label}"
    for alias in ("embedding", "rake", "llm")
    for column, label in (("claimed_at", "claimed_at"), ("attempts", "claim_attempts"))
)

_SNAPSHOT_SQL = f"""
WITH focus AS MATERIALIZED (
    SELECT r.ref_id, r.title, r.meta->>'hook' AS hook, r.updated_at,
           statement_timestamp() AS observed_at,
           (SELECT name FROM embedders WHERE is_default ORDER BY name LIMIT 1)
               AS embedder,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'namespace', t.namespace, 'value', t.value,
                   'created_at', rt.created_at, 'expires_at', rt.expires_at))
               FROM ref_tags rt JOIN tags t USING (tag_id)
               WHERE rt.ref_id = r.ref_id AND t.namespace IN
                   ('DREAM', 'MOVE', 'MOVECASCADE', 'MOVECASCADEATTEMPT')
           ), '[]'::jsonb) AS processing_tags
    FROM refs r
    WHERE r.ref_id = %(ref_id)s AND r.kind = 'memory' AND r.retired_at IS NULL
)
SELECT focus.*, c.chunk_id, c.chunk_kind, c.ord, c.text, c.content_sha,
       c.created_at, c.meta->>'no_index' AS no_index,
       c.keywords, c.keywords_meta,
       embedding.vector IS NOT NULL AS has_vector,
       rake.text IS NOT NULL AS rake_has_text,
       llm.text IS NOT NULL AS llm_has_text,
       {_ARTIFACT_COLUMNS}, {_CLAIM_COLUMNS}
FROM focus
LEFT JOIN chunks c ON c.ref_id = focus.ref_id AND c.retired_at IS NULL
    AND (c.chunk_kind = 'memory_body'
         OR (c.chunk_kind = 'card_combined' AND c.ord = -1))
LEFT JOIN chunk_embeddings embedding
    ON embedding.chunk_id = c.chunk_id AND embedding.embedder = focus.embedder
LEFT JOIN chunk_summaries rake
    ON rake.chunk_id = c.chunk_id AND rake.summarizer = 'rake-lemma'
LEFT JOIN chunk_summaries llm
    ON llm.chunk_id = c.chunk_id AND llm.summarizer = %(summarizer)s
LEFT JOIN chunk_claims embedding_claim
    ON embedding_claim.chunk_id = c.chunk_id AND embedding_claim.artifact = focus.embedder
LEFT JOIN chunk_claims rake_claim
    ON rake_claim.chunk_id = c.chunk_id AND rake_claim.artifact = 'rake-lemma'
LEFT JOIN chunk_claims llm_claim
    ON llm_claim.chunk_id = c.chunk_id AND llm_claim.artifact = %(summarizer)s
ORDER BY c.chunk_id
"""


@dataclass(frozen=True)
class Stage:
    name: str
    state: str
    detail: str
    outstanding_since: datetime | None = None


@dataclass(frozen=True)
class ProcessingReport:
    ref_id: int
    observed_at: datetime
    embedder: str | None
    revisions: list[str]
    stages: list[Stage]

    @property
    def outstanding(self) -> bool:
        return any(s.state in {"pending", "failed", "disabled"} for s in self.stages)

    def render(self) -> str:
        lines = [
            f"memory me{self.ref_id} processing — observed {_stamp(self.observed_at)}",
            f"Active embedder: {self.embedder or 'none registered'}",
            "Compared: current live memory_body chunk IDs/content_sha and text SHA256; "
            "header card additionally equals current refs.title + meta.hook.",
            *self.revisions,
        ]
        outstanding = [
            s.outstanding_since for s in self.stages if s.outstanding_since is not None
        ]
        if outstanding:
            oldest = min(outstanding)
            age = max(0, int((self.observed_at - oldest).total_seconds()))
            lines.append(
                f"Oldest outstanding current item: {_stamp(oldest)} (age {age}s). "
                "Age starts at chunk creation, not queue admission."
            )
        lines.extend(f"{s.name}: {s.state} — {s.detail}" for s in self.stages)
        lines.append(
            "Queue policy (gr563702): memory chunks claim in each stage's "
            "interactive lane ahead of the bulk corpus backlog; every claim "
            "batch still reserves a floor of slots for bulk work, so a "
            "pending memory waits for a pass tick or an interactive burst, "
            "never for the paper backlog to drain. phase=queued means "
            "eligible and awaiting a worker claim; phase=claimed or "
            "phase=attempt-lease means a worker lease is out — executing or "
            "abandoned, which stored evidence cannot distinguish; "
            "phase=awaiting-embedding means the keyword claim's current-"
            "embedding prerequisite has not landed — the stage is not yet "
            "eligible and holds no lane position. Keyword failures "
            "and completion times are not persisted. Artifact created_at is "
            "first insertion, not last completion. Worker enablement outside "
            "this DB is not inferred."
        )
        return "\n".join(lines)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tag_time(tag: dict[str, Any], key: str) -> datetime | None:
    value = tag.get(key)
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _revision(row: dict[str, Any]) -> str:
    digest = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
    return (
        f"{row['chunk_kind']} pc{row['chunk_id']}: text_sha256={digest}; "
        f"content_sha={row['content_sha'] or 'NULL (append-only chunk identity)'}; "
        f"created_at={_stamp(row['created_at'])}"
    )


def _artifact(row: dict[str, Any], alias: str, owner: str) -> Stage:
    name = f"{owner} / {row['chunk_kind']} pc{row['chunk_id']}"
    created = row["created_at"]
    if row["no_index"] == "true":
        return Stage(name, "disabled", "chunk meta.no_index=true", created)
    if alias == "embedding" and row["embedder"] is None:
        return Stage(name, "disabled", "no default embedder registered", created)
    if alias == "llm" and (
        row["chunk_kind"] != "memory_body"
        or not MIN_CHUNK_CHARS <= len(row["text"]) <= MAX_CHUNK_CHARS
    ):
        return Stage(
            name,
            "not applicable",
            f"llm_summarize accepts body text of {MIN_CHUNK_CHARS}"
            f"–{MAX_CHUNK_CHARS} characters; this has {len(row['text'])}",
        )
    if row["chunk_kind"] == "card_combined" and row["text"] != header_card_text(
        row["title"], row["hook"]
    ):
        return Stage(
            name,
            "pending",
            "stored card differs from current refs.title + meta.hook; "
            "its artifact does not count; next: ask the operator to repair the header card",
            created,
        )
    status = row[f"{alias}_status"]
    sha = row[f"{alias}_content_sha"]
    current = sha == row["content_sha"]
    # Base worker failure rows have no input hash. On append-only memory
    # chunks the current ID still identifies the failed revision.
    if status == "failed" and (current or sha is None):
        state = "failed"
    elif (
        status == "ok"
        and current
        and row["has_vector" if alias == "embedding" else f"{alias}_has_text"]
    ):
        state = "complete"
    else:
        state = "pending"
    detail = f"worker={owner}; artifact content_sha={sha or 'NULL'}"
    if status is not None and not current:
        detail += "; artifact hash differs from current chunk"
    if row[f"{alias}_created_at"] is not None:
        detail += f"; artifact_created_at={_stamp(row[f'{alias}_created_at'])}"
        detail += f"; attempts={row[f'{alias}_attempts']}"
    if state == "pending":
        # Eligibility vs queueing vs execution (gr563702): a live
        # chunk_claims row is the only execution evidence a lease table
        # offers — running now or abandoned, never distinguishable.
        detail += (
            "; phase=claimed (worker lease held — executing or abandoned; "
            "re-opens on its ~20 min cooldown)"
            if row[f"{alias}_claimed_at"] is not None
            else "; phase=queued (eligible, awaiting a worker claim)"
        )
    if row[f"{alias}_claimed_at"] is not None:
        detail += f"; claimed_at={_stamp(row[f'{alias}_claimed_at'])}"
        detail += f"; claim_attempts={row[f'{alias}_claim_attempts']}"
    if state == "failed":
        # Exceptions may contain newlines; keep each stage a single readable line.
        error = " ".join(str(row[f"{alias}_last_error"] or "no error recorded").split())
        detail += f"; error={error[:500]}; next: ask the worker operator to inspect "
        detail += f"{owner} for pc{row['chunk_id']} and arrange a targeted retry, "
        detail += "then recheck view='processing' before recall"
    return Stage(name, state, detail, None if state == "complete" else created)


def _keywords(row: dict[str, Any]) -> Stage:
    name = f"keywords / memory_body pc{row['chunk_id']}"
    if row["no_index"] == "true":
        return Stage(name, "disabled", "chunk meta.no_index=true", row["created_at"])
    if len(row["text"]) < KEYWORD_MIN_CHARS:
        return Stage(
            name, "not applicable", f"body shorter than {KEYWORD_MIN_CHARS} characters"
        )
    meta = row["keywords_meta"] or {}
    complete = (
        row["keywords"] is not None
        and meta.get("version") == KEYWORDS_VERSION
        and meta.get("content_sha") == row["content_sha"]
    )
    detail = (
        f"worker=chunk_keywords; keywords_meta.version={meta.get('version')!r}; "
        f"keywords_meta.content_sha={meta.get('content_sha')!r}; "
        "compared with current body content_sha; requires current body embedding"
    )
    if not complete:
        # The claim's gate (claim_chunks_without_keywords): an ok, current
        # body embedding must exist or the chunk is not claimable — an
        # eligibility delay, not a lane position.
        embedding_current = (
            row["has_vector"]
            and row["embedding_status"] == "ok"
            and row["embedding_content_sha"] == row["content_sha"]
        )
        detail += (
            "; phase=queued (eligible, awaiting a worker claim; "
            "the keyword pass holds no durable lease)"
            if embedding_current
            else "; phase=awaiting-embedding (eligibility delay — the "
            "claim gates on a current body embedding, so the stage holds "
            "no lane position until the vector lands)"
        )
    return Stage(
        name,
        "complete" if complete else "pending",
        detail,
        None if complete else row["created_at"],
    )


def _categorization(focus: dict[str, Any], bodies: list[dict[str, Any]]) -> Stage:
    tags = focus["processing_tags"]
    # Match axis_pass._applies_when_clauses: its tags_any predicate includes
    # expired ref tags. This read describes that worker without changing it.
    if not bodies or not any(
        t["namespace"] == "DREAM" and t["value"] == "speculative" for t in tags
    ):
        return Stage(
            "categorization",
            "not applicable",
            "axis:move requires DREAM:speculative and a live body; "
            "chunk axes require paragraphs, not memory_body",
        )
    since = min(b["created_at"] for b in bodies)
    markers = [t for t in tags if t["namespace"] == "MOVECASCADE"]
    details = [
        "worker=axis:move; current-body revision cannot be certified by ref tags"
    ]
    if any(
        t["namespace"] == "MOVECASCADEATTEMPT"
        and (at := _tag_time(t, "expires_at")) is not None
        and at > focus["observed_at"]
        for t in tags
    ):
        details.append(
            "phase=attempt-lease (a classify attempt is running or in its "
            "~60 min retry brake)"
        )
    else:
        details.append(
            "phase=queued (axis:move sweeps kind='memory' refs only — a "
            "memory-scoped queue, no paper backlog shares it)"
        )
    for tag in tags:
        if tag["namespace"] not in {"MOVECASCADE", "MOVECASCADEATTEMPT"}:
            continue
        at = _tag_time(tag, "created_at")
        details.append(
            f"{tag['namespace']}:{tag['value']} created_at={_stamp(at) if at else 'unknown'}"
            f" expires_at={tag.get('expires_at') or 'none'}"
        )
        if (
            tag["namespace"] == "MOVECASCADE"
            and at is not None
            and at < max(b["created_at"] for b in bodies)
        ):
            details.append("marker predates current body: old revision does not count")
    if markers:
        details.append(
            "no input hash stored; a matching-version marker blocks automatic "
            "reclassification; next: ask the categorizer operator to review current "
            "body versus marker before arranging reclassification"
        )
    elif any(t["namespace"] == "MOVE" for t in tags):
        return Stage(
            "categorization",
            "disabled",
            "axis:move preserves a MOVE value supplied without a cascade marker; "
            "no current-body classification evidence",
            since,
        )
    return Stage("categorization", "pending", "; ".join(details), since)


#: The lane a pending stage claims from (gr563702) — the *effective
#: priority* the memory-first scheduling gives this ref at that stage.
_LANES = {
    "embedding": "lane=memory (interactive; claims before finding/draft/conv "
    "and the paragraph/structured/other corpus tiers)",
    "rake": "lane=memory (interactive; claims before the corpus FIFO)",
    "llm": "lane=memory (interactive; claims before draft/conv/hot/rest)",
    "keywords": "lane=interactive tier 1 (conv/draft/memory refs, "
    "newest-first; before the fresh and stale backlogs)",
}

#: Chunks ahead of a queued memory chunk in its stage's claim order — the
#: same pending-work predicate and lane ordering each worker's claim
#: applies, scoped to that lane's kinds. ``{pending}`` / ``{extra}`` are
#: fixed fragments per stage, never caller text.
_AHEAD_SQL = """
    SELECT count(*) AS n
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
     WHERE r.kind = %(kinds)s AND r.retired_at IS NULL AND c.retired_at IS NULL
       AND c.chunk_id <> %(cid)s
       AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
       AND c.chunk_kind <> ALL(%(skip)s)
       {extra}
       AND NOT EXISTS (
             SELECT 1 FROM {table} o
              WHERE o.chunk_id = c.chunk_id AND o.{col} = %(model)s
                AND ({pending})
           )
       AND (c.ref_id < %(ref)s OR (c.ref_id = %(ref)s AND c.ord < %(ord)s))
"""

#: The keyword stage's variant: the lane spans conv/draft/memory refs and
#: orders ``chunk_id DESC``, so 'ahead' is a higher chunk_id, and work
#: counts only once a current body embedding exists (the claim's gate).
_AHEAD_KEYWORDS_SQL = """
    SELECT count(*) AS n
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
     WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL
       AND c.retired_at IS NULL AND c.chunk_id > %(cid)s
       AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
       AND c.chunk_kind <> ALL(%(skip)s)
       AND length(c.text) >= %(minc)s
       AND (c.keywords IS NULL
            OR (c.keywords_meta->>'version') IS DISTINCT FROM %(kwv)s
            OR (c.keywords_meta->>'content_sha') IS DISTINCT FROM c.content_sha)
       AND EXISTS (
             SELECT 1 FROM chunk_embeddings ce
              WHERE ce.chunk_id = c.chunk_id AND ce.embedder = 'bge-m3'
                AND ce.status = 'ok' AND ce.vector IS NOT NULL
                AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
           )
"""


def _queue_ahead(
    cur: Any, focus: dict[str, Any], row: dict[str, Any], alias: str
) -> int | None:
    """Chunks the stage's lane would claim before ``row`` — the position.

    ``None`` when the figure can't be stated (no default embedder for the
    keyword gate, or an unknown stage). Approximate: live claims and
    locked rows are included — they are still work ahead."""
    ref_id, ord_ = int(focus["ref_id"]), int(row["ord"] or 0)
    if alias == "keywords":
        if focus["embedder"] is None:
            return None
        hit = cur.execute(
            _AHEAD_KEYWORDS_SQL,
            {
                "kinds": ["conv", "draft", "memory"],
                "cid": int(row["chunk_id"]),
                "skip": list(KEYWORD_SKIP_KINDS),
                "minc": KEYWORD_MIN_CHARS,
                "kwv": KEYWORDS_VERSION,
            },
        ).fetchone()
        return int(hit["n"]) if hit else None
    if focus["embedder"] is None:
        return None
    if alias == "embedding":
        sql = _AHEAD_SQL.format(
            table="chunk_embeddings",
            col="embedder",
            pending="o.status = 'failed' "
            "OR o.content_sha IS NOT DISTINCT FROM c.content_sha",
            extra="",
        )
        params: dict[str, Any] = {
            "model": focus["embedder"],
            "skip": list(EmbedHandler.skip_chunk_kinds),
        }
    elif alias == "llm":
        sql = _AHEAD_SQL.format(
            table="chunk_summaries",
            col="summarizer",
            pending="o.content_sha IS NOT DISTINCT FROM c.content_sha "
            "OR (o.status = 'failed' AND o.content_sha IS NULL)",
            extra="AND length(c.text) >= %(minc)s AND length(c.text) <= %(maxc)s",
        )
        params = {
            "model": SUMMARIZER_NAME,
            "skip": list(LLM_SKIP_KINDS),
            "minc": MIN_CHUNK_CHARS,
            "maxc": MAX_CHUNK_CHARS,
        }
    elif alias == "rake":
        sql = _AHEAD_SQL.format(
            table="chunk_summaries",
            col="summarizer",
            pending="o.status = 'failed' "
            "OR o.content_sha IS NOT DISTINCT FROM c.content_sha",
            extra="",
        )
        params = {
            "model": "rake-lemma",
            "skip": list(RakeLemmaHandler.skip_chunk_kinds),
        }
    else:
        return None
    hit = cur.execute(
        sql,
        {
            "kinds": "memory",
            "ref": ref_id,
            "ord": ord_,
            "cid": int(row["chunk_id"]),
            **params,
        },
    ).fetchone()
    return int(hit["n"]) if hit else None


def processing_report(store: Store, ref_id: int) -> ProcessingReport:
    """Observe one statement snapshot without touching salience or scheduling."""
    try:
        with (
            store.pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            rows = cur.execute(
                _SNAPSHOT_SQL, {"ref_id": ref_id, "summarizer": SUMMARIZER_NAME}
            ).fetchall()
            if not rows:
                raise NotFound(
                    f"live memory me{ref_id} not found",
                    next="search(kind='memory', q='<topic>') for a live memory",
                )
            focus = rows[0]
            chunks = [r for r in rows if r["chunk_id"] is not None]
            bodies = [r for r in chunks if r["chunk_kind"] == "memory_body"]
            cards = [r for r in chunks if r["chunk_kind"] == "card_combined"]
            expected_header = header_card_text(focus["title"], focus["hook"])
            stages = [_keywords(r) for r in bodies]
            lanes: list[tuple[int, dict[str, Any], str]] = [
                (i, r, "keywords") for i, r in enumerate(bodies)
            ]
            if not bodies:
                stages.append(
                    Stage("keywords", "not applicable", "no live memory_body")
                )
                stages.append(
                    Stage(
                        "embeddings / memory_body",
                        "pending",
                        "body chunk missing",
                        focus["updated_at"],
                    )
                )
            for row in chunks:
                lanes.append((len(stages), row, "embedding"))
                stages.append(_artifact(row, "embedding", f"embed:{focus['embedder']}"))
            if not cards:
                stages.append(
                    Stage(
                        "embeddings / title-hook card",
                        "pending" if expected_header else "not applicable",
                        "header card missing"
                        if expected_header
                        else "title and hook are empty",
                        focus["updated_at"] if expected_header else None,
                    )
                )
            for row in chunks:
                lanes.append((len(stages), row, "rake"))
                stages.append(_artifact(row, "rake", "summary/summarize:rake-lemma"))
            for row in bodies:
                lanes.append((len(stages), row, "llm"))
                stages.append(_artifact(row, "llm", "summary/llm_summarize"))
            if not bodies:
                stages.append(
                    Stage(
                        "summary/llm_summarize",
                        "not applicable",
                        "no live memory_body",
                    )
                )
            # Annotate pending unclaimed chunk stages with their lane and
            # queue position (gr563702): eligibility vs queueing vs
            # execution — a ``phase=queued`` stage's ``queue_ahead`` counts
            # the work its lane claims first.
            for i, row, alias in lanes:
                stage = stages[i]
                if stage.state != "pending" or "phase=queued" not in stage.detail:
                    continue
                ahead = _queue_ahead(cur, focus, row, alias)
                note = f"; {_LANES[alias]}"
                if ahead is not None:
                    note += f"; queue_ahead≈{ahead} chunk(s) in this lane"
                stages[i] = Stage(
                    stage.name,
                    stage.state,
                    stage.detail + note,
                    stage.outstanding_since,
                )
            stages.append(_categorization(focus, bodies))
            return ProcessingReport(
                ref_id,
                focus["observed_at"],
                focus["embedder"],
                [_revision(r) for r in chunks],
                stages,
            )
    except (DatabaseError, PoolTimeout) as exc:
        raise Upstream(
            "memory processing evidence could not be read",
            next=f"retry get(kind='memory', id={ref_id}, view='processing')",
        ) from exc
