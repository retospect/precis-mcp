"""Taproot atomic-claims migration runner — Phase 0 (score) + Phase 1
(dry-run), both **strictly read-only**. Design: ``docs/backlog/
taproot-atomic-claims.md`` §Strategy; the Phase-1 pilot findings that
motivated the gates below: ``docs/backlog/
taproot-migration-extraction-quality-gates.md``. Phase 2 (apply) and Phase
3 (human review) are not built here.

**Phase 0 — :func:`score_hubs`.** A deterministic, pure-function
compoundness score over every live claim hub's **claim sentence** — the
``finding_body`` chunk at ``ord=0``, falling back to the hub's ``title``
when that chunk is missing/empty (LEFT JOINed in :data:`_CANDIDATE_HUBS_SQL`;
the same string :func:`dry_run` extracts, single source per the P0-1 fix —
title-only scoring under-called ~19% of the population compound because
short "topic" titles hide a compound body). No model, no ANN:
:func:`score_sentence` counts conjunctions (" and " / " but " / " while ",
word-bounded — never a false hit inside "band"), length, semicolons, comma
count. The weighted sum buckets each hub into one of three cohorts
(:data:`Cohort`) — likely-compound / uncertain / likely-atomic — cheap
enough to re-run any time (``precis taproot-migrate score``), used to size
and prioritize Phase 1.

**Phase 1 — :func:`dry_run`.** Runs the *first* stage of the canonicalizer
cascade only — :func:`~precis.taproot.canon.extract_claim_strict` — over
the top-scored hubs' claim sentences, i.e. :attr:`HubScore.sentence` from
Phase 0 (never ``block``/``dedup_judge``/``place``: those decide
convergence against *other* hubs, which is Phase 2's concern, not "does
this sentence split"). Every outcome is run through
:func:`classify_extraction` — the P0-2/P0-3 gates — before landing in a
:class:`DryRunReport`; ``controls`` are a **uniform random sample**
(:func:`random.Random`, seeded by ``control_seed`` — deterministic by
default) over the likely-atomic cohort, not the score-sorted tail (P2-11:
the tail correlates with short "topic" titles, which biased pilot controls
toward non-claims). :func:`render_report` turns the result into a markdown
table a human reviews; :func:`dump_outcomes_jsonl` persists every outcome
(gate metadata included) so a review doesn't require re-running LLM calls
(P0-4). **Zero writes through ``store`` itself** (no refs, links, meta, or
chunk mutation) — only the real extractor's (and, if ``escalate_fn`` is
set, the escalation extractor's) LLM dispatch touches the network, and
when the process has bound a store to :mod:`precis.budget.meter` (the CLI
does), that dispatch is budget-metered: it writes ``llm_call_log``
telemetry + transient serving-slot rows, never claim data. A consecutive
run of infra failures (dispatch errors, not semantic no-claims) aborts the
run rather than reporting a full batch of misclassified ``no-claim``
verdicts — see :func:`dry_run`.

**Gates (P0-2/P0-3) — never stamp ``lossy``/``nested``/``junk_candidate``.**
:func:`classify_extraction` degrades a would-be ``pass-through``/``split``
into ``nested`` (a fake split: one atom's content is contained in another's,
or an atom ≈ the compound — the containment/coverage gate runs *before*
coverage) or ``lossy`` (the union of atoms + scope values + not-claims
doesn't cover the original sentence's content words above a calibrated
recall floor, or drops a number-bearing token verbatim — see the
calibration notes on :data:`_LOSSY_RECALL_THRESHOLD_PASS_THROUGH` /
:data:`_LOSSY_RECALL_THRESHOLD_SPLIT`). A ``no-claim`` verdict on a
**non-control** hub (:attr:`DryRunOutcome.junk_candidate`) means the hub
isn't a claim at all — Phase 2 must route it to junk-triage, never stamp
``meta.taproot_decomposed_at`` on it (nor on ``lossy``/``nested``). P2-10's
selective escalation (``escalate_fn``) re-runs exactly these three outcome
shapes through a bigger extractor and records both results, rather than
bumping every hub to BIG tier.

Both phases exclude a hub that is already a **compound** (has a live
inbound ``conjunct-of`` edge — see :func:`~precis.taproot.hub._is_compound_hub`,
whose predicate this module's query mirrors) or already stamped
``meta.taproot_decomposed_at`` (the Phase-2 idempotency marker this build
doesn't write yet, but is checked here so a re-run of scoring after Phase 2
starts landing stamps automatically shrinks the candidate pool) — nothing
left to migrate on either hub shape.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    ClaimExtraction,
    extract_claim_strict_haiku,
)

if TYPE_CHECKING:
    from precis.store.store import Store

__all__ = [
    "COHORTS",
    "DryRunOutcome",
    "DryRunReport",
    "HubScore",
    "classify_extraction",
    "cohort_for_score",
    "dry_run",
    "dump_outcomes_jsonl",
    "render_report",
    "score_hubs",
    "score_sentence",
]

Cohort = Literal["likely-compound", "uncertain", "likely-atomic"]

#: Every cohort, in display/priority order — the single source other
#: modules (CLI, tests) iterate over rather than re-listing the three
#: string literals.
COHORTS: tuple[Cohort, ...] = ("likely-compound", "uncertain", "likely-atomic")

Verdict = Literal["pass-through", "split", "lossy", "nested", "no-claim", "error"]

#: :func:`dry_run`'s consecutive-infra-failure breaker: this many
#: ``verdict="error"`` outcomes in a row aborts the whole run rather than
#: silently producing a full-size report of misclassified NO-CLAIMs — the
#: melchior incident (every dispatch ECONNREFUSED, all 25 hubs reported
#: "no-claim") that motivated this guard.
_CONSECUTIVE_ERROR_LIMIT = 3

#: The Phase-2 idempotency stamp key (not yet written by any pass — Phase 2
#: is a separate build) checked here so scoring degrades the candidate pool
#: automatically once it starts landing.
_DECOMPOSED_AT_META_KEY = "taproot_decomposed_at"

# ── Phase 0 — title heuristics (pure, no model/DB) ──────────────────────

#: Word-bounded so "band"/"buttress"/"whiled" never false-hit — ``\b`` sits
#: at a word/non-word boundary, which "and" inside "band" never crosses.
_CONJUNCTION_RE = re.compile(r"\b(?:and|but|while)\b", re.IGNORECASE)

#: docs/backlog/taproot-atomic-claims.md's Population probe: "39% are >160
#: chars".
_LONG_TITLE_CHARS = 160

#: docs/backlog/taproot-atomic-claims.md's Population probe: "3% contain
#: ';'". A semicolon splices two independent clauses — as strong a
#: bundling signal as a conjunction.
_SEMICOLON_WEIGHT = 2
_CONJUNCTION_WEIGHT = 2
_LONG_TITLE_WEIGHT = 2

#: Two-or-more commas is a softer signal on its own (lists of qualifiers,
#: not necessarily two conjuncts) — weighted lower than the other three.
_MIN_COMMAS = 2
_COMMA_WEIGHT = 1

#: A score at/above this needs at least two of the weight-2 signals (or
#: their equivalent) to fire — the "confidently looks bundled" band.
_COMPOUND_THRESHOLD = 4


def score_sentence(sentence: str) -> tuple[int, tuple[str, ...]]:
    """The compoundness score for one claim hub's claim sentence — pure
    function, no model/DB. Returns ``(score, signals)``; ``signals`` names
    every heuristic that fired (empty when none did), for the report/CLI to
    show its work.

    Heuristics (docs/backlog/taproot-atomic-claims.md's Population probe,
    run against titles originally — the signals transfer unchanged to the
    claim sentence per the P0-1 fix): a word-bounded " and "/" but "/"
    while " conjunction (+2), length over :data:`_LONG_TITLE_CHARS` chars
    (+2), a semicolon (+2), two or more commas (+1). Deliberately simple
    and additive — a proxy to *prioritize* Phase 1's real LLM extraction,
    not a claim about ground truth.
    """
    signals: list[str] = []
    score = 0
    if _CONJUNCTION_RE.search(sentence):
        signals.append("conjunction")
        score += _CONJUNCTION_WEIGHT
    if len(sentence) > _LONG_TITLE_CHARS:
        signals.append("long-title")
        score += _LONG_TITLE_WEIGHT
    if ";" in sentence:
        signals.append("semicolon")
        score += _SEMICOLON_WEIGHT
    if sentence.count(",") >= _MIN_COMMAS:
        signals.append("multi-comma")
        score += _COMMA_WEIGHT
    return score, tuple(signals)


def cohort_for_score(score: int) -> Cohort:
    """Bucket a :func:`score_sentence` score into a :data:`Cohort`.

    ``0`` -> ``likely-atomic`` (no signal fired at all); at/above
    :data:`_COMPOUND_THRESHOLD` -> ``likely-compound`` (at least two
    signals, or one length/semicolon signal alongside another); everything
    in between -> ``uncertain`` (exactly one weak signal — not enough on
    its own to call it either atomic or compound).
    """
    if score <= 0:
        return "likely-atomic"
    if score >= _COMPOUND_THRESHOLD:
        return "likely-compound"
    return "uncertain"


@dataclass(frozen=True)
class HubScore:
    """One live claim hub's :func:`score_sentence` result, plus its
    identity. ``sentence`` is the exact string that was scored — the same
    string :func:`dry_run` feeds to ``extract_fn`` (P0-1: one source, not a
    title score paired with a body extraction)."""

    ref_id: int
    title: str
    sentence: str
    score: int
    cohort: Cohort
    signals: tuple[str, ...]


#: Mirrors :func:`precis.taproot.hub._is_compound_hub` / :mod:`precis.
#: workers.hub_refine`'s ``_claim_hubs_due_for_refine`` ``NOT EXISTS``
#: filter — the "cross-task seam" precedent this build follows throughout
#: (each module keeps its own copy of the compound predicate rather than
#: sharing a connection-agnostic helper).
#:
#: The LEFT JOIN reads the same ``finding_body`` chunk
#: :func:`~precis.taproot.hub.mint_hub` writes (``ord=0``) — P0-1: scoring
#: ``refs.title`` alone missed ~19% of the population that has a short
#: "topic" title but a compound body. ``COALESCE(NULLIF(btrim(...), ''), …)``
#: falls back to the title when the chunk is missing, retired, or blank.
_CANDIDATE_HUBS_SQL = """
    SELECT r.ref_id, r.title,
           COALESCE(NULLIF(btrim(c.text), ''), r.title) AS sentence
      FROM refs r
      JOIN ref_tags rt ON rt.ref_id = r.ref_id
      JOIN tags t ON t.tag_id = rt.tag_id
                 AND t.namespace = %(ns)s AND t.value = %(val)s
      LEFT JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                        AND c.chunk_kind = 'finding_body' AND c.retired_at IS NULL
     WHERE r.kind = 'finding' AND r.deleted_at IS NULL
       AND NOT (r.meta ? %(stamp_key)s)
       AND NOT EXISTS (
             SELECT 1 FROM links l
               JOIN refs a ON a.ref_id = l.src_ref_id
              WHERE l.dst_ref_id = r.ref_id
                AND l.relation = 'conjunct-of'
                AND a.kind = 'finding'
                AND a.deleted_at IS NULL
           )
"""


def score_hubs(store: Store) -> list[HubScore]:
    """Score+cohort every live claim hub eligible for migration — Phase 0.

    Reads (never writes) every live ``TAPROOT:claim`` finding that is
    **not** already a compound (no live inbound ``conjunct-of`` edge) and
    not already stamped ``meta.taproot_decomposed_at``. Scores the hub's
    claim sentence (``finding_body`` ord=0, falling back to ``title`` — see
    :data:`_CANDIDATE_HUBS_SQL`), not the title alone (P0-1). Sorted by
    score descending, tie-broken by ``ref_id`` ascending (deterministic —
    so "top N" is stable across re-runs against an unchanged population;
    control sampling is a separate random draw, see :func:`dry_run`).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            _CANDIDATE_HUBS_SQL,
            {
                "ns": TAPROOT_NAMESPACE,
                "val": TAPROOT_CLAIM,
                "stamp_key": _DECOMPOSED_AT_META_KEY,
            },
        ).fetchall()

    scores = []
    for ref_id, title, sentence in rows:
        title_str = str(title or "")
        sentence_str = str(sentence or "") or title_str
        score, signals = score_sentence(sentence_str)
        scores.append(
            HubScore(
                ref_id=int(ref_id),
                title=title_str,
                sentence=sentence_str,
                score=score,
                cohort=cohort_for_score(score),
                signals=signals,
            )
        )
    scores.sort(key=lambda s: (-s.score, s.ref_id))
    return scores


# ── Phase 1 — dry-run decomposition (extract_claim only, no ANN/judge) ──

#: Injectable so tests run with a deterministic fake and no LLM call —
#: mirrors :mod:`precis.taproot.backfill`'s ``ExtractFn`` injection point.
ExtractFn = Callable[[str], ClaimExtraction]


# ── extraction quality gates (P0-2 lossy / P0-3 nested) — pure, no DB ────
#
# Calibrated against tests/fixtures/taproot/migration_pilot_25.jsonl (see
# tests/test_taproot_migrate_gates.py, which asserts every one of the 25
# labelled rows). The gates are lexical (token-set overlap), not semantic —
# they can't tell "reworded but complete" from "the actual clause is gone"
# any better than bag-of-words ever can, so a few known false positives on
# genuinely-fine pass-throughs are accepted deliberately (xfail in the
# fixture test, with the reasoning) per the sequencing note in
# docs/backlog/taproot-migration-extraction-quality-gates.md: the coverage
# gate's job is to catch every truly-lossy hub (the dangerous class — a
# silent, permanent stamp on a still-compound hub), not to be precise about
# the safe ones (a false "lossy" just costs an extra escalation call).

_WORD_RE = re.compile(r"[A-Za-z0-9μ]+")

#: Stopwords dropped before computing content-word recall — function words
#: only; anything domain-specific (units, material names, numbers) is
#: deliberately left in so it counts toward/against coverage.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "for",
        "with",
        "as",
        "by",
        "from",
        "into",
        "onto",
        "upon",
        "than",
        "then",
        "so",
        "such",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
        "it",
        "its",
        "their",
        "they",
        "them",
        "he",
        "she",
        "his",
        "her",
        "we",
        "our",
        "you",
        "your",
        "i",
        "my",
        "mine",
        "not",
        "no",
        "nor",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "also",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "any",
        "all",
        "only",
        "own",
        "same",
        "too",
        "very",
        "s",
        "t",
        "just",
        "don",
        "now",
        "etc",
        "e.g",
        "i.e",
        "per",
        "within",
        "without",
        "about",
        "across",
        "over",
        "under",
        "between",
        "among",
        "during",
        "after",
        "before",
        "while",
        "whereas",
        "if",
        "unless",
        "once",
    ]
)

#: A single explicit "(e.g., ...)"/"(i.e., ...)" parenthetical is an
#: illustrative example list, not required content — dropping it in a
#: split/pass-through isn't loss (fi176361's "(e.g., pip, npm, Cargo)").
#: Bounded to the parenthetical itself so it can't over-strip a following
#: clause the way an open-ended "such as ... <next comma>" pattern would.
_EG_PAREN_RE = re.compile(r"\((?:e\.g\.|i\.e\.)[^)]*\)", re.IGNORECASE)

#: A number-bearing token: starts with a digit (optionally "~"/"±"-prefixed
#: for an approximation), so "409", "10^9", "2.54", "1960s", "400:1" count
#: but a catalog name that merely *contains* a digit ("MOF-5", "ZIF-8",
#: "DMOF-1") does not — those are identifiers in an "(e.g., ...)" example
#: list, not measurements, and treating them as required would false-flag
#: sound splits that generalize the list away (fi176435).
_NUMBER_TOKEN_RE = re.compile(r"^[~±]?\d")
_NUMBER_TOKEN_STRIP = ".,;:()[]{}\"'"

#: Unicode notation folding for number comparison (100-hub prod run,
#: 2026-08-15): claude-haiku normalizes "10^4-10^6" to "10⁴–10⁶" and
#: rephrases "13-residue" as scope "13 residues" — exact-token membership
#: then reads every such measurement as simultaneously missing AND
#: invented (12 of 46 false-lossy rows). Folding is applied to BOTH sides
#: before tokenizing, so it can't reopen the digit-substring hole the
#: exact-token rule exists to close.
_SUPERSCRIPT_RUN_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+")
_SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_UNICODE_DASH_TRANS = str.maketrans("–—−‒―", "-----")

#: An explicit reference-block marker: everything after it is bibliography,
#: not claim content. Hub bodies on prod frequently end in "Canonical
#: references: Solomon ... (2008); Lambert, Chem. Soc. Rev. 44, 875
#: (2015)" — an extraction that (correctly) drops the bibliography must
#: not be scored lossy for it (23 of 46 false-lossy rows on the 100-hub
#: run). Tight on purpose: only an explicit "<...> references:" / "refs:"
#: label, never a bare year — and only stripped when it starts past the
#: first third of the text, so a body that IS a reference list still gates
#: on its full content.
_CITATION_TAIL_RE = re.compile(
    r"\b(?:canonical\s+references|key\s+references|references|refs?)\s*:",
    re.IGNORECASE,
)

#: Inline citation spans — reference fragments embedded mid-sentence with
#: no "References:" label (35 of the 40 residual false-lossy rows on the
#: 100-hub run): journal "vol, page (year)" runs, "(PRL 2007)"-style
#: parenthesized years, "5:487" vol:page shorthands, arXiv/DOI handles.
#: Stripped from the LOSS-direction original only — a citation number is
#: never *required* content, but one an atom kept is still allowed (the
#: invention direction sees the full text). Errs permissive on
#: parenthesized years ("(since 2020)" strips too) — acceptable: the
#: pattern only excuses a *dropped* year, it never lets an invented one
#: through.
_CITATION_SPAN_RES = (
    re.compile(r"[^.;()]{0,60}?\b\d{1,4},\s*\d{3,7}\s*\(\s*(?:19|20)\d\d\s*\)"),
    # "vol, page, year" (no parens): "Phys. Rev. Lett. 57, 1761, 1986"
    re.compile(r"\b\d{1,4},\s*\d{3,7},\s*(?:19|20)\d\d\b"),
    # "vol(issue), page(-page)": "123(24), 5035-5047"
    re.compile(r"\b\d{1,4}\(\d{1,3}\),?\s*\d{3,7}(?:\s*-\s*\d{3,7})?"),
    # any short parenthetical ending in a year — "(PRL 2007)",
    # "(Zhang 2009; Mak 2008)" — bounded so a full clause never strips
    re.compile(r"\(\s*[^()]{0,60}?(?:19|20)\d\d\s*\)"),
    re.compile(r"\b\d{1,3}:\d{2,6}\b"),
    re.compile(r"\barxiv[:\s]\s*\S+", re.IGNORECASE),
    re.compile(r"\bdoi[:\s]\s*\S+", re.IGNORECASE),
)


def _normalize_number_text(text: str) -> str:
    """Fold unicode notation into the ASCII forms prod hub bodies use:
    superscript runs -> ``^``-prefixed digits (``10⁴`` -> ``10^4``,
    ``10⁻⁶`` -> ``10^-6``), unicode dashes/minus -> ``-``."""
    text = text.translate(_UNICODE_DASH_TRANS)
    return _SUPERSCRIPT_RUN_RE.sub(
        lambda m: "^" + m.group(0).translate(_SUPERSCRIPT_TRANS), text
    )


def _strip_citation_tail(text: str) -> str:
    """Cut an explicit trailing reference block (:data:`_CITATION_TAIL_RE`)
    and blank inline citation spans (:data:`_CITATION_SPAN_RES`) for the
    *loss*-direction coverage checks. The invention direction (precision,
    invented numbers) deliberately keeps the full text — an atom that
    copied a citation year didn't invent it."""
    m = _CITATION_TAIL_RE.search(text)
    if m is not None and m.start() > len(text) // 3:
        text = text[: m.start()]
    for span_re in _CITATION_SPAN_RES:
        text = span_re.sub(" ", text)
    return text


#: Below this recall (fraction of the original's :func:`_content_words`
#: found in the extraction union), a **pass-through**
#: (single atom, no compound — extraction claims the sentence was already
#: atomic) is `lossy`. Set just above fi176360's 13/18≈0.722 recall (a real
#: dropped scope-qualifier clause) and just below fi176361's 11/15≈0.733
#: (a dropped illustrative example + summary clause — judged acceptable) —
#: the closest a lexical threshold gets on this fixture; three correct
#: pass-throughs still fall below it (see the fixture test's xfails).
_LOSSY_RECALL_THRESHOLD_PASS_THROUGH = 0.73

#: Below this recall, a **split** (compound present, ≥2 atoms) is `lossy`.
#: Legitimate splits naturally lose more raw token overlap than a
#: pass-through does — the compound's connective/summarizing words get
#: redistributed or dropped across atoms even when nothing is *lost* — so
#: this floor sits lower. Set between fi177406's ≈0.619 recall (real
#: dropped provenance clause) and fi176427's ≈0.75 (sound 3-way split), with
#: margin below every other sound split in the fixture (0.75–1.0).
_LOSSY_RECALL_THRESHOLD_SPLIT = 0.65

#: Above this containment ratio (fraction of one atom's — or the bundle's —
#: tokens found in another), P0-3 calls the split `nested`: fi176441's
#: three "atoms" are a strictly nested A1⊂A2⊂A3, with A3 ratio 0.90 against
#: the compound — a fake split, not three facts. The next-highest ratio on
#: any *sound* split in the fixture is ~0.5, so this has a wide margin.
_NESTED_CONTAINMENT_THRESHOLD = 0.9

#: A **pass-through** that drops at least this many content words is
#: `lossy` regardless of its recall ratio (round 2, from the labelled-25
#: A/B re-run): on a short sentence the ratio has too little resolution —
#: fi176441's truncated re-extraction dropped an entire predicate
#: ("supporting charge transport") plus a relation ("between metals and
#: ligands") yet cleared the 0.73 ratio at 0.765 (4 of 17 content words
#: gone). The absolute count is the complementary signal: the fixture's
#: correct pass-throughs drop at most 3 (fi176448) — except fi176361 at
#: exactly 4, accepted as a fourth known false positive (see the gates
#: test's xfails). **Splits are exempt**: redistributing a compound across
#: atoms legitimately drops 4–5 connective/summarizing words (fi176427,
#: fi176435 — both sound splits).
_LOSSY_MISSING_CONTENT_CAP_PASS_THROUGH = 4

#: Below this content-word precision (fraction of the extraction union's
#: content words that come from the original), the extraction *added*
#: material — the hallucination direction recall is blind to (round 2):
#: recall checks what was kept, never what was invented. Every sound
#: fixture extraction sits at ≥0.833; the one real offender (fi176275, a
#: rewrite that invents its own framing) is at 0.600.
_HALLUCINATION_PRECISION_THRESHOLD = 0.8


def _token_set(text: str) -> frozenset[str]:
    """Casefold + strip punctuation -> the token set used by the
    containment/nested gate (P0-3) — no stopword filtering; containment is
    about whether one atom's *entire* content sits inside another's, so
    function words matter too."""
    return frozenset(t.casefold() for t in _WORD_RE.findall(text))


def _content_words(text: str) -> frozenset[str]:
    """Casefold + strip punctuation + drop stopwords and an "(e.g., ...)"
    example list -> the token set used by the coverage gate's recall
    (P0-2). Single-char tokens (stray possessive "s", etc.) are dropped."""
    text = _EG_PAREN_RE.sub(" ", text)
    return frozenset(
        t
        for t in (m.casefold() for m in _WORD_RE.findall(text))
        if t not in _STOPWORDS and len(t) > 1
    )


def _number_bearing_tokens(text: str) -> list[str]:
    """Every whitespace token in ``text`` that starts with a digit (see
    :data:`_NUMBER_TOKEN_RE`), casefolded, punctuation-stripped at the
    edges only (interior ``/``/``^``/``:``/``.`` — unit separators — are
    kept, since "409" and "μA/μm" are a single token but "10^9" and
    "2.54" are not split by their internal punctuation either).

    Unicode notation is folded first (:func:`_normalize_number_text`), and
    a **digit-leading** token is further split on ``-`` so "13-residue"
    yields "13" and the range "10^4-10^6" yields both bounds — matching
    what an extractor legitimately writes as "13 residues" or "10⁴–10⁶".
    A letter-leading token is never split: "MOF-5" stays a catalog name,
    not a measurement (fi176435's exclusion is preserved)."""
    tokens = []
    for raw in _normalize_number_text(text).split():
        core = raw.strip(_NUMBER_TOKEN_STRIP)
        if not core or not _NUMBER_TOKEN_RE.match(core):
            continue
        for part in core.split("-"):
            part = part.strip(_NUMBER_TOKEN_STRIP)
            if part and _NUMBER_TOKEN_RE.match(part):
                # Fold the approximation prefix: "near 10 kHz" rendered as
                # "~10 kHz" is the same measurement, not an invented one.
                tokens.append(part.lstrip("~±").casefold())
    return tokens


def _missing_number_tokens(original: str, union_text: str) -> tuple[str, ...]:
    """Number-bearing tokens in ``original`` absent from ``union_text``'s
    own number-bearing token set. Exact token membership, not substring —
    a dropped "9" must never hide inside a retained "409" (digit-substring
    collision). Number formatting is copied verbatim by the extractor, so
    an equal measurement yields an identical token; a formatting mismatch
    flags lossy, which is the direction this gate is allowed to err. The
    hard half of the coverage gate (P0-2): a dropped measurement is lossy
    regardless of recall."""
    union_tokens = frozenset(_number_bearing_tokens(union_text))
    missing: list[str] = []
    for token in _number_bearing_tokens(original):
        if token not in union_tokens and token not in missing:
            missing.append(token)
    return tuple(missing)


def _invented_number_tokens(original: str, union_text: str) -> tuple[str, ...]:
    """Number-bearing tokens in ``union_text`` absent from ``original`` —
    the mirror of :func:`_missing_number_tokens`, catching the opposite
    failure: a measurement the extractor *invented* rather than lost (the
    A/B re-run's fi201713 hallucinated "10^208" into an atom; the pilot's
    fi177406 invented "113"/"13"). Any invented number is a hard `lossy`
    flag — a fabricated measurement in a mint-bound atom is worse than a
    dropped one, and the same verbatim-copy contract that makes a missing
    number decisive makes an invented one decisive too."""
    original_tokens = frozenset(_number_bearing_tokens(original))
    invented: list[str] = []
    for token in _number_bearing_tokens(union_text):
        if token not in original_tokens and token not in invented:
            invented.append(token)
    return tuple(invented)


def _extraction_union_text(extraction: ClaimExtraction) -> str:
    """Everything the extraction *kept*: every atom sentence, every atom
    scope value, every not-claim's text — a deliberately-rejected conjunct
    is accounted for, not lost. The compound sentence deliberately does
    **not** count: it IS the original (or a close paraphrase of it), so
    including it would make the coverage gate trivially pass on the exact
    lossy pattern it exists to catch (P0-2)."""
    parts: list[str] = []
    for atom in extraction.atoms:
        parts.append(atom.sentence)
        parts.extend(atom.scope.values())
    for not_claim in extraction.not_claims:
        parts.append(not_claim["text"])
    return " ".join(parts)


def _containment_findings(
    atoms: tuple[CanonicalClaim, ...], reference_sentence: str
) -> list[dict[str, Any]]:
    """P0-3's containment check: every atom-pair or atom-vs-reference
    (compound, or the original sentence when there's no compound) whose
    containment ratio exceeds :data:`_NESTED_CONTAINMENT_THRESHOLD`. Empty
    when nothing is nested — the caller treats a non-empty result as
    `nested`. ``reference_sentence`` is checked as "how much of the
    reference is covered by this one atom" (fi176441: A3 alone covers 90%
    of the compound), not the other direction — every sound atom covers
    *some* of the compound by construction, so that direction would fire on
    every split.
    """
    token_sets = [_token_set(atom.sentence) for atom in atoms]
    reference_tokens = _token_set(reference_sentence)
    findings: list[dict[str, Any]] = []
    for i, j in itertools.combinations(range(len(token_sets)), 2):
        a, b = token_sets[i], token_sets[j]
        ratio = max(
            len(a & b) / len(a) if a else 1.0,
            len(a & b) / len(b) if b else 1.0,
        )
        if ratio > _NESTED_CONTAINMENT_THRESHOLD:
            findings.append({"atoms": (i, j), "ratio": round(ratio, 3)})
    for i, atom_tokens in enumerate(token_sets):
        if not reference_tokens:
            continue
        ratio = len(reference_tokens & atom_tokens) / len(reference_tokens)
        if ratio > _NESTED_CONTAINMENT_THRESHOLD:
            findings.append({"atom": i, "vs": "compound", "ratio": round(ratio, 3)})
    return findings


def classify_extraction(
    sentence: str, extraction: ClaimExtraction
) -> tuple[Verdict, dict[str, Any]]:
    """``extract_claim``'s outcome, gated (P0-2/P0-3) into migration-report
    terms. Returns ``(verdict, gate_meta)`` — ``gate_meta`` carries whatever
    the fired (or checked) gates computed (``recall``, ``missing_numbers``,
    ``precision``, ``invented_numbers``, ``containment``,
    ``missing_content``), for the report/JSONL to show its work. NO-CLAIM
    skips every gate (nothing was kept to check coverage of, and nothing to
    nest).

    The coverage gate (P0-2) checks **both directions** (round 2): loss —
    a dropped number or content-word recall below the shape's threshold —
    and hallucination — an invented number, or content-word precision
    below :data:`_HALLUCINATION_PRECISION_THRESHOLD` (the extraction added
    material the sentence never said). A pass-through additionally fails on
    an absolute missing-content-word count
    (:data:`_LOSSY_MISSING_CONTENT_CAP_PASS_THROUGH`) — the recall ratio
    alone has too little resolution on short sentences (fi176441).

    Order: containment (`nested`) before coverage (`lossy`) — a nested
    "split" is never a valid unit to run a coverage check against (its
    "atoms" restate each other, not partition the sentence).
    """
    if extraction.is_empty:
        return "no-claim", {}

    if len(extraction.atoms) >= 2:
        reference = (
            extraction.compound.sentence
            if extraction.compound is not None
            else sentence
        )
        containment = _containment_findings(extraction.atoms, reference)
        if containment:
            return "nested", {"containment": containment}

    union_text = _extraction_union_text(extraction)
    # Loss-direction checks score against the citation-stripped original
    # (dropping a trailing bibliography is correct, not lossy); the
    # invented-number allowlist keeps the full sentence — a copied
    # citation year was never invented.
    coverage_original = _strip_citation_tail(sentence)
    missing_numbers = _missing_number_tokens(coverage_original, union_text)
    invented_numbers = _invented_number_tokens(sentence, union_text)
    original_words = _content_words(coverage_original)
    union_words = _content_words(union_text)
    kept = original_words & union_words
    recall = len(kept) / len(original_words) if original_words else 1.0
    # Precision (the invention direction) allowlists the FULL original —
    # like invented_numbers, a word the sentence said anywhere (citation
    # tail included) was not invented; only recall scores the stripped
    # text.
    kept_full = _content_words(sentence) & union_words
    precision = len(kept_full) / len(union_words) if union_words else 1.0
    gate_meta: dict[str, Any] = {
        "recall": round(recall, 3),
        "missing_numbers": missing_numbers,
        "precision": round(precision, 3),
        "invented_numbers": invented_numbers,
    }
    is_split = extraction.compound is not None
    threshold = (
        _LOSSY_RECALL_THRESHOLD_SPLIT
        if is_split
        else _LOSSY_RECALL_THRESHOLD_PASS_THROUGH
    )
    if missing_numbers or recall < threshold:
        return "lossy", gate_meta
    if invented_numbers or precision < _HALLUCINATION_PRECISION_THRESHOLD:
        return "lossy", gate_meta
    if not is_split:
        missing_content = sorted(original_words - union_words)
        if len(missing_content) >= _LOSSY_MISSING_CONTENT_CAP_PASS_THROUGH:
            gate_meta["missing_content"] = tuple(missing_content)
            return "lossy", gate_meta

    return ("split" if is_split else "pass-through"), gate_meta


@dataclass(frozen=True)
class DryRunOutcome:
    """One hub's Phase-1 outcome — the original sentence plus what
    :func:`~precis.taproot.canon.extract_claim` proposed, gated through
    :func:`classify_extraction`.

    **Phase-2 invariant** (P2-12): a hub whose ``verdict`` is
    ``lossy``/``nested``, or whose ``junk_candidate`` is ``True``, must
    never be stamped ``meta.taproot_decomposed_at`` by the (not-yet-built)
    apply pass — ``lossy``/``nested`` are still-compound hubs a gate
    rejected; ``junk_candidate`` isn't a claim at all.
    """

    hub: HubScore
    claim_sentence: str
    verdict: Verdict
    extraction: ClaimExtraction | None = None
    #: Whatever :func:`classify_extraction`'s gates computed (``recall``,
    #: ``missing_numbers``, ``containment``) — empty on no-claim/error.
    gate_meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: True for a hub sampled as a pass-through control (a uniform random
    #: draw from the likely-atomic cohort, see :func:`dry_run`'s
    #: ``control_seed``) rather than a top-scored candidate.
    is_control: bool = False
    #: True on a `no-claim` verdict for a **non-control** hub (P2-12): the
    #: hub isn't a claim at all (a research note, a task-prose title, …),
    #: not a compound that simply didn't decompose — route to junk-triage,
    #: never treat as "nothing to do".
    junk_candidate: bool = False
    #: P2-10 selective escalation: set only when ``escalate_fn`` was passed
    #: to :func:`dry_run` *and* this outcome's gated verdict was
    #: lossy/nested/junk-candidate. Both the original and the escalated
    #: result are kept — nothing here overwrites ``verdict``/``extraction``.
    escalated_extraction: ClaimExtraction | None = None
    escalated_verdict: Verdict | None = None
    escalated_gate_meta: dict[str, Any] = field(default_factory=dict)
    #: A per-hub escalation dispatch/parse failure — isolated the same way
    #: the primary ``extract_fn`` failure is, but never counts toward
    #: :data:`_CONSECUTIVE_ERROR_LIMIT` (that breaker is about the primary
    #: extractor's infra health, not the escalation tier's).
    escalation_error: str | None = None


@dataclass(frozen=True)
class DryRunReport:
    """The full Phase-1 dry-run result — what :func:`render_report`
    formats for a human reviewer."""

    outcomes: list[DryRunOutcome] = field(default_factory=list)
    requested_limit: int = 0
    cohort_filter: Cohort | None = None
    requested_controls: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """Verdict -> count, over every outcome (top-scored + controls)."""
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
        return counts


def dry_run(
    store: Store,
    *,
    limit: int,
    cohort: Cohort | None = None,
    controls: int = 0,
    control_seed: int = 0,
    extract_fn: ExtractFn = extract_claim_strict_haiku,
    escalate_fn: ExtractFn | None = None,
) -> DryRunReport:
    """Phase 1: run ``extract_fn`` over the top ``limit`` scored hubs
    (optionally restricted to one ``cohort``), plus ``controls`` hubs drawn
    as a **uniform random sample** (:class:`random.Random` seeded by
    ``control_seed`` — deterministic by default, so a re-run against an
    unchanged population picks the same controls) from the whole
    likely-atomic cohort, excluding any hub already selected above (a
    pass-through sanity check — an already-atomic hub should extract to
    one atom, no compound). P2-11: earlier this sampled the score-sorted
    tail, which correlates with short "topic" titles and biased pilot
    controls toward non-claims — a uniform draw doesn't share that bias.
    **Zero writes through ``store`` itself** — no ref/link/meta/chunk
    mutation. The default ``extract_fn`` is the real
    :func:`~precis.taproot.canon.extract_claim_strict_haiku` (round 2 +
    the 4-hub raw-response probe): SMALL collapses multi-clause sentences
    to single truncated atoms, and the BIG chain's OSS models
    intermittently break the JSON contract into silent NO-CLAIMs — both
    are now opt-in (CLI ``--tier small`` / ``--tier big``), injectable
    for tests; when the calling
    process has bound a store to
    :mod:`precis.budget.meter` (the CLI does this), its LLM dispatch is
    budget-metered — it writes ``llm_call_log`` telemetry and transient
    serving-slot rows, but never touches the claim tables (refs/chunks/
    links/ref_tags).

    Every outcome is gated through :func:`classify_extraction` (P0-2/P0-3).
    When ``escalate_fn`` is given (P2-10), any outcome whose gated verdict
    is ``lossy``/``nested``, or ``no-claim`` on a **non-control** hub
    (:attr:`DryRunOutcome.junk_candidate`), is re-extracted with it — both
    results are kept (:attr:`DryRunOutcome.escalated_extraction`/
    ``escalated_verdict``), never just the escalated one, so a reviewer can
    see what changed. Selective, not a blanket bump: only the outcome
    shapes a stable-verdict pilot showed are the systematic error classes.

    A per-hub dispatch/parse failure is caught (``verdict="error"``) rather
    than aborting the whole run — one bad hub must not lose every other
    outcome in a ~1.3k-hub pass. But :data:`_CONSECUTIVE_ERROR_LIMIT`
    consecutive errors aborts the run outright (``RuntimeError``): that
    many in a row is infra failure (a dead LLM endpoint), not sporadic
    per-hub noise, and continuing would silently produce a full-size report
    of misclassified NO-CLAIMs instead of a signal that the run never
    really happened. An escalation failure is caught per-hub too
    (:attr:`DryRunOutcome.escalation_error`) but never trips this breaker —
    see :attr:`DryRunOutcome.escalation_error`'s docstring.
    """
    all_scores = score_hubs(store)
    pool = (
        all_scores if cohort is None else [s for s in all_scores if s.cohort == cohort]
    )
    selected = list(pool[:limit])
    selected_ids = {s.ref_id for s in selected}

    control_hubs: list[HubScore] = []
    if controls > 0:
        atomic_cohort = [
            s
            for s in all_scores
            if s.cohort == "likely-atomic" and s.ref_id not in selected_ids
        ]
        rng = random.Random(control_seed)
        control_hubs = rng.sample(atomic_cohort, k=min(controls, len(atomic_cohort)))

    outcomes: list[DryRunOutcome] = []
    consecutive_errors = 0
    for hub_score, is_control in [(s, False) for s in selected] + [
        (s, True) for s in control_hubs
    ]:
        sentence = hub_score.sentence
        try:
            extraction = extract_fn(sentence)
        except Exception as exc:  # isolate one hub, keep the batch going
            consecutive_errors += 1
            outcomes.append(
                DryRunOutcome(
                    hub=hub_score,
                    claim_sentence=sentence,
                    verdict="error",
                    error=str(exc),
                    is_control=is_control,
                )
            )
            if consecutive_errors >= _CONSECUTIVE_ERROR_LIMIT:
                raise RuntimeError(
                    f"taproot-migrate dry-run: LLM dispatch unavailable "
                    f"({consecutive_errors} consecutive failures, last: {exc}) "
                    "— aborting dry-run; a dead LLM must not produce a "
                    "full-size garbage report"
                ) from exc
            continue
        consecutive_errors = 0
        verdict, gate_meta = classify_extraction(sentence, extraction)
        junk_candidate = verdict == "no-claim" and not is_control

        escalated_extraction: ClaimExtraction | None = None
        escalated_verdict: Verdict | None = None
        escalated_gate_meta: dict[str, Any] = {}
        escalation_error: str | None = None
        if escalate_fn is not None and (
            verdict in ("lossy", "nested") or junk_candidate
        ):
            try:
                escalated_extraction = escalate_fn(sentence)
                escalated_verdict, escalated_gate_meta = classify_extraction(
                    sentence, escalated_extraction
                )
            except Exception as exc:  # escalation failure isolates too
                escalation_error = str(exc)

        outcomes.append(
            DryRunOutcome(
                hub=hub_score,
                claim_sentence=sentence,
                verdict=verdict,
                extraction=extraction,
                gate_meta=gate_meta,
                is_control=is_control,
                junk_candidate=junk_candidate,
                escalated_extraction=escalated_extraction,
                escalated_verdict=escalated_verdict,
                escalated_gate_meta=escalated_gate_meta,
                escalation_error=escalation_error,
            )
        )

    return DryRunReport(
        outcomes=outcomes,
        requested_limit=limit,
        cohort_filter=cohort,
        requested_controls=controls,
    )


# ── report rendering ─────────────────────────────────────────────────────


def _render_extraction(extraction: ClaimExtraction) -> list[str]:
    lines: list[str] = []
    if extraction.atoms:
        lines.append("**Proposed atoms**:")
        lines.extend(
            f"{i}. {atom.sentence}" for i, atom in enumerate(extraction.atoms, 1)
        )
        lines.append("")
    lines.append(
        f"**Compound**: {extraction.compound.sentence}"
        if extraction.compound is not None
        else "**Compound**: (none)"
    )
    if extraction.not_claims:
        lines.append("")
        lines.append("**Not-claims**:")
        lines.extend(f"- {nc['text']} — {nc['reason']}" for nc in extraction.not_claims)
    return lines


def _render_outcome(outcome: DryRunOutcome) -> list[str]:
    hub = outcome.hub
    tags = []
    if outcome.is_control:
        tags.append("control")
    if outcome.junk_candidate:
        tags.append("JUNK CANDIDATE")
    tag_suffix = f" ({', '.join(tags)})" if tags else ""
    lines = [
        f"## fi{hub.ref_id} — cohort={hub.cohort} score={hub.score}{tag_suffix}",
        "",
        f"**Original**: {outcome.claim_sentence}",
        "",
    ]
    if outcome.verdict == "error":
        lines.append(f"**Error**: {outcome.error}")
    elif outcome.extraction is not None:
        lines.extend(_render_extraction(outcome.extraction))
    if outcome.gate_meta:
        lines.append("")
        lines.append(f"**Gate**: {outcome.gate_meta}")
    lines.append("")
    lines.append(f"**Verdict**: {outcome.verdict.upper()}")
    if outcome.escalated_verdict is not None or outcome.escalation_error is not None:
        lines.append("")
        lines.append("**Escalated**:")
        if outcome.escalation_error is not None:
            lines.append(f"- error: {outcome.escalation_error}")
        else:
            lines.append(f"- verdict: {outcome.escalated_verdict.upper()}")  # type: ignore[union-attr]
            if outcome.escalated_gate_meta:
                lines.append(f"- gate: {outcome.escalated_gate_meta}")
            if outcome.escalated_extraction is not None:
                lines.extend(
                    f"  {line}"
                    for line in _render_extraction(outcome.escalated_extraction)
                )
    lines.append("")
    return lines


def render_report(report: DryRunReport) -> str:
    """Render a :class:`DryRunReport` as markdown for a human reviewer:
    summary counts up top, then one section per hub (id, cohort+score,
    original sentence, proposed atoms/compound/not_claims, gate metadata,
    verdict, and — when ``escalate_fn`` was used — the escalated result
    alongside it)."""
    counts = report.counts
    junk_count = sum(1 for o in report.outcomes if o.junk_candidate)
    lines = ["# Taproot migration dry-run report", ""]
    scope_bits = [f"limit={report.requested_limit}"]
    if report.cohort_filter is not None:
        scope_bits.append(f"cohort={report.cohort_filter}")
    if report.requested_controls:
        scope_bits.append(f"controls={report.requested_controls}")
    lines.append(f"**Scope**: {', '.join(scope_bits)}")
    lines.append(f"**Evaluated**: {len(report.outcomes)} hub(s)")
    lines.append(
        f"**Junk candidates** (non-control no-claim — route to junk-triage): {junk_count}"
    )
    lines.append("")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for verdict in ("pass-through", "split", "lossy", "nested", "no-claim", "error"):
        lines.append(f"| {verdict} | {counts.get(verdict, 0)} |")
    lines.append("")

    for outcome in report.outcomes:
        lines.extend(_render_outcome(outcome))

    return "\n".join(lines)


# ── outcome persistence (P0-4) ───────────────────────────────────────────


def _claim_to_dict(claim: CanonicalClaim) -> dict[str, Any]:
    return {"sentence": claim.sentence, "scope": dict(claim.scope)}


def _extraction_to_dict(extraction: ClaimExtraction | None) -> dict[str, Any] | None:
    if extraction is None:
        return None
    return {
        "atoms": [_claim_to_dict(atom) for atom in extraction.atoms],
        "compound": (
            _claim_to_dict(extraction.compound)
            if extraction.compound is not None
            else None
        ),
        "not_claims": [dict(nc) for nc in extraction.not_claims],
    }


def _outcome_to_dict(outcome: DryRunOutcome) -> dict[str, Any]:
    return {
        "hub": outcome.hub.ref_id,
        "score": outcome.hub.score,
        "cohort": outcome.hub.cohort,
        "control": outcome.is_control,
        "sentence": outcome.claim_sentence,
        "verdict": outcome.verdict,
        "gate_meta": outcome.gate_meta,
        "extraction": _extraction_to_dict(outcome.extraction),
        "error": outcome.error,
        "junk_candidate": outcome.junk_candidate,
        "escalated_verdict": outcome.escalated_verdict,
        "escalated_gate_meta": outcome.escalated_gate_meta,
        "escalated_extraction": _extraction_to_dict(outcome.escalated_extraction),
        "escalation_error": outcome.escalation_error,
    }


def dump_outcomes_jsonl(report: DryRunReport) -> str:
    """Serialize every outcome in ``report`` to JSONL — one JSON object per
    line (hub id, score, cohort, control flag, sentence, verdict, gate
    metadata, the full extraction including atom scope dicts, error, the
    junk-candidate flag, and the escalated result if any). P0-4: the pilot
    had to re-run prod LLM calls to review outcomes because nothing was
    persisted — this is that artifact, meant to sit alongside
    :func:`render_report`'s markdown, not replace it."""
    return "\n".join(
        json.dumps(_outcome_to_dict(outcome)) for outcome in report.outcomes
    )
