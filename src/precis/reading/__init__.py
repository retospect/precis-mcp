"""Reading-prep loop — the adaptive concept-graph study system.

Design-of-record docs/backlog/reading-prep-loop.md (ships dark, in progress).
The spine is the ``concept`` graph: a node is a term with a continuous
mastery field + an embeddable ``card_combined`` definition (a concept *is a
vector*) and typed edges (``has-prerequisite``/``prerequisite-of``,
``analogy-of``, ``contrasts-with``, ``represents``).

Slice 1 (per-paper glossary) lives in `precis.workers.paper_glossary`; this
package holds the concept-graph layer:

- `concepts` — the node model (shared by handler + promotion, so manual and
  promoted nodes are identical).
- `promote` — glossary terms → concept nodes; corpus-wide name-anchored
  dedup via ``meta.norm_name``; cohorts in ``meta.cohorts``;
  ``derived-from`` → paper provenance.
- `term_quality` — the non-concept filter (the ``precis-cloze`` rule-0
  taxonomy), gating both the glossary build and the promotion chokepoint so
  topic-labels / stock phrases / front-matter never become concepts
  (gripe 186183).
- `briefing_cast` — lane-supplied finding handles stay in their discussing
  paragraphs, with chunk-level cites and rail-only reader display. Validate
  markers before storing prose: the Claims rail resolves text directly, so
  omitting an unsupplied handle's graph edge alone cannot prevent attribution.

Remaining: graph-edge inference, mastery-from-Anki, embedding routing,
booklet. Anki is a **renderer, not the brain** — the concept
graph is the source of truth; leaf cards sync down.
"""
