"""The one shared evidence-eligibility predicate.

docs/backlog/graph-node-trust-state.md slice 2a. Taproot support counting
(:mod:`precis.taproot.seniority`), the nanopub mint gate
(:func:`precis.nanopub.gates.run_mint_gates`) and the draft cite nudge
(:func:`precis.taproot.lookup.hubs_grounded_by_paper`) all call
:func:`evidence_use`, so "may this edge act as evidence?" has one answer.

Definition-only grounding is excluded by type: a definition supports
structural navigation, never a claim. Every existing stronger rule
(hypothesis exclusion, ``support == "no"`` exclusion, verification,
staleness) stays with its caller. No defined grounding is stored yet
(slice 2b), so in production nothing yields :attr:`Grounding.DEFINED`
today; the predicate and its wiring are ready for it.

Deliberately free of ``precis.taproot.hub`` imports (hub -> seniority ->
here); the ``SUPPORT_RELATIONS == HUB_ROLES - {"contradicts"}`` pin lives in
``tests/test_trust_evidence_eligibility.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from precis.store.types import _INVERSE_RELATIONS


class RelationClass(StrEnum):
    STRUCTURAL = "structural"
    EVIDENTIAL = "evidential"
    OTHER = "other"


# Forward spellings; inverses (generalises, contains, represented-by,
# realizes, used-in, qualified-by, has-instance) resolve to these through
# canonical_relation. Unregistered is-a, measured-by and synonym-of are NOT
# added here — a relation joins a class only once it is a registered slug.
STRUCTURAL_RELATIONS: frozenset[str] = frozenset(
    {
        "specialises",
        "part-of",
        "represents",
        "realized-by",
        "made-of",
        "qualifies",
        "instance-of",
    }
)
# ``contradicted-by`` resolves to ``contradicts``.
EVIDENTIAL_RELATIONS: frozenset[str] = frozenset(
    {"establishes", "corroborates", "contradicts", "refines"}
)
SUPPORT_RELATIONS: frozenset[str] = frozenset({"establishes", "corroborates"})
# In the evidential gate class but never carries support.
ADVISORY_RELATIONS: frozenset[str] = frozenset({"refines"})

_CLASSED = STRUCTURAL_RELATIONS | EVIDENTIAL_RELATIONS


def canonical_relation(slug: str) -> str:
    """Resolve an inverse spelling to its classed forward slug; any other
    slug comes back unchanged."""
    if slug in _CLASSED:
        return slug
    inverse = _INVERSE_RELATIONS.get(slug)
    if inverse is not None and inverse in _CLASSED:
        return inverse
    return slug


def relation_class(slug: str) -> RelationClass:
    canon = canonical_relation(slug)
    if canon in STRUCTURAL_RELATIONS:
        return RelationClass.STRUCTURAL
    if canon in EVIDENTIAL_RELATIONS:
        return RelationClass.EVIDENTIAL
    return RelationClass.OTHER


class Grounding(StrEnum):
    """Typed support behind an edge or node — not an ordering. DEFINED is
    never a rung below SOURCED; do not compare these numerically."""

    NONE = "none"
    MOTIVATED = "motivated"
    DEFINED = "defined"
    SOURCED = "sourced"
    VERIFIED = "verified"


def edge_groundings(
    relation: str, src_chunk_id: int | None, meta: Mapping[str, Any] | None
) -> frozenset[Grounding]:
    """Groundings derivable from an edge's existing storage. Read-only
    derivation: never yields DEFINED — slice 2b's storage adds that."""
    meta = meta or {}
    out: set[Grounding] = set()
    if meta.get("verified_by") and meta.get("verified_claim_sha"):
        out.add(Grounding.VERIFIED)
    if src_chunk_id is not None or meta.get("source_handle"):
        out.add(Grounding.SOURCED)
    if canonical_relation(relation) == "motivated-by":
        out.add(Grounding.MOTIVATED)
    return frozenset(out) or frozenset({Grounding.NONE})


@dataclass(frozen=True)
class EvidenceUse:
    #: May act as evidence, for or against.
    eligible: bool
    carries_support: bool
    #: Named reason when not eligible or not support.
    rule: str | None = None


RULE_DEFINITION_ONLY = "definition-only-grounding"
RULE_NOT_EVIDENTIAL = "not-an-evidential-relation"
RULE_ADVISORY = "refines-is-advisory"
RULE_COUNTER = "counter-evidence"

DEFINITION_ONLY_MESSAGE = (
    "definition-only grounding is not evidence (graph-node-trust-state "
    "rule 2): a definition supports structural navigation only; attach a "
    "sourced or verified grounding"
)


def definition_only_message(subject: str = "") -> str:
    """The shared refusal wording, optionally prefixed with its subject."""
    if subject:
        return f"{subject}: {DEFINITION_ONLY_MESSAGE}"
    return DEFINITION_ONLY_MESSAGE


#: The groundings that are evidence. Only one of these lifts a definition's
#: exclusion; ``none``/``motivated`` alongside a definition do not.
_EVIDENTIAL_GROUNDINGS = frozenset({Grounding.SOURCED, Grounding.VERIFIED})


def is_definition_only(groundings: Iterable[Grounding]) -> bool:
    """A definition with no sourced or verified grounding beside it."""
    gs = frozenset(groundings)
    return Grounding.DEFINED in gs and not gs & _EVIDENTIAL_GROUNDINGS


def evidence_use(
    relation: str,
    groundings: Iterable[Grounding],
    *,
    src_groundings: Iterable[Grounding] | None = None,
    dst_groundings: Iterable[Grounding] | None = None,
) -> EvidenceUse:
    """Whether an edge may act as evidence, and whether it carries support.

    Trusted endpoints never upgrade a loose edge; a definition-only endpoint
    disqualifies the edge."""
    canon = canonical_relation(relation)
    if relation_class(canon) is not RelationClass.EVIDENTIAL:
        return EvidenceUse(False, False, RULE_NOT_EVIDENTIAL)
    if (
        is_definition_only(groundings)
        or (src_groundings is not None and is_definition_only(src_groundings))
        or (dst_groundings is not None and is_definition_only(dst_groundings))
    ):
        return EvidenceUse(False, False, RULE_DEFINITION_ONLY)
    if canon in ADVISORY_RELATIONS:
        return EvidenceUse(True, False, RULE_ADVISORY)
    if canon == "contradicts":
        return EvidenceUse(True, False, RULE_COUNTER)
    return EvidenceUse(True, True, None)
