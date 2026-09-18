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
``proves``/``establishes``), a ban on attribution-preamble openers ("The
authors report", "This paper presents", "We show", ...), and a ~35-word cap
are enforced IN CODE (:func:`lint_violation`) — the model is not trusted.
On a lint violation
the pass regenerates ONCE; a second violation drops the sentence entirely
("no sentence beats a bad one") and stamps
``refs.meta['context_sentence_failed']`` so the paper converges rather
than being re-billed every sweep. Only a *model verdict* (decline, or two
lint violations) earns that stamp: a sweep where the model never answered
(dispatch failure, empty completion — e.g. the cloud rung rate-limited)
raises :class:`TransientFailure` and leaves the paper untouched for the
next sweep. Prod stamped three papers permanently failed on 2026-09-17
during a rate-limit window before this distinction existed.

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

**The input can also be no paper at all.** The same fallback usually
yields the paper's MASTHEAD — title, byline, affiliations — which has no
method content to characterize: on prod, 788 of the 841 grounding-source
papers have no ``card_abstract`` chunk, and the mastheads score 0.08-0.23
on the prose measure where real abstracts score 0.76-0.94. Asked to
describe one anyway, the model supplies the method from background recall
("NEC Corporation researchers observed helical microtubules ... using
transmission electron microscopy", written from a name and a postal
address), and ``NO_CONTEXT`` does not fire because the text *does* belong
to the paper — it is just empty. :func:`usable_context` therefore refuses
to call the model at all on such a block and stamps the NON-terminal
:data:`META_NO_ABSTRACT_KEY`: the paper needs an abstract, not a retry
(gr346458).

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
    "META_NO_ABSTRACT_KEY",
    "NO_CONTEXT",
    "TransientFailure",
    "backfill_candidate_ref_ids",
    "is_decline",
    "lint_violation",
    "propose_context_sentence",
    "run_context_sentence_pass",
    "usable_context",
]

#: ``refs.meta`` key the finished sentence is written to.
META_KEY = "context_sentence"

#: ``refs.meta`` key stamped when a paper converged on "no sentence" (both
#: the original proposal and its one regenerate failed the lint) — without
#: this a permanently-failing paper would be re-claimed and re-billed on
#: every backfill/scheduler sweep.
META_FAILED_KEY = "context_sentence_failed"

#: ``refs.meta`` key stamped when the paper's context block carried no
#: paper prose to characterize (:func:`usable_context`) — the model was
#: never called, so this is deliberately NOT :data:`META_FAILED_KEY`,
#: which asserts a model verdict. Non-terminal by intent: an abstract
#: backfill that gives the paper real text must clear this key to make it
#: claimable again.
META_NO_ABSTRACT_KEY = "context_sentence_no_abstract"

#: Hard word cap (spec: "≤ ~35 words") — enforced in code, not just prompted.
MAX_WORDS = 35

#: Abstract/body-chunk context handed to the model — a header, not the
#: whole paper (mirrors ``paper_glossary._ABSTRACT_CHARS``).
_ABSTRACT_CHARS = 1500

#: Minimum words the context block must carry BEYOND the paper's own title
#: for a method sentence to be groundable in it, and the minimum share of
#: those words that must be ordinary lowercase prose. A masthead (title +
#: byline + affiliations) clears neither: it is proper nouns, initials and
#: postcodes. Calibrated on the 841 prod grounding-source papers
#: (2026-09-18) — the 53 with a real ``card_abstract`` chunk score 0.76 to
#: 0.94, the first-body-chunk fallbacks have a median of 0.38, and the four
#: mastheads that triggered gr346458 score 0.08 to 0.23. 0.45 sits in that
#: gap, well clear of every genuine abstract in the cohort.
_MIN_CONTEXT_WORDS = 25
_MIN_PROSE_FRACTION = 0.45

#: Markup stripped before the prose measurement — the ``<sup>`` runs around
#: author affiliation marks otherwise read as lowercase prose words and
#: score a byline as an abstract (observed on ref 42558, 0.08 -> 0.40).
_TAG_RE = re.compile(r"<[^>]{1,40}>")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")

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

#: Attribution-preamble openers the sentence must never lead with — the
#: reader already knows the sentence is about the paper, so "The authors
#: report ..." / "This paper presents ..." / "We show ..." is pure filler.
#: The first alternative catches the dodge prod produced once the plain
#: forms were banned ("NEC Corporation researchers observed ..."): an
#: attribution noun anywhere in the first five words. Anchored at the
#: START of the sentence (past optional leading punctuation/quote chars),
#: case-insensitive, word-boundary after the phrase.
_PREAMBLE_RE = re.compile(
    r"^\W*(?:"
    r"(?:[^\s.,;:]+\s+){0,4}(?:researchers|scientists|investigators|co-?workers)"
    r"|the\ authors?"
    r"|this\ (?:paper|study|work|article|report|letter)"
    r"|the\ (?:paper|study|work|article|report|letter)"
    r"|the\ present\ (?:paper|study|work)"
    r"|in\ this\ (?:paper|study|work|article)"
    r"|here(?:,)?\ we"
    r"|we"
    r")\b",
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
    "the method/evidence type only, in a neutral register. State what the "
    "paper did directly, with the method or the measured thing as the "
    "subject of the sentence -- for example: 'Elastic properties and "
    "intrinsic strength were measured by atomic force microscopy.' or "
    "'Helical microtubules of graphitic carbon were observed by "
    "transmission electron microscopy.' Never open with attribution filler "
    "such as 'The authors report', 'This paper presents', 'This study "
    "describes', 'We show', 'Here we report', or 'X researchers observed' "
    "-- no attribution subject at all, not the authors, not a group or "
    "institution name; the reader already knows the sentence is about "
    "the paper. Never use words like proof, "
    "definitive, confirms, demonstrates, proves, or establishes. At most "
    "35 words, ONE sentence, no preamble, no markdown. Reply with ONLY "
    "the sentence and nothing else.\n\n"
    "The TITLE is authoritative about what the paper is; the supplied text is "
    "machine-extracted and is sometimes NOT this paper's own prose -- a "
    "neighbouring article's reference list, front matter, or an unrelated "
    "column can bleed in. If the text does not plausibly belong to a paper "
    f"with that title, or is too thin to characterize it, reply with exactly "
    f"{NO_CONTEXT} and nothing else. Never infer the subject matter from "
    "text that contradicts the title -- a wrong context line is worse than "
    "none, because it is attached to someone's quote as fact."
)


# ── input guard (the model is only as good as what it is fed) ────────────


def usable_context(title: str, abstract: str) -> bool:
    """True when ``abstract`` plausibly carries paper prose a method
    sentence could be grounded in; False for a content-free block.

    :func:`_claim` falls back to the paper's FIRST BODY CHUNK when it has
    no ``card_abstract``, and on prod that fallback is a masthead — the
    title, the byline, the affiliations — for ~94% of the grounding cohort
    (gr346458). A sentence written from a byline is the model's background
    recall, not the paper: prod produced "NEC Corporation researchers
    observed helical microtubules ... using transmission electron
    microscopy" from an input whose only content was the author's name and
    the NEC Tsukuba address. Not calling the model is the only honest
    answer, and it belongs in code rather than the prompt because the
    model's own :data:`NO_CONTEXT` escape hatch — which asks for exactly
    this judgement — demonstrably does not fire on a masthead.

    Heuristic, deliberately conservative: words beyond the title, and the
    share of them that are ordinary lowercase prose (calibration in
    :data:`_MIN_PROSE_FRACTION`). Passing is not a promise the text IS an
    abstract — it only rules out blocks that certainly are not;
    ``NO_CONTEXT`` stays the second line of defence for prose that belongs
    to a different paper."""
    title_words = {w.lower() for w in _WORD_RE.findall(title or "")}
    words = [
        w
        for w in _WORD_RE.findall(_TAG_RE.sub(" ", (abstract or "")[:_ABSTRACT_CHARS]))
        if w.lower() not in title_words
    ]
    if len(words) < _MIN_CONTEXT_WORDS:
        return False
    prose = sum(1 for w in words if w.islower() and len(w) >= 2)
    return prose / len(words) >= _MIN_PROSE_FRACTION


# ── lint (belt over the LLM) ─────────────────────────────────────────────


def lint_violation(sentence: str) -> str | None:
    """The reason ``sentence`` fails the context-sentence contract, or
    ``None`` when clean. Never raises. Three checks, all code-enforced
    (the model is not trusted): the ~35-word cap, the claim-strength
    blocklist (word-boundary, case-insensitive — see
    :data:`_BLOCKLIST_WORDS`), and a ban on leading attribution preamble
    (see :data:`_PREAMBLE_RE`)."""
    if not sentence or not sentence.strip():
        return "empty"
    words = sentence.split()
    if len(words) > MAX_WORDS:
        return f"over word cap ({len(words)} words)"
    m = _BLOCKLIST_RE.search(sentence)
    if m:
        return f"claim-strength word {m.group(0)!r}"
    m = _PREAMBLE_RE.match(sentence)
    if m:
        return f"attribution preamble {m.group(0)!r}"
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


class TransientFailure(RuntimeError):
    """The model never delivered a verdict this sweep (dispatch failure or
    empty completion on the attempts that mattered). The caller must NOT
    stamp :data:`META_FAILED_KEY` — the paper stays claimable and the next
    sweep retries; only a decline or two real lint violations converge."""


def _generate_with_lint(
    client: Any, title: str, abstract: str, *, ref_id: int | None = None
) -> str | None:
    """Propose + lint; on a violation, regenerate ONCE; a second violation
    drops the sentence ("no sentence beats a bad one" — spec) by returning
    ``None``. Raises :class:`TransientFailure` when neither attempt was a
    model verdict — a ``None`` from :func:`propose_context_sentence` is an
    outage, not an opinion, and must not converge the paper.

    Every drop is logged with the rejected text. Without that the pass
    emits only a batch count, and diagnosing a converging paper means
    replaying the claim query and the model call by hand against prod
    (gr346458)."""
    sentence = propose_context_sentence(client, title, abstract)
    if sentence is not None and is_decline(sentence):
        log.info("context_sentence: ref_id=%s model declined (NO_CONTEXT)", ref_id)
        return None
    if sentence is not None and lint_violation(sentence) is None:
        return sentence
    if sentence is not None:
        log.info(
            "context_sentence: ref_id=%s lint rejected %r (%s); regenerating",
            ref_id,
            sentence,
            lint_violation(sentence),
        )
    retry = propose_context_sentence(client, title, abstract)
    if retry is not None and is_decline(retry):
        log.info(
            "context_sentence: ref_id=%s model declined (NO_CONTEXT) on retry", ref_id
        )
        return None
    if retry is not None and lint_violation(retry) is None:
        return retry
    if sentence is not None and retry is not None:
        log.warning(
            "context_sentence: ref_id=%s dropped — lint rejected both attempts "
            "(%r: %s | %r: %s)",
            ref_id,
            sentence,
            lint_violation(sentence),
            retry,
            lint_violation(retry),
        )
        return None  # two real lint violations — the model's verdict
    raise TransientFailure("no model reply this sweep")


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
    carrying a sentence, one that converged on "no sentence"
    (:data:`META_FAILED_KEY`), and one whose context block had nothing to
    work with (:data:`META_NO_ABSTRACT_KEY`)."""
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
           AND r.meta->>%(no_abstract_key)s IS DISTINCT FROM 'true'
         ORDER BY r.ref_id
         LIMIT %(limit)s
        """,
        {
            "ids": list(candidates),
            "key": META_KEY,
            "failed_key": META_FAILED_KEY,
            "no_abstract_key": META_NO_ABSTRACT_KEY,
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
    paper is not re-claimed forever.

    Two outcomes converge a paper WITHOUT a model verdict and so stay out
    of ``failed``, which counts verdicts: a content-free context block
    (:func:`usable_context`) stamps :data:`META_NO_ABSTRACT_KEY` before any
    call, and a sweep the model never answered
    (:class:`TransientFailure`) stamps nothing at all. Both are logged."""
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = deferred = no_abstract = 0
    for ref_id, title, abstract in rows:
        try:
            if not usable_context(title, abstract):
                # No paper prose to characterize — calling the model here
                # buys background recall dressed as provenance (gr346458).
                store.update_ref(ref_id, meta_patch={META_NO_ABSTRACT_KEY: True})
                no_abstract += 1
                continue
            try:
                sentence = _generate_with_lint(client, title, abstract, ref_id=ref_id)
            except TransientFailure:
                deferred += 1
                continue
            if sentence is None:
                store.update_ref(ref_id, meta_patch={META_FAILED_KEY: True})
                failed += 1
                continue
            store.update_ref(ref_id, meta_patch={META_KEY: sentence})
            ok += 1
        except Exception:
            log.exception("context_sentence: failed ref_id=%s", ref_id)
            failed += 1
    if deferred:
        log.warning(
            "context_sentence: %d paper(s) deferred — no model reply "
            "(rate limit / dispatch failure); left unstamped for the next sweep",
            deferred,
        )
    if no_abstract:
        log.info(
            "context_sentence: %d paper(s) skipped — context block carries no "
            "paper prose (masthead/front matter); needs an abstract backfill, "
            "not a retry",
            no_abstract,
        )
    return {"claimed": len(rows), "ok": ok, "failed": failed}
