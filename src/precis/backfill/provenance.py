"""Provenance tiers for recall candidates (source-backfill slice 6).

Not all sources are interchangeable evidence — a candidate's **kind** fixes how
it may be used, and conflating them is how a draft ends up "citing" a datasheet
for a scientific consensus or a private note as if it were external. So every
candidate carries a *provenance tier*:

- **peer-reviewed** (`paper`, `cfp`) — external, reviewed: citable support for a
  scientific claim.
- **prior-art** (`patent`, `datasheet`, `web`) — external but *not* peer-reviewed:
  cite for "this exists / was built / is specified," never for consensus.
- **lead** (`memory` and other own-authored kinds) — your *own* thinking, not
  external evidence at all: a lead to chase, never a citation.

The tier rides on every candidate (a bracketed tag in the render), **down-weights**
lower tiers in the gap-rank (``weight``), and drives the standing skill admonition
on how to treat each. This is a policy, not a universal — venue norms differ — but
the default keeps the model honest about what a source can bear.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tier:
    """One provenance tier. ``rank`` orders tiers (0 = strongest); ``weight`` is
    the multiplicative gap-rank down-weight (1.0 = no penalty); ``admonition`` is
    the one-line "how to treat this" the skill surfaces."""

    id: str
    rank: int
    tag: str
    weight: float
    admonition: str


PEER_REVIEWED = Tier(
    "peer_reviewed",
    0,
    "peer-reviewed",
    1.0,
    "external and reviewed — citable support for a scientific claim.",
)
PRIOR_ART = Tier(
    "prior_art",
    1,
    "prior-art",
    0.7,
    "external but NOT peer-reviewed — cite for 'this exists / was built / is "
    "specified,' never for scientific consensus.",
)
LEAD = Tier(
    "lead",
    2,
    "own-note",
    0.4,
    "your OWN thinking, not external evidence — a lead, never a citation. To use "
    "it, write the claim yourself and find a real source, or flag an open thread.",
)

#: The ordered tier ladder, strongest first.
TIERS: tuple[Tier, ...] = (PEER_REVIEWED, PRIOR_ART, LEAD)

_TIER_BY_KIND: dict[str, Tier] = {
    "paper": PEER_REVIEWED,
    "cfp": PEER_REVIEWED,
    "patent": PRIOR_ART,
    "datasheet": PRIOR_ART,
    "web": PRIOR_ART,
    "memory": LEAD,
}

#: Kinds the recall sweep searches by default. The citeable evidence kinds each
#: expose a **chunk** handle the workspace opens at the chunk (``pc``/``qc``/``pk``/
#: ``dk``). ``memory`` (the ``lead`` tier) has **no** chunk handle, so it rides as a
#: **ref-level** candidate — addressed + rendered by its ``me<id>`` record handle as
#: a flat note eye (a lead to chase, never a citation; the ``[own-note]`` admonition
#: keeps the model honest). ``web`` stays out: it has no record handle *either*, so
#: there is nothing to open it under yet — its enabling follow-up is a web record
#: code in the handle registry.
SOURCE_KINDS: tuple[str, ...] = ("paper", "cfp", "patent", "datasheet", "memory")


def tier_for(kind: str | None) -> Tier:
    """The provenance tier of a source kind. An unknown / own-authored kind
    defaults to ``LEAD`` — the conservative "treat as a lead, not evidence"
    assumption, so a new kind can never silently masquerade as peer-reviewed."""
    return _TIER_BY_KIND.get(kind or "", LEAD)


def tier_tag(kind: str | None) -> str:
    """The bracketed render tag for a kind's tier, e.g. ``[peer-reviewed]``."""
    return f"[{tier_for(kind).tag}]"
