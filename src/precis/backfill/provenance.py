"""Provenance grades for recall candidates (source-backfill slice 6).

Not all sources are interchangeable evidence — a candidate's **kind** fixes how
it may be used, and conflating them is how a draft ends up "citing" a datasheet
for a scientific consensus or a private note as if it were external. So every
candidate carries a *provenance grade*:

- **peer-reviewed** (`paper`, `cfp`) — external, reviewed: citable support for a
  scientific claim.
- **prior-art** (`patent`, `datasheet`, `web`) — external but *not* peer-reviewed:
  cite for "this exists / was built / is specified," never for consensus.
- **lead** (`memory` and other own-authored kinds) — your *own* thinking, not
  external evidence at all: a lead to chase, never a citation. No default
  scope searches lead-tier kinds (gr553420): this is the conservative
  *default* a candidate gets when its kind is unknown or a caller opted one
  in explicitly via ``kinds=`` — not a tier the sweep produces.

The grade rides on every candidate (a bracketed tag in the render), **down-weights**
lower grades in the gap-rank (``weight``), and drives the standing skill admonition
on how to treat each. This is a policy, not a universal — venue norms differ — but
the default keeps the model honest about what a source can bear.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceGrade:
    """One provenance grade. ``rank`` orders grades (0 = strongest); ``weight``
    is the multiplicative gap-rank down-weight (1.0 = no penalty); ``admonition``
    is the one-line "how to treat this" the skill surfaces."""

    id: str
    rank: int
    tag: str
    weight: float
    admonition: str


PEER_REVIEWED = SourceGrade(
    "peer_reviewed",
    0,
    "peer-reviewed",
    1.0,
    "external and reviewed — citable support for a scientific claim.",
)
PRIOR_ART = SourceGrade(
    "prior_art",
    1,
    "prior-art",
    0.7,
    "external but NOT peer-reviewed — cite for 'this exists / was built / is "
    "specified,' never for scientific consensus.",
)
LEAD = SourceGrade(
    "lead",
    2,
    "own-note",
    0.4,
    "your OWN thinking, not external evidence — a lead, never a citation. To use "
    "it, write the claim yourself and find a real source, or flag an open thread.",
)

#: The ordered grade ladder, strongest first.
SOURCE_GRADES: tuple[SourceGrade, ...] = (PEER_REVIEWED, PRIOR_ART, LEAD)

_GRADE_BY_KIND: dict[str, SourceGrade] = {
    "paper": PEER_REVIEWED,
    "cfp": PEER_REVIEWED,
    "patent": PRIOR_ART,
    "datasheet": PRIOR_ART,
    "web": PRIOR_ART,
    "memory": LEAD,
}

#: Kinds the recall sweep searches — the kinds a draft can actually **cite**
#: (``handlers/citation._ACCEPTED_SOURCE_KINDS``; ``DraftHandler._CITABLE_KINDS``
#: adds ``finding``, but an ``fi`` hub is already-cited evidence, never a
#: suggestion). ``cfp``/``datasheet``/``web`` stay out: surfacing a source the
#: agent then cannot cite is noise (gr553420). Internal kinds are never
#: candidates either — a ``memory``/``todo``/``gripe`` (``SPACE:repo-dev`` ops
#: notes, runbooks, gotchas) is a note you already wrote, not a citable
#: source, so the lead tier stays a *grade* (``grade_for``'s conservative
#: default) but never a *search scope*; the ref-level rendering survives only
#: for a handle-less kind a caller passes ``kinds=`` explicitly. Each searched
#: kind exposes a **chunk** handle (``pc``/``pk``) the workspace opens.
SOURCE_KINDS: tuple[str, ...] = ("paper", "patent")


def grade_for(kind: str | None) -> SourceGrade:
    """The provenance grade of a source kind. An unknown / own-authored kind
    defaults to ``LEAD`` — the conservative "treat as a lead, not evidence"
    assumption, so a new kind can never silently masquerade as peer-reviewed."""
    return _GRADE_BY_KIND.get(kind or "", LEAD)


def grade_tag(kind: str | None) -> str:
    """The bracketed render tag for a kind's grade, e.g. ``[peer-reviewed]``."""
    return f"[{grade_for(kind).tag}]"
