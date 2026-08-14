"""Taproot atomic-claims migration runner — Phase 0 (score) + Phase 1
(dry-run), both **strictly read-only**. Design: ``docs/backlog/
taproot-atomic-claims.md`` §Strategy. Phase 2 (apply) and Phase 3 (human
review) are not built here.

**Phase 0 — :func:`score_hubs`.** A deterministic, pure-function
compoundness score over every live claim hub's title (no model, no ANN):
title conjunctions (" and " / " but " / " while ", word-bounded — never a
false hit inside "band"), length, semicolons, comma count. The weighted sum
buckets each hub into one of three cohorts (:data:`Cohort`) — likely-
compound / uncertain / likely-atomic — cheap enough to re-run any time
(``precis taproot-migrate score``), used to size and prioritize Phase 1.

**Phase 1 — :func:`dry_run`.** Runs the *first* stage of the canonicalizer
cascade only — :func:`~precis.taproot.canon.extract_claim` — over the top-
scored hubs' claim sentences (never ``block``/``dedup_judge``/``place``:
those decide convergence against *other* hubs, which is Phase 2's concern,
not "does this sentence split"). The claim sentence is read the same way
:mod:`.backfill`/:mod:`.hub` do: the hub's ``finding_body`` chunk
(``ord=0``), falling back to the hub's ``title`` when that chunk is
missing/empty. Every outcome is recorded into a :class:`DryRunReport` —
:func:`render_report` turns it into a markdown table a human reviews.
**Zero writes of any kind** (no refs, links, meta, or chunk mutation) —
only :func:`~precis.taproot.canon.extract_claim`'s LLM spend touches the
network; the store is read-only throughout.

Both phases exclude a hub that is already a **compound** (has a live
inbound ``conjunct-of`` edge — see :func:`~precis.taproot.hub._is_compound_hub`,
whose predicate this module's query mirrors) or already stamped
``meta.taproot_decomposed_at`` (the Phase-2 idempotency marker this build
doesn't write yet, but is checked here so a re-run of scoring after Phase 2
starts landing stamps automatically shrinks the candidate pool) — nothing
left to migrate on either hub shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    ClaimExtraction,
    extract_claim,
)

if TYPE_CHECKING:
    from precis.store.store import Store

__all__ = [
    "COHORTS",
    "DryRunOutcome",
    "DryRunReport",
    "HubScore",
    "cohort_for_score",
    "dry_run",
    "render_report",
    "score_hubs",
    "score_title",
]

Cohort = Literal["likely-compound", "uncertain", "likely-atomic"]

#: Every cohort, in display/priority order — the single source other
#: modules (CLI, tests) iterate over rather than re-listing the three
#: string literals.
COHORTS: tuple[Cohort, ...] = ("likely-compound", "uncertain", "likely-atomic")

Verdict = Literal["pass-through", "split", "no-claim", "error"]

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


def score_title(title: str) -> tuple[int, tuple[str, ...]]:
    """The compoundness score for one claim-hub title — pure function, no
    model/DB. Returns ``(score, signals)``; ``signals`` names every
    heuristic that fired (empty when none did), for the report/CLI to show
    its work.

    Heuristics (docs/backlog/taproot-atomic-claims.md's Population probe):
    a word-bounded " and "/" but "/" while " conjunction (+2), title length
    over :data:`_LONG_TITLE_CHARS` chars (+2), a semicolon (+2), two or
    more commas (+1). Deliberately simple and additive — a proxy to
    *prioritize* Phase 1's real LLM extraction, not a claim about ground
    truth.
    """
    signals: list[str] = []
    score = 0
    if _CONJUNCTION_RE.search(title):
        signals.append("conjunction")
        score += _CONJUNCTION_WEIGHT
    if len(title) > _LONG_TITLE_CHARS:
        signals.append("long-title")
        score += _LONG_TITLE_WEIGHT
    if ";" in title:
        signals.append("semicolon")
        score += _SEMICOLON_WEIGHT
    if title.count(",") >= _MIN_COMMAS:
        signals.append("multi-comma")
        score += _COMMA_WEIGHT
    return score, tuple(signals)


def cohort_for_score(score: int) -> Cohort:
    """Bucket a :func:`score_title` score into a :data:`Cohort`.

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
    """One live claim hub's :func:`score_title` result, plus its identity."""

    ref_id: int
    title: str
    score: int
    cohort: Cohort
    signals: tuple[str, ...]


#: Mirrors :func:`precis.taproot.hub._is_compound_hub` / :mod:`precis.
#: workers.hub_refine`'s ``_claim_hubs_due_for_refine`` ``NOT EXISTS``
#: filter — the "cross-task seam" precedent this build follows throughout
#: (each module keeps its own copy of the compound predicate rather than
#: sharing a connection-agnostic helper).
_CANDIDATE_HUBS_SQL = """
    SELECT r.ref_id, r.title
      FROM refs r
      JOIN ref_tags rt ON rt.ref_id = r.ref_id
      JOIN tags t ON t.tag_id = rt.tag_id
                 AND t.namespace = %(ns)s AND t.value = %(val)s
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
    not already stamped ``meta.taproot_decomposed_at``. Sorted by score
    descending, tie-broken by ``ref_id`` ascending (deterministic — so
    "top N" and a control sample from the tail are both stable across
    re-runs against an unchanged population).
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
    for ref_id, title in rows:
        title_str = str(title or "")
        score, signals = score_title(title_str)
        scores.append(
            HubScore(
                ref_id=int(ref_id),
                title=title_str,
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


def _read_claim_sentence(store: Store, ref_id: int, title: str) -> str:
    """The claim sentence for one hub: its ``finding_body`` chunk
    (``ord=0``), falling back to ``title`` when the chunk is missing or
    blank — mirrors how :func:`~precis.taproot.hub.mint_hub` writes a hub
    (title + a ``finding_body`` chunk at ``ord=0``) and how
    :mod:`precis.taproot.backfill`/:mod:`precis.workers.hub_refine` read a
    hub's claim text back. Read-only."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 "
            "AND chunk_kind = 'finding_body' AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is not None and row[0]:
        text = str(row[0]).strip()
        if text:
            return text
    return title


def _classify_extraction(extraction: ClaimExtraction) -> Verdict:
    """``extract_claim``'s outcome, in migration-report terms."""
    if extraction.is_empty:
        return "no-claim"
    if extraction.compound is not None:
        return "split"
    return "pass-through"


@dataclass(frozen=True)
class DryRunOutcome:
    """One hub's Phase-1 outcome — the original sentence plus what
    :func:`~precis.taproot.canon.extract_claim` proposed."""

    hub: HubScore
    claim_sentence: str
    verdict: Verdict
    extraction: ClaimExtraction | None = None
    error: str | None = None
    #: True for a hub sampled as a pass-through control (bottom of the
    #: likely-atomic cohort) rather than a top-scored candidate.
    is_control: bool = False


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
    extract_fn: ExtractFn = extract_claim,
) -> DryRunReport:
    """Phase 1: run ``extract_fn`` over the top ``limit`` scored hubs
    (optionally restricted to one ``cohort``), plus ``controls`` hubs
    sampled from the bottom of the likely-atomic cohort (a pass-through
    sanity check — an already-atomic hub should extract to one atom, no
    compound). **Writes nothing** — no ``store`` write of any kind; the
    only side effect is ``extract_fn``'s LLM dispatch (real
    :func:`~precis.taproot.canon.extract_claim` by default, injectable for
    tests).

    A dispatch/parse failure inside ``extract_fn`` is caught per-hub
    (``verdict="error"``) rather than aborting the whole run — one bad hub
    must not lose every other outcome in a ~1.3k-hub pass.
    """
    all_scores = score_hubs(store)
    pool = (
        all_scores if cohort is None else [s for s in all_scores if s.cohort == cohort]
    )
    selected = list(pool[:limit])
    selected_ids = {s.ref_id for s in selected}

    control_hubs: list[HubScore] = []
    if controls > 0:
        # `all_scores` sorts score descending, so the likely-atomic cohort
        # (score 0) sits at the tail; walking it in reverse samples from
        # the very bottom of the full ranking, per the spec.
        atomic_tail = [s for s in all_scores if s.cohort == "likely-atomic"]
        for hub_score in reversed(atomic_tail):
            if hub_score.ref_id in selected_ids:
                continue
            control_hubs.append(hub_score)
            if len(control_hubs) >= controls:
                break

    outcomes: list[DryRunOutcome] = []
    for hub_score, is_control in [(s, False) for s in selected] + [
        (s, True) for s in control_hubs
    ]:
        sentence = _read_claim_sentence(store, hub_score.ref_id, hub_score.title)
        try:
            extraction = extract_fn(sentence)
        except Exception as exc:  # isolate one hub, keep the batch going
            outcomes.append(
                DryRunOutcome(
                    hub=hub_score,
                    claim_sentence=sentence,
                    verdict="error",
                    error=str(exc),
                    is_control=is_control,
                )
            )
            continue
        outcomes.append(
            DryRunOutcome(
                hub=hub_score,
                claim_sentence=sentence,
                verdict=_classify_extraction(extraction),
                extraction=extraction,
                is_control=is_control,
            )
        )

    return DryRunReport(
        outcomes=outcomes,
        requested_limit=limit,
        cohort_filter=cohort,
        requested_controls=controls,
    )


# ── report rendering ─────────────────────────────────────────────────────


def _render_outcome(outcome: DryRunOutcome) -> list[str]:
    hub = outcome.hub
    control_tag = " (control)" if outcome.is_control else ""
    lines = [
        f"## fi{hub.ref_id} — cohort={hub.cohort} score={hub.score}{control_tag}",
        "",
        f"**Original**: {outcome.claim_sentence}",
        "",
    ]
    if outcome.verdict == "error":
        lines.append(f"**Error**: {outcome.error}")
    elif outcome.extraction is not None:
        extraction = outcome.extraction
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
            lines.extend(
                f"- {nc['text']} — {nc['reason']}" for nc in extraction.not_claims
            )
    lines.append("")
    lines.append(f"**Verdict**: {outcome.verdict.upper()}")
    lines.append("")
    return lines


def render_report(report: DryRunReport) -> str:
    """Render a :class:`DryRunReport` as markdown for a human reviewer:
    summary counts up top, then one section per hub (id, cohort+score,
    original sentence, proposed atoms/compound/not_claims, one-word
    verdict)."""
    counts = report.counts
    lines = ["# Taproot migration dry-run report", ""]
    scope_bits = [f"limit={report.requested_limit}"]
    if report.cohort_filter is not None:
        scope_bits.append(f"cohort={report.cohort_filter}")
    if report.requested_controls:
        scope_bits.append(f"controls={report.requested_controls}")
    lines.append(f"**Scope**: {', '.join(scope_bits)}")
    lines.append(f"**Evaluated**: {len(report.outcomes)} hub(s)")
    lines.append("")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for verdict in ("pass-through", "split", "no-claim", "error"):
        lines.append(f"| {verdict} | {counts.get(verdict, 0)} |")
    lines.append("")

    for outcome in report.outcomes:
        lines.extend(_render_outcome(outcome))

    return "\n".join(lines)
