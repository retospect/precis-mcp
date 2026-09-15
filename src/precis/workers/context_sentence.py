"""``context_sentence`` — one neutral paper-context sentence per source paper.

A grounding quote arrives context-free: a quoted result sentence can be
meaningless without paper-intrinsic context the quote doesn't carry (e.g.
"all numbers here are DFT-computed; no wet-lab work"). This pass writes
ONE neutral, descriptive sentence per paper characterizing its methodology
or evidence type — e.g. "Computational study; properties are
DFT-calculated, no experimental synthesis or measurement." — to
``refs.meta['context_sentence']``. ``refs.meta`` is already an allowlisted
jsonb column (per-column, not per-key — ``tests/test_schema_design.py::
JSONB_COLUMNS``), so no migration/allowlist change is needed. Full spec:
``docs/backlog/paper-context-sentence.md``.

Context, never endorsement: the sentence is descriptive of method/evidence
type only, never a claim-strength assertion. A small word-boundary
blocklist (``proof``/``definitive``/``confirms``/``demonstrates``/
``proves``/``establishes``) plus a ~35-word cap are enforced IN CODE
(:func:`lint_violation`) — the model is not trusted. On a lint violation
the pass regenerates ONCE; a second violation drops the sentence entirely
("no sentence beats a bad one") and stamps
``refs.meta['context_sentence_failed']`` so the paper converges rather
than being re-billed every sweep.

**The input can be the wrong paper.** Few held papers have a
``card_abstract``, so the usual input is the first body chunk — and in a
scanned journal PDF that is often a NEIGHBOURING article's reference list
or front matter, not this paper's prose. Prod's first run wrote
"a review of literature regarding atmospheric chemistry and planetary
science" onto *Solid C60: a new form of carbon*, because ord 0..2 of that
ref are an ozone-chemistry bibliography. The model was not hallucinating;
it described what it was handed. So the prompt makes the TITLE
authoritative and gives the model :data:`NO_CONTEXT` to decline with when
the text can't belong to a paper of that title — a decline is terminal
(:func:`is_decline`), never retried, since a retry that invents a sentence
is the exact failure the hatch exists to stop. A wrong context line is
worse than none: it rides along with someone's literal quote as fact.

Self-contained ref-pass (shaped like ``paper_glossary``/``hub_tagline`` —
DB reads + one outbound LLM call per paper, not a pure ``WorkerHandler``).

**Population (decided, not a corpus-wide sweep):**

1. **Batch backfill** — :func:`backfill_candidate_ref_ids` selects the
   distinct grounding source ``ref_id``s of every LIVE (hub
   ``retired_at IS NULL``) ``nanopub_publish`` row, in ANY state
   (candidate through published/superseded/rejected — the artifact/
   footnote reachability set, not a publication-state filter),
   soft-delete filtered on the hub, the grounding chunk, and the source
   ref. This is also the scheduled pass's default cohort (no explicit
   ``ref_ids``).
2. **Lazy** — ``precis_web.nanopub_render._suggested_payload`` enqueues
   this pass (fire-and-forget, ``ref_ids=[ref_id]``) for a grounding
   source paper missing a sentence the moment it enters a prefill.

Whole-corpus backfill is explicitly out of scope.

**Consumers**: ``precis.nanopub.mint.approve`` freezes a copy into the
approved grounding payload (per-source, mirroring how
``contiguous_group`` is frozen); ``precis.nanopub.assemble`` emits it as
``precis:sourceContext`` when present at mint time; ``precis.export.latex``
renders it under the paper-title line in a hub footnote. Never a search/
card/UI feature — see the "explicitly NOT in scope" section of the spec.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "MAX_WORDS",
    "META_FAILED_KEY",
    "META_KEY",
    "NO_CONTEXT",
    "backfill_candidate_ref_ids",
    "is_decline",
    "lint_violation",
    "propose_context_sentence",
    "run_context_sentence_pass",
]

#: ``refs.meta`` key the finished sentence is written to.
META_KEY = "context_sentence"

#: ``refs.meta`` key stamped when a paper converged on "no sentence" (both
#: the original proposal and its one regenerate failed the lint) — without
#: this a permanently-failing paper would be re-claimed and re-billed on
#: every backfill/scheduler sweep.
META_FAILED_KEY = "context_sentence_failed"

#: Hard word cap (spec: "≤ ~35 words") — enforced in code, not just prompted.
MAX_WORDS = 35

#: Abstract/body-chunk context handed to the model — a header, not the
#: whole paper (mirrors ``paper_glossary._ABSTRACT_CHARS``).
_ABSTRACT_CHARS = 1500

#: Claim-strength words the sentence must never carry — endorsing the
#: finding is a different pass's job (and never this one's); this sentence
#: is context, not a verdict. Word-boundary, case-insensitive: "confirmsX"
#: is not a match, "Confirms" is.
_BLOCKLIST_WORDS: tuple[str, ...] = (
    "proof",
    "definitive",
    "confirms",
    "demonstrates",
    "proves",
    "establishes",
)
_BLOCKLIST_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _BLOCKLIST_WORDS) + r")\b",
    re.IGNORECASE,
)

_QUOTE_CHARS = "\"'`“”‘’"

#: Exact token the model must return when the supplied text can't ground a
#: sentence. Treated as a drop, not a sentence — see :data:`_SYS`.
NO_CONTEXT = "NO_CONTEXT"

_SYS = (
    "You write ONE neutral, descriptive sentence characterizing a scientific "
    "paper's methodology or evidence type -- context a reader needs before "
    "trusting a quote pulled from it (for example: whether a result is "
    "computational, experimental, a review, or a case report). Never assert, "
    "endorse, or judge whether the paper's claims are correct -- describe "
    "the method/evidence type only, in a neutral register. Never use words "
    "like proof, definitive, confirms, demonstrates, proves, or establishes. "
    "At most 35 words, ONE sentence, no preamble, no markdown. Reply with "
    "ONLY the sentence and nothing else.\n\n"
    "The TITLE is authoritative about what the paper is; the supplied text is "
    "machine-extracted and is sometimes NOT this paper's own prose -- a "
    "neighbouring article's reference list, front matter, or an unrelated "
    "column can bleed in. If the text does not plausibly belong to a paper "
    f"with that title, or is too thin to characterize it, reply with exactly "
    f"{NO_CONTEXT} and nothing else. Never infer the subject matter from "
    "text that contradicts the title -- a wrong context line is worse than "
    "none, because it is attached to someone's quote as fact."
)


# ── lint (belt over the LLM) ─────────────────────────────────────────────


def lint_violation(sentence: str) -> str | None:
    """The reason ``sentence`` fails the context-sentence contract, or
    ``None`` when clean. Never raises. Two checks, both code-enforced
    (the model is not trusted): the ~35-word cap, and the claim-strength
    blocklist (word-boundary, case-insensitive — see
    :data:`_BLOCKLIST_WORDS`)."""
    if not sentence or not sentence.strip():
        return "empty"
    words = sentence.split()
    if len(words) > MAX_WORDS:
        return f"over word cap ({len(words)} words)"
    m = _BLOCKLIST_RE.search(sentence)
    if m:
        return f"claim-strength word {m.group(0)!r}"
    return None


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        return text[1:-1].strip()
    return text


def _clean_sentence(raw: str) -> str:
    """First line only (a model occasionally appends a trailing aside
    despite the instruction), quote-stripped, whitespace-collapsed."""
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    return " ".join(_strip_quotes(first_line.strip()).split())


# ── the LLM call ─────────────────────────────────────────────────────────


def _build_messages(title: str, abstract: str) -> list[dict[str, str]]:
    user = f"PAPER TITLE: {title}"
    if abstract:
        user += f"\n\nABSTRACT:\n{abstract[:_ABSTRACT_CHARS]}"
    return [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": user},
    ]


def propose_context_sentence(client: Any, title: str, abstract: str) -> str | None:
    """One proposal call. Returns the cleaned candidate sentence, or
    ``None`` on a dispatch failure / empty completion — never raises (the
    caller counts it as a failed attempt and moves on, same contract as
    ``hub_tagline.propose_tagline``)."""
    try:
        out = client.complete(_build_messages(title, abstract))
    except Exception:
        log.warning("context_sentence: propose call failed", exc_info=True)
        return None
    text = getattr(out, "text", "") or ""
    sentence = _clean_sentence(text)
    return sentence or None


def is_decline(sentence: str) -> bool:
    """True when the model used its :data:`NO_CONTEXT` escape hatch — the
    supplied text didn't plausibly belong to a paper with that title (a
    neighbouring article's reference list bleeding into a scan is the
    common shape). Terminal: never retried, because a retry that invents
    a sentence anyway is exactly the outcome the escape hatch prevents."""
    return sentence.strip().rstrip(".").upper() == NO_CONTEXT


def _generate_with_lint(client: Any, title: str, abstract: str) -> str | None:
    """Propose + lint; on a violation, regenerate ONCE; a second violation
    drops the sentence ("no sentence beats a bad one" — spec). Never
    raises."""
    sentence = propose_context_sentence(client, title, abstract)
    if sentence is not None and is_decline(sentence):
        return None
    if sentence is not None and lint_violation(sentence) is None:
        return sentence
    retry = propose_context_sentence(client, title, abstract)
    if retry is not None and is_decline(retry):
        return None
    if retry is not None and lint_violation(retry) is None:
        return retry
    return None


# ── DB: backfill cohort + claim + write ──────────────────────────────────

#: Distinct grounding source ref_ids of every live nanopub_publish row, any
#: state. ``hub``/``src`` are both soft-delete filtered; the grounding
#: chunk_id must resolve to a live, non-retired chunk.
#:
#: The ``CASE`` around the cast is load-bearing, not ceremony: the frozen
#: payload is reviewer-submitted JSON, and the mint gate only asserts
#: ``int(chunk_id)`` works — which accepts a JSON *float* (``12.0``) that
#: ``'12.0'::bigint`` then rejects. A bare cast would fail the WHOLE query
#: (not skip the row) on one such passage, starving the entire backfill
#: cohort. ``CASE`` fixes evaluation order — a non-integral literal yields
#: NULL and simply doesn't join, where a ``WHERE``-clause regex guard
#: would not be ordering-guaranteed.
_BACKFILL_SQL = r"""
    SELECT DISTINCT c.ref_id
      FROM nanopub_publish p
      JOIN refs hub ON hub.ref_id = p.claim_ref_id AND hub.retired_at IS NULL
      CROSS JOIN LATERAL
           jsonb_array_elements(COALESCE(p.grounding -> 'passages', '[]'::jsonb))
               AS passage
      JOIN chunks c
        ON c.chunk_id = CASE
               WHEN passage ->> 'chunk_id' ~ '^[0-9]+$'
               THEN (passage ->> 'chunk_id')::bigint
           END
       AND c.retired_at IS NULL
      JOIN refs src ON src.ref_id = c.ref_id AND src.retired_at IS NULL
     WHERE p.grounding IS NOT NULL
     ORDER BY c.ref_id
"""


def backfill_candidate_ref_ids(conn: Any) -> list[int]:
    """Distinct grounding source ``ref_id``s of every live ``nanopub_publish``
    row (see :data:`_BACKFILL_SQL`) — the batch-backfill selection, and the
    scheduled pass's default cohort when no explicit ``ref_ids`` are given."""
    rows = conn.execute(_BACKFILL_SQL).fetchall()
    return [int(r[0]) for r in rows]


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None
) -> list[tuple[int, str, str]]:
    """``(ref_id, title, abstract)`` for up to ``limit`` still-unsentenced
    live papers. ``ref_ids``, when given, IS the candidate set (the lazy
    single-paper enqueue, or a targeted backfill/test run); ``None``
    sweeps :func:`backfill_candidate_ref_ids`. Excludes a paper already
    carrying a sentence, or one that converged on "no sentence"
    (:data:`META_FAILED_KEY`)."""
    candidates = ref_ids if ref_ids is not None else backfill_candidate_ref_ids(conn)
    if not candidates:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id, r.title,
               COALESCE(
                   (SELECT text FROM chunks
                     WHERE ref_id = r.ref_id AND chunk_kind = 'card_abstract'
                       AND retired_at IS NULL
                     ORDER BY ord LIMIT 1),
                   (SELECT text FROM chunks
                     WHERE ref_id = r.ref_id AND ord >= 0 AND retired_at IS NULL
                     ORDER BY ord LIMIT 1),
                   ''
               ) AS abstract
          FROM refs r
         WHERE r.ref_id = ANY(%(ids)s)
           AND r.retired_at IS NULL
           AND r.meta->>%(key)s IS NULL
           AND r.meta->>%(failed_key)s IS DISTINCT FROM 'true'
         ORDER BY r.ref_id
         LIMIT %(limit)s
        """,
        {
            "ids": list(candidates),
            "key": META_KEY,
            "failed_key": META_FAILED_KEY,
            "limit": limit,
        },
    ).fetchall()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows]


def run_context_sentence_pass(
    store: Any,
    *,
    client: Any,
    batch_size: int = 8,
    ref_ids: list[int] | None = None,
) -> dict[str, int]:
    """One claim → generate → lint → write cycle. Returns
    ``{claimed, ok, failed}``.

    ``ref_ids`` optionally restricts the sweep to specific papers (lazy
    enqueue / targeted backfill / tests); ``None`` sweeps
    :func:`backfill_candidate_ref_ids` — the live-publish-row grounding
    cohort. Never raises on a single paper's failure; a dropped/lint-failed
    sentence bumps ``failed`` and stamps :data:`META_FAILED_KEY` so the
    paper is not re-claimed forever."""
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    for ref_id, title, abstract in rows:
        try:
            sentence = _generate_with_lint(client, title, abstract)
            if sentence is None:
                store.update_ref(ref_id, meta_patch={META_FAILED_KEY: True})
                failed += 1
                continue
            store.update_ref(ref_id, meta_patch={META_KEY: sentence})
            ok += 1
        except Exception:
            log.exception("context_sentence: failed ref_id=%s", ref_id)
            failed += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed}
