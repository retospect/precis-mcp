"""Reading-prep loop — the adaptive concept-graph study system.

Design-of-record me690334 (ships dark, in progress).
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
- `known` — records exactly which cards a login dismissed and the prior
  shared mastery fields. Undo restores those cards in the same transaction
  as the last dismissal's reversal of the shared mastery stamp.
- `flashcards` — mint and rework record the model's recoverability verdict.
  Missing verdicts are marked ``unchecked`` to preserve compatibility with
  older replies and distinguish them from explicit passes. Both prompts ask
  for the Back Extra symbol glossary (``_GLOSSARY_RULE``, ``precis-cloze``
  rule 7) and tell the model to repair a missing line before it replies; no
  code checks the reply for undefined symbols.
- `asked` — the ``twin:asked`` capture queue (what-is grammar, the ``/anki``
  textarea and the page-selection "Card this" chip, gr743234). Every ask
  files a ``meta.asked_submissions`` token — the highlight toast's Undo is
  bound to that one submission and its owner — and the forge's
  ``_cardless_concepts`` carries ``meta.asked_by`` onto the minted card's
  ``refs.owner_login`` so the card lands in the asker's review and sync.
- `term_quality` — the non-concept filter (the ``precis-cloze`` rule-0
  taxonomy), gating both the glossary build and the promotion chokepoint so
  topic-labels / stock phrases / front-matter never become concepts
  (gripe 186183).
- `briefing_cast` — lane-supplied finding handles stay in their discussing
  paragraphs, with chunk-level cites and rail-only reader display. Validate
  markers before storing prose: the Claims rail resolves text directly, so
  omitting an unsupplied handle's graph edge alone cannot prevent attribution.
- `asked` — page highlights queue against a concept owned by the requester.
  The forge reads the selected text from the owner-scoped queue table and
  rechecks its submission under the same row lock as Undo before publishing
  one card batch per capture, including for concepts with older cards.
  Undo removes a queue-created page edge once its last supporting submission
  is withdrawn. Shared concept and taxon matches provide graph references.

Remaining: graph-edge inference, mastery-from-Anki, embedding routing,
booklet. Anki is a **renderer, not the brain** — the concept
graph is the source of truth; leaf cards sync down.
"""
