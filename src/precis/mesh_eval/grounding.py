"""Grounding-auditor false-approval evals (gr634496).

Before the grounding auditor (:mod:`precis.graph.garden.grounding`) is
trusted to propose — let alone apply — repairs, its judgments need a
measured false-approval rate. This module is the harness: a JSONL corpus
of cases with adjudicated gold, a database-free runner that drives the
*production* pipeline pieces (span scan, classification, passage
verification, warrant review, lineage rollup, repair policy) against
stubbed model replies and stubbed source passages, and scorers that
attribute every miss to the stage that caused it.

Four stages, kept apart so a failure names its cause:

- ``extraction`` — a gold claim was not reported, was mis-kinded, lost its
  evidence handle, or sat past a scan bound.
- ``retrieval`` — the claim cites evidence, but no passage came back.
- ``verification`` — the passage/warrant judgment disagrees with gold.
- ``policy`` — the judgments were right but the rollup (lineage, terms,
  freshness) produced the wrong scoped verdict.

A fluent judgment or a resolved reference never passes alone: support
needs a retrieved passage *and* a verifier verdict, so a claim whose
citation resolves but returns no passage rolls up ``unresolved``.

Replay vs live: each case carries ``recorded`` model replies (classifier,
support verifier, warrant reviewer, conflict probe). The default run
replays them, which tests the pipeline's policy deterministically; a live
trial passes real ``call`` / ``support_fn`` callables and keeps the same
gold. Repairs are scored in :func:`score_repairs`, apart from audit
judgments. Rates carry Wilson intervals; no pass/fail threshold exists
here — :func:`evaluate_criteria` applies only what an owner-written
criteria file states.

Gold claim fields: ``quote`` (verbatim), ``kind``, ``status`` (one of
:data:`GOLD_STATUSES`), optional ``substantive`` (default true),
``evidence`` (handles the claim must cite), ``support`` (the verifier's
expected verdict when it differs from ``status``), ``independent`` (the
cited evidence is independent of the claim's other premises),
``missing_check`` (the follow-up an unresolved claim needs) and
``repair`` (the repair it earns: ``rewrite`` / ``qualify`` / ``ask`` /
``none``). Case-level: ``scan_truncated``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from precis.errors import BadInput
from precis.graph.garden import grounding as g
from precis.mesh_eval.stats import wilson_interval

#: Gold status vocabulary. ``abstain`` = the right answer is "unresolved".
GOLD_STATUSES: tuple[str, ...] = (
    "supported",
    "partial",
    "unsupported",
    "contradicted",
    "abstain",
    "not_applicable",
)
CATEGORIES: tuple[str, ...] = (
    "missing_citation",
    "wrong_claim_citation",
    "quantity_condition_overreach",
    "defined_term_existence",
    "ambiguity",
    "author_assertion_vs_mechanism",
    "simulation_vs_experiment",
    "weak_retracted_conflicting",
    "corrupt_extraction",
    "cycle",
    "copied_sources",
    "missing_premise",
    "supported_multistep",
    "unresolved_hypothesis",
    "source_edit",
    "truncated_scan",
    "research_handoff",
)
STAGES: tuple[str, ...] = ("extraction", "retrieval", "verification", "policy")
DEFAULT_CASES = Path(__file__).parent / "data" / "grounding_cases.jsonl"
_FLAGGED = frozenset({"partial", "unsupported", "contradicted"})


@dataclass(frozen=True, slots=True)
class GoldClaim:
    quote: str
    kind: str
    status: str
    substantive: bool = True
    evidence: tuple[str, ...] = ()
    support: str = ""
    independent: bool | None = None
    missing_check: str | None = None
    repair: str | None = None


@dataclass(frozen=True, slots=True)
class StubSource:
    """A stand-in cited source: the passage retrieval would return."""

    text: str
    paper: str = ""
    sha: str = "sha-1"
    #: The sha the audit pinned; a differing ``sha`` is a source edit.
    audited_sha: str = ""
    #: False simulates a retrieval outage for a source that does exist.
    retrievable: bool = True


@dataclass(frozen=True, slots=True)
class GroundingCase:
    case_id: str
    category: str
    title: str
    body: str
    gold: tuple[GoldClaim, ...]
    sources: dict[str, StubSource]
    recorded: dict[str, Any]
    #: Memory handles / refs that exist (resolve) without a passage.
    resolvable: tuple[str, ...] = ()
    scan_truncated: bool = False
    adjudication: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _gold_claim(case_id: str, raw: Any) -> GoldClaim:
    if not isinstance(raw, dict):
        raise BadInput(f"{case_id}: gold claim must be an object")
    status = raw.get("status")
    if status not in GOLD_STATUSES:
        raise BadInput(f"{case_id}: gold claim needs a status in {GOLD_STATUSES}")
    quote = raw.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise BadInput(f"{case_id}: gold claim needs a quote")
    return GoldClaim(
        quote=quote,
        kind=str(raw.get("kind") or ""),
        status=status,
        substantive=bool(raw.get("substantive", True)),
        evidence=tuple(raw.get("evidence") or ()),
        support=str(raw.get("support") or ""),
        independent=raw.get("independent"),
        missing_check=raw.get("missing_check"),
        repair=raw.get("repair"),
    )


def _pad(body: str, raw: dict[str, Any]) -> str:
    n = int(raw.get("pad_sentences") or 0)
    filler = " ".join(
        f"Maintenance note {i} records routine upkeep of the shared notebook."
        for i in range(n)
    )
    if "{PAD}" in body:
        return body.replace("{PAD}", filler)
    return f"{body} {filler}".strip() if n else body


def parse_case(raw: Any) -> GroundingCase:
    """One JSONL object -> a validated :class:`GroundingCase`."""
    if not isinstance(raw, dict):
        raise BadInput("grounding case must be a JSON object")
    case_id = str(raw.get("id") or "")
    if not case_id:
        raise BadInput("grounding case needs an id")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise BadInput(f"{case_id}: category must be one of {len(CATEGORIES)} listed")
    gold_raw = raw.get("gold")
    if not isinstance(gold_raw, dict) or not gold_raw.get("claims"):
        raise BadInput(f"{case_id}: gold.claims is required (gold status per claim)")
    gold = tuple(_gold_claim(case_id, c) for c in gold_raw["claims"])
    sources = {
        handle: StubSource(
            text=str(s.get("text") or ""),
            paper=str(s.get("paper") or ""),
            sha=str(s.get("sha") or "sha-1"),
            audited_sha=str(s.get("audited_sha") or s.get("sha") or "sha-1"),
            retrievable=bool(s.get("retrievable", True)),
        )
        for handle, s in (raw.get("sources") or {}).items()
    }
    return GroundingCase(
        case_id=case_id,
        category=category,
        title=str(raw.get("title") or case_id),
        body=_pad(str(raw.get("body") or ""), raw),
        gold=gold,
        sources=sources,
        recorded=dict(raw.get("recorded") or {}),
        resolvable=tuple(raw.get("resolvable") or ()),
        scan_truncated=bool(gold_raw.get("scan_truncated", False)),
        adjudication=dict(raw.get("adjudication") or {}),
        note=str(raw.get("note") or ""),
    )


def load_cases(path: Path | str | None = None) -> list[GroundingCase]:
    """Load a JSONL corpus; a malformed line fails the whole load."""
    src = Path(path) if path is not None else DEFAULT_CASES
    cases: list[GroundingCase] = []
    seen: set[str] = set()
    for n, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        try:
            case = parse_case(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BadInput(f"{src}:{n}: invalid JSON ({exc})") from exc
        if case.case_id in seen:
            raise BadInput(f"{src}:{n}: duplicate case id {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    return cases


# ── stubs ────────────────────────────────────────────────────────────


@dataclass
class CostMeter:
    classifier_calls: int = 0
    verifier_calls: int = 0
    warrant_calls: int = 0
    prompt_chars: int = 0

    @property
    def calls(self) -> int:
        return self.classifier_calls + self.verifier_calls + self.warrant_calls


def _span_index(body: str, title: str, quote: str) -> int | None:
    scan = g.extract_claim_spans(body, title)
    for i, (text, _span) in enumerate(scan.spans):
        if quote in text:
            return i
    return None


def stub_classifier(case: GroundingCase, meter: CostMeter) -> g.Call:
    """Replay the case's recorded classifier reply.

    Recorded entries omit ``i``; it is resolved from the quote against the
    production span scan. Spans no entry names are returned as ``nonclaim``
    — an omitted substantive sentence is therefore a recorded miss. A
    ``classifier_raw`` string is replayed verbatim (corrupt replies).
    """
    raw = case.recorded.get("classifier_raw")
    entries = list((case.recorded.get("classifier") or {}).get("claims") or [])

    def call(prompt: str) -> tuple[str, str | None]:
        meter.classifier_calls += 1
        meter.prompt_chars += len(prompt)
        if isinstance(raw, str):
            return raw, None
        scan = g.extract_claim_spans(case.body, case.title)
        out: list[dict[str, Any]] = []
        named: set[int] = set()
        for entry in entries:
            idx = _span_index(case.body, case.title, str(entry.get("quote") or ""))
            if idx is None:
                continue
            named.add(idx)
            out.append({**entry, "i": idx})
        out.extend(
            {"i": i, "kind": "nonclaim"}
            for i in range(len(scan.spans))
            if i not in named
        )
        out.sort(key=lambda e: e["i"])
        return json.dumps({"claims": out}), None

    return call


def stub_retrieve(case: GroundingCase, handles: Sequence[str]) -> list[Any]:
    """Passages for cited handles: only retrievable stub sources return one."""
    from precis.taproot.reground import PaperChunk

    chunks = []
    for handle in handles:
        src = case.sources.get(handle)
        if src is None or not src.retrievable:
            continue
        digits = re.sub(r"\D", "", handle) or "0"
        chunks.append(PaperChunk(int(digits), 0, None, src.text))
    return chunks


def stub_support(case: GroundingCase, meter: CostMeter) -> g.SupportFn:
    recorded = case.recorded.get("support") or {}

    def fn(claim: g.ClaimRecord, passages: Sequence[Any]) -> g.RawSupport | None:
        meter.verifier_calls += 1
        meter.prompt_chars += len(claim.text) + sum(len(p.text) for p in passages)
        entry = recorded.get(claim.text)
        if entry is None:
            return None
        return g.RawSupport(**entry)

    return fn


def stub_warrant(case: GroundingCase, meter: CostMeter) -> g.Call:
    recorded = case.recorded.get("warrants") or {}

    def call(prompt: str) -> tuple[str, str | None]:
        meter.warrant_calls += 1
        meter.prompt_chars += len(prompt)
        data = json.loads(prompt.rsplit("\n", 1)[1])
        entry = recorded.get(data["claim"])
        if entry is None:
            return "", "no recorded warrant review"
        return json.dumps(entry), None

    return call


# ── run ──────────────────────────────────────────────────────────────


@dataclass
class CaseRun:
    """One audited case: the record plus the pipeline's side outputs."""

    case: GroundingCase
    record: g.AuditRecord
    lineage: Any
    rollups: dict[str, tuple[str, str]]
    cost: CostMeter
    retrieved: dict[int, list[str]]  # claim index -> handles that returned a passage


def run_case(
    case: GroundingCase,
    *,
    call: g.Call | None = None,
    support_fn: g.SupportFn | None = None,
    warrant_call: g.Call | None = None,
) -> CaseRun:
    """Audit one case end to end without a database.

    Live trials pass real ``call`` / ``support_fn`` / ``warrant_call``;
    the defaults replay ``case.recorded``. Source passages and handles
    always come from the case's stub sources.
    """
    from precis.graph.garden.lineage import ClaimLineageVerdict

    meter = CostMeter()
    classify = call or stub_classifier(case, meter)
    verify = support_fn or stub_support(case, meter)
    warrant = warrant_call or stub_warrant(case, meter)
    links = ", ".join(sorted(case.sources))
    record = g.audit_body(
        case.body, case.title, "sha-current", call=classify, candidate_links=links
    )
    retrieved: dict[int, list[str]] = {}
    conflicts = case.recorded.get("conflict") or {}
    spent = 0
    for i, claim in enumerate(record.claims):
        eligible = claim.kind in ("empirical", "source_report") and bool(claim.evidence)
        if not eligible or spent >= g.MAX_VERIFY_CLAIMS:
            continue
        passages = stub_retrieve(case, claim.evidence)
        retrieved[i] = [h for h in claim.evidence if stub_retrieve(case, [h])]
        claim.passages = list(retrieved[i])
        verdict = g.verify_claim_support(claim, passages, support_fn=verify)
        claim.support = asdict(verdict)
        spent += 1
        probe = conflicts.get(claim.text)
        if probe is not None:
            claim.conflict = asdict(g.ConflictProbe(**probe))
    g.review_warrants(record, warrant, external_premise=lambda _id: None)
    judgments = case.recorded.get("premise_judgments") or {}

    def root(handle: str) -> str:
        src = case.sources.get(handle)
        return (src.paper or handle) if src is not None else handle

    # Lineage compares shas by root key (a paper's chunks are one source).
    now = {root(h): s.sha for h, s in case.sources.items()}
    pinned = {root(h): s.audited_sha for h, s in case.sources.items()}
    report, rollups = g.rollup_with(
        record,
        is_self_certified=lambda _c: False,
        resolve_source=lambda h: h in case.sources or h in case.resolvable,
        known_cycles=[],
        premise_verdict=lambda h: (
            ClaimLineageVerdict(verdict=judgments[h]) if h in judgments else None
        ),
        source_key=root,
        source_sha=lambda key: now.get(key),
        audited_sha=pinned,
    )
    return CaseRun(case, record, report, rollups, meter, retrieved)


# ── scoring ──────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return " ".join(text.split())


def match_claims(
    gold: Sequence[GoldClaim], claims: Sequence[g.ClaimRecord]
) -> dict[int, int]:
    """Greedy 1:1 gold index -> audit claim index by quote (whitespace
    normalised; containment either way). Unchecked entries never match."""
    out: dict[int, int] = {}
    used: set[int] = set()
    for gi, gc in enumerate(gold):
        want = _norm(gc.quote)
        for ci, claim in enumerate(claims):
            if ci in used or claim.kind in ("", "nonclaim"):
                continue
            have = _norm(claim.text)
            if have == want or want in have or have in want:
                out[gi] = ci
                used.add(ci)
                break
    return out


def system_status(claim: g.ClaimRecord) -> str:
    """The audit's verdict on a claim in gold vocabulary."""
    if claim.kind in ("definition", "human_decision"):
        return "not_applicable"
    return claim.rollup or "unresolved"


def _comparable(status: str) -> str:
    return "abstain" if status == "unresolved" else status


@dataclass
class ClaimOutcome:
    case_id: str
    category: str
    quote: str
    gold_status: str
    system_status: str  # "" when the claim was never extracted
    matched: bool
    correct: bool
    #: ``false_approval`` / ``false_alarm`` / ``missed`` / ``abstained`` /
    #: ``over_abstained`` / ``mismatch`` / ``ok``.
    outcome: str
    stage: str  # "" when correct
    disclosed: bool = False  # a miss the coverage record admits to
    reason: str = ""


def _expected_support(gc: GoldClaim) -> str:
    return gc.support or _verify_equivalent(gc.status)


def _verify_equivalent(status: str) -> str:
    return {"abstain": "unresolved"}.get(status, status)


def attribute_stage(
    gc: GoldClaim, claim: g.ClaimRecord | None, run: CaseRun, ci: int | None
) -> tuple[str, str]:
    """The faulty stage for a wrong outcome, and the reason."""
    if claim is None or ci is None:
        return "extraction", "gold claim was not reported by the classifier"
    if gc.kind and claim.kind != gc.kind:
        return "extraction", f"kind {claim.kind or '?'} != gold {gc.kind}"
    missing = [h for h in gc.evidence if h not in claim.evidence]
    if missing:
        return "extraction", f"evidence handle not extracted: {', '.join(missing)}"
    if claim.kind in ("empirical", "source_report") and gc.evidence:
        if not run.retrieved.get(ci):
            return "retrieval", "no passage retrieved for the cited evidence"
    if claim.kind == "inference":
        if gc.support and claim.warrant != gc.support:
            return "verification", f"warrant {claim.warrant or '?'} != {gc.support}"
    elif claim.support is not None:
        got = str(claim.support.get("verdict"))
        if got != _expected_support(gc):
            return "verification", f"verifier {got} != expected {_expected_support(gc)}"
    return "policy", f"rollup {claim.rollup or 'unresolved'}: {claim.rollup_reason}"


def score_run(run: CaseRun) -> list[ClaimOutcome]:
    """Per gold claim: matched?, status confusion bucket, faulty stage."""
    case, claims = run.case, run.record.claims
    matches = match_claims(case.gold, claims)
    disclosed = bool(
        run.record.coverage.get("truncated")
        or run.record.coverage.get("classifier_incomplete")
    )
    out: list[ClaimOutcome] = []
    for gi, gc in enumerate(case.gold):
        ci = matches.get(gi)
        claim = claims[ci] if ci is not None else None
        if claim is None:
            stage, reason = attribute_stage(gc, None, run, None)
            out.append(
                ClaimOutcome(
                    case.case_id,
                    case.category,
                    gc.quote,
                    gc.status,
                    "",
                    False,
                    False,
                    "missed",
                    stage,
                    disclosed,
                    reason,
                )
            )
            continue
        sys_status = system_status(claim)
        gold_cmp, sys_cmp = _comparable(gc.status), _comparable(sys_status)
        if sys_cmp == "supported" and gold_cmp != "supported":
            outcome = "false_approval"
        elif sys_cmp in _FLAGGED and gold_cmp == "supported":
            outcome = "false_alarm"
        elif sys_cmp == "abstain" and gold_cmp == "abstain":
            outcome = "abstained"
        elif sys_cmp == "abstain":
            outcome = "over_abstained"
        elif sys_cmp != gold_cmp:
            outcome = "mismatch"
        else:
            outcome = "ok"
        correct = outcome in ("ok", "abstained")
        stage, reason = ("", "")
        if not correct:
            stage, reason = attribute_stage(gc, claim, run, ci)
        elif gc.kind and claim.kind != gc.kind:
            # right verdict, wrong kind: still an extraction error
            stage, reason = "extraction", f"kind {claim.kind} != gold {gc.kind}"
        out.append(
            ClaimOutcome(
                case.case_id,
                case.category,
                gc.quote,
                gc.status,
                sys_status,
                True,
                correct and not stage,
                outcome,
                stage,
                False,
                reason,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Rate:
    k: int
    n: int
    lo: float
    hi: float

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None


def rate(k: int, n: int) -> Rate:
    lo, hi = wilson_interval(k, n)
    return Rate(k, n, lo, hi)


@dataclass
class GroundingReport:
    cases: int
    categories: dict[str, dict[str, Any]]
    metrics: dict[str, Rate]
    stages: dict[str, Rate]
    cost: dict[str, Any]
    coverage: dict[str, Any]
    independence: Rate
    handoff: Rate
    adjudication: dict[str, int]
    failures: list[ClaimOutcome]

    def to_json(self) -> dict[str, Any]:
        def r(x: Rate) -> dict[str, Any]:
            return {"k": x.k, "n": x.n, "rate": x.value, "lo": x.lo, "hi": x.hi}

        return {
            "cases": self.cases,
            "categories": self.categories,
            "metrics": {k: r(v) for k, v in self.metrics.items()},
            "stages": {k: r(v) for k, v in self.stages.items()},
            "cost": self.cost,
            "coverage": {
                k: r(v) if isinstance(v, Rate) else v for k, v in self.coverage.items()
            },
            "independence": r(self.independence),
            "handoff": r(self.handoff),
            "adjudication": self.adjudication,
            "failures": [asdict(f) for f in self.failures],
        }


def _independent(run: CaseRun, ci: int) -> bool:
    verdict = run.lineage.verdicts.get(f"c{ci}")
    claim = run.record.claims[ci]
    if verdict is None or not claim.evidence:
        return False
    return bool(verdict.independent_evidence)


def build_report(runs: Sequence[CaseRun]) -> GroundingReport:
    outcomes: list[ClaimOutcome] = []
    per_case: dict[str, list[ClaimOutcome]] = {}
    indep_k = indep_n = ho_k = ho_n = 0
    retr_k = retr_n = ver_n = pol_n = 0
    kind_k = kind_n = 0
    insp = total = trunc_k = 0
    for run in runs:
        scored = score_run(run)
        outcomes.extend(scored)
        per_case[run.case.case_id] = scored
        claims = run.record.claims
        matches = match_claims(run.case.gold, claims)
        cov = run.record.coverage
        insp += int(cov.get("spans_inspected") or 0)
        total += int(cov.get("spans_total") or 0)
        trunc_k += bool(cov.get("truncated")) == run.case.scan_truncated
        for gi, gc in enumerate(run.case.gold):
            ci = matches.get(gi)
            if gc.evidence and gc.kind in ("empirical", "source_report"):
                retr_n += 1
                got = ci is not None and bool(run.retrieved.get(ci))
                retr_k += (not got) and ci is not None
            if ci is None:
                continue
            claim = claims[ci]
            if gc.kind:
                kind_n += 1
                kind_k += claim.kind == gc.kind
            if claim.kind in ("empirical", "source_report", "inference"):
                ver_n += 1
            if claim.kind not in ("", "nonclaim"):
                pol_n += 1
            if gc.independent is not None:
                indep_n += 1
                indep_k += _independent(run, ci) == gc.independent
            if gc.missing_check is not None:
                ho_n += 1
                ho_k += claim.missing_check == gc.missing_check
    substantive = [o for o in outcomes if _is_substantive(runs, o)]
    matched = [o for o in outcomes if o.matched]
    gold_unsupported = [o for o in matched if o.gold_status != "supported"]
    gold_supported = [o for o in matched if o.gold_status == "supported"]
    gold_abstain = [o for o in matched if o.gold_status == "abstain"]
    non_abstain = [o for o in matched if o.gold_status != "abstain"]
    count = Counter(o.outcome for o in outcomes)
    metrics = {
        "extraction_recall": rate(len(matched), len(outcomes)),
        "missed_substantive_claims": rate(
            sum(1 for o in substantive if not o.matched), len(substantive)
        ),
        "kind_accuracy": rate(kind_k, kind_n),
        "status_accuracy": rate(sum(o.correct for o in matched), len(matched)),
        "false_approvals": rate(count["false_approval"], len(gold_unsupported)),
        "false_alarms": rate(count["false_alarm"], len(gold_supported)),
        "appropriate_abstentions": rate(count["abstained"], len(gold_abstain)),
        "unnecessary_abstentions": rate(count["over_abstained"], len(non_abstain)),
        "evidence_retrieval_failures": rate(retr_k, retr_n),
    }
    stage_n = {
        "extraction": len(outcomes),
        "retrieval": retr_n,
        "verification": ver_n,
        "policy": pol_n,
    }
    stage_k = Counter(o.stage for o in outcomes if o.stage)
    stages = {s: rate(stage_k[s], stage_n[s]) for s in STAGES}
    categories: dict[str, dict[str, Any]] = {}
    for cat in CATEGORIES:
        rows = [o for o in outcomes if o.category == cat]
        cats = Counter(o.outcome for o in rows)
        categories[cat] = {
            "cases": len({o.case_id for o in rows}),
            "claims": len(rows),
            "correct": sum(o.correct for o in rows),
            "false_approvals": cats["false_approval"],
            "false_alarms": cats["false_alarm"],
            "missed": cats["missed"],
            "stages": dict(Counter(o.stage for o in rows if o.stage)),
        }
    cost = {
        "cases": len(runs),
        "model_calls": sum(r.cost.calls for r in runs),
        "classifier_calls": sum(r.cost.classifier_calls for r in runs),
        "verifier_calls": sum(r.cost.verifier_calls for r in runs),
        "warrant_calls": sum(r.cost.warrant_calls for r in runs),
        "prompt_chars": sum(r.cost.prompt_chars for r in runs),
        "prompt_chars_per_case": (
            sum(r.cost.prompt_chars for r in runs) // len(runs) if runs else 0
        ),
    }
    coverage = {
        "spans_inspected": insp,
        "spans_total": total,
        "span_coverage": rate(insp, total),
        "truncation_flag_agrees": rate(trunc_k, len(runs)),
        "misses_disclosed": sum(
            1 for o in outcomes if o.outcome == "missed" and o.disclosed
        ),
        "misses_silent": sum(
            1 for o in outcomes if o.outcome == "missed" and not o.disclosed
        ),
    }
    adjudication = Counter(
        "independent" if r.case.adjudication.get("independent") else "unreviewed"
        for r in runs
    )
    return GroundingReport(
        cases=len(runs),
        categories=categories,
        metrics=metrics,
        stages=stages,
        cost=cost,
        coverage=coverage,
        independence=rate(indep_k, indep_n),
        handoff=rate(ho_k, ho_n),
        adjudication=dict(adjudication),
        failures=[o for o in outcomes if not o.correct or o.stage],
    )


def _is_substantive(runs: Sequence[CaseRun], outcome: ClaimOutcome) -> bool:
    for run in runs:
        if run.case.case_id == outcome.case_id:
            return any(
                gc.quote == outcome.quote and gc.substantive for gc in run.case.gold
            )
    return False


def run_corpus(
    cases: Sequence[GroundingCase],
    *,
    runner: Callable[[GroundingCase], CaseRun] = run_case,
) -> GroundingReport:
    return build_report([runner(c) for c in cases])


# ── repairs, scored apart from audit judgments ──────────────────────


@dataclass
class RepairReport:
    """Repair proposals vs gold ``repair``: a wrong repair is a policy
    error even when the judgment under it was right, so this suite never
    feeds the audit-judgment rates above."""

    claims: int
    correct: Rate
    spurious: Rate
    missing: Rate
    rows: list[dict[str, str]]


def score_repairs(runs: Sequence[CaseRun]) -> RepairReport:
    rows: list[dict[str, str]] = []
    n = k = spurious = missing = n_none = n_some = 0
    for run in runs:
        matches = match_claims(run.case.gold, run.record.claims)
        for gi, gc in enumerate(run.case.gold):
            if gc.repair is None:
                continue
            ci = matches.get(gi)
            got = "none"
            if ci is not None:
                claim = run.record.claims[ci]
                cid = f"c{ci}"
                action = g.repair_action(
                    claim,
                    claim.rollup,
                    cid in run.lineage.unresolved_premises,
                )
                got = action or "none"
            n += 1
            k += got == gc.repair
            if gc.repair == "none":
                n_none += 1
                spurious += got != "none"
            else:
                n_some += 1
                missing += got == "none"
            if got != gc.repair:
                rows.append(
                    {
                        "case": run.case.case_id,
                        "quote": gc.quote,
                        "gold": gc.repair,
                        "got": got,
                    }
                )
    return RepairReport(
        claims=n,
        correct=rate(k, n),
        spurious=rate(spurious, n_none),
        missing=rate(missing, n_some),
        rows=rows,
    )


# ── criteria (owner-supplied; nothing is defaulted) ─────────────────

#: criteria key -> how it reads the report. ``*_upper_max`` compares the
#: Wilson upper bound, ``*_lower_min`` the lower bound.
CRITERIA_KEYS: dict[str, tuple[str, str]] = {
    "false_approval_upper_max": ("false_approvals", "hi"),
    "false_alarm_upper_max": ("false_alarms", "hi"),
    "missed_substantive_upper_max": ("missed_substantive_claims", "hi"),
    "retrieval_failure_upper_max": ("evidence_retrieval_failures", "hi"),
    "status_accuracy_lower_min": ("status_accuracy", "lo"),
    "span_coverage_lower_min": ("span_coverage", "lo"),
}
_BUDGET_KEYS = ("prompt_chars_per_case_max", "model_calls_per_case_max")


def load_criteria(path: Path | str) -> dict[str, float | None]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BadInput("criteria file must be a JSON object")
    allowed = set(CRITERIA_KEYS) | set(_BUDGET_KEYS) | {"_note"}
    unknown = set(data) - allowed
    if unknown:
        raise BadInput(f"unknown criteria keys: {sorted(unknown)}")
    out: dict[str, float | None] = {}
    for key, value in data.items():
        if key == "_note":
            continue
        if value is not None and not isinstance(value, int | float):
            raise BadInput(f"criteria {key} must be a number or null")
        out[key] = None if value is None else float(value)
    return out


def evaluate_criteria(
    report: GroundingReport, criteria: dict[str, float | None]
) -> list[dict[str, Any]]:
    """One row per *set* criterion; blank (null) criteria are not judged.
    Each row: ``name``, ``threshold``, ``observed``, ``passed``."""
    rows: list[dict[str, Any]] = []
    metrics = dict(report.metrics)
    metrics["span_coverage"] = report.coverage["span_coverage"]
    for key, (metric, edge) in CRITERIA_KEYS.items():
        limit = criteria.get(key)
        if limit is None:
            continue
        r = metrics[metric]
        observed = r.hi if edge == "hi" else r.lo
        passed = observed <= limit if key.endswith("_max") else observed >= limit
        rows.append(
            {"name": key, "threshold": limit, "observed": observed, "passed": passed}
        )
    per_case = report.cost["cases"] or 1
    for key, observed in (
        ("prompt_chars_per_case_max", report.cost["prompt_chars_per_case"]),
        ("model_calls_per_case_max", report.cost["model_calls"] / per_case),
    ):
        limit = criteria.get(key)
        if limit is not None:
            rows.append(
                {
                    "name": key,
                    "threshold": limit,
                    "observed": observed,
                    "passed": observed <= limit,
                }
            )
    return rows


# ── formatting ───────────────────────────────────────────────────────


def _fmt(r: Rate) -> str:
    if r.n == 0:
        return "n/a (0/0)"
    return f"{r.k}/{r.n} = {r.k / r.n:.3f}  [95% Wilson {r.lo:.3f}-{r.hi:.3f}]"


def format_report(
    report: GroundingReport,
    *,
    criteria_rows: list[dict[str, Any]] | None = None,
    repairs: RepairReport | None = None,
) -> str:
    lines = [
        f"grounding eval: {report.cases} cases, "
        f"{sum(c['claims'] for c in report.categories.values())} gold claims",
        f"adjudication: {report.adjudication or {}} (only 'independent' cases "
        "count as independently adjudicated)",
        "",
        "metrics (k/n, Wilson 95%):",
    ]
    for name, r in report.metrics.items():
        lines.append(f"  {name:32s} {_fmt(r)}")
    lines.append(f"  {'source_independence':32s} {_fmt(report.independence)}")
    lines.append(f"  {'handoff_check_accuracy':32s} {_fmt(report.handoff)}")
    lines += ["", "faulty stage (errors / opportunities):"]
    for stage, r in report.stages.items():
        lines.append(f"  {stage:14s} {_fmt(r)}")
    cov = report.coverage
    lines += [
        "",
        f"coverage: spans {cov['spans_inspected']}/{cov['spans_total']} inspected; "
        f"truncation flag agrees {_fmt(cov['truncation_flag_agrees'])}; "
        f"misses disclosed {cov['misses_disclosed']}, silent {cov['misses_silent']}",
        f"cost: {report.cost['model_calls']} model calls "
        f"({report.cost['classifier_calls']} classify, "
        f"{report.cost['verifier_calls']} verify, "
        f"{report.cost['warrant_calls']} warrant), "
        f"{report.cost['prompt_chars']} prompt chars "
        f"({report.cost['prompt_chars_per_case']}/case)",
        "",
        f"{'category':32s} cases claims correct  FA  alarm miss  stages",
    ]
    for cat, row in report.categories.items():
        stages = ",".join(f"{s}:{n}" for s, n in sorted(row["stages"].items())) or "-"
        lines.append(
            f"{cat:32s} {row['cases']:5d} {row['claims']:6d} {row['correct']:7d} "
            f"{row['false_approvals']:3d} {row['false_alarms']:5d} "
            f"{row['missed']:4d}  {stages}"
        )
    if repairs is not None:
        lines += [
            "",
            "repairs (scored apart from audit judgments):",
            f"  repair_correct   {_fmt(repairs.correct)}",
            f"  spurious_repair  {_fmt(repairs.spurious)}",
            f"  missing_repair   {_fmt(repairs.missing)}",
        ]
    if criteria_rows is None:
        lines += ["", "no --criteria: report only, no pass/fail."]
    elif not criteria_rows:
        lines += ["", "criteria file sets no values: report only, no pass/fail."]
    else:
        lines.append("")
        for row in criteria_rows:
            mark = "PASS" if row["passed"] else "FAIL"
            lines.append(
                f"  {mark} {row['name']}: observed {row['observed']:.3f} "
                f"vs {row['threshold']}"
            )
    return "\n".join(lines)


__all__ = [
    "CATEGORIES",
    "DEFAULT_CASES",
    "GOLD_STATUSES",
    "STAGES",
    "CaseRun",
    "ClaimOutcome",
    "GoldClaim",
    "GroundingCase",
    "GroundingReport",
    "Rate",
    "RepairReport",
    "build_report",
    "evaluate_criteria",
    "format_report",
    "load_cases",
    "load_criteria",
    "parse_case",
    "run_case",
    "run_corpus",
    "score_repairs",
    "score_run",
]
