"""Premise-lineage checks for grounding audits (gr634486 slice 1).

A citation's mere presence is not support. :func:`analyze_lineage` takes
the claims of one audit (or any claim set) and computes the structural
support problems the classifier cannot see:

- **support cycles** — claim A premises on B while B premises (directly
  or transitively) on A. Circular support grounds nothing; every member
  of a cycle is ``unsupported``.
- **shared-root duplicates** — two claims citing the same source are not
  independent corroboration. A claim whose premises carry it and whose
  entire evidence list is shared with those premises has no independent
  evidence of its own, so it is at best ``partial``.
- **hidden unsupported premises** — one sourced premise cannot conceal
  another premise that has no support: any unmet necessary premise caps
  the claim at ``partial`` (or ``unsupported`` when nothing supports it).
- **definition-only paths** — a ``definition``-kind premise establishes
  meaning, not existence; it never transfers support to an empirical or
  inference claim.
- **inference depth** — the count of ``inference``-kind hops on the
  longest premise chain (graph distance is ignored: only inference hops
  count). Depth at or above :data:`INFERENCE_REVIEW_DEPTH` sets
  ``needs_source_review`` — deep derivation stacks warrants, and each
  warrant deserves a look. It does not change the verdict.
- **stale sources** — an evidence handle whose current sha no longer
  matches the sha recorded at audit time (or that no longer resolves).
- **external premise judgments** — a premise that names another audited
  memory resolves to that memory's current claim judgment, never to mere
  ref existence; a judgment of ``partial`` caps the claim at ``partial``
  even when every other premise holds.

The module is pure: no DB, no LLM. Callers supply premise/evidence
handles and optional resolvers; the grounding audit wraps its
:class:`~precis.graph.garden.grounding.ClaimRecord` list in
:class:`LineageClaim` and passes store-backed resolvers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: An inference chain this deep (inference-kind hops, not graph distance)
#: flags ``needs_source_review`` on the claim and its dependants.
INFERENCE_REVIEW_DEPTH = 3

VERDICT_SUPPORTED = "supported"
VERDICT_PARTIAL = "partial"
VERDICT_UNSUPPORTED = "unsupported"
VERDICT_UNRESOLVED = "unresolved"
LINEAGE_VERDICTS: tuple[str, ...] = (
    VERDICT_SUPPORTED,
    VERDICT_PARTIAL,
    VERDICT_UNSUPPORTED,
    VERDICT_UNRESOLVED,
)

#: Premise kinds that never transfer support: a definition establishes
#: meaning, a hypothesis is a confirmation target, a decision is
#: provenance. Only empirical / source_report / inference premises can
#: ground a claim.
_NON_TRANSFER_KINDS = frozenset({"definition", "hypothesis", "human_decision"})


@dataclass(slots=True)
class LineageClaim:
    """The lineage view of one audited claim.

    ``premises`` may name other claims in the same set (by
    :attr:`claim_id`) or external source handles. ``evidence`` lists
    external source handles. ``status`` is the independent claim assessment
    or, for inference, the warrant assessment. Classifier suggestions
    cannot provide a supported status.
    """

    claim_id: str
    kind: str = ""
    premises: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    status: str = ""


@dataclass(slots=True)
class ClaimLineageVerdict:
    """One claim's lineage outcome."""

    verdict: str
    reasons: list[str] = field(default_factory=list)
    #: Inference-kind hops on the longest premise chain.
    inference_depth: int = 0
    #: Set when inference_depth >= INFERENCE_REVIEW_DEPTH — a review
    #: flag, never a verdict change.
    needs_source_review: bool = False
    #: Evidence handles not shared with any other claim in the set.
    independent_evidence: list[str] = field(default_factory=list)
    #: Evidence handles whose source changed (or vanished) since the
    #: audited revision — a freshness cap, not a support verdict.
    stale_evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LineageReport:
    """The whole claim set's lineage analysis."""

    verdicts: dict[str, ClaimLineageVerdict]
    #: Sorted members of each strongly connected cyclic group.
    cycles: list[list[str]] = field(default_factory=list)
    #: Evidence handles cited by more than one claim — shared roots are
    #: duplicate lineage, not independent corroboration.
    shared_sources: dict[str, list[str]] = field(default_factory=dict)
    #: claim_id -> external premise handles that did not resolve.
    unresolved_premises: dict[str, list[str]] = field(default_factory=dict)
    #: Evidence handles whose current sha differs from the audited sha
    #: (or that no longer resolve).
    stale_sources: list[str] = field(default_factory=list)


def _find_cycles(ids: set[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Return the strongly connected groups that contain a cycle."""
    reachable: dict[str, set[str]] = {}
    for start in ids:
        seen: set[str] = set()
        todo = list(edges.get(start, []))
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(edges.get(node, []))
        reachable[start] = seen
    remaining = {start for start in ids if start in reachable[start]}
    groups = []
    while remaining:
        start = min(remaining)
        group = {end for end in reachable[start] if start in reachable[end]}
        groups.append(sorted(group))
        remaining.difference_update(group)
    return groups


def analyze_lineage(
    claims: Iterable[LineageClaim],
    *,
    resolve_source: Callable[[str], bool] | None = None,
    source_sha: Callable[[str], str | None] | None = None,
    audited_sha: dict[str, str] | None = None,
    known_cycles: Iterable[Iterable[str]] | None = None,
    premise_verdict: Callable[[str], ClaimLineageVerdict | None] | None = None,
    source_key: Callable[[str], str] | None = None,
) -> LineageReport:
    """Compute cycles, shared roots, unresolved premises, stale sources
    and per-claim verdicts for a claim set.

    ``resolve_source`` decides whether an external premise handle names a
    live source for diagnostics. Every external premise needs an explicit
    judgment from ``premise_verdict`` to transfer support, even if it resolves.
    ``source_key``
    normalizes evidence handles to a root identity for shared-root and
    stale detection (two chunks of one paper are one source).
    ``source_sha`` + ``audited_sha`` drive stale-source detection: a handle
    whose current sha differs from the audited one — or that no longer
    resolves — is stale. ``known_cycles`` lets a caller fold in cycles it
    detected outside this claim set (e.g. across audits); listed claim ids
    are treated exactly like cycle members found here.
    """
    claim_list = list(claims)
    by_id = {c.claim_id: c for c in claim_list}
    edges: dict[str, list[str]] = {
        c.claim_id: [p for p in c.premises if p in by_id] for c in claim_list
    }
    cycles = _find_cycles(set(by_id), edges)
    cycle_members: set[str] = {m for c in cycles for m in c}
    for extra in known_cycles or ():
        # A caller-detected cycle may close through state outside this
        # claim set (e.g. another memory's audit), so a single local
        # member still grounds-nothing here.
        members = [m for m in extra if m in by_id]
        if members:
            cycles.append(sorted(members))
            cycle_members.update(members)
    unique_cycles = sorted({tuple(c) for c in cycles})
    cycles = [list(c) for c in unique_cycles]

    def _key(handle: str) -> str:
        return source_key(handle) if source_key is not None else handle

    # Shared roots: one source cited by several claims is duplicate
    # lineage, not independent corroboration.
    by_source: dict[str, list[str]] = {}
    for c in claim_list:
        for root in {_key(handle) for handle in c.evidence}:
            by_source.setdefault(root, []).append(c.claim_id)
    shared_sources = {h: ids for h, ids in by_source.items() if len(ids) > 1}

    stale_sources: list[str] = []
    if source_sha is not None:
        for handle in by_source:
            now_sha = source_sha(handle)
            if now_sha is None or (audited_sha and audited_sha.get(handle) != now_sha):
                stale_sources.append(handle)
    stale_set = set(stale_sources)

    def _external_resolves(handle: str) -> bool:
        return resolve_source(handle) if resolve_source is not None else False

    unresolved_premises: dict[str, list[str]] = {}
    verdicts: dict[str, ClaimLineageVerdict] = {}
    in_progress: set[str] = set()

    def _verdict(claim: LineageClaim) -> ClaimLineageVerdict:
        cid = claim.claim_id
        if cid in verdicts:
            return verdicts[cid]
        if cid in cycle_members or cid in in_progress:
            member_of = next(
                (list(c) for c in cycles if cid in c), sorted(cycle_members)
            )
            v = ClaimLineageVerdict(
                VERDICT_UNSUPPORTED,
                reasons=["support cycle through claims: " + ", ".join(member_of)],
            )
            verdicts[cid] = v
            return v
        in_progress.add(cid)
        reasons: list[str] = []
        unmet: list[str] = []
        unmet_definitive: list[str] = []
        partial_met: list[str] = []
        met = False
        inherited_review = False
        inherited_depth = 0
        for p in claim.premises:
            if p in by_id:
                child = by_id[p]
                if child.kind in _NON_TRANSFER_KINDS:
                    unmet.append(p)
                    unmet_definitive.append(p)
                    reasons.append(
                        f"premise {p} is a {child.kind}: meaning or provenance,"
                        " not existence"
                    )
                    continue
                child_verdict = _verdict(child)
                inherited_depth = max(inherited_depth, child_verdict.inference_depth)
                inherited_review = inherited_review or child_verdict.needs_source_review
                if child_verdict.verdict == VERDICT_SUPPORTED:
                    met = True
                elif child_verdict.verdict == VERDICT_PARTIAL:
                    partial_met.append(p)
                else:
                    unmet.append(p)
                    if child_verdict.verdict == VERDICT_UNRESOLVED:
                        unresolved_premises.setdefault(cid, []).append(p)
                    else:
                        unmet_definitive.append(p)
                    if child_verdict.reasons:
                        reasons.append(f"premise {p}: {child_verdict.reasons[0]}")
                    else:
                        reasons.append(f"premise {p} is {child_verdict.verdict}")
            else:
                judged = premise_verdict(p) if premise_verdict is not None else None
                if judged is not None:
                    # The premise resolves to a current judgment, not mere
                    # existence: a partial judgment caps the claim even
                    # when every other premise holds.
                    if judged.verdict == VERDICT_SUPPORTED:
                        met = True
                    elif judged.verdict == VERDICT_PARTIAL:
                        partial_met.append(p)
                        reasons.append(
                            f"premise {p}: {judged.reasons[0] if judged.reasons else 'only partially supported'}"
                        )
                    elif judged.verdict == VERDICT_UNSUPPORTED:
                        unmet.append(p)
                        unmet_definitive.append(p)
                        reasons.append(
                            f"premise {p}: {judged.reasons[0] if judged.reasons else 'unsupported'}"
                        )
                    else:
                        unmet.append(p)
                        unresolved_premises.setdefault(cid, []).append(p)
                        reasons.append(
                            f"premise {p}: {judged.reasons[0] if judged.reasons else 'no current judgment'}"
                        )
                    inherited_review = inherited_review or judged.needs_source_review
                    inherited_depth = max(inherited_depth, judged.inference_depth)
                else:
                    unmet.append(p)
                    unresolved_premises.setdefault(cid, []).append(p)
                    reasons.append(
                        f"premise {p} has no support judgment"
                        if _external_resolves(p)
                        else f"premise {p} did not resolve to a source"
                    )
        independent = [h for h in claim.evidence if _key(h) not in shared_sources]
        stale_evidence = [h for h in claim.evidence if _key(h) in stale_set]
        depth = inherited_depth + (1 if claim.kind == "inference" else 0)
        needs_review = (
            depth >= INFERENCE_REVIEW_DEPTH or bool(stale_evidence) or inherited_review
        )

        if unmet:
            has_support = (
                met
                or bool(partial_met)
                or (
                    claim.kind != "inference"
                    and claim.status in (VERDICT_SUPPORTED, VERDICT_PARTIAL)
                )
            )
            if unmet_definitive:
                verdict = VERDICT_PARTIAL if has_support else VERDICT_UNSUPPORTED
            else:
                # Every unmet premise is an unresolvable route: unknown,
                # not refuted.
                verdict = VERDICT_PARTIAL if has_support else VERDICT_UNRESOLVED
            extra = []
        elif claim.premises:
            # Every premise holds (fully or in part). Resting entirely on
            # premises whose sources are shared with this claim is not
            # independence.
            if partial_met:
                verdict = VERDICT_PARTIAL
                extra = [
                    f"premise {p} is only partially supported" for p in partial_met
                ]
            elif claim.evidence and not independent:
                verdict = VERDICT_PARTIAL
                extra = [
                    "no independent evidence: every cited source is shared"
                    " with another claim"
                ]
            elif claim.status == VERDICT_UNSUPPORTED:
                verdict = VERDICT_UNSUPPORTED
                extra = ["claim support or inference warrant rejected"]
            elif claim.status != VERDICT_SUPPORTED:
                verdict = VERDICT_PARTIAL
                extra = ["premises supported but claim support or warrant unverified"]
            else:
                verdict = VERDICT_SUPPORTED
                extra = []
        elif claim.evidence:
            if claim.status == VERDICT_SUPPORTED:
                verdict, extra = VERDICT_SUPPORTED, []
            elif claim.status == VERDICT_UNSUPPORTED:
                verdict, extra = (
                    VERDICT_UNSUPPORTED,
                    ["claim marked unsupported and no premise supports it"],
                )
            else:
                verdict = (
                    VERDICT_PARTIAL
                    if claim.status == VERDICT_PARTIAL
                    else VERDICT_UNRESOLVED
                )
                extra = [f"support {claim.status or 'unverified'}"]
        elif claim.status == VERDICT_UNSUPPORTED:
            verdict, extra = VERDICT_UNSUPPORTED, []
        else:
            verdict, extra = VERDICT_UNRESOLVED, ["no premises and no evidence"]
        if stale_evidence and verdict == VERDICT_SUPPORTED:
            # The source moved under the claim since the audit pinned it:
            # the citation cannot certify current content.
            verdict = VERDICT_PARTIAL
            extra.append("evidence source changed since audit")
        in_progress.discard(cid)
        v = ClaimLineageVerdict(
            verdict,
            reasons=reasons + extra,
            inference_depth=depth,
            needs_source_review=needs_review,
            independent_evidence=independent,
            stale_evidence=stale_evidence,
        )
        verdicts[cid] = v
        return v

    for c in claim_list:
        _verdict(c)
    return LineageReport(
        verdicts=verdicts,
        cycles=cycles,
        shared_sources=shared_sources,
        unresolved_premises=unresolved_premises,
        stale_sources=sorted(stale_sources),
    )


__all__ = [
    "INFERENCE_REVIEW_DEPTH",
    "LINEAGE_VERDICTS",
    "VERDICT_PARTIAL",
    "VERDICT_SUPPORTED",
    "VERDICT_UNRESOLVED",
    "VERDICT_UNSUPPORTED",
    "ClaimLineageVerdict",
    "LineageClaim",
    "LineageReport",
    "analyze_lineage",
]
