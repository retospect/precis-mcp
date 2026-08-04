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

from dataclasses import dataclass
from typing import Any, Literal

from precis.taproot.cite import finding_cite_keys
from precis.taproot.seniority import is_claim_hub
from precis.workers._chase_llm import is_corroborating

TrustLabel = Literal["clean", "unverified", "unsupported"]

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
    #: True iff ``meta.unacquirable_override`` converted an otherwise-
    #: unverified label to clean. Never true for "unsupported" (a negative
    #: terminal verification always renders, override or not) or when the
    #: derived label was already "clean".
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


def _hub_trust(store: Any, ref_id: int) -> tuple[TrustLabel, str | None, str]:
    """A ``TAPROOT:claim`` hub's trust: empty print set (no originators AND
    no corroborators, i.e. :attr:`FindingCite.inflight`) → unverified; any
    print-visible supporter → clean. Hub "unsupported" is deferred — a
    contradictor alongside support is normal science, already surfaced on
    the claim page (``render_claim_evidence``)."""
    fc = finding_cite_keys(store, ref_id)
    if fc.inflight:
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


def claim_trust(store: Any, finding_ref_id: int) -> TrustState:
    """Derive a finding's trust label — the ONE mapping every trust
    surface (export marking, the smartdraft badge) reads.

    Branches hub vs. lifecycle finding exactly as
    :func:`precis.taproot.cite.finding_cite_keys` does. An author's
    ``meta.unacquirable_override`` (set via ``edit(kind='finding',
    unacquirable_note=…)``) then converts an otherwise-**unverified**
    label to clean — never an **unsupported** one (a negative terminal
    verification outranks the override: the paper was read; "trust me"
    doesn't unread it)."""
    ref = store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)
    meta = (ref.meta or {}) if ref is not None else {}

    if is_claim_hub(store, finding_ref_id):
        label, note, status = _hub_trust(store, finding_ref_id)
    else:
        label, note, status = _lifecycle_trust(store, finding_ref_id, meta)

    if meta.get("unacquirable_override") and label == "unverified":
        return TrustState(label="clean", note=note, overridden=True, status=status)
    return TrustState(label=label, note=note, overridden=False, status=status)


__all__ = ["TrustLabel", "TrustState", "claim_trust"]
