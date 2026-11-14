"""The lit-search's cheap pre-link relevance gate (gr592951).

A lit-search hit used to become a ``serves`` edge on the strength of a
score alone, so a quest accumulated off-topic and metadata-less papers —
smoke-adsorption and nanofluid records serving an NO→NH3-on-Pd(111)
quest, each spending a link row and polluting ``view='links'``. Two
filters now sit between a hit and its edge:

* **The no-metadata rule** — a candidate with neither a title nor an
  abstract is never linked (:func:`ref_has_metadata`). An S2 acquire
  stub minted before ``fetch_oa`` fills it has nothing to judge; the
  logbook line names it ``(no title/abstract)``. A candidate whose ref
  **cannot be read** (:data:`UNREADABLE`, a store blip) is *deferred* —
  not linked this run, named ``(unreadable — deferred)`` in the line,
  free to link on a later read. The mandatory rule is verified, not
  assumed: an unreadable candidate never passes it on suspicion.
* **The term gate** — :func:`gate_verdict` matches a paper candidate's
  title+abstract against the quest's ``meta.lit_gate`` term lists
  (``{"host": [...], "reaction": [...]}`` — host metal/facet and
  reaction family). A candidate must name at least one term from EACH
  configured list; a miss reports ``no host term`` / ``no reaction
  term`` as the line's per-candidate verdict. The gate is **opt-in**:
  a quest with no ``lit_gate`` config links as before, so the gate
  stays off until an operator writes the lists.

**No durable rejection.** A gate failure is ephemeral — it names the
verdict in the lit-search logbook line and stops there; nothing here
writes a ``considered`` row for a failed candidate (cheap triage must
never become a durable rejection — the consideration-history spec,
gr626718/me626852). A source the tick judges off-topic gets its durable
verdict through the payload's ``judgments`` channel
(:func:`precis.quest.search.apply_judgments`); the ``considered``
ledger then holds it out of every later leg via
:func:`precis.quest.search.judged_out`. And because cheap triage must
not be the only discovery path, every
:data:`GATE_SAMPLE_MODULUS`-th rejected ref id is flagged ``— sampled
for review`` in the line — never linked past the gate, but named for
the next tick to weigh through ``judgments``. Sampling triggers
further review; it does not bypass a required relevance gate.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Ref, Store

log = logging.getLogger(__name__)

#: Quest meta key carrying the gate's term lists:
#: ``{"host": ["Pd", "Pd(111)", …], "reaction": ["NO", "NH3", …]}`` —
#: set via ``edit(kind='quest', id=N, meta={'lit_gate': {...}})``.
LIT_GATE_META_KEY = "lit_gate"

#: Deterministic cheap-triage sample rate: every Nth gate-rejected ref
#: id is flagged ``— sampled for review`` in the line (never linked
#: past the gate), so a later tick weighs it through the ``judgments``
#: channel and the gate can't quietly starve discovery of adjacent
#: literature.
GATE_SAMPLE_MODULUS = 5

#: Sentinel for "the candidate's ref could not be read" — distinct from
#: ``None`` (the ref resolved to nothing). An unreadable candidate is
#: *deferred*, never linked: the mandatory no-metadata rule is
#: verified, not assumed, so a store blip must not smuggle a
#: titleless/abstractless paper past it. Deferral excludes nothing —
#: the candidate is simply judged again on a later run, matching
#: ``judged_out``'s miss-not-break contract.
UNREADABLE: Any = object()


def candidate_ref(store: Store, kind: str, ref_id: int) -> Any:
    """The candidate's ref — ``None`` when it resolves to nothing, or
    the :data:`UNREADABLE` sentinel on a store error."""
    try:
        return store.get_ref(kind=kind, id=ref_id)
    except Exception:
        log.debug("lit gate: get_ref failed for %s ref %s", kind, ref_id, exc_info=True)
        return UNREADABLE


def gate_terms(store: Store, quest_id: int) -> dict[str, list[str]]:
    """The quest's ``meta.lit_gate`` term lists, normalised to
    ``{"host": […], "reaction": […]}`` (a list is dropped when absent
    or empty — an unconfigured axis gates nothing).

    Empty dict when the key is unset, malformed, both lists are empty,
    or the quest can't be read — every such case means *no gate*: the
    gate is opt-in and stays off until an operator writes the terms.
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return {}
    cfg = (getattr(ref, "meta", None) or {}).get(LIT_GATE_META_KEY)
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, list[str]] = {}
    for field in ("host", "reaction"):
        raw = cfg.get(field)
        if not isinstance(raw, list):
            continue
        terms = [str(t).strip() for t in raw if str(t or "").strip()]
        if terms:
            out[field] = terms
    return out


def _abstract(store: Store, ref: Ref) -> str:
    """The candidate's abstract: ``refs.meta['abstract']`` when carried,
    else the leading-chunk preview (:meth:`ChunkStore.abstract_previews`
    — Marker-ingested papers keep the abstract prose in the first body
    chunks, not the meta key)."""
    meta = getattr(ref, "meta", None) or {}
    ab = str(meta.get("abstract") or "").strip()
    if ab:
        return ab
    try:
        previews = store.chunks.abstract_previews([ref.id])
    except Exception:
        return ""
    return str(previews.get(ref.id) or "").strip()


def ref_has_metadata(store: Store, ref: Any) -> bool:
    """The no-metadata rule's unit: the candidate carries a title or an
    abstract. ``None`` (no such ref) is metadata-less by definition;
    :data:`UNREADABLE` is False too — the rule is mandatory, so an
    unreadable candidate defers rather than linking unverified."""
    if ref is UNREADABLE or ref is None:
        return False
    if (ref.title or "").strip():
        return True
    return bool(_abstract(store, ref))


def candidate_text(store: Store, ref: Any) -> str:
    """Title + abstract — the surface the term gate matches (the same
    snippet depth the tick's served-literature list shows)."""
    if ref is None or ref is UNREADABLE:
        return ""
    return "\n".join(p for p in [(ref.title or "").strip(), _abstract(store, ref)] if p)


def _matches(terms: list[str], text: str) -> bool:
    """Any term present as a case-insensitive *token* — bounded by
    non-word characters so ``NO`` doesn't match inside "novel" while
    ``Pd(111)`` still matches itself."""
    return any(
        re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE)
        for term in terms
    )


def check_terms(text: str, terms: dict[str, list[str]]) -> tuple[bool, str]:
    """The gate verdict on candidate text: ``(pass, reason)`` — the
    candidate must name at least one term from EACH configured list
    (host metal/facet AND reaction family); a missing axis gates
    nothing."""
    if terms.get("host") and not _matches(terms["host"], text):
        return False, "no host term"
    if terms.get("reaction") and not _matches(terms["reaction"], text):
        return False, "no reaction term"
    return True, "host+reaction terms match"


def gate_verdict(store: Store, quest_id: int, ref_id: int) -> tuple[bool, str]:
    """The pre-link term gate on one paper candidate → ``(pass, reason)``.

    No ``meta.lit_gate`` terms configured → pass (the gate is opt-in).
    An unreadable candidate → ``(False, "candidate unreadable —
    deferred")``: a store blip is no verdict, so the link is deferred
    rather than either approved or rejected. Otherwise the
    title+abstract must carry a host term and a reaction term, reported
    as ``no host term`` / ``no reaction term`` when missing — the
    one-line rejection reason the lit-search logbook line names per
    candidate.
    """
    terms = gate_terms(store, quest_id)
    if not terms:
        return True, f"no {LIT_GATE_META_KEY} terms configured"
    ref = candidate_ref(store, "paper", ref_id)
    if ref is UNREADABLE:
        return False, "candidate unreadable — deferred"
    return check_terms(candidate_text(store, ref), terms)


__all__ = [
    "GATE_SAMPLE_MODULUS",
    "LIT_GATE_META_KEY",
    "UNREADABLE",
    "candidate_ref",
    "candidate_text",
    "check_terms",
    "gate_terms",
    "gate_verdict",
    "ref_has_metadata",
]
