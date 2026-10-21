"""The sampled reviewer — a frontier-model burn-down over open proposals.

Proposals pile up faster than a human reads them. :func:`review_sample`
takes up to ``per_day`` still-pending, still-current garden proposals
older than ``min_age_days`` — oldest first, global over detectors — asks
a reviewing model (Opus, FRONTIER tier) for ``approve``/``reject`` plus a
one-line why, and records each answer as a NON-TERMINAL review row. A
Fable re-review runs beside it on the first ``fable_per_day`` of the
day's sampled proposals (actor ``garden-reviewer-fable``, same payload
shape) so Opus's verdicts get an independent check:

- actor ``garden-reviewer``; the payload lives under
  ``note.garden_review`` (``verdict``, ``why``, ``sampled: true``,
  ``reviewed_by_model``, plus ``proposal``/``key``/``detector`` for
  joining back) — never under ``note.garden``, so the pending fold in
  :mod:`precis.graph.garden.proposals` does not see it and the proposal
  stays open for a human.
- It must NOT call ``resolve_proposal``: a terminal ``approved`` row
  echoing the garden key would retire the key and read as applied for
  a merge. Merge stays the only auto-apply path; the reviewer is an
  opinion, not a bar.

The agreement rates per detector — the share of sampled proposals the
Opus reviewer approved (proposer–Opus agreement) and, on the paired
subset, the share where Fable's verdict matches Opus's (Opus–Fable
agreement) — are the proposer's quality bar per local-mesh-upkeep: a
detector the reviewer keeps rejecting is noise.
:func:`agreement_stats` reports them (``precis garden stats``);
reporting only, no gate. ``model_call`` / ``fable_call`` are the test
seams — the defaults route through the same ``route()`` seam the
proposer uses (``graph_garden:review`` at FRONTIER;
``graph_garden:review_fable`` pinned to the fable id).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.graph.garden.proposals import (
    GARDEN_ACTOR,
    GARDEN_VERSION,
    GardenProposal,
    _parse_note,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The reviews.actor the Opus sample's rows carry; the Fable re-review's
#: rows carry ``REVIEWER_ACTOR_FABLE``.
REVIEWER_ACTOR = "garden-reviewer"
REVIEWER_ACTOR_FABLE = "garden-reviewer-fable"

#: Registered operation sources — ``llm.op.graph_garden:review`` /
#: ``llm.op.graph_garden:review_fable`` steer tier/model/chain at
#: runtime (see ``LLM_OPERATIONS``).
OP_SOURCE_REVIEW = "graph_garden:review"
OP_SOURCE_REVIEW_FABLE = "graph_garden:review_fable"

DEFAULT_PER_DAY = 20
DEFAULT_MIN_AGE_DAYS = 3
DEFAULT_FABLE_PER_DAY = 5

#: How far past ``per_day`` the candidate scan may read — resolved,
#: stale and already-reviewed rows do not count against the day's
#: sample, so the scan over-reads to fill it.
_SCAN_FACTOR = 25


@dataclass(frozen=True, slots=True)
class ReviewerVerdict:
    """The reviewing model's answer for one proposal."""

    verdict: str  # "approve" | "reject" ("" only alongside error/paused)
    why: str = ""
    model: str | None = None
    error: str | None = None
    paused: bool = False  # window-scoped — the run should park, not fail


@dataclass(slots=True)
class ReviewReport:
    """One ``review_sample`` run's tallies."""

    sampled: int = 0
    approvals: int = 0
    rejections: int = 0
    errors: int = 0
    paused: bool = False
    fable_sampled: int = 0
    fable_errors: int = 0
    review_ids: list[int] = field(default_factory=list)


ModelCall = Callable[["Store", GardenProposal], ReviewerVerdict]

_PROMPT_HEAD = """You are the sampled reviewer for a knowledge graph's gardener
proposals (refs = typed nodes, links = typed edges, tags = closed
namespaces).

You are given ONE pending maintenance proposal — a cheaper proposer
already accepted it and a human would carry it out. Give your verdict on
the proposal itself: does the evidence support the action? You are a
bar, not a cheerleader — false accepts hide a noisy detector.

Respond with a single JSON object:
{"verdict": "approve"|"reject",
 "why": "one sentence naming the evidence that decided you"}

Rules: never invent a why that cites nothing in the proposal's evidence;
when unsure, reject.
"""


def _prompt_for(proposal: GardenProposal) -> str:
    return (
        _PROMPT_HEAD
        + "\nProposal:\n"
        + json.dumps(
            {
                "detector": proposal.detector,
                "action": proposal.action,
                "target": f"{proposal.target_kind}:{proposal.target_id}",
                "title": proposal.title,
                "evidence": proposal.evidence,
                "payload": proposal.payload,
                "proposer_rationale": proposal.rationale,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """First JSON object in the reviewer's reply, or None."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _routed_verdict(proposal: GardenProposal, *, source: str) -> ReviewerVerdict:
    """One routed verdict call under operation ``source``."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    result = route(
        LlmRequest(
            tier=Tier.FRONTIER,
            prompt=_prompt_for(proposal),
            source=source,
            ref_id=proposal.target_id if proposal.target_kind == "ref" else None,
            max_tokens=300,
            log_blobs=False,
        )
    )
    if getattr(result, "paused", False):
        return ReviewerVerdict(verdict="", paused=True, error=result.error)
    if result.error is not None:
        return ReviewerVerdict(verdict="", error=str(result.error))
    body = _parse_verdict(result.text or "")
    if body is None:
        return ReviewerVerdict(
            verdict="",
            error="reviewer returned no JSON object",
            model=result.model,
        )
    verdict = str(body.get("verdict") or "")
    if verdict not in ("approve", "reject"):
        return ReviewerVerdict(
            verdict="",
            error=f"unusable reviewer verdict {verdict!r}",
            model=result.model,
        )
    return ReviewerVerdict(
        verdict=verdict, why=str(body.get("why") or ""), model=result.model
    )


def review_llm(store: Store, proposal: GardenProposal) -> ReviewerVerdict:
    """The default Opus model call — one routed FRONTIER call under
    ``graph_garden:review``. Takes ``store`` it never uses: the
    ``model_call=`` seam mirrors the runner's ``propose=`` so tests
    inject a deterministic verdict."""
    del store
    return _routed_verdict(proposal, source=OP_SOURCE_REVIEW)


def fable_llm(store: Store, proposal: GardenProposal) -> ReviewerVerdict:
    """The default Fable model call — one routed call under
    ``graph_garden:review_fable`` (the op pins the fable model id).
    Same unused-``store`` seam as :func:`review_llm`."""
    del store
    return _routed_verdict(proposal, source=OP_SOURCE_REVIEW_FABLE)


def _parse_review_note(note: str | None) -> dict[str, Any] | None:
    """The ``note.garden_review`` payload of a reviewer row, or None."""
    if not note:
        return None
    try:
        body = json.loads(note)
    except (TypeError, ValueError):
        return None
    gr = body.get("garden_review")
    return gr if isinstance(gr, dict) else None


def _old_pending_rows(store: Store, cutoff: datetime, scan_limit: int) -> list[Any]:
    """Proposed garden rows older than ``cutoff``, oldest first."""
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT review_id, target_kind, target_id, model, at, note "
            "FROM reviews WHERE verdict = 'proposed' AND actor = %(actor)s "
            "  AND at < %(cutoff)s "
            "ORDER BY at, review_id LIMIT %(n)s",
            {"actor": GARDEN_ACTOR, "cutoff": cutoff, "n": scan_limit},
        ).fetchall()


def _proposal_state(
    store: Store, review_id: int, key: str, target_kind: str, target_id: int
) -> str:
    """Why a scanned row does or does not enter the day's sample:

    ``pending`` — still the open garden row for its key, current, and
    not yet reviewer-sampled; ``reviewed`` — a ``garden_review`` row by
    ``REVIEWER_ACTOR`` already names it (one Opus sample per proposal —
    a Fable row does not suppress the Opus slot); ``resolved`` — a
    newer row with the key decided or superseded it; ``stale`` — the
    target drifted past the sha the proposal was written against.
    """
    newest_same_key = None
    for rev in store.reviews_for(target_kind, target_id):
        gr = _parse_review_note(rev.note)
        if (
            gr is not None
            and gr.get("proposal") == review_id
            and rev.actor == REVIEWER_ACTOR
        ):
            return "reviewed"
        g = _parse_note(rev.note)
        if g is not None and g.get("key") == key and newest_same_key is None:
            newest_same_key = rev  # reviews_for is newest-first
    if newest_same_key is None or int(newest_same_key.review_id) != review_id:
        return "resolved"
    if not newest_same_key.current:
        return "stale"
    return "pending"


def _record_verdict(
    store: Store,
    prop: GardenProposal,
    verdict: ReviewerVerdict,
    *,
    actor: str,
) -> Any:
    """Append one non-terminal ``garden_review`` reviews row — the
    payload never carries a top-level ``garden`` object, so the pending
    fold never sees it."""
    body = json.dumps(
        {
            "garden_review": {
                "v": 1,
                "proposal": prop.review_id,
                "key": prop.key,
                "action": prop.action,
                "detector": prop.detector,
                "verdict": verdict.verdict,
                "why": verdict.why,
                "sampled": True,
                "reviewed_by_model": verdict.model,
            }
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return store.record_target_review(
        prop.target_kind,
        prop.target_id,
        actor=actor,
        verdict="approved" if verdict.verdict == "approve" else "rejected",
        model=verdict.model,
        version=GARDEN_VERSION,
        note=body,
    )


def _call_verdict(
    call: ModelCall, store: Store, prop: GardenProposal
) -> ReviewerVerdict:
    try:
        return call(store, prop)
    except Exception as exc:
        log.warning("garden reviewer: model raised on %s: %s", prop.key, exc)
        return ReviewerVerdict(verdict="", error=str(exc))


def review_sample(
    store: Store,
    *,
    model_call: ModelCall | None = None,
    fable_call: ModelCall | None = None,
    now: datetime | None = None,
    per_day: int = DEFAULT_PER_DAY,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    fable_per_day: int = DEFAULT_FABLE_PER_DAY,
) -> ReviewReport:
    """Sample up to ``per_day`` open proposals older than
    ``min_age_days`` and record the Opus model's verdict on each; then
    re-review the first ``fable_per_day`` of the day's sampled proposals
    with ``fable_call`` (actor ``garden-reviewer-fable``) so Opus's
    verdicts get an independent check.

    Selection is global (all detectors share the budget), oldest first —
    deterministic with no seed. Returns the tallies; the only writes are
    non-terminal ``garden_review`` rows.
    """
    if model_call is None:
        model_call = review_llm
    if fable_call is None:
        fable_call = fable_llm
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(days=min_age_days)
    report = ReviewReport()
    rows = _old_pending_rows(store, cutoff, scan_limit=per_day * _SCAN_FACTOR)
    sampled: list[GardenProposal] = []
    for review_id, target_kind, target_id, model, at, note in rows:
        if report.sampled >= per_day:
            break
        g = _parse_note(note)
        if g is None:
            continue
        state = _proposal_state(
            store,
            int(review_id),
            str(g.get("key") or ""),
            str(target_kind),
            int(target_id),
        )
        if state != "pending":
            continue
        prop = GardenProposal(
            review_id=int(review_id),
            target_kind=str(target_kind),
            target_id=int(target_id),
            action=str(g.get("action") or ""),
            key=str(g.get("key") or ""),
            detector=str(g.get("detector") or ""),
            title=str(g.get("title") or ""),
            payload=g.get("payload") or {},
            evidence=g.get("evidence") or {},
            rationale=str(g.get("rationale") or ""),
            model=None if model is None else str(model),
            at=at,
            current=True,
        )
        verdict = _call_verdict(model_call, store, prop)
        if verdict.paused:
            report.paused = True
            break
        if verdict.error is not None or verdict.verdict not in (
            "approve",
            "reject",
        ):
            report.errors += 1
            continue
        rev = _record_verdict(store, prop, verdict, actor=REVIEWER_ACTOR)
        report.review_ids.append(int(rev.review_id))
        report.sampled += 1
        sampled.append(prop)
        if verdict.verdict == "approve":
            report.approvals += 1
        else:
            report.rejections += 1

    # The Fable re-review: an independent second opinion on a sample of
    # the same proposals — the Opus–Fable agreement column reads off it.
    for prop in sampled[:fable_per_day]:
        verdict = _call_verdict(fable_call, store, prop)
        if verdict.paused:
            break
        if verdict.error is not None or verdict.verdict not in (
            "approve",
            "reject",
        ):
            report.fable_errors += 1
            continue
        _record_verdict(store, prop, verdict, actor=REVIEWER_ACTOR_FABLE)
        report.fable_sampled += 1
    return report


def agreement_stats(store: Store) -> dict[str, dict[str, Any]]:
    """Reviewer agreements per detector, both bars side by side:

    ``{detector: {sampled, approvals, rate, fable_sampled,
    fable_agreements, fable_rate}}`` — ``rate`` is the share of sampled
    proposals the Opus reviewer approved (proposer–Opus agreement);
    ``fable_rate`` is the share of paired proposals where the Fable
    verdict matched Opus's (Opus–Fable agreement). Reporting only — no
    gate.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT actor, note FROM reviews WHERE actor IN (%(a)s, %(b)s)",
            {"a": REVIEWER_ACTOR, "b": REVIEWER_ACTOR_FABLE},
        ).fetchall()
    opus: dict[int, tuple[str, str]] = {}  # proposal → (detector, verdict)
    fable: dict[int, tuple[str, str]] = {}
    for actor, note in rows:
        gr = _parse_review_note(note)
        if gr is None:
            continue
        pid = gr.get("proposal")
        if not isinstance(pid, int):
            continue
        pair = (str(gr.get("detector") or "(unknown)"), str(gr.get("verdict") or ""))
        if str(actor) == REVIEWER_ACTOR:
            opus[pid] = pair
        else:
            fable[pid] = pair
    out: dict[str, dict[str, Any]] = {}
    for det, verdict in opus.values():
        d = out.setdefault(
            det,
            {
                "sampled": 0,
                "approvals": 0,
                "rate": 0.0,
                "fable_sampled": 0,
                "fable_agreements": 0,
                "fable_rate": 0.0,
            },
        )
        d["sampled"] += 1
        if verdict == "approve":
            d["approvals"] += 1
    for pid, (det, fverdict) in fable.items():
        o = opus.get(pid)
        if o is None:
            continue  # no Opus row to agree with — unpaired
        d = out.setdefault(
            det,
            {
                "sampled": 0,
                "approvals": 0,
                "rate": 0.0,
                "fable_sampled": 0,
                "fable_agreements": 0,
                "fable_rate": 0.0,
            },
        )
        d["fable_sampled"] += 1
        if fverdict == o[1]:
            d["fable_agreements"] += 1
    for d in out.values():
        if d["sampled"]:
            d["rate"] = d["approvals"] / d["sampled"]
        if d["fable_sampled"]:
            d["fable_rate"] = d["fable_agreements"] / d["fable_sampled"]
    return out


__all__ = [
    "DEFAULT_FABLE_PER_DAY",
    "DEFAULT_MIN_AGE_DAYS",
    "DEFAULT_PER_DAY",
    "OP_SOURCE_REVIEW",
    "OP_SOURCE_REVIEW_FABLE",
    "REVIEWER_ACTOR",
    "REVIEWER_ACTOR_FABLE",
    "ModelCall",
    "ReviewReport",
    "ReviewerVerdict",
    "agreement_stats",
    "fable_llm",
    "review_llm",
    "review_sample",
]
