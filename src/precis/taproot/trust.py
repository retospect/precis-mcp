"""The single shared trust derivation for a finding-backed citation.

The acquisition-mode mint gave a claim a life *before* its evidence is verified
(``STATUS:acquiring``, and the pre-existing ``tracing``) — this module is
the read-time mapping from that machine state to the human-facing trust
labels, so a citation never quietly launders an unverified claim into
finished prose. **Read/render-time only** — no new machine STATUS, no
writes here.

Two DIFFERENT ``unacquirable_override`` shapes feed this module, and they
must not be confused:

* **Paper-level** (``refs.meta.unacquirable_override = {note, by, at}``,
  written from a paper's Meta tab, :mod:`precis_web.routes.papers`) is a
  pure *acquirability fact* — "I tried hard and could not get this; the
  metadata is correct." It never softens a claim's label. A lifecycle
  finding blocked on such a paper only gets its note enriched (the
  *note-enrichment* rule below); a clean hub whose every print-visible
  grounding paper carries one is *hardened* down to ``unverified`` (the
  *harden* rule), never assumed to fabricate a claim-backing assertion
  nobody made.
* **Claim-level** (``meta.unacquirable_override = {mode, note, by, at}``
  on the *finding itself* — ``edit(kind='finding', unacquirable_note=…)``
  or, for a hub, ``POST /claim/<head>/unacquirable``) is the only
  softener: an explicit author assertion that Ⓐ (``mode='abstract'``) the
  abstract on file backs THIS claim, or ✍ (``mode='vouched'``, also the
  legacy no-``mode`` shape) the author vouches for it. It converts an
  otherwise-**unverified** label (including one just hardened by the
  harden rule) to the calmer ``abstract``/``vouched`` state — never
  ``unsupported`` (a negative terminal verification always outranks it:
  the paper WAS read; "trust me" doesn't unread it).

:func:`claim_trust` branches hub vs. lifecycle finding exactly as
:func:`precis.taproot.cite.finding_cite_keys` does, so a caller can never
hit the wrong arm. Both the draft exporters (:mod:`precis.export.docx` /
:mod:`precis.export.latex`) and (stage b) the smartdraft editor badge
import THIS function — the label mapping lives in one place so the two
surfaces cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from precis.taproot.cite import finding_cite_keys, hub_cite_keys
from precis.taproot.seniority import (
    HubEvidence,
    derive_evidence_bulk,
    is_claim_hub,
    is_claim_hub_bulk,
)
from precis.workers._chase_llm import is_corroborating

TrustLabel = Literal["clean", "abstract", "vouched", "unverified", "unsupported"]

#: Confidence ladder, worst (loudest) → best, for a "worst-of" reduction
#: across a block's cite heads (``smartdraft.claim_trust_for_block``) and
#: for CSS precedence. ``clean`` (verified full text) is quietest; the two
#: author-declared/abstract-only middle states sit *below* ``unverified``
#: (they're more trustworthy than "not looked yet") but are still marked;
#: ``unsupported`` (read and contradicts) is loudest.
_TRUST_SEVERITY: dict[str, int] = {
    "clean": 0,
    "abstract": 1,
    "vouched": 2,
    "unverified": 3,
    "unsupported": 4,
}


def worse_trust(a: str, b: str) -> str:
    """The louder of two trust labels (higher :data:`_TRUST_SEVERITY`)."""
    return a if _TRUST_SEVERITY.get(a, 0) >= _TRUST_SEVERITY.get(b, 0) else b


_STATUS_NAMESPACE = "STATUS"
_STATUS_ESTABLISHED = "established"
_STATUS_DEAD_CHAIN = "dead_chain"
_STATUS_MULTI_CANDIDATE = "multi_candidate"

#: A hub with no print-visible supporter — mirrors ``FindingCite.inflight``.
_HUB_UNVERIFIED_NOTE = "claim hub has no print-visible supporter yet"

#: The harden-rule note (rule 3): a clean hub whose every print-visible
#: grounding paper is declared unacquirable overstates itself, so it's
#: downgraded here — this is a fact-driven harden, not an author assertion
#: (``TrustState.overridden`` stays ``False``); a claim-level override can
#: still soften it right back down, same as any other unverified label.
_HUB_GROUNDING_UNACQUIRABLE_NOTE = "grounded only on sources declared unacquirable"

#: ``dead_chain`` reasons with a specific human note (Motivation table);
#: any other reason renders as its own reason slug.
_DEAD_CHAIN_NOTES = {
    "unacquirable": "no OA copy obtainable; hand-download queued",
}

#: The generic "hasn't reached a terminal hop yet" note — covers
#: ``acquiring``/``tracing`` and any other in-progress lifecycle state
#: (``cycle``, an unrecognized status, a missing STATUS tag).
_PENDING_NOTE = "source pending"


@dataclass(frozen=True)
class TrustState:
    """One finding's derived trust — the shared read model both export
    and the (stage b) smartdraft badge consume."""

    label: TrustLabel
    note: str | None
    #: True iff a **claim-level** ``unacquirable_override`` — the author's
    #: assertion made on the finding/hub itself (never inherited from a
    #: source paper's acquirability fact) — converted an otherwise-
    #: **unverified** label to the softer ``abstract`` (Ⓐ — the abstract
    #: backs it) or ``vouched`` (✍ — author vouches, source unobtainable).
    #: Never true for "unsupported" (a negative terminal verification
    #: always renders, override or not) or a label that was already
    #: "clean". Also never true for a paper-level-only harden (a clean hub
    #: downgraded because every grounding paper is declared unacquirable —
    #: a fact, not an assertion, so ``overridden`` stays False even though
    #: the label moved off "clean"). The override no longer folds all the
    #: way to clean: no one read the full text, so the claim keeps a
    #: (calm) mark.
    overridden: bool
    #: The raw machine status this was derived from — ``'hub'`` for a
    #: Taproot claim hub, else the STATUS tag value (or 'tracing' when
    #: absent, matching the handler's own default). Debugging aid only;
    #: never surfaced as the human-facing label.
    status: str


def _status_of(store: Any, ref_id: int) -> str | None:
    """The STATUS:* tag value on ``ref_id``, or ``None`` if untagged."""
    for tag in store.tags_for(ref_id):
        if getattr(tag, "namespace", None) == "closed" and (
            getattr(tag, "prefix", None) == _STATUS_NAMESPACE
        ):
            return str(tag.value)
    return None


def _hub_trust(
    store: Any,
    ref_id: int,
    *,
    evidence: HubEvidence | None = None,
    cite_key_map: dict[int, list[str]] | None = None,
) -> tuple[TrustLabel, str | None, str]:
    """A ``TAPROOT:claim`` hub's trust: empty print set (no originators AND
    no corroborators, i.e. :attr:`FindingCite.inflight`) → unverified; any
    print-visible supporter → clean. Hub "unsupported" is deferred — a
    contradictor alongside support is normal science, already surfaced on
    the claim page (``render_claim_evidence``).

    ``evidence`` — when the caller (``precis_web.claim_render``) already
    :func:`~precis.taproot.seniority.derive_evidence`'d this hub a moment
    ago, thread it through rather than re-deriving it here (the "derives
    each hub TWICE" defect, OPEN-ITEMS.md batch C). ``cite_key_map``
    likewise skips the per-supporter ``ref_cite_keys`` round trip when a
    bulk caller already resolved it."""
    if evidence is not None:
        cite_keys, _notes = hub_cite_keys(store, evidence, cite_key_map=cite_key_map)
        inflight = not cite_keys
    else:
        fc = finding_cite_keys(store, ref_id)
        inflight = fc.inflight
    if inflight:
        return "unverified", _HUB_UNVERIFIED_NOTE, "hub"
    return "clean", None, "hub"


def _lifecycle_trust(
    store: Any, ref_id: int, meta: dict[str, Any]
) -> tuple[TrustLabel, str | None, str]:
    """A plain (non-hub) finding's trust, derived from its STATUS tag."""
    status = _status_of(store, ref_id) or "tracing"
    if status == _STATUS_ESTABLISHED:
        chain = meta.get("chain") or []
        verification = None
        if chain and isinstance(chain[-1], dict):
            verification = chain[-1].get("verification")
        # A non-dict blob (legacy shape / corrupted meta) can't establish a
        # negative verdict — treat it the same as absent: clean (the chain
        # traced to ground, today's bar). Only a real verification dict can
        # push a claim into "unsupported".
        if isinstance(verification, dict) and not is_corroborating(verification):
            note = verification.get("support_reason")
            return "unsupported", note, status
        return "clean", None, status
    if status == _STATUS_DEAD_CHAIN:
        reason = meta.get("dead_reason")
        note = _DEAD_CHAIN_NOTES.get(str(reason), str(reason) if reason else None)
        return "unverified", note, status
    if status == _STATUS_MULTI_CANDIDATE:
        return "unverified", "ambiguous citation awaiting pick", status
    # acquiring, tracing, cycle, or any other/unrecognized in-progress state.
    return "unverified", _PENDING_NOTE, status


def claim_trust(
    store: Any,
    finding_ref_id: int,
    *,
    evidence: HubEvidence | None = None,
    cite_key_map: dict[int, list[str]] | None = None,
    ref: Any = None,
    paper_refs: dict[int, Any] | None = None,
) -> TrustState:
    """Derive a finding's trust label — the ONE mapping every trust
    surface (export marking, the smartdraft badge) reads.

    Branches hub vs. lifecycle finding exactly as
    :func:`precis.taproot.cite.finding_cite_keys` does. Three rules then
    apply, in order:

    1. **Hub harden.** A clean hub whose every print-visible grounding
       paper carries a *paper-level* ``unacquirable_override`` (a pure
       acquirability fact, set from a paper's Meta tab) overstates itself
       — no one read any of those sources in full — so it's downgraded to
       ``unverified`` with an explanatory note. This is a fact-driven
       harden, not an author assertion: ``TrustState.overridden`` stays
       ``False``.
    2. **Lifecycle note enrichment.** An unverified lifecycle finding
       blocked on a paper that itself carries a paper-level override gets
       its note enriched (naming the blocking source's declared reason) —
       the label is untouched; a paper being unobtainable is not itself a
       claim-backing assertion.
    3. **Claim-level softener.** The finding's/hub's OWN ``meta.
       unacquirable_override`` (set via ``edit(kind='finding',
       unacquirable_note=…)`` or, for a hub, ``POST /claim/<head>/
       unacquirable``) then converts an otherwise-**unverified** label
       (including one just hardened by rule 1) to the softer ``abstract``
       (Ⓐ) / ``vouched`` (✍). Composes with rule 1: a hardened hub can
       still be individually vouched for here. Never applied to an
       **unsupported** label (a negative terminal verification outranks
       any override: the paper was read; "trust me" doesn't unread it).

    ``evidence``/``cite_key_map`` thread a caller's already-derived hub
    evidence + bulk cite_key resolution straight into :func:`_hub_trust`
    (batch/de-dup fix — a caller passing ``evidence`` also implies the
    ref IS a hub, so the ``is_claim_hub`` re-check is skipped too).
    ``ref`` lets a caller that already fetched the finding's ``Ref``
    (e.g. for its title) skip this function's own ``fetch_refs_by_ids``
    call. ``paper_refs`` is the bulk twin for the grounding-paper meta the
    hub-harden check reads (mirrors ``cite_key_map``). All four default to
    the old single-hub, no-cache behaviour.
    """
    if ref is None:
        ref = store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)
    meta = (ref.meta or {}) if ref is not None else {}

    hub_evidence: HubEvidence | None = None
    if evidence is not None or is_claim_hub(store, finding_ref_id):
        # Resolve the hub's evidence once (thread the caller's when given) so
        # both the label AND the harden check below read the same derivation
        # — no second derive.
        if evidence is None:
            evidence = finding_cite_keys(
                store, finding_ref_id, assume_hub=True, cite_key_map=cite_key_map
            ).evidence
        hub_evidence = evidence
        label, note, status = _hub_trust(
            store, finding_ref_id, evidence=evidence, cite_key_map=cite_key_map
        )
    else:
        label, note, status = _lifecycle_trust(store, finding_ref_id, meta)

    if hub_evidence is not None and label == "clean":
        # Rule 1 — harden: every print-visible grounding paper declared
        # unacquirable (a fact, not an author claim-backing assertion), so
        # 'clean' overstates it.
        if _hub_grounding_unacquirable(store, hub_evidence, cite_key_map, paper_refs):
            label = "unverified"
            note = _HUB_GROUNDING_UNACQUIRABLE_NOTE
    elif hub_evidence is None and label == "unverified":
        # Rule 2 — a lifecycle finding's blocking source paper being
        # declared unacquirable is a fact about acquirability, never a
        # softener: enrich the note only, so a human knows to vouch at the
        # claim level (rule 3) or drop the cite.
        paper_override = _source_paper_override(store, meta)
        paper_note = (
            paper_override.get("note") if isinstance(paper_override, dict) else None
        )
        if paper_note:
            addition = f"blocking source declared unacquirable: {paper_note}"
            note = f"{note} — {addition}" if note else addition

    # Rule 3 — the only softener: the finding's/hub's OWN claim-level
    # declaration (never inherited from a paper). Never applied to
    # 'unsupported' (only 'unverified' reaches here, by construction above).
    if label == "unverified":
        override = meta.get("unacquirable_override")
        if isinstance(override, dict) and override:
            # Mode picks the softer state: 'abstract' → Ⓐ (the abstract on
            # file backs it), anything else (incl. a legacy override with no
            # mode) → ✍ vouched (author asserts; source unobtainable). Never
            # all the way to clean — no one read the full text.
            soft: TrustLabel = (
                "abstract" if override.get("mode") == "abstract" else "vouched"
            )
            return TrustState(
                label=soft,
                note=override.get("note") or note,
                overridden=True,
                status=status,
            )
    return TrustState(label=label, note=note, overridden=False, status=status)


def _source_paper_override(store: Any, meta: dict[str, Any]) -> dict[str, Any] | None:
    """The paper-level ``unacquirable_override`` (``{note, by, at}``, no
    ``mode``) on a lifecycle finding's *source paper* — the read-through
    that lets rule 2 in :func:`claim_trust` enrich an unverified finding's
    note with WHY its blocking source can't be obtained, without editing
    each finding. Never a softener — a paper's acquirability is a fact
    about the paper, not an assertion about any particular claim resting
    on it.

    A chased finding names its blocking paper as its chain frontier
    (``meta.chain[-1].ref_id`` — the stub whose PDF it's waiting on). A hub
    has no such chain (its trust comes from supporters), so this is a no-op
    there — the hub harden rule (:func:`_hub_grounding_unacquirable`) is
    its own, separate check."""
    chain = meta.get("chain") or []
    if not (chain and isinstance(chain[-1], dict)):
        return None
    frontier_id = chain[-1].get("ref_id")
    if not isinstance(frontier_id, int):
        return None
    ref = store.fetch_refs_by_ids([frontier_id]).get(frontier_id)
    pmeta = (getattr(ref, "meta", None) or {}) if ref is not None else {}
    override = pmeta.get("unacquirable_override")
    return override if isinstance(override, dict) else None


def _has_cite_key(
    store: Any, paper_ref_id: int, cite_key_map: dict[int, list[str]] | None
) -> bool:
    """Whether a supporter paper is *print-visible* — has a resolvable
    cite_key. Mirrors :func:`~precis.taproot.cite._cite_keys_for_group`'s
    per-edge lookup (bulk map when threaded, else the per-paper query)."""
    aliases = (
        cite_key_map.get(paper_ref_id, [])
        if cite_key_map is not None
        else store.ref_cite_keys(paper_ref_id)
    )
    return bool(aliases)


def _hub_grounding_unacquirable(
    store: Any,
    evidence: HubEvidence,
    cite_key_map: dict[int, list[str]] | None,
    paper_refs: dict[int, Any] | None = None,
) -> bool:
    """Whether EVERY print-visible grounding paper behind a clean hub
    carries a paper-level ``unacquirable_override`` — rule 1 (harden) in
    :func:`claim_trust`'s check. ``True`` means no one read any of the
    hub's grounding sources in full, so 'clean' overstates the claim.

    Returns ``False`` when at least one grounding paper is genuinely
    acquirable (a real read-grounding remains) or the hub has no
    print-visible supporter at all (not actually a clean hub — defensive;
    the caller only reaches this when ``label == 'clean'``).

    The grounding group mirrors :func:`~precis.taproot.cite.hub_cite_keys`:
    originators (those with a cite_key) when any exist, else corroborators —
    exactly the papers that reach the print citation."""
    for group in (evidence.originators, evidence.corroborators):
        grounding = [
            e.paper_ref_id
            for e in group
            if _has_cite_key(store, e.paper_ref_id, cite_key_map)
        ]
        if grounding:
            break
    else:
        return False  # inflight — no print-visible supporter (not a clean hub)
    # ``paper_refs`` — a bulk caller's pre-fetched grounding-paper refs (mirrors
    # ``cite_key_map``); ``None`` falls back to the per-call fetch.
    refs = paper_refs if paper_refs is not None else store.fetch_refs_by_ids(grounding)
    for pid in grounding:
        r = refs.get(pid)
        override = (getattr(r, "meta", None) or {}).get("unacquirable_override")
        if not isinstance(override, dict):
            return False  # this grounding paper IS acquirable → hub stays clean
    return True


def claim_trust_bulk(
    store: Any, finding_ref_ids: Iterable[int]
) -> dict[int, TrustState]:
    """Bulk twin of :func:`claim_trust` — resolve many findings' trust in a
    handful of queries instead of one ``claim_trust`` call (~7 round trips
    once a hub's supporters are counted) per finding.

    Splits ``finding_ref_ids`` into hub vs. lifecycle findings with ONE
    :func:`~precis.taproot.seniority.is_claim_hub_bulk` query, derives every
    hub's evidence with :func:`~precis.taproot.seniority.derive_evidence_bulk`
    (3 more queries, regardless of hub count) plus one bulk cite_key
    resolution, then calls :func:`claim_trust` per hub with everything
    pre-threaded (0 further queries per hub — just the in-Python
    ``unacquirable_override``/ref-title lookup, batched via
    ``fetch_refs_by_ids``). A lifecycle (non-hub) finding still costs its
    own :func:`claim_trust` call — its STATUS-tag derivation isn't itself
    N+1 today, so batching it is out of this fix's scope.

    The smartdraft reader's per-block claim-trust badge
    (``smartdraft.py::claim_trust_for_block``) is this function's
    motivating caller: many rendered blocks citing many distinct hubs used
    to cost one full ``claim_trust`` derivation per distinct head."""
    ids = list(dict.fromkeys(int(r) for r in finding_ref_ids))
    if not ids:
        return {}

    hub_flags = is_claim_hub_bulk(store, ids)
    hub_ids = [r for r in ids if hub_flags.get(r)]
    lifecycle_ids = [r for r in ids if not hub_flags.get(r)]

    out: dict[int, TrustState] = {}
    if hub_ids:
        evidence_by_hub = derive_evidence_bulk(store, hub_ids)
        supporter_ids = {
            e.paper_ref_id
            for ev in evidence_by_hub.values()
            for e in (*ev.originators, *ev.corroborators)
        }
        cite_key_map = store.ref_cite_keys_bulk(supporter_ids) if supporter_ids else {}
        refs = store.fetch_refs_by_ids(hub_ids)
        # Supporter-paper refs for the hub-clean unacquirable-override check,
        # batched once (mirrors cite_key_map) rather than per-hub in claim_trust.
        paper_refs = (
            store.fetch_refs_by_ids(list(supporter_ids)) if supporter_ids else {}
        )
        for rid in hub_ids:
            out[rid] = claim_trust(
                store,
                rid,
                evidence=evidence_by_hub[rid],
                cite_key_map=cite_key_map,
                ref=refs.get(rid),
                paper_refs=paper_refs,
            )
    for rid in lifecycle_ids:
        out[rid] = claim_trust(store, rid)
    return out


__all__ = [
    "TrustLabel",
    "TrustState",
    "claim_trust",
    "claim_trust_bulk",
    "worse_trust",
]
