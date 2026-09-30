"""Stage 4 — selection: the usage test that earns ``systematic``, and the list.

``taxonomy-bootstrap.md`` §Design, stage 4, and §Thresholds. Two rule-based
passes, both reading only what stage 3 (:mod:`~precis.taxonomy.normalise`)
produced plus a hub-count read the caller supplies:

* :func:`promote` runs the usage test over the *whole* node set (it needs to
  see key collisions across nodes, not just one node's own evidence) and
  returns nodes with ``status``/``notes`` updated.
* :func:`select_entries` turns ``systematic`` nodes that also clear the
  campaign's hub-count thresholds into frozen :class:`ListEntry` rows for
  ``list.vN.yaml``, with rejections carrying their reason.

**Promotion asserts usage, never truth.** ``systematic`` means "the corpus
uses this term systematically" — nothing here checks whether the term means
what a reader assumes, whether it is measured correctly, or whether two
papers using it agree. Validity, polysemy and consensus are ``finding``
annotations linked to the node through the existing verdict/dispute path
(``taxonomy-bootstrap.md`` §Explicitly NOT in scope). That is exactly why
promotion is safe to automate: it claims almost nothing, so a human only
ever needs to *veto* (demote), never approve each row.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.normalise import alias_key
from precis.taxonomy.types import (
    DimensionSpec,
    DiscoveredTerm,
    HubCount,
    ListEntry,
    TermNode,
    Thresholds,
)


def promote(nodes: Sequence[TermNode], thresholds: Thresholds) -> tuple[TermNode, ...]:
    """Run the usage test over the whole node set; return updated nodes.

    A node earns ``systematic`` only when all of:

    * ``len(paper_ref_ids) >= thresholds.min_papers``. ``paper_ref_ids`` holds
      source *papers*, not scanned row ids — `run.attribute_papers` fills it
      before this function ever sees the node, replacing whatever
      `normalise` put there. The distinction is the whole point of the test:
      on the norr-her-meta snapshot, 1664 hubs map to only 620 papers
      (median ~3 hubs per paper), so counting hub rows instead of papers
      would inflate independence roughly 2.7x and promote a term that in
      fact appears three times in a single paper;
    * both halves present, when ``thresholds.require_both_halves``;
    * it has a resolved dimension;
    * no sibling node shares its ``key`` with a different, non-comparable
      dimension, when ``thresholds.require_single_dimension`` — a
      reference-state (or convention, or normalisation-basis) split — same
      key, same dimension, different ``reference_state``/``convention``/
      ``normalisation_basis`` field — does not count as "different
      dimension" here, since the two nodes'
      :class:`~precis.taxonomy.types.DimensionSpec` values are themselves
      equal.

    Every failure appends a ``notes`` line stating the measured value
    against the threshold that rejected it — "why is X still proposed" is
    the question this pass exists to answer, so the answer has to be on the
    node.
    """
    dims_by_key: dict[str, set[DimensionSpec]] = defaultdict(set)
    for node in nodes:
        if node.dimension is not None:
            dims_by_key[node.key].add(node.dimension)

    out = []
    for node in nodes:
        reasons: list[str] = []
        paper_count = len(node.paper_ref_ids)
        if paper_count < thresholds.min_papers:
            reasons.append(f"{paper_count} paper(s) < required {thresholds.min_papers}")
        if thresholds.require_both_halves and node.halves != frozenset({"A", "B"}):
            reasons.append(
                f"halves {sorted(node.halves)} != both A and B (required_both_halves)"
            )
        if node.dimension is None:
            reasons.append("no dimension resolved")
        elif thresholds.require_single_dimension and len(dims_by_key[node.key]) > 1:
            reasons.append(
                f"key '{node.key}' spans {len(dims_by_key[node.key])} distinct "
                "dimensions, not single-dimension"
            )
        if reasons:
            out.append(
                replace(node, status="proposed", notes=node.notes + tuple(reasons))
            )
        else:
            out.append(replace(node, status="systematic"))
    return tuple(out)


def vocabulary_stability(
    terms: Sequence[DiscoveredTerm], config: CampaignConfig | None = None
) -> float:
    """Mention-weighted A/B vocabulary overlap (AC2's fixture check).

    Weighted Jaccard over ``alias_key`` mention counts per half: for each
    key, the shared weight is ``min(count_A, count_B)`` and the combined
    weight is ``max(count_A, count_B)``; the ratio of the sums is the
    overlap. Returns ``1.0`` for empty input rather than dividing by zero.
    With ``config`` the key is the campaign-folded one (synonym families
    collapsed, the same key stage 3 groups on), so the number reads the
    vocabulary the run actually produces; without it the key is purely
    lexical.

    A run below ``thresholds.min_stability`` means **fix the discovery
    prompt**, never lower the bar — the threshold exists precisely to catch
    a prompt drifting into inconsistent vocabulary before it reaches
    selection.
    """
    aliases = config.measurand_aliases if config is not None else None
    counts_a: Counter[str] = Counter()
    counts_b: Counter[str] = Counter()
    for term in terms:
        key = alias_key(term.measurand, aliases)
        if term.half == "A":
            counts_a[key] += 1
        else:
            counts_b[key] += 1
    keys = set(counts_a) | set(counts_b)
    if not keys:
        return 1.0
    intersection = sum(min(counts_a[k], counts_b[k]) for k in keys)
    union = sum(max(counts_a[k], counts_b[k]) for k in keys)
    if union == 0:
        return 1.0
    return intersection / union


def select_entries(
    nodes: Sequence[TermNode],
    hub_counts: Mapping[tuple[str, ...], HubCount],
    thresholds: Thresholds,
    config: CampaignConfig,
    *,
    join_sides: tuple[str, str] | None = None,
) -> tuple[tuple[ListEntry, ...], tuple[tuple[str, str], ...]]:
    """Turn ``systematic`` nodes clearing the hub thresholds into list entries.

    Only ``systematic`` nodes are considered; a ``proposed`` node is
    rejected with that reason without consulting hub counts at all. A
    surviving node then needs
    ``hub_counts[node.identity()].total >= thresholds.min_hubs`` and, when
    ``join_sides`` is given, ``by_side[side] >= min_join_side`` for *both*
    named sides. ``hub_counts`` is keyed by :meth:`TermNode.identity`, never
    by ``node.key`` — a bare key is shared by every reference-state or
    convention split (the ``TOF`` case), and keying by it would hand every
    sibling variant the union of the others' evidence, letting a `vs Ag/AgCl`
    potential's hubs count toward the `vs RHE` variant's threshold.
    Rejections come back as ``(key, reason)`` pairs so a list can always
    answer "why is X missing".

    ``canonical_unit`` comes straight from ``node.canonical_unit`` (the
    most-used raw unit the node saw, or ``None`` when no mention carried
    one); ``allowed_reference_states``/``normalisation_bases`` are one-tuples
    of the node's own ``reference_state``/``normalisation_basis`` (empty
    when unset — stage 3 already split any node that disagreed on either,
    so there is at most one of each per node). ``convert`` is always
    ``False`` — two normalisation bases stay two entries, never converted
    into one (``norr-her-meta.md`` step 2). ``contributing_hubs`` is the
    hub count's own ``ref_ids`` — AC4 requires every frozen entry to list
    the hubs that put it there.
    """
    entries = []
    rejected: list[tuple[str, str]] = []
    for node in sorted(nodes, key=lambda n: n.key):
        if node.status != "systematic":
            reason = "not systematic"
            if node.notes:
                reason += ": " + "; ".join(node.notes)
            rejected.append((node.key, reason))
            continue

        assert node.dimension is not None, (
            "a systematic node always has a resolved dimension (promote() requires it)"
        )

        count = hub_counts.get(node.identity())
        total = count.total if count is not None else 0
        if total < thresholds.min_hubs:
            rejected.append(
                (node.key, f"{total} hubs < required {thresholds.min_hubs}")
            )
            continue

        if join_sides is not None:
            failing = [
                side
                for side in join_sides
                if (count.by_side.get(side, 0) if count is not None else 0)
                < thresholds.min_join_side
            ]
            if failing:
                rejected.append(
                    (
                        node.key,
                        f"join side(s) {', '.join(failing)} below "
                        f"{thresholds.min_join_side} hubs",
                    )
                )
                continue

        entries.append(
            ListEntry(
                key=node.key,
                label=node.label,
                dimension=node.dimension,
                canonical_unit=node.canonical_unit,
                convention=node.convention,
                allowed_reference_states=(
                    (node.reference_state,) if node.reference_state is not None else ()
                ),
                normalisation_bases=(
                    (node.normalisation_basis,)
                    if node.normalisation_basis is not None
                    else ()
                ),
                required_conditions=config.required_conditions(node.key),
                convert=False,
                hub_count=total,
                paper_count=len(node.paper_ref_ids),
                contributing_hubs=count.ref_ids if count is not None else (),
                aliases=node.aliases,
            )
        )
    return tuple(entries), tuple(rejected)


def escape_rate(observed_keys: Sequence[str], entries: Sequence[ListEntry]) -> float:
    """Share of ``observed_keys`` matching no entry's key or alias.

    Forces list regeneration for the next round when it exceeds
    ``thresholds.max_escape_rate`` — never a hand patch to the list
    (``taxonomy-bootstrap.md`` §Thresholds).
    """
    if not observed_keys:
        return 0.0
    known: set[str] = set()
    for entry in entries:
        known.add(entry.key)
        known.update(entry.aliases)
    unmatched = sum(1 for key in observed_keys if key not in known)
    return unmatched / len(observed_keys)
