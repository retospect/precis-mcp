"""The single shared trust derivation for a finding-backed citation.

Design: ``docs/proposals/finding-trust-surfaces.md``. `finding-acquisition-
mode` gave a claim a life *before* its evidence is verified
(``STATUS:acquiring``, and the pre-existing ``tracing``) — this module is
the read-time mapping from that machine state to the two human-facing
trust labels, so a citation never quietly launders an unverified claim
into finished prose. **Read/render-time only** — no new machine STATUS,
no writes here (the one write path, the author's unacquirable override,
lives in :mod:`precis.handlers.finding`).

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
    #: True iff an ``unacquirable_override`` (the author's, on the finding
    #: itself, or the source paper's, set from its Meta tab) converted an
    #: otherwise-**unverified** label to the softer ``abstract`` (Ⓐ — the
    #: abstract backs it) or ``vouched`` (✍ — author vouches, source
    #: unobtainable). Never true for "unsupported" (a negative terminal
    #: verification always renders, override or not) or a label that was
    #: already "clean". The override no longer folds all the way to clean:
    #: no one read the full text, so the claim keeps a (calm) mark.
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
) -> TrustState:
    """Derive a finding's trust label — the ONE mapping every trust
    surface (export marking, the smartdraft badge) reads.

    Branches hub vs. lifecycle finding exactly as
    :func:`precis.taproot.cite.finding_cite_keys` does. An author's
    ``meta.unacquirable_override`` (set via ``edit(kind='finding',
    unacquirable_note=…)``) then converts an otherwise-**unverified**
    label to clean — never an **unsupported** one (a negative terminal
    verification outranks the override: the paper was read; "trust me"
    doesn't unread it).

    ``evidence``/``cite_key_map`` thread a caller's already-derived hub
    evidence + bulk cite_key resolution straight into :func:`_hub_trust`
    (batch/de-dup fix — a caller passing ``evidence`` also implies the
    ref IS a hub, so the ``is_claim_hub`` re-check is skipped too).
    ``ref`` lets a caller that already fetched the finding's ``Ref``
    (e.g. for its title) skip this function's own ``fetch_refs_by_ids``
    call. All three default to the old single-hub, no-cache behaviour.
    """
    if ref is None:
        ref = store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)
    meta = (ref.meta or {}) if ref is not None else {}

    if evidence is not None or is_claim_hub(store, finding_ref_id):
        label, note, status = _hub_trust(
            store, finding_ref_id, evidence=evidence, cite_key_map=cite_key_map
        )
    else:
        label, note, status = _lifecycle_trust(store, finding_ref_id, meta)

    if label == "unverified":
        override = meta.get("unacquirable_override") or _source_paper_override(
            store, meta
        )
        if override:
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
    """The ``unacquirable_override`` on a lifecycle finding's *source paper*
    — the read-through that lets an author mark a paper unobtainable from
    its own Meta tab and have every claim resting on it render calm,
    without editing each finding.

    A chased finding names its blocking paper as its chain frontier
    (``meta.chain[-1].ref_id`` — the stub whose PDF it's waiting on). A hub
    has no such chain (its trust comes from supporters, and an unverified
    hub simply has none), so this is a no-op there — only the finding-level
    override applies to hubs."""
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
        for rid in hub_ids:
            out[rid] = claim_trust(
                store,
                rid,
                evidence=evidence_by_hub[rid],
                cite_key_map=cite_key_map,
                ref=refs.get(rid),
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
