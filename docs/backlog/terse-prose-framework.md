---
status: idea
pillar: memory-graph
title: terse-prose writing framework — tag-driven lint, story graph, card
---

# Terse-prose writing framework (Reto, 2026-10-14)

Design state handed over by Reto 2026-10-14 ("file it to backlog please"); filed
in substance as given, not re-decided. A text tree (chapter > paragraph >
sentence) plus a separate story graph, linted only through NLP-derived tags.
Rules are scored lint, never generation constraints. The writer view and the
LLM drafting prompt are one object, the card.

## Relation to existing precis

Not yet mapped by Reto; the overlap found 2026-10-14:

- `draft` kind (`src/precis/draft/`, skill `precis-draft-help`): chunk tree
  with `[dc…]` links, outline/verbatim views, `view='hygiene'`, house-style
  hint on write. The text tree here is the candidate analogue of the draft
  chunk tree.
- [draft-hygiene-lints](draft-hygiene-lints.md): rule checks over a draft; the
  nearest existing lint surface. This design replaces text-reading lints with
  tag-reading ones.
- [authoring-tex-lint](authoring-tex-lint.md): TeX/math lint, orthogonal.
- [paper-writing-pipeline](paper-writing-pipeline.md): claims, scaffold,
  section-batch integrate loop, review ledger. Overlaps the SCI WRITING variant
  (claims with confidence, obligations ledger).
- [draft-linearization](draft-linearization.md): a draft as a render of a graph
  subgraph in reading order. Overlaps the story graph and compile step.
- `src/precis/draft/scaffolds.py` (genre briefs, section styles, skeleton per
  `doc_type`): the existing seed of Genres / templating.
- [draft-section-styles](draft-section-styles.md): per-section style, adjacent
  to the genre/voice split.
- `docs/conventions/llm-facing-prose.md`: repo-dev prose rules, a fixed
  hand-applied instance of what this makes a scored rule set.

## Design

### Core

- Text tree: chapter > paragraph > sentence (ltree). Each node: text, summary, status, mood tags.
- Window(node) = last 3 sibling sentences verbatim, last 3 paragraph summaries, next if present, ancestor summaries.
- Rules are scored lint, never generation constraints. Draft freely, lint after, pick best of 3 candidates, human reads aloud last. Stiltedness comes from writing to rules.
- Goals: short sentences (~12-15 words cap, `deliberate=` override), re-entrant prose (any paragraph re-anchors who/where/stakes in its first line; cold-open score), engaging but dense.

### Story graph (separate from the text tree)

- entity, beat (a unit of change), edge(kind: causes, reveals, seeds, echoes, inverts, contradicts, depends_on, motif_of), realises(text_node, graph_node, how).
- Gestalt foreshadowing = seeds/inverts/arc edges placed before any text. reader_knows(chapter, entity, fact) is a materialised view. feel(beat) = aesthetic target.
- Co-build loop: seed graph -> shape (order beats, place seeds/mirrors, cut chapters) -> write (text realises beats; unplanned content backfilled into graph as proposed edits).

### Tags are the only lint surface

- tag(target, key, value, source: declared|derived|inherited, confidence). NLP worker writes derived tags; lints never read text.
- tag_key DAG with specialises. rule = CSS-style selector over tags + check + tier(must|should|nice). Dismiss a finding by adding `deliberate=`, never by editing it.

### Strands / braid (2-3 stories in one stream)

- strand table; cross-strand edges rhymes_with, double_codes, crossing; braid pattern (e.g. AABACBA, crossing every N) checked by shape lint.
- Double-coded sentences rationed; per-strand refrains; triads as a form; register per strand; switch=hard|soft marking; per-strand cold-open lint; strand-owned vocabulary.

### Undertone (second channel, never surfaces)

- tone vs undertone tags; carries(text, undertone, via: image|verb|rhythm|omission|wrong detail); unsaid(beat, fact).
- Lints: never-named word family, carrier present, carrier variety, pressure curve rising to reveal, tone contrast, strand bleed.

### Compile / tractability

- graph -> partial order (acyclic). order -> braid sequence via CP-SAT or anneal, hard constraints only on structure, aesthetics soft, unsat core = "too many constraints" detector, warm start on recompile. sequence -> chapters by DP. node -> card -> text is local lint, always tractable.
- Rule budget on the card: top N ranked, rest summarised. New rules enter at `nice`, promoted when useful.

### Passes

- A pass = rule bundle + allowed ops + selector. Order is a policy by worst finding and tier weight, not a pipeline. Locks replaced by cost; only `deliberate=` is immovable. Human pulls passes from the card. Default: realise -> clarity -> cut -> undertone -> sound -> echo -> human read.

### Readability features (derived tags)

- Comprehension: dependency distance, nesting depth, LM surprisal mean and variance, referent distance, given->new, Zipf frequency, concreteness.
- Phonetic: g2p; near-repeat onset articulatory distance (small nonzero = tongue twister), consonant cluster load, stress clash, vowel monotony. Calibrate thresholds from read-aloud stumbles; register can loosen.

### Sci writing variant

- beats -> claims with confidence; entities -> quantities, methods, figures, acronyms; edges supports, assumes, measured_by, shown_in, cites.
- Obligations ledger: abstract <-> results, methods before use, every assumption gets a discussion line, every figure referenced.
- Strands: argument / procedure / evidence. Hedge-calibration lint (verbs vs declared confidence). Number consistency, claim-evidence proximity, causal-language audit, caption cold-open. One refrain: the finding sentence.

### Genres / templating

- genre DAG; skeleton slots; genre_rule overrides; genre_strand; obligation patterns; slot_card prompts. Instantiate stamps tree + tags. Learn a genre from 3 examples. A doc may mix a structure genre and a voice genre.

### Summarisability (third parties summarise the bare text)

- Skim-path lint: headers + first/last sentences alone must summarise the chapter. must_survive beats repeated in near-identical words; undertone never repeated in the same form. Strand-owned nouns at every switch. Word proportion per strand = intended summary weight. Test blind with 2-3 models; summary-tier beats must survive in all, undertone in none.

### Card (writer view and LLM drafting prompt are the same object)

- card(node) = window + due promises/threads + active constraints ranked + findings + forbidden words + tone/undertone/texture targets + one suggested shape.

### MCP verbs

- read, write (triggers recompute of node + neighbours), card, lint(scope), ledger, move/split/merge, summarise.

## Open questions

- Separate service, or a precis kind alongside `draft`?
- Mapping onto the draft tree: is the sentence-level ltree node a new chunk level under `draft`, or a parallel store linked to draft chunks?
- Tags: reuse the precis `tag` verb and `kind='tag'`, or a dedicated `tag(target, key, value, source, confidence)` table with the `tag_key` DAG?
- Story graph: reuse the mesh/memory edges, or keep it separate as the design states?

## Next step

Reto's default suggestion: schema + `window()` + `card()` SQL, then a dozen sentence-window lints.
