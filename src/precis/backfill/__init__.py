"""``source-backfill`` — find corpus sources a draft *should* cite but doesn't,
and assemble the editing workspace to weave them in (design:
``docs/design/source-backfill.md``).

The **recall** mirror of the citation **verifier**: the verifier asks "is what
I cited true?" (precision); source-backfill asks "did I miss anything?"
(recall). The one distinction the whole flow turns on is **cited vs uncited** —
the uncited-but-relevant hits are the product.

Built (read-only workspace — FIND + WORKSPACE, no auto-weave):

- **Recall lenses**: the deterministic **text** lens (semantic+lexical sweep
  over the target chunk(s), :func:`candidates.find_candidates`) + the
  **citation** lens (provable-omission: held-but-uncited neighbours one S2
  citation hop from what we cite, materialised corpus-internally into
  ``links``, :mod:`~precis.backfill.citation_lens`).
- **Tier-0 dedup** against the draft-wide cited **and** dismissed sets
  (:func:`candidates.draft_cited_ref_ids` /
  :func:`dismissed.dismissed_ref_ids`). The cited set folds in every cited
  ``[fi]`` claim hub's evidence-supporter papers
  (``candidates._hub_supporter_ref_ids``, Build 2 §G1) — otherwise a paper
  already backing a cited hub would re-surface as a false "gap" once
  ``[pc]``/``[pa]`` cites backfill to ``[fi]``.
- **Workspace**: the eyes working set rendered through the ADR-0051 composer
  with folded-in ``★ cited`` / ``○ candidate`` source roles + a ✓/⚠
  grounding block (:func:`workspace.assemble` /
  :func:`workspace.render_backfill`). Section-scoped:
  ``get(kind='draft', id='dc<id>', view='backfill')``.
- **Whole-draft roll-up** (Build 2 §G2): ``view='backfill'`` on the draft
  slug runs the section-scoped sweep once per top-level section
  (:func:`workspace.assemble_draft`) and merges by source ref
  (:func:`candidates.merge_recurrence`) — a source recalled across multiple
  sections ranks first. Deliberately a slimmer aggregate render
  (:func:`workspace.render_backfill_draft`); full eyes/grounding detail
  stays per-section.
- **Topic precision gate** (Build 2 §G3): :func:`candidates.draft_topic_slugs`
  derives the draft's dominant ``topic:<slug>`` domain from its cited-paper
  closure; ``candidates._apply_topic_gate`` confirms on-domain hits and
  demotes (never drops) off-domain/untagged ones. Degrades to a no-op when
  no cited paper carries a ``topic:`` tag (``classify_topics`` is a
  dark/default-off pass, so coverage may be sparse-to-absent).
- **Heading intents + link roll-up**: durable per-heading intent notes
  (:mod:`~precis.backfill.heading_intent`) and coarse link aggregation
  (:mod:`~precis.backfill.link_rollup`) — see their module docstrings.

Not yet built: model-authored recall lenses (HyDE ``answers=``, the Tier-1
relevance cull) and the **integrate** coroutine that weaves accepted
candidates into the draft.
"""

from __future__ import annotations

from precis.backfill.candidates import (
    Candidate,
    draft_cited_ref_ids,
    draft_topic_slugs,
    find_candidates,
    merge_recurrence,
)
from precis.backfill.citation_lens import (
    find_citation_candidates,
    materialize_citation_edges,
)
from precis.backfill.dismissed import (
    dismiss_source,
    dismissed_ref_ids,
    resolve_source_ref_id,
)
from precis.backfill.heading_intent import (
    Intent,
    IntentContext,
    Rung,
    intents_for,
    intents_for_draft,
    prune_dangling,
    retire_intent,
    section_intents,
    set_intent,
)
from precis.backfill.link_rollup import (
    ChunkEdge,
    LinkRollup,
    NamedTarget,
    TailBucket,
    coarsest_visible_ancestor,
    rollup_edges,
)
from precis.backfill.provenance import SOURCE_KINDS, tier_for, tier_tag
from precis.backfill.workspace import (
    assemble,
    assemble_draft,
    recall_embedder,
    render_backfill,
    render_backfill_draft,
)

__all__ = [
    "SOURCE_KINDS",
    "Candidate",
    "ChunkEdge",
    "Intent",
    "IntentContext",
    "LinkRollup",
    "NamedTarget",
    "Rung",
    "TailBucket",
    "assemble",
    "assemble_draft",
    "coarsest_visible_ancestor",
    "dismiss_source",
    "dismissed_ref_ids",
    "draft_cited_ref_ids",
    "draft_topic_slugs",
    "find_candidates",
    "find_citation_candidates",
    "intents_for",
    "intents_for_draft",
    "materialize_citation_edges",
    "merge_recurrence",
    "prune_dangling",
    "recall_embedder",
    "render_backfill",
    "render_backfill_draft",
    "resolve_source_ref_id",
    "retire_intent",
    "rollup_edges",
    "section_intents",
    "set_intent",
    "tier_for",
    "tier_tag",
]
