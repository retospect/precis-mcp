"""Stage 3 — normalisation: alias merge, unit → dimension, the mismatch gate.

``taxonomy-bootstrap.md`` §Design, stage 3. Two deterministic passes over
stage-2 output (:class:`~precis.taxonomy.types.DiscoveredTerm`):

* :func:`alias_key` / :func:`normalise` group rows into
  :class:`~precis.taxonomy.types.TermNode` candidates by a lexical key —
  Unicode fold, casefold, Greek-letter and US/UK spelling normalisation,
  then the campaign's synonym families (:func:`fold_aliases`: species
  qualifier position and spelling, whole-phrase synonyms — blocker 4 of
  ``taxonomy-bootstrap.md``). This key is for **resolution only**:
  ``term-taxonomy.md`` makes the node's ref_id the identity, so two nodes
  colliding on ``alias_key`` is a merge candidate, never an identity
  collision.
* :func:`canonical_qualifier` maps stage 2's prose answers for
  ``reference_state`` / ``convention`` / ``normalisation_basis`` onto the
  campaign's :class:`~precis.taxonomy.config.QualifierVocabulary` before
  grouping, so ``"RHE"`` and ``"reversible hydrogen electrode (RHE)"`` are
  one reference state and sign prose in ``convention`` does not split a
  node.
* :func:`resolve_dimension` turns a raw unit string into a
  :class:`~precis.taxonomy.types.DimensionSpec` via ``pint`` plus the
  campaign's extra unit definitions. **No embeddings anywhere in this
  module** — ``taxonomy-bootstrap.md`` rules embeddings out as a decision
  input; :func:`normalise`'s merge suggestions are lexical
  (``difflib.SequenceMatcher`` + token-set Jaccard), not vector similarity.

Dimension mismatch is a hard gate (AC5): two rows with the same
``alias_key`` but non-comparable dimensions become two ``TermNode``\\ s, not
one with a dropped exponent. Merge *suggestions* (:class:`MergeSuggestion`)
are advisory only — v1 of ``term-taxonomy`` has no merge-as-redirect, so
nothing here ever merges two nodes on its own initiative.
"""

from __future__ import annotations

import difflib
import itertools
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

import pint

from precis.taxonomy.config import (
    CampaignConfig,
    MeasurandAliases,
    QualifierVocabulary,
)
from precis.taxonomy.types import (
    SI_BASE_ORDER,
    DimensionSpec,
    DiscoveredTerm,
    TermNode,
)

#: pint spells a dimensionality's keys ``[<base>]``; this is the set of keys
#: that are pure SI bases per ``SI_BASE_ORDER``. Anything else in a parsed
#: dimensionality (``[decade]`` from the campaign's ``mV/dec`` definition)
#: means the quantity is not SI-comparable at all.
_SI_BASE_KEYS: Final[frozenset[str]] = frozenset(f"[{name}]" for name in SI_BASE_ORDER)

#: Greek letters that occur in the norr-her-meta vocabulary (ΔG, η, μ...).
#: Mapped to their spelled-out name, padded with spaces so the substitution
#: never glues onto an adjacent word (``ΔG`` must fold the same as ``delta
#: G``, not collapse to ``deltag``) — the padding is undone by the
#: punctuation-collapse step in :func:`alias_key`. Covers the full lowercase
#: alphabet (casefold already lowers ``Δ`` to ``δ`` before this map is
#: consulted) since the cost of completeness is one dict, not "the handful
#: that occur" plus a gap for the next one.
_GREEK_TO_NAME: Final[dict[str, str]] = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "ζ": "zeta",
    "η": "eta",
    "θ": "theta",
    "ι": "iota",
    "κ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "ο": "omicron",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "ς": "sigma",
    "τ": "tau",
    "υ": "upsilon",
    "φ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
}

#: British → American substitutions for the handful of spellings that occur
#: in this corpus (electrochemistry writes "normalisation"/"polarisation";
#: papers vary within one venue). Applied as plain substring replacement —
#: "-isation" → "-ization" also handles "polarisation", so it is one rule,
#: not two.
_BRITISH_TO_AMERICAN: Final[tuple[tuple[str, str], ...]] = (
    ("isation", "ization"),
    ("ised", "ized"),
)

#: Collapses any run of non-alphanumeric characters (whitespace, punctuation,
#: the NFKD-folded minus sign U+2212, the padding spaces the Greek map adds)
#: into a single hyphen.
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: The English link words a measurand puts between the quantity and its
#: species qualifier: ``faradaic efficiency for NH3`` / ``toward NH3`` /
#: ``of NH3``. Not chemistry, so they live here rather than in the campaign
#: YAML (AC8); the species tokens they attach to come from the campaign.
#: ``to`` is deliberately absent — ``nitrate-to-ammonia`` and ``NO to NHO``
#: name a step, and eating the ``to`` would fold a reaction into a species.
_QUALIFIER_LINK_WORDS: Final[tuple[str, ...]] = ("for", "toward", "towards", "of")

#: Below this composite score, two differently-keyed nodes are not offered
#: as a merge suggestion. See :func:`_lexical_similarity` for why 0.82 sits
#: close to 1.0 rather than difflib's usual "close enough" cutoff (~0.6).
_MERGE_SIMILARITY_THRESHOLD: Final[float] = 0.82

#: Weight on the character-level ratio in :func:`_lexical_similarity`'s
#: composite; the remainder goes to token-set Jaccard. 4:1 rather than an
#: even split: most near-duplicates worth suggesting are a typo or a
#: pluralization inside an otherwise-identical token sequence
#: (``onset-potential`` / ``onset-potental``), where the ratio is ~0.95 but
#: a single differing token still tanks Jaccard to ~0.33 — an even weighting
#: would suppress exactly the case this function exists to catch. A purely
#: reordered pair (``yield-rate`` / ``rate-yield``, ratio ~0.5, Jaccard 1.0)
#: still lands below threshold under this weighting, which is intentional:
#: reordering is a distinct-enough phrasing that a human should see it as
#: two rows, not a silent auto-suggestion.
_RATIO_WEIGHT: Final[float] = 0.8


def si_vector(dimensionality: Mapping[str, int] | Any) -> str:
    """Render a pint dimensionality mapping as the canonical SI exponent string.

    ``SI_BASE_ORDER`` fixes the slot order (``types.py``): the output is
    always seven comma-joined integers, e.g. ``"0,0,-1,0,0,0,0"`` for s⁻¹
    (slot 3 is time). A base absent from ``dimensionality`` is exponent 0.
    This function does not check for non-SI bases (``[decade]``) — that
    check is :func:`resolve_dimension`'s job, done *before* it decides
    whether to call this at all, so a Tafel slope's ``[decade]`` exponent is
    never silently dropped into an SI vector that would then compare equal
    to a voltage.
    """
    exponents = [str(int(dimensionality.get(f"[{name}]", 0))) for name in SI_BASE_ORDER]
    return ",".join(exponents)


def resolve_dimension(
    raw_unit: str | None,
    registry: pint.UnitRegistry,
    config: CampaignConfig,
    *,
    base_year: int | None = None,
) -> DimensionSpec | None:
    """Classify a raw unit string into a :class:`DimensionSpec`, or ``None``.

    Order of checks, per ``taxonomy-bootstrap.md`` stage 3:

    1. No unit (``None`` or empty) ⇒ ``None`` — unknown, the caller escalates.
    2. A ``config.currency_codes`` member ⇒ ``kind="currency"``. Checked
       before parsing because a currency code like ``USD`` is not itself in
       the unit registry.
    3. Parses dimensionless (``%``, ``ppm``) ⇒ ``kind="dimensionless"``.
    4. Parses with a non-SI base in its dimensionality (the campaign's
       ``[decade]`` from ``mV/dec``) ⇒ ``kind="scale"`` — never a truncated
       SI vector.
    5. Otherwise, a clean SI dimensionality ⇒ ``kind="si"`` with
       :func:`si_vector`.
    6. Does not parse at all ⇒ ``None``. ``pint`` raises several distinct
       exception types for a bad token (``UndefinedUnitError``,
       ``TypeError`` from its parser on stray punctuation); any parse
       failure means "unknown unit" to this caller, so the catch is broad
       by design, not sloppiness.
    """
    if not raw_unit:
        return None
    code = raw_unit.strip()
    if not code:
        return None
    if code in config.currency_codes:
        return DimensionSpec(kind="currency", currency_code=code, base_year=base_year)
    try:
        quantity = registry.Quantity(1, code)
    except Exception:
        return None
    if quantity.dimensionless:
        return DimensionSpec(kind="dimensionless")
    dimensionality = quantity.dimensionality
    if any(base not in _SI_BASE_KEYS for base in dimensionality):
        return DimensionSpec(kind="scale")
    return DimensionSpec(kind="si", si_vector=si_vector(dimensionality))


def _lexical_key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).casefold()
    for british, american in _BRITISH_TO_AMERICAN:
        folded = folded.replace(british, american)
    folded = "".join(
        f" {_GREEK_TO_NAME[ch]} " if ch in _GREEK_TO_NAME else ch for ch in folded
    )
    return _NON_ALNUM.sub("-", folded).strip("-")


def _replace_tokens(key: str, variant: str, canonical: str) -> str:
    """Replace ``variant`` with ``canonical`` where it sits on token bounds.

    Both are hyphen-joined token runs; a match must start and end at a
    hyphen or the key's edge, so ``no`` never matches inside ``no3``.
    """
    pattern = rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])"
    return re.sub(pattern, canonical, key)


def fold_aliases(key: str, aliases: MeasurandAliases) -> str:
    """Collapse the campaign's synonym families onto one key each.

    Input is an already-lexical key (:func:`_lexical_key`); output is the
    same shape. Four deterministic rewrites, in this order:

    1. every species variant → its canonical token
       (``ammonia`` → ``nh3``, ``nh4`` from ``NH4+`` stays ``nh4``);
    2. a trailing ``<link word>-<species>`` → ``-<species>`` for the link
       words in :data:`_QUALIFIER_LINK_WORDS` (``faradaic-efficiency-for-nh3``
       → ``faradaic-efficiency-nh3``). Tail only: ``ratio-of-nh3-yield-rate``
       is a ratio, not an NH3-qualified ratio, and keeps its ``of``;
    3. a species that *leads* the key moves to its tail
       (``nh3-yield-rate`` → ``yield-rate-nh3``), so qualifier position no
       longer splits a family. Only the leading token moves; a species in
       the middle (``change-in-nh3-selectivity``) is left where it is
       because that key names a different quantity and the tail form would
       collide with it;
    4. every phrase variant → its canonical phrase, longest variant first
       so ``applied-electrode-potential`` is rewritten before
       ``electrode-potential`` could match inside it.

    Nothing here folds one species onto another: two keys that differ only
    in ``nh3`` vs ``nh4`` stay two keys — ``taxonomy-bootstrap.md`` blocker
    4 makes that a campaign decision, not a normalisation.
    """
    for canonical, variants in aliases.species.items():
        for variant in variants:
            key = _replace_tokens(key, variant, canonical)
    species = tuple(aliases.species)
    if species:
        alternation = "|".join(
            re.escape(s) for s in sorted(species, key=len, reverse=True)
        )
        links = "|".join(_QUALIFIER_LINK_WORDS)
        key = re.sub(rf"-(?:{links})-({alternation})$", r"-\1", key)
        key = re.sub(rf"^({alternation})-(.+)$", r"\2-\1", key)
    phrase_rules = sorted(
        (
            (variant, canonical)
            for canonical, vs in aliases.phrases.items()
            for variant in vs
        ),
        key=lambda pair: (-len(pair[0]), pair[0]),
    )
    for variant, canonical in phrase_rules:
        key = _replace_tokens(key, variant, canonical)
    return key


def alias_key(text: str, aliases: MeasurandAliases | None = None) -> str:
    """A lexical resolution key for merge grouping — **not an identity**.

    NFKD-fold (``⁻¹``/``₂`` → ASCII), casefold, fold Greek letters to their
    spelled-out name, fold the handful of British spellings this corpus
    uses to American, then collapse every run of punctuation/whitespace to
    a single hyphen and strip the ends. With ``aliases`` (the campaign's
    :class:`~precis.taxonomy.config.MeasurandAliases`) the key then goes
    through :func:`fold_aliases`, so the synonym families the campaign
    names collapse onto one key; without it the key is purely lexical.
    Two raw strings sharing an ``alias_key`` are candidates for the same
    :class:`TermNode`; ``term-taxonomy.md`` makes the node's ref_id the
    identity, so a collision here is a resolution decision, never an
    identity one.
    """
    key = _lexical_key(text)
    if aliases is None:
        return key
    return fold_aliases(key, aliases)


def canonical_qualifier(
    field: str, value: str | None, vocabulary: QualifierVocabulary
) -> tuple[str | None, str | None]:
    """Map one qualifier value onto the campaign vocabulary.

    Returns ``(canonical_or_kept_value, note)``. Matching is by
    :func:`alias_key` on both sides, so ``"RHE"``, ``"vs. RHE"`` and
    ``"reversible hydrogen electrode (RHE)"`` all land on ``rhe`` when the
    vocabulary lists them; the canonical id matches itself. A listed field
    with an unlisted value follows the field's policy (see
    :class:`~precis.taxonomy.config.QualifierVocabulary`): ``convention``
    drops to ``None``; the other two keep the value's lexical key. Either
    way the note names the raw value so it can be added to the vocabulary
    or refused deliberately — the vocabulary grows from these notes, which
    is why an unlisted value is never silent.
    """
    if value is None:
        return None, None
    entries: dict[str, tuple[str, ...]] = getattr(vocabulary, field)
    key = _lexical_key(value)
    for canonical, variants in entries.items():
        if key == _lexical_key(canonical) or any(
            key == _lexical_key(v) for v in variants
        ):
            return canonical, None
    if field == "convention":
        return None, (
            f"convention {value!r} is not in the campaign vocabulary — dropped "
            "(sign/direction prose does not make two values non-comparable)"
        )
    return key, f"{field} {value!r} is not in the campaign vocabulary — kept as {key!r}"


@dataclass(frozen=True, slots=True)
class MergeSuggestion:
    """An advisory near-duplicate pairing across two ``alias_key``\\ s.

    Never applied automatically — v1 emits suggestions only (see module
    docstring). ``left``/``right`` are sorted so a suggestion has one
    canonical direction.
    """

    left: str
    right: str
    similarity: float
    reason: str


def _lexical_similarity(a: str, b: str) -> float:
    """Composite of character-level and token-level similarity.

    ``difflib.SequenceMatcher``'s character ratio carries most of the
    weight (see :data:`_RATIO_WEIGHT`) because the near-duplicates worth
    suggesting are typos/pluralizations within an otherwise-shared token
    sequence; token-set Jaccard is the secondary signal that still lets a
    pure word-reordering (high Jaccard, mediocre ratio) contribute without
    letting it dominate. The threshold
    (``_MERGE_SIMILARITY_THRESHOLD = 0.82``) sits close to 1.0 rather than
    difflib's usual "close enough" cutoff around 0.6 because this is a
    merge *suggestion* surfaced in a report, not an automatic merge — a
    false negative just means a human sees two rows instead of one; a false
    positive clutters the report with unrelated pairs.
    """
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    tokens_a = set(a.split("-"))
    tokens_b = set(b.split("-"))
    if not tokens_a and not tokens_b:
        jaccard = 1.0
    else:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return _RATIO_WEIGHT * ratio + (1 - _RATIO_WEIGHT) * jaccard


def _dimension_label(dimension: DimensionSpec | None) -> str:
    """Short human-readable tag for a dimension, used in collision notes."""
    if dimension is None:
        return "unresolved"
    if dimension.kind == "si":
        vector = dimension.si_vector or ""
        parts = []
        for name, exponent in zip(SI_BASE_ORDER, vector.split(","), strict=False):
            if exponent in ("", "0"):
                continue
            parts.append(f"[{name}]" if exponent == "1" else f"[{name}]^{exponent}")
        return "*".join(parts) if parts else "dimensionless-si"
    if dimension.kind == "currency":
        return f"currency:{dimension.currency_code}:{dimension.base_year}"
    return dimension.kind


def _units_seen(rows: Sequence[DiscoveredTerm]) -> tuple[tuple[str, int], ...]:
    """``(raw_unit, count)`` from the group's mentions, ``None`` skipped.

    Sorted by descending count then unit string (``TermNode.units_seen``'s
    own contract) so :attr:`TermNode.canonical_unit` — ``units_seen[0][0]``
    — is deterministic under input reordering.
    """
    counts: Counter[str] = Counter(
        row.mention.raw_unit for row in rows if row.mention.raw_unit is not None
    )
    return tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _build_node(
    key: str,
    dimension: DimensionSpec | None,
    reference_state: str | None,
    convention: str | None,
    normalisation_basis: str | None,
    rows: Sequence[DiscoveredTerm],
    notes: tuple[str, ...] = (),
) -> TermNode:
    label_counts: Counter[str] = Counter(row.measurand for row in rows)
    top = max(label_counts.values())
    # Alphabetical tie-break keeps the label choice deterministic (AC1-style
    # determinism requirement, restated for stage 3 in the module docstring).
    label = min(m for m, c in label_counts.items() if c == top)
    aliases = tuple(sorted(m for m in label_counts if m != label))
    paper_ref_ids = frozenset(row.mention.anchor.source_ref_id for row in rows)
    halves = frozenset(row.half for row in rows)
    anchors = tuple(
        sorted(
            (row.mention.anchor for row in rows),
            key=lambda a: (a.source_ref_id, a.start, a.end),
        )
    )
    return TermNode(
        key=key,
        label=label,
        dimension=dimension,
        reference_state=reference_state,
        convention=convention,
        normalisation_basis=normalisation_basis,
        aliases=aliases,
        units_seen=_units_seen(rows),
        paper_ref_ids=paper_ref_ids,
        halves=halves,
        mention_count=len(rows),
        status="proposed",
        anchors=anchors,
        notes=notes,
    )


def _annotate_mismatches(nodes: Sequence[TermNode]) -> tuple[TermNode, ...]:
    """AC5: note every dimension collision sharing a node's key.

    A ``TOF`` node resolved as s⁻¹ and a ``TOF`` node resolved as µs must
    never merge; each gets a note naming the other. A ``TOF`` row with no
    unit resolves to neither and gets a note saying it escalates.
    """
    by_key: dict[str, list[TermNode]] = defaultdict(list)
    for node in nodes:
        by_key[node.key].append(node)

    out = []
    for node in nodes:
        siblings = [n for n in by_key[node.key] if n is not node]
        mismatched = [
            s
            for s in siblings
            if node.dimension is None
            or s.dimension is None
            or not node.dimension.comparable_with(s.dimension)
        ]
        if not mismatched:
            out.append(node)
            continue
        if node.dimension is None:
            new_notes = node.notes + (
                f"no unit resolved for '{node.key}' — escalates "
                f"(shares key with {len(mismatched)} resolved-dimension node(s))",
            )
        else:
            new_notes = node.notes + tuple(
                f"shares key '{node.key}' with a {_dimension_label(s.dimension)} node"
                for s in mismatched
            )
        out.append(replace(node, notes=new_notes))
    return tuple(out)


def _suggest_merges(nodes: Sequence[TermNode]) -> tuple[MergeSuggestion, ...]:
    dims_by_key: dict[str, list[DimensionSpec]] = defaultdict(list)
    for node in nodes:
        if node.dimension is not None:
            dims_by_key[node.key].append(node.dimension)

    suggestions = []
    for left, right in itertools.combinations(sorted(dims_by_key), 2):
        comparable = any(
            l.comparable_with(r) for l in dims_by_key[left] for r in dims_by_key[right]
        )
        if not comparable:
            continue
        similarity = _lexical_similarity(left, right)
        if similarity > _MERGE_SIMILARITY_THRESHOLD:
            a, b = sorted((left, right))
            suggestions.append(
                MergeSuggestion(
                    left=a,
                    right=b,
                    similarity=similarity,
                    reason=(
                        f"lexical similarity {similarity:.3f} (character ratio "
                        "weighted 4:1 over token-set overlap) with a "
                        "comparable dimension"
                    ),
                )
            )
    return tuple(sorted(suggestions, key=lambda s: (s.left, s.right)))


def normalise(
    terms: Sequence[DiscoveredTerm],
    registry: pint.UnitRegistry,
    config: CampaignConfig,
) -> tuple[tuple[TermNode, ...], tuple[MergeSuggestion, ...]]:
    """Group stage-2 rows into candidate nodes, with merge suggestions.

    **Dimension comes from the mention's observed ``raw_unit``, not from
    stage 2's ``dimension_text``.** ``resolve_dimension`` parses its argument
    with pint, and the model answers the dimension question in prose —
    ``"potential"``, ``"mass per time per area"``, ``"current per geometric
    electrode area"`` — none of which pint can parse. The first real discovery
    run (100 hubs, 2026-09-29) resolved **5 dimensions across 182 nodes** for
    exactly this reason, which left every node failing the promotion gate.
    The unit in the corpus text is both parseable and better evidence than a
    model's description of it, and `_units_seen` already reads it, so the node
    was carrying the right answer and grouping on the wrong one.
    ``dimension_text`` is kept on the row for audit — it is how that bug was
    diagnosed — and is deliberately not parsed here. A row whose mention has
    no unit resolves to ``None`` and escalates via ``_annotate_mismatches``,
    which is the honest outcome: the alternative is letting the model invent a
    dimension for a bare number.

    Grouping key is the full compare identity: ``(alias_key(measurand),
    dimension, reference_state, convention, normalisation_basis)`` —
    ``TermNode.identity()`` minus the alphabetical-vs-canonical distinction
    between ``key`` and ``alias_key``. The measurand key goes through the
    campaign's synonym families (:func:`fold_aliases`) and the three
    qualifiers through its vocabulary (:func:`canonical_qualifier`) first,
    so a spelling is never a split; what the vocabulary could not place is
    recorded on the node's ``notes``. The dimension mismatch gate (AC5)
    keeps differently-dimensioned rows apart even when they share an
    ``alias_key``; rows disagreeing on ``reference_state``, ``convention``
    or ``normalisation_basis`` are split rather than pooled for the same
    reason a potential vs RHE and a potential vs Ag/AgCl are not one
    measurand — a yield rate per geometric area and one per catalyst mass
    reduce to the same pint dimension but are two measurands
    (``norr-her-meta.md`` step 2; ``TermNode.identity()`` docstring).
    Output nodes are sorted by ``(key, dimension label, reference_state,
    convention, normalisation_basis, label)`` — the trailing fields are
    determinism tie-breakers beyond what the spec's ``(key, dimension
    string)`` pair alone provides for nodes that share both. Suggestions
    are sorted by ``(left, right)``.
    """
    GroupKey = tuple[str, DimensionSpec | None, str | None, str | None, str | None]
    groups: dict[GroupKey, list[DiscoveredTerm]] = defaultdict(list)
    group_notes: dict[GroupKey, Counter[str]] = defaultdict(Counter)
    vocabulary = config.qualifier_vocabulary
    for term in terms:
        # The OBSERVED unit, never the model's prose. See the docstring: the
        # first real run resolved 5 dimensions out of 182 nodes because this
        # passed `dimension_text`, which `resolve_dimension` then tried to
        # parse with pint.
        dimension = resolve_dimension(term.mention.raw_unit, registry, config)
        key = alias_key(term.measurand, config.measurand_aliases)
        reference_state, ref_note = canonical_qualifier(
            "reference_state", term.reference_state, vocabulary
        )
        convention, conv_note = canonical_qualifier(
            "convention", term.convention, vocabulary
        )
        normalisation_basis, basis_note = canonical_qualifier(
            "normalisation_basis", term.normalisation_basis, vocabulary
        )
        group_key: GroupKey = (
            key,
            dimension,
            reference_state,
            convention,
            normalisation_basis,
        )
        groups[group_key].append(term)
        for note in (ref_note, conv_note, basis_note):
            if note is not None:
                group_notes[group_key][note] += 1

    records = []
    for group_key, rows in groups.items():
        notes = tuple(
            f"{note} ({count} mention{'s' if count != 1 else ''})"
            for note, count in sorted(group_notes[group_key].items())
        )
        records.append((*group_key, _build_node(*group_key, rows, notes=notes)))
    records.sort(
        key=lambda r: (
            r[0],
            _dimension_label(r[1]),
            r[2] or "",
            r[3] or "",
            r[4] or "",
            r[5].label,
        )
    )
    nodes = _annotate_mismatches(tuple(r[5] for r in records))
    suggestions = _suggest_merges(nodes)
    return nodes, suggestions
