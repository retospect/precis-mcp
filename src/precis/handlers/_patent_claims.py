"""Claim-structure classification for ingested patents (slice 1).

A patent's claims carry legal structure the freedom-to-operate writing
loop needs (``docs/design/patent-authoring-loop.md``): which claims are
**independent** — they define standalone legal scope, the claims a new
application must design around and can never silently drop from a novelty
view — versus **dependent**, which merely narrow an antecedent claim.
This module derives that structure from the claim text at ingest so each
claim block can be marked in ``chunks.meta`` and retrieved on its own
(``view='claims'``), and so a scoping decision can point at *the exact*
prior-art claim it designs around.

It is a **heuristic**, like :func:`precis.ingest.blocks.classify_density`:
a dependent claim back-references a lower-numbered claim ("The method of
claim 3, wherein…", "according to any preceding claim"); an independent
claim does not. The result is stored, not authoritative — a review pass
or a re-run can refine it without a migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: "…any preceding claim" / "any one of the preceding claims" — a
#: dependent claim that narrows *every* earlier claim.
_PRECEDING_RE = re.compile(r"preceding\s+claims?", re.I)

#: A reference to specific claim number(s): the "claim(s)" anchor followed
#: by a number spec ("1", "1 to 3", "1 or 2", "1, 2 and 5"). Anchoring on
#: the word "claim" is what keeps stray magnitudes ("0.5 to 2.0 wt%") out
#: of the dependency set.
_CLAIM_REF_RE = re.compile(
    r"claims?\s+(\d+(?:\s*(?:to|through|[-–]|or|and|,)\s*\d+)*)",
    re.I,
)

#: A "N to M" / "N-M" / "N through M" span inside a number spec → the
#: inclusive range of claim numbers.
_RANGE_RE = re.compile(r"(\d+)\s*(?:to|through|[-–])\s*(\d+)", re.I)


@dataclass(frozen=True, slots=True)
class ClaimStructure:
    """The derived structure of one claim.

    ``number`` is 1-based document order; ``depends_on`` is the sorted set
    of earlier claim numbers a dependent claim references (empty for an
    independent claim).
    """

    number: int
    independent: bool
    depends_on: list[int]


def classify_claim(text: str, number: int) -> ClaimStructure:
    """Classify claim ``number`` (1-based, document order) from its text.

    Only references to claims *before* this one count as a dependency — a
    self- or forward-reference is ignored (defensive; well-formed claims
    never forward-reference). A claim that references no earlier claim is
    independent.
    """
    deps: set[int] = set()
    if _PRECEDING_RE.search(text):
        deps.update(range(1, number))
    for m in _CLAIM_REF_RE.finditer(text):
        span = m.group(1)
        for r in _RANGE_RE.finditer(span):
            lo, hi = int(r.group(1)), int(r.group(2))
            if lo <= hi:
                deps.update(range(lo, hi + 1))
        for n in re.findall(r"\d+", span):
            deps.add(int(n))
    earlier = sorted(d for d in deps if 0 < d < number)
    return ClaimStructure(number=number, independent=not earlier, depends_on=earlier)


def claim_block_meta(text: str, number: int) -> dict[str, object]:
    """The ``chunks.meta`` marker for a claim block (patent ingest)."""
    s = classify_claim(text, number)
    return {
        "patent_block": "claim",
        "claim_number": s.number,
        "claim_independent": s.independent,
        "depends_on": s.depends_on,
    }


#: The ``chunks.meta`` marker for a description block.
DESCRIPTION_BLOCK_META: dict[str, object] = {"patent_block": "description"}
