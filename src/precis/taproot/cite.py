"""Taproot Phase 1 + 2 — the ONE hub cite-key resolution policy, plus the
shared authorial-pin overlay.

Both ``precis resolve`` (:mod:`precis.cli.resolve`) and the draft export
(:mod:`precis.export.latex` / :mod:`precis.export.docx`) resolve a
``TAPROOT:claim`` hub's ``[<pub_id>]`` / ``[fi<id>]`` cite to the SAME
derived ``establishes`` originator(s) — falling back to the citation
standard over the corroborators (:func:`hub_cite_keys`), then to in-flight
when the hub has no supporting evidence at all. That
policy is locked here, once, and imported by both surfaces so they can
never quietly diverge (that divergence was the exact bug Phase 1 fixes).

Pins (Taproot slice A2, ``[<pub_id>>…]`` / ``[<pub_id>+…]``) are the same
story one level up: :func:`apply_pin` / :func:`resolve_pin_handle` are the
ONE pin-application policy, shared by ``precis resolve`` (base32 ``[pub_id]``
token grammar, :mod:`precis.cli.resolve`) and the draft ``mentions`` bracket
grammar (Phase 2, :mod:`precis.utils.mentions`'s ``pin`` capture group,
consumed by the draft exporters) — so a pin behaves identically wherever an
author writes it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.taproot.review_like import PATTERNS_VERSION, review_reason
from precis.taproot.seniority import (
    EvidenceEdge,
    HubEvidence,
    derive_evidence,
    is_claim_hub,
)

if TYPE_CHECKING:
    from precis.store.protocols import ClaimTrustStore, PinStore


def _keyed_edges(
    store: ClaimTrustStore,
    edges: list[EvidenceEdge],
    *,
    cite_key_map: dict[int, list[str]] | None = None,
) -> tuple[list[tuple[EvidenceEdge, str]], list[int]]:
    """Pair each edge with its paper's (oldest) ``cite_key`` alias.

    Returns ``(keyed, skipped_ref_ids)`` — a paper with no ``cite_key``
    alias at all is dropped rather than failing the whole hub, and its
    ``ref_id`` is reported back so the caller can warn about it.

    ``cite_key_map`` — a pre-fetched ``{paper_ref_id: aliases}`` map
    (:func:`~precis.store.Store.ref_cite_keys_bulk`) — skips the
    per-paper ``store.ref_cite_keys`` round trip entirely when given
    (the batch B fix: this was an N+1 per hub, and a *second* N+1 when
    ``claim_trust`` re-derived the same hub — see :mod:`precis.taproot.trust`).
    ``None`` (the default) preserves the old per-edge query behaviour.
    """
    keyed: list[tuple[EvidenceEdge, str]] = []
    skipped: list[int] = []
    for edge in edges:
        aliases = (
            cite_key_map.get(edge.paper_ref_id, [])
            if cite_key_map is not None
            else store.ref_cite_keys(edge.paper_ref_id)
        )
        if aliases:
            keyed.append((edge, aliases[0]))
        else:
            skipped.append(edge.paper_ref_id)
    return keyed, skipped


def _cite_keys_for_group(
    store: ClaimTrustStore,
    edges: list[EvidenceEdge],
    *,
    cite_key_map: dict[int, list[str]] | None = None,
) -> tuple[list[str], list[int]]:
    """:func:`_keyed_edges` reduced to ``(cite_keys, skipped_ref_ids)``."""
    keyed, skipped = _keyed_edges(store, edges, cite_key_map=cite_key_map)
    return [key for _edge, key in keyed], skipped


def _year_order(edge: EvidenceEdge) -> tuple[bool, int, int]:
    """Earliest-published first: year asc, ties by ref_id asc, no year last."""
    return (edge.year is None, edge.year or 0, edge.paper_ref_id)


def _norm_family(raw: str) -> str:
    """Accent-folded, lower-cased, letters-only family name (``"Müller-Lee"``
    → ``"mullerlee"``) so spelling/punctuation variants of one surname match."""
    folded = unicodedata.normalize("NFKD", raw).casefold()
    return re.sub(r"[^a-z]", "", folded.encode("ascii", "ignore").decode("ascii"))


def _author_families(ref: Any) -> frozenset[str]:
    """The normalised family names of a paper's ``refs.authors``.

    The real shape is a list of dicts: ``{"given", "family", "orcid"?, …}``,
    or ``{"name": "…"}`` for an unsplit byline — for those the family is the
    part before a comma, else the last whitespace token. Empty when the ref
    is missing or has no (usable) authors."""
    out: set[str] = set()
    for entry in getattr(ref, "authors", None) or []:
        if not isinstance(entry, dict):
            continue
        family = str(entry.get("family") or "")
        if not family:
            name = str(entry.get("name") or "").strip()
            if "," in name:
                family = name.split(",", 1)[0]
            elif name:
                family = name.split()[-1]
        fam = _norm_family(family)
        if fam:
            out.add(fam)
    return frozenset(out)


@dataclass(frozen=True)
class CandidateVerdict:
    """One fallback candidate's row in the decision record: what the
    review heuristic said (and which pattern fired) and what happened to it."""

    paper_ref_id: int
    cite_key: str
    year: int | None
    title: str
    #: ``None`` = research-like ("primary"); else the pattern that fired
    #: (:func:`~precis.taproot.review_like.review_reason`).
    review_reason: str | None
    #: Why it was / wasn't printed — one of the ``OUTCOME_*`` strings.
    outcome: str


OUTCOME_FIRST = "printed: earliest primary"
OUTCOME_CONFIRMATION = "printed: independent confirmation"
OUTCOME_SHARED_AUTHOR = "not printed: shares an author with the first primary"
OUTCOME_CAP = "not printed: cap of 3 reached"
OUTCOME_REVIEW_PRINTED = "printed: earliest review (no primary)"
OUTCOME_REVIEW_HELD = "not printed: review, a primary exists"
OUTCOME_REVIEW_LATER = "not printed: review, an earlier review is printed"
OUTCOME_UNVERIFIED = "printed: earliest corroborator (nothing grounded + verified)"


@dataclass(frozen=True)
class FallbackDecision:
    """The decision record for a hub's no-originator fallback — which tier
    fired, why, and the heuristic's verdict on every candidate. Computed
    in memory from what the resolver already read (no DB write: the export
    and ``precis resolve`` paths are read-only); rendered by
    ``get(kind='finding', view='evidence')``."""

    #: ``'primary'`` | ``'review'`` | ``'unverified'``.
    tier: str
    #: One-line statement of the rule that fired.
    rule: str
    #: Corroborators with a cite_key that were considered (grounded or not).
    n_corroborators: int
    #: Every grounded + verified + clean candidate (for ``'unverified'``: the
    #: one printed corroborator, since there are no candidates), in
    #: earliest-first order.
    candidates: list[CandidateVerdict]
    #: :data:`precis.taproot.review_like.PATTERNS_VERSION` the verdicts were
    #: computed under.
    patterns_version: str = PATTERNS_VERSION

    def summary(self) -> str:
        """Compact one-line form for a caller's warning log."""
        rows = "; ".join(
            f"{c.cite_key} ({c.year or 'n.d.'}) "
            f"{'PRIMARY' if c.review_reason is None else f'REVIEW[{c.review_reason}]'}"
            f" -> {c.outcome}"
            for c in self.candidates
        )
        return (
            f"{self.rule} [review patterns {self.patterns_version}]; candidates: {rows}"
        )


@dataclass(frozen=True)
class HubPrint:
    """What a claim hub prints, and which rule produced it."""

    #: The printed papers' edges, in print order (parallel to ``cite_keys``).
    edges: list[EvidenceEdge]
    cite_keys: list[str]
    #: ``(status, detail)`` diagnostics for the caller's warning log.
    notes: list[tuple[str, str]]
    #: ``'originator'`` | ``'primary'`` | ``'review'`` | ``'unverified'`` |
    #: ``'none'`` (in-flight) — which tier of the policy fired.
    tier: str
    #: Set only when the corroborator fallback fired (not for originators).
    decision: FallbackDecision | None = None


def _refs_for(
    store: ClaimTrustStore,
    ref_ids: list[int],
    paper_refs: dict[int, Any] | None,
) -> dict[int, Any]:
    """``{ref_id: Ref}`` for ``ref_ids`` — from a bulk caller's pre-fetched
    ``paper_refs`` where present, the rest in ONE ``fetch_refs_by_ids``."""
    out = {i: paper_refs[i] for i in ref_ids if paper_refs and i in paper_refs}
    missing = [i for i in ref_ids if i not in out]
    if missing:
        out.update(store.fetch_refs_by_ids(missing))
    return out


def resolve_hub_print(
    store: ClaimTrustStore,
    evidence: HubEvidence,
    *,
    cite_key_map: dict[int, list[str]] | None = None,
    paper_refs: dict[int, Any] | None = None,
) -> HubPrint:
    """The body of :func:`hub_cite_keys`, returning the printed *edges* too —
    surfaces that mirror the print set (the web ★, the trust harden rule)
    read ``.edges`` so they cannot drift from what the export cites."""
    notes: list[tuple[str, str]] = []
    originators, skipped = _keyed_edges(
        store, evidence.originators, cite_key_map=cite_key_map
    )
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"originator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if originators:
        return HubPrint(
            [e for e, _k in originators],
            [k for _e, k in originators],
            notes,
            "originator",
        )

    corroborators, skipped = _keyed_edges(
        store, evidence.corroborators, cite_key_map=cite_key_map
    )
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"corroborator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if not corroborators:
        return HubPrint([], [], notes, "none")
    corroborators.sort(key=lambda ek: _year_order(ek[0]))

    # Candidates: passage-grounded (the edge's own source_handle, or a
    # grounding pointer for the paper that supports rather than contradicts),
    # verified, and not retracted/corrected.
    grounded_ids = {
        g.paper_ref_id
        for g in evidence.grounding
        if g.relation in ("establishes", "corroborates")
    }
    candidates = [
        (e, k)
        for e, k in corroborators
        if e.support == "yes"
        and e.integrity == "clean"
        and (e.source_handle or e.paper_ref_id in grounded_ids)
    ]
    if not candidates:
        # Nothing grounded + verified: never the whole list — the single
        # earliest corroborator, flagged as unverified.
        edge, key = corroborators[0]
        detail = (
            "resolved via 1 unverified corroborator — no grounded verified supporter"
        )
        decision = FallbackDecision(
            tier="unverified",
            rule=detail,
            n_corroborators=len(corroborators),
            candidates=[
                CandidateVerdict(
                    edge.paper_ref_id,
                    key,
                    edge.year,
                    edge.title,
                    None,
                    OUTCOME_UNVERIFIED,
                )
            ],
        )
        notes.append(("established", detail))
        notes.append(("cite-fallback", decision.summary()))
        return HubPrint([edge], [key], notes, "unverified", decision)

    refs = _refs_for(store, [e.paper_ref_id for e, _k in candidates], paper_refs)

    def _reason(edge: EvidenceEdge) -> str | None:
        ref = refs.get(edge.paper_ref_id)
        journal = (getattr(ref, "meta", None) or {}).get("journal")
        return review_reason(
            getattr(ref, "title", None) or edge.title,
            journal if isinstance(journal, str) else None,
        )

    reasons = {e.paper_ref_id: _reason(e) for e, _k in candidates}
    primaries = [(e, k) for e, k in candidates if reasons[e.paper_ref_id] is None]
    outcomes: dict[int, str] = {}
    if not primaries:
        picked = [candidates[0]]
        tier = "review"
        rule = (
            "resolved via corroborator(s) — no derived originator yet "
            "(no grounded verified primary; earliest grounded verified review)"
        )
        for i, (e, _k) in enumerate(candidates):
            outcomes[e.paper_ref_id] = (
                OUTCOME_REVIEW_PRINTED if i == 0 else OUTCOME_REVIEW_LATER
            )
    else:
        first, first_key = primaries[0]
        first_authors = _author_families(refs.get(first.paper_ref_id))
        picked = [(first, first_key)]
        outcomes[first.paper_ref_id] = OUTCOME_FIRST
        for edge, key in primaries[1:]:
            if len(picked) == 3:
                outcomes[edge.paper_ref_id] = OUTCOME_CAP
                continue
            authors = _author_families(refs.get(edge.paper_ref_id))
            if not first_authors or not authors:
                # No authorship to compare: counted as independent, but say so.
                notes.append(
                    (
                        "established",
                        f"corroborator paper ref_id={edge.paper_ref_id} counted as "
                        "an independent confirmation without an author check — "
                        "authors missing",
                    )
                )
            elif first_authors & authors:
                outcomes[edge.paper_ref_id] = OUTCOME_SHARED_AUTHOR
                continue
            picked.append((edge, key))
            outcomes[edge.paper_ref_id] = OUTCOME_CONFIRMATION
        for e, _k in candidates:
            outcomes.setdefault(e.paper_ref_id, OUTCOME_REVIEW_HELD)
        extra = len(picked) - 1
        tier = "primary"
        rule = (
            "resolved via corroborator(s) — no derived originator yet "
            f"(earliest grounded verified primary + {extra} independent "
            f"confirmation{'' if extra == 1 else 's'})"
        )
    decision = FallbackDecision(
        tier=tier,
        rule=rule,
        n_corroborators=len(corroborators),
        candidates=[
            CandidateVerdict(
                e.paper_ref_id,
                k,
                e.year,
                e.title,
                reasons[e.paper_ref_id],
                outcomes[e.paper_ref_id],
            )
            for e, k in candidates
        ],
    )
    notes.append(("established", rule))
    notes.append(("cite-fallback", decision.summary()))
    return HubPrint(
        [e for e, _k in picked], [k for _e, k in picked], notes, tier, decision
    )


def hub_cite_keys(
    store: ClaimTrustStore,
    evidence: HubEvidence,
    *,
    cite_key_map: dict[int, list[str]] | None = None,
    paper_refs: dict[int, Any] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Locked resolution policy for a claim hub's living citation.

    1. Derived ``establishes`` originators, if any have a cite_key.
    2. Else the citation standard over the corroborators (Reto's ruling,
       2026-10-02): *cite the originating primary source plus up to 2
       independent confirmations; reviews only for claims about the field;
       never a list of every paper that mentions a result.* Concretely, among
       corroborators with a cite_key that are **candidates** — passage-grounded
       (edge ``source_handle`` or a grounding pointer), verified
       (``support == 'yes'``) and integrity-clean:

       a. the earliest-published **primary** (not
          :func:`~precis.taproot.review_like.is_review_like`; year asc, ties
          by ref_id, no year last), plus up to 2 more primaries in year order
          that share no author (normalised family name) with the first — a
          paper or first-primary with no authors is counted independent, with
          a note;
       b. else the single earliest review-like candidate. Reviews are only
          ever printed when there is no primary: there is no survey-claim
          detector yet, so a field-level claim gets its review only by that
          fallback or a pin;
       c. else (nothing grounded + verified) the single earliest corroborator
          with a cite_key, noted as unverified.
    3. Else empty — the caller treats the hub as in-flight.

    "Primary" is a title/journal heuristic today (there is no review flag);
    it tightens to "states it as its own result" when
    ``evidence-edge-verification`` lands. A pin (``[fi…>pc…]`` /
    :func:`apply_pin`) overrides all of this.

    Returns ``(cite_keys, notes)`` where ``notes`` are ``(status,
    detail)`` diagnostic pairs meant for a caller's warning/summary log
    (skipped no-cite_key papers, which fallback tier fired).

    ``cite_key_map`` threads through to :func:`_keyed_edges` — a bulk
    caller resolving many hubs at once passes one pre-fetched map covering
    every supporter across every hub instead of paying a query per supporter
    here. ``paper_refs`` is its twin for the candidates' title/journal/
    authors; any candidate it lacks is fetched in one ``fetch_refs_by_ids``.
    """
    result = resolve_hub_print(
        store, evidence, cite_key_map=cite_key_map, paper_refs=paper_refs
    )
    return result.cite_keys, result.notes


@dataclass(frozen=True)
class FindingCite:
    """The resolved cite_key(s) for one finding handle (hub or plain)."""

    cite_keys: list[str]  # resolved bib keys; empty = in-flight / no source
    is_hub: bool
    inflight: bool  # a hub with no resolvable evidence
    notes: list[tuple[str, str]]  # (status, detail) diagnostics, for the caller
    #: The hub's freshly-derived evidence — set only when ``is_hub`` (``None``
    #: for a plain finding), so a caller can :func:`apply_pin` without
    #: re-deriving it a second time.
    evidence: HubEvidence | None = None


def finding_cite_keys(
    store: ClaimTrustStore,
    ref_id: int,
    *,
    assume_hub: bool = False,
    cite_key_map: dict[int, list[str]] | None = None,
) -> FindingCite:
    """Resolve a finding ref to its bibliographic cite_key(s) — the one
    entry point both ``precis resolve`` and the draft exporters call.

    A ``TAPROOT:claim`` hub resolves via :func:`hub_cite_keys` over its
    freshly :func:`~precis.taproot.seniority.derive_evidence` evidence — a
    living citation, recomputed on every call. A plain finding resolves
    off its own ``meta``: the ``primary_cite_key`` once the chase has
    established it, else the ``pub_id`` placeholder.

    ``assume_hub``/``cite_key_map`` are the batch-B perf knobs — a caller
    that already confirmed ``ref_id`` is a hub (skips the redundant
    ``is_claim_hub`` re-check) and/or pre-fetched cite_key aliases in bulk
    can thread both through here. Both default to the old per-call
    behaviour.
    """
    if assume_hub or is_claim_hub(store, ref_id):
        evidence = derive_evidence(store, ref_id, assume_hub=assume_hub)
        cite_keys, notes = hub_cite_keys(store, evidence, cite_key_map=cite_key_map)
        return FindingCite(
            cite_keys=cite_keys,
            is_hub=True,
            inflight=not cite_keys,
            notes=notes,
            evidence=evidence,
        )

    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return FindingCite(cite_keys=[], is_hub=False, inflight=True, notes=[])
    meta = ref.meta or {}
    key = meta.get("primary_cite_key") or meta.get("pub_id")
    cite_keys = [str(key)] if key else []
    return FindingCite(
        cite_keys=cite_keys,
        is_hub=False,
        inflight=not cite_keys,
        notes=[],
    )


# ── Authorial pins (Taproot slice A2) ──────────────────────────────────
#
# A hub's living-citation default (`hub_cite_keys`) can be overridden
# inline: `[<label>>pa5,pc293]` cites exactly those universal handles
# instead of the derived originators (**replace**); `[<label>+pa5]` cites
# the derived originators *plus* those (**supplement**, deduped). A
# `pc<id>` (paper-chunk/passage) handle resolves to its parent paper's
# cite_key. Purely syntactic — no storage, no draft-side edge. This is the
# ONE application policy: `precis resolve` (the base32 `[pub_id]` token
# grammar) and the draft ``mentions`` bracket grammar (Phase 2) both call
# `apply_pin` so a pin behaves identically wherever an author writes it.


def resolve_pin_handle(store: PinStore, handle: str) -> tuple[int, str] | None:
    """Resolve one authorial pin handle to ``(paper_ref_id, cite_key)``.

    A ``pc<id>`` (paper-chunk/passage) handle resolves to its **parent
    paper** — the ``.bib`` is paper-level, so pinning a passage means
    "grounded at this figure," not a separate citable unit.
    :func:`~precis.store.Store.resolve_handle` already does that
    parent-lookup for a chunk handle (``ResolvedHandle.ref_id`` is the
    owning ref), so this reuses it rather than hand-rolling chunk→paper
    resolution.

    ``None`` when the handle isn't well-formed, doesn't resolve to a
    live paper, or that paper has no ``cite_key`` alias — the caller
    warns and skips.
    """
    resolved = store.resolve_handle(handle)
    if resolved is None or resolved.kind != "paper":
        return None
    aliases = store.ref_cite_keys(resolved.ref_id)
    if not aliases:
        return None
    return resolved.ref_id, aliases[0]


@dataclass(frozen=True)
class PinResult:
    """The outcome of applying one authorial pin to a hub's derived
    cite_keys."""

    cite_keys: list[str]
    diverged: bool = False
    #: advisory text when a **replace** pin diverges from the derived
    #: ``establishes`` originators; ``None`` when not diverged (or op=='+').
    divergence: str | None = None
    #: ``(status, detail)`` diagnostics — unresolvable pinned handle, an
    #: empty-replace fallback, etc. Meant for the caller's warning log.
    warnings: list[tuple[str, str]] = field(default_factory=list)


def apply_pin(
    store: PinStore,
    *,
    label: str,
    op: str,
    handles: list[str],
    derived_cite_keys: list[str],
    evidence: HubEvidence,
) -> PinResult:
    """Apply an authorial pin to a hub's derived cite_keys — ``op`` is
    ``'>'`` (replace) or ``'+'`` (supplement).

    Resolves each pinned handle (:func:`resolve_pin_handle`, deduped by
    paper ref_id, first-seen order), warning + skipping an unresolvable
    one. Reports a divergence advisory when the pinned paper set differs
    from the hub's *actually derived* ``establishes`` originators (not the
    corroborator-fallback set — a pin only "diverges" from a real
    seniority split) — **replace (``>``) only**. A supplement (``+``) pin
    is purely additive ("derived plus these"), so its handle set
    legitimately differs from the full derived set on every normal use;
    it has no divergence concept and never fires the advisory.

    ``'>'`` (replace) with an empty resolved pin set falls back to
    ``derived_cite_keys`` unchanged, with a warning — a citation must
    never silently disappear because a pin went stale.

    ``label`` is the pinned finding's display label (a base32 pub_id for
    ``precis resolve``, a ``fi<id>`` handle for the draft grammar) —
    used only in warning/divergence message text.
    """
    from precis.utils import handle_registry

    warnings: list[tuple[str, str]] = []
    pinned: list[tuple[int, str]] = []
    seen_ref_ids: set[int] = set()
    for handle in handles:
        resolved = resolve_pin_handle(store, handle)
        if resolved is None:
            warnings.append(
                (
                    "pin",
                    f"pinned handle {handle} did not resolve to a cited "
                    "paper — skipped",
                )
            )
            continue
        ref_id, cite_key = resolved
        if ref_id in seen_ref_ids:
            continue
        seen_ref_ids.add(ref_id)
        pinned.append((ref_id, cite_key))

    pinned_ref_ids = {ref_id for ref_id, _ in pinned}
    pinned_keys = [cite_key for _, cite_key in pinned]

    if op == ">":
        # Divergence advisory — replace only (see docstring: a supplement
        # pin has no divergence concept).
        diverged = False
        divergence: str | None = None
        originator_ref_ids = {edge.paper_ref_id for edge in evidence.originators}
        if (
            pinned_ref_ids
            and originator_ref_ids
            and pinned_ref_ids != originator_ref_ids
        ):
            pinned_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r) for r in pinned_ref_ids
                )
            )
            derived_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r)
                    for r in originator_ref_ids
                )
            )
            divergence = (
                f"[{label}] pinned {{{pinned_str}}} but derived originator "
                f"is {{{derived_str}}} — reconsider"
            )
            diverged = True

        if pinned_keys:
            return PinResult(
                cite_keys=pinned_keys,
                diverged=diverged,
                divergence=divergence,
                warnings=warnings,
            )
        warnings.append(
            (
                "pin",
                "replace pin resolved to no usable cite_keys — falling "
                "back to derived hub resolution",
            )
        )
        return PinResult(
            cite_keys=derived_cite_keys,
            diverged=diverged,
            divergence=divergence,
            warnings=warnings,
        )
    # op == "+": supplement — derived originators first, pinned appended,
    # deduped by cite_key, deterministic (pin order after derived order).
    return PinResult(
        cite_keys=derived_cite_keys
        + [key for key in pinned_keys if key not in derived_cite_keys],
        warnings=warnings,
    )


__all__ = [
    "CandidateVerdict",
    "FallbackDecision",
    "FindingCite",
    "HubPrint",
    "PinResult",
    "apply_pin",
    "finding_cite_keys",
    "hub_cite_keys",
    "resolve_hub_print",
    "resolve_pin_handle",
]
