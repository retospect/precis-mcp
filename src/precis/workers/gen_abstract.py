"""gen_abstract — a LABELLED generated abstract for papers with none (gr635825).

A paper with no publisher abstract on file (``refs.meta['abstract']``
blank, no ``card_abstract`` chunk) has no summary at all: ``view='abstract'``
answers "no abstract on file" and the card falls back to raw leading body
text. This pass writes one derived ``card_gen_abstract`` chunk per such
paper (ord −1002, one per ref, replaced by DELETE+INSERT) — an
abstract-style summary (aims, method, key results with numbers) generated
from the paper's own body prose.

Contract:

* **Marked generated.** The chunk is its own kind, never ``card_abstract``;
  ``meta.generated = true`` and the readers (``view='abstract'``, the
  overview card) print :data:`GENERATED_LABEL` next to the text so it is
  never mistaken for the published abstract.
* **Never from bylines/mastheads.** The input is built from body
  ``paragraph`` chunks only, dropping end-matter, front-matter furniture
  and any paragraph that is not prose (:func:`body_input`, reusing
  :mod:`precis.workers.context_sentence`'s guards). A body with too little
  prose is never sent to the model — the ref is benched instead — and the
  prompt lets the model decline with :data:`NO_SUMMARY`.
* **The publisher abstract wins.** The claim excludes any ref that has an
  abstract, :func:`precis.ingest.cards.ensure_abstract_card` deletes
  the generated chunk when a real ``card_abstract`` is minted later, and
  :func:`write_gen_abstract` rechecks under the ``refs`` row lock so a
  fill that lands mid-generation is never re-superseded by the pending
  write.
* **Replaces** the retired ``context_sentence`` as the "what is this paper"
  summary for abstract-less papers.

Claim/write shape mirrors :mod:`precis.workers.card_describe`: a
single-runner advisory lock scoped to the claim, a claim-time attempt lease
(:mod:`precis.workers.ref_lease`) that brakes failing refs, a bench after
:data:`_BENCH_AFTER` consecutive failures. Embedding is the embed worker's
job (``card_gen_abstract`` is not a skip kind there).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.ingest.cards import GENERATED_LABEL
from precis.store.advisory import try_xact_advisory_lock
from precis.utils.db_retry import retry_locked
from precis.utils.llm.router import DispatchError
from precis.workers import ref_lease
from precis.workers.context_sentence import (
    _FRONT_MATTER_RE,
    _end_matter,
    usable_context,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "GENERATED_LABEL",
    "GEN_ABSTRACT_VERSION",
    "NO_SUMMARY",
    "body_input",
    "claim_refs_without_abstract",
    "run_gen_abstract_pass",
    "write_gen_abstract",
]

#: Bump when the prompt or input derivation changes so stored summaries
#: are regenerated (a version mismatch re-claims).
GEN_ABSTRACT_VERSION = "1.0"

#: ``chunks.chunk_kind`` of the generated summary (migration 0311).
CHUNK_KIND = "card_gen_abstract"

#: Exact reply the model gives when the body text cannot ground a summary.
NO_SUMMARY = "NO_SUMMARY"

#: Fixed signed-bigint key for the single-runner advisory lock.
_LOCK_KEY = 0x67_65_6E_5F_61_62_73_00 - 2**63

_LEASE_NS = "GENABSTRACT"
_SOURCE = "gen_abstract"

#: Consecutive failures after which a ref is benched for a day.
_BENCH_AFTER = 3
_BENCH_COOLDOWN_MIN = 24 * 60

#: A ref needs at least this many live body paragraphs to be claimed.
_MIN_PARAGRAPHS = 8

#: Input budget (chars): the lead of the body, then the results-bearing
#: sections (results / discussion / conclusion), each capped.
_LEAD_CHARS = 6000
_RESULT_CHARS = 6000
_MIN_INPUT_WORDS = 150

_RESULT_SECTION_RE = re.compile(
    r"result|discussion|conclusion|summary|finding|performance|evaluation",
    re.IGNORECASE,
)

#: Completion bounds for the stored summary.
_MIN_SUMMARY_CHARS = 200
_MAX_SUMMARY_CHARS = 2400
GEN_ABSTRACT_MAX_TOKENS = 900

_INSTRUCTION = (
    "Below is body text extracted from a scientific paper that has no "
    "published abstract on file. Write an abstract-style summary of 120 to "
    "200 words in plain prose: the paper's aims, its method, and its key "
    "results with the specific numbers, units and materials or systems "
    "given in the text. Use only what the text states; never add facts "
    "from memory, never speculate, and write nothing about authors, "
    "affiliations or journals. The TITLE is authoritative: if the text "
    "does not plausibly belong to a paper with that title or is too thin "
    f"to summarise, reply with exactly {NO_SUMMARY} and nothing else. "
    "Reply with only the summary text, no preamble, headings or markdown."
)


def _section_heading(section_path: Any) -> str:
    return " ".join(str(h) for h in (section_path or []))


def body_input(paragraphs: list[tuple[str, Any]], title: str) -> str:
    """The model input assembled from ``(text, section_path)`` body
    paragraphs in reading order, or ``""`` when the body carries too
    little prose to summarise.

    Drops end-matter (references, acknowledgements, citation-dense runs),
    front-matter furniture (licence/correspondence/affiliation lines) and
    any paragraph that does not read as prose
    (:func:`~precis.workers.context_sentence.usable_context`) — so a
    byline or masthead can never reach the model. Budget: the lead of the
    body, then paragraphs under a results-bearing heading."""
    lead: list[str] = []
    results: list[str] = []
    lead_len = result_len = 0
    for text, section_path in paragraphs:
        t = text.strip()
        if (
            not t
            or _end_matter(t, section_path)
            or _FRONT_MATTER_RE.match(t)
            or not usable_context(title, t)
        ):
            continue
        if lead_len < _LEAD_CHARS:
            lead.append(t[: _LEAD_CHARS - lead_len])
            lead_len += len(lead[-1])
        elif result_len < _RESULT_CHARS and _RESULT_SECTION_RE.search(
            _section_heading(section_path)
        ):
            results.append(t[: _RESULT_CHARS - result_len])
            result_len += len(results[-1])
    block = "\n\n".join([*lead, *results])
    if len(block.split()) < _MIN_INPUT_WORDS:
        return ""
    return block


_CLAIM_SQL = (
    """
    SELECT r.ref_id, r.title
      FROM refs r
      LEFT JOIN chunks g
        ON g.ref_id = r.ref_id AND g.chunk_kind = 'card_gen_abstract'
     WHERE r.kind = 'paper'
       AND r.retired_at IS NULL
       AND btrim(coalesce(r.meta->>'abstract', '')) = ''
       AND NOT EXISTS (
            SELECT 1 FROM chunks a
             WHERE a.ref_id = r.ref_id AND a.chunk_kind = 'card_abstract')
       AND (SELECT count(*) FROM (
                SELECT 1 FROM chunks p
                 WHERE p.ref_id = r.ref_id AND p.chunk_kind = 'paragraph'
                   AND p.ord >= 0 AND p.retired_at IS NULL
                 LIMIT %(min_par)s) q) >= %(min_par)s
       AND (g.chunk_id IS NULL OR (g.meta->>'version') IS DISTINCT FROM %(ver)s)
    """
    + ref_lease.exclude_clause("r.ref_id", "lease_ns")
    + """
     ORDER BY r.ref_id
     LIMIT %(limit)s
       FOR UPDATE OF r SKIP LOCKED
    """
)


def claim_refs_without_abstract(
    conn: Connection, *, limit: int
) -> list[tuple[int, str]]:
    """Up to ``limit`` ``(ref_id, title)`` of papers with no publisher
    abstract and no current-version generated one, outside an unexpired
    attempt lease."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = conn.execute(
        _CLAIM_SQL,
        {
            "min_par": _MIN_PARAGRAPHS,
            "ver": GEN_ABSTRACT_VERSION,
            "lease_ns": ref_lease.attempt_ns(_LEASE_NS),
            "limit": limit,
        },
    ).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _load_paragraphs(store: Store, ref_id: int) -> list[tuple[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text, meta, section_path FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'paragraph' AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [
        (str(text or ""), (meta or {}).get("section_path") or section_path)
        for text, meta, section_path in rows
    ]


def clean_summary(raw: str) -> str:
    """Normalise a model reply to the stored summary, or ``""`` to refuse
    it (a decline, a fragment, a chat reply)."""
    text = (raw or "").strip().strip("`").strip()
    text = re.sub(r"^(?:abstract|summary)\s*[:\-–—]\s*", "", text, flags=re.I)
    text = text.strip(" \"'“”")
    if not text or text.upper().rstrip(".") == NO_SUMMARY:
        return ""
    if len(text) < _MIN_SUMMARY_CHARS:
        return ""
    return text[:_MAX_SUMMARY_CHARS]


def generate_abstract(client: Any, *, title: str, block: str) -> tuple[str, str]:
    """One generation call; returns ``(summary, model)``, summary ``""``
    when the model declined or the reply failed :func:`clean_summary`."""
    res = client.complete(
        [
            {
                "role": "user",
                "content": f"{_INSTRUCTION}\n\nTITLE: {title}\n\nBODY TEXT:\n{block}",
            }
        ]
    )
    return clean_summary(str(getattr(res, "text", "") or "")), str(
        getattr(res, "model", "") or ""
    )


def _publisher_abstract_on_file(conn: Connection, ref_id: int) -> bool:
    """Recheck predicate for :func:`write_gen_abstract`, run under the
    ``refs`` row lock: a non-blank ``meta.abstract`` or a ``card_abstract``
    chunk means the publisher abstract won while generation ran."""
    row = conn.execute(
        "SELECT btrim(coalesce(meta->>'abstract', '')) <> '' OR EXISTS ("
        "SELECT 1 FROM chunks a WHERE a.ref_id = refs.ref_id "
        "AND a.chunk_kind = 'card_abstract') "
        "FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    return row is None or bool(row[0])


def write_gen_abstract(store: Store, ref_id: int, *, text: str, model: str) -> bool:
    """DELETE+INSERT the ref's one generated-abstract chunk and clear its
    attempt lease in the same transaction. ``meta`` marks it generated.

    Returns ``False`` — writing nothing but the lease clear — when a
    publisher abstract landed while generation ran. The claim's
    ``FOR UPDATE SKIP LOCKED`` is released at the claim commit, so an
    abstract fill can commit between claim and write
    (:func:`precis.ingest.cards.ensure_abstract_card` then deletes the
    generated chunk — an unlocked insert here would recreate the very
    chunk it removed, leaving a generated summary parked next to the
    published abstract). The ``refs`` row lock serialises with the meta
    write; under READ COMMITTED the recheck then reads its committed
    state. A gone ref reads as superseded too.
    """
    meta = {
        "generated": True,
        "generator": _SOURCE,
        "version": GEN_ABSTRACT_VERSION,
        "model": model,
        "label": GENERATED_LABEL,
    }
    with store.pool.connection() as conn:
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref_id,))
        superseded = _publisher_abstract_on_file(conn, ref_id)
        if superseded:
            log.info(
                "gen_abstract: ref_id=%s gained a publisher abstract "
                "mid-generation — not writing the generated one",
                ref_id,
            )
        else:
            store.chunks._replace_card_chunk(
                conn, ref_id=ref_id, chunk_kind=CHUNK_KIND, text=text, meta=meta
            )
        ref_lease.clear_attempt(store, ref_id, _LEASE_NS, conn=conn)
        conn.commit()
    return not superseded


def _bench(store: Store, ref_id: int, attempts: int) -> None:
    with store.pool.connection() as conn:
        ref_lease.stamp_attempt(
            store,
            ref_id,
            _LEASE_NS,
            conn=conn,
            attempts=attempts,
            ttl_minutes=_BENCH_COOLDOWN_MIN,
        )
        conn.commit()


def _claim_batch(
    store: Store, *, batch_size: int
) -> tuple[list[tuple[int, str]], dict[int, int]]:
    with store.pool.connection() as conn:
        rows = claim_refs_without_abstract(conn, limit=batch_size)
        attempts: dict[int, int] = {}
        for ref_id, _title in rows:
            n = ref_lease.attempt_count(conn, ref_id, _LEASE_NS) + 1
            attempts[ref_id] = n
            ref_lease.stamp_attempt(store, ref_id, _LEASE_NS, conn=conn, attempts=n)
        conn.commit()
    return rows, attempts


def run_gen_abstract_pass(
    store: Store, client: Any, *, batch_size: int = 8
) -> dict[str, Any]:
    """One pass: claim abstract-less papers, generate, write. Returns the
    ``{claimed, ok, failed}`` counts (plus ``skipped`` for refs whose body
    had no usable prose or that gained a publisher abstract mid-generation,
    ``deferred`` when dispatch is paused)."""
    idle: dict[str, Any] = {"claimed": 0, "ok": 0, "failed": 0, "skipped": 0}
    if not store.dsn:
        return idle
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle
        rows, attempts = _claim_batch(store, batch_size=batch_size)
    ok = failed = skipped = 0
    for i, (ref_id, title) in enumerate(rows):
        n = attempts[ref_id]
        try:
            block = body_input(_load_paragraphs(store, ref_id), title)
            if not block:
                # No prose to summarise (masthead-only / scanned junk):
                # the model is never called; bench so it is not re-scanned
                # every cooldown. A grown body re-claims after the window.
                skipped += 1
                _bench(store, ref_id, n)
                continue
            text, model = generate_abstract(client, title=title, block=block)
            if not text:
                raise RuntimeError("model declined or returned an unusable summary")

            def _write(
                ref_id: int = ref_id, text: str = text, model: str = model
            ) -> bool:
                return write_gen_abstract(store, ref_id, text=text, model=model)

            if retry_locked(_write, label=f"gen_abstract ref_id={ref_id}"):
                ok += 1
            else:
                skipped += 1
        except DispatchError as exc:
            if exc.paused:
                reason = "cli_unavailable" if exc.cli_unavailable else str(exc)
                log.warning(
                    "gen_abstract: dispatch paused (%s); deferring %d refs",
                    reason,
                    len(rows) - i,
                )
                with store.pool.connection() as conn:
                    for skipped_id, _t in rows[i + 1 :]:
                        ref_lease.clear_attempt(store, skipped_id, _LEASE_NS, conn=conn)
                    conn.commit()
                return {**idle, "deferred": reason}
            log.exception("gen_abstract: ref_id=%s dispatch failed", ref_id)
            failed += 1
        except Exception as exc:
            log.warning(
                "gen_abstract: ref_id=%s failed: %s: %s",
                ref_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            failed += 1
            if n >= _BENCH_AFTER:
                _bench(store, ref_id, n)
    return {"claimed": len(rows), "ok": ok, "failed": failed, "skipped": skipped}
