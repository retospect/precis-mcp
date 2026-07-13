"""``source-backfill`` — find corpus sources a draft *should* cite but doesn't,
and assemble the editing workspace to weave them in (design:
``docs/design/source-backfill.md``).

The **recall** mirror of the citation **verifier**: the verifier asks "is what
I cited true?" (precision); source-backfill asks "did I miss anything?"
(recall). The one distinction the whole flow turns on is **cited vs uncited** —
the uncited-but-relevant hits are the product.

Slices landed (read-only workspace): the deterministic **text lens** + the
**citation-graph lens** (provable-omission: held-but-uncited neighbours one S2
citation hop from what we cite, materialised corpus-internally into ``links``),
Tier-0 dedup against the draft's cited set, assembly of the eyes working set
rendered through the ADR-0051 composer with folded-in ``★ cited`` / ``○
candidate`` source roles + a ✓/⚠ grounding block. Still ahead: model-authored
lenses (HyDE, the Tier-1 relevance cull) and the **integrate** coroutine that
weaves accepted candidates into the draft.
"""

from __future__ import annotations

from precis.backfill.candidates import (
    Candidate,
    draft_cited_ref_ids,
    find_candidates,
    merge_recurrence,
)
from precis.backfill.citation_lens import (
    find_citation_candidates,
    materialize_citation_edges,
)
from precis.backfill.dismissed import dismiss_source, dismissed_ref_ids
from precis.backfill.provenance import SOURCE_KINDS, tier_for, tier_tag
from precis.backfill.workspace import assemble, recall_embedder, render_backfill

__all__ = [
    "SOURCE_KINDS",
    "Candidate",
    "assemble",
    "dismiss_source",
    "dismissed_ref_ids",
    "draft_cited_ref_ids",
    "find_candidates",
    "find_citation_candidates",
    "materialize_citation_edges",
    "merge_recurrence",
    "recall_embedder",
    "render_backfill",
    "tier_for",
    "tier_tag",
]
