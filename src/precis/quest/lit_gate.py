"""Quest lit-search relevance gate — the cheap pre-link term check (gr592951).

The lit-search step used to link every candidate its legs returned:
qu164903 accreted smoke-adsorption, nanofluid and Zn–CO2 papers for an
NO→NH3-on-doped-Pd(111) striving — each one a ``serves`` row, tick tokens
and logbook noise, and the tick's own logbook already called them
host-mismatched false positives. This module is the mechanical gate before
``store.add_link``: a candidate's title+abstract must hit the quest's
declared rubric terms, or it is logged "seen, not linked" with the reason.

**Config** — ``meta.lit_gate`` (an ``edit(kind='quest', meta=...)``
allowlist key): ``{"host": [<terms>], "reaction": [<terms>]}`` — the host
metal/facet vocabulary (``Pd``, ``Pd(111)``, dopant names) and the reaction
family (``NO``, ``NH3``, ``nitric oxide reduction``). A missing or
misshaped config means the gate is OFF — every candidate passes: the term
lists are chemistry judgment, so the gate stays off until an operator sets
them. An absent or empty list skips that side of the check. Terms match as
case-insensitive whole tokens (``(?<!\\w)term(?!\\w)``): ``NO`` hits "NO
reduction" but not "notable", and ``Pd(111)`` hits "on Pd(111) surfaces".
A hit anywhere in title+abstract passes — the check is "does the text
mention the host/reaction", not "is it the subject".

**What the gate is not.** Cheap triage never becomes a durable rejection:
a gate failure writes NO ``considered`` verdict (spec me626852 — only an
explicit judgment records one; the tick's ``judgments`` channel is that
door). It is also not the only discovery path: ``run_search_step`` links
every ``ref_id % 5 == 0`` rejection anyway ("sampled past gate"), so a bad
term list costs recall at 4/5, not 5/5. And it is separate from the
always-on metadata rule (:func:`has_metadata`): a record carrying neither
title nor abstract is *never* linked, sampled or not.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Ref, Store

__all__ = [
    "LIT_GATE_META_KEY",
    "candidate_text",
    "gate_terms",
    "gate_verdict",
    "has_metadata",
    "verdict_for_text",
]

#: Quest meta key holding the relevance-gate term lists —
#: ``{"host": [...], "reaction": [...]}``. Patchable via
#: ``edit(kind='quest', meta={'lit_gate': {...}})``.
LIT_GATE_META_KEY = "lit_gate"

#: Term lists the config dict may carry — an unknown key is a typo the
#: ``edit`` door's ``_check_lit_gate`` refuses; read-side it is ignored.
GATE_TERM_KEYS: tuple[str, ...] = ("host", "reaction")


def _term_hit(term: str, text: str) -> bool:
    """Case-insensitive whole-token match: ``term`` appears in ``text``
    unbracketed by word characters, so a reaction term like ``NO`` matches
    "NO reduction" but not "notable", and a facet like ``Pd(111)`` matches
    "on Pd(111) surfaces"."""
    return (
        re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE)
        is not None
    )


def gate_terms(store: Store, quest_id: int) -> dict[str, list[str]]:
    """The quest's ``meta.lit_gate`` term lists, normalised to
    ``{"host": [...], "reaction": [...]}`` with blanks dropped — ``{}``
    (gate off) when the key is unset, misshaped, or the read fails (the
    common case in unit tests: a store double with no ``get_ref``).
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return {}
    meta = (getattr(ref, "meta", None) or {}) if ref is not None else {}
    cfg = meta.get(LIT_GATE_META_KEY)
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key in GATE_TERM_KEYS:
        raw = cfg.get(key)
        if not isinstance(raw, list):
            continue
        terms = [t for t in (str(v or "").strip() for v in raw) if t]
        if terms:
            out[key] = terms
    return out


def candidate_text(store: Store, ref: Ref | Any) -> str:
    """The text the gate judges a candidate on: its title plus its abstract
    (``meta.abstract``, else the first non-heading body chunk — the same
    "substance" fallback :func:`precis.quest.tick._paper_abstract_snippet`
    uses). Empty when neither exists."""
    title = (getattr(ref, "title", "") or "").strip()
    meta = getattr(ref, "meta", None) or {}
    abstract = ""
    if isinstance(meta, dict):
        raw = meta.get("abstract")
        if isinstance(raw, str):
            abstract = raw.strip()
    if not abstract:
        try:
            blocks = store.chunks.list_chunks_for_ref(ref.id)
        except Exception:
            blocks = []
        for b in blocks:
            if getattr(b, "chunk_kind", None) == "heading":
                continue
            t = (getattr(b, "text", "") or "").strip()
            if t:
                abstract = t
                break
    return f"{title}\n{abstract}".strip()


def has_metadata(store: Store, ref: Ref | Any) -> bool:
    """The always-on rule (gr592951 slice 1): a candidate carrying neither
    a title nor any abstract text is never linked — there is nothing to
    judge relevance from, and the qu164903 logbook itself labelled those
    host-mismatched false positives."""
    return bool(candidate_text(store, ref))


def verdict_for_text(terms: dict[str, list[str]], text: str) -> tuple[bool, str]:
    """The pure check — ``(pass, reason)`` for already-resolved term lists
    and candidate text. A configured list with no hit names its failure;
    an absent/empty list skips that side."""
    host = terms.get("host") or []
    reaction = terms.get("reaction") or []
    if host and not any(_term_hit(t, text) for t in host):
        return False, "no host term"
    if reaction and not any(_term_hit(t, text) for t in reaction):
        return False, "no reaction term"
    return True, ""


def gate_verdict(
    store: Store, quest_id: int, ref_id: int, *, ref: Ref | Any = None
) -> tuple[bool, str]:
    """``(pass, reason)`` for one lit-search candidate — the spec'd gate
    API. ``(True, "")`` when the quest declares no ``lit_gate`` terms or
    the candidate can't be read; a fetch failure degrades to an empty
    text, which fails a configured gate ("no host term") — an unreadable
    candidate is exactly what the gate exists to hold back. ``ref`` lets
    a caller that already fetched the candidate (``run_search_step``'s
    batch ``fetch_refs_by_ids``) skip the second read."""
    terms = gate_terms(store, quest_id)
    if not terms:
        return True, ""
    if ref is None:
        try:
            ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
        except Exception:
            ref = None
    text = candidate_text(store, ref) if ref is not None else ""
    return verdict_for_text(terms, text)
