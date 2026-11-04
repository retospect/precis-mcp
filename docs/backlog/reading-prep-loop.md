---
status: idea
pillar: personal
---

# Reading-prep loop — an adaptive, activity-driven study system

> Design-of-record for the adaptive learning layer on top of the `anki`
> kind. Every slice ships dark (gated). Owner: Reto.

Shipped portion: see the `src/precis/reading/` module docstrings
(`concepts.py`, `promote.py`, `graph.py`, `mastery.py`, `cards.py`,
`term_quality.py`, `meditation.py`) and `workers/paper_glossary.py`;
full design conversation in git history. Live (dark-gated): the
per-paper inferred glossary (slice 1, `card_glossary` chunks), the
`concept` kind + corpus-wide name-anchored promotion dedup (slice 2),
LLM+embedding graph-edge inference (slice 3), the scalar mastery field
folded from `anki_stats` (slice 4, `PRECIS_MASTERY_THRESHOLD`), the
`card_forge` morning job (mint + diagnosis-ladder rework, observe-first
via `PRECIS_CARD_FORGE_AUTONOMY`), the non-concept term filter
(gripe 186183), and the nidra's mastered-drift meditation.

Core model (decided): objectives are **`concept` nodes** in a personal
knowledge graph (decision 7 superseded todo-reuse, 2026-07-14) —
embeddable `card_combined` definition (the concept is a vector in the
same manifold as the papers), continuous `meta.mastery` (state is a
derived view), `has-prerequisite`/`analogy-of`/`contrasts-with` edges,
`derived-from` provenance. **Anki is a renderer, not the brain** — a
thin sync adapter; scheduling intelligence migrates up into the graph.

The shared scalar below describes shipped code. The proposed learner model
in [quest-learning-plan.md](quest-learning-plan.md) adds owner-scoped temporal
evidence, concept/relationship estimates and uncertainty. Reto's clarification
makes that memory twin foundational for adaptive learning; it is not yet built.

## Open scope

### Quest guided daily study

[quest-learning-plan.md](quest-learning-plan.md) specifies the proposed
personal memory twin, importance-ordered continuing queue, private study
activity calendar, Users preferences, advance Anki preparation and calibration
of challenge. Thirty minutes is adjustable guidance rather than a fixed budget.
This extends routing and intake;
the concept-quality and corpus-wide enablement gates below still apply.

### Routing (revised slice 5)

- **Reading-readiness as a number** — a paper's glossary terms →
  nearest concept nodes → known / new / near-frontier breakdown +
  a distance ("you know 80% of this; 6 new concepts, 2 one hop from
  mastered").
- **Shortest-path curricula** — from the mastered set to a target
  concept over the prereq DAG (+ embedding to bridge missing edges).
- **Daily review walk** — a greedy walk over due concepts along
  edges/embedding proximity into a connected narrative path,
  different each day; structured interleaving with a tunable semantic
  step-size. Only a task that explains/uses a relationship contributes
  personal edge evidence; adjacent recall of A then B is sequence metadata,
  not demonstrated understanding of their connection. Constraint:
  maximize narrative coherence subject to spacing validity.

### Booklet (revised slice 6)

A traversal over the concept graph rendered to a `draft`:
reading-project strategic root + weekly draft synthesis +
shared-background rollup (`utils/section_keywords.py` c-TF-IDF) +
per-paper distinctive terms + optional perplexity enrichment for
high-value terms only. Cohort nomination is a query (recent papers +
`soon-reading` tag + dream `thread:` memories), with a `not-reading`
veto tag — the cohort must be surfaced for steering *before* it drives
expensive downstream work (decision 1: boost AND veto, dream
nominations veer strangely sometimes).

### Intake throttle (slice 3 of the build plan)

`workers/reading_release.py` — cheap SQL pass, daily-gated
(`app_state` marker): promotes `candidate → active` up to a per-day
cap, gated by topological readiness (an unlearned `prerequisite`
blocks release unless dive-in), ordered by PRIO + cohort relevance +
paper recency. No migration.

### Briefing + audio (revised slice 8; partial)

The morning reading brief and evening nidra already compose drafts for the
`cast_audio` narration/publication pass, served by the podcast routes.
Open: personal memory forecasts/queue context in the morning lanes and
owner-specific familiar-concept walks in nidra. See
[quest-learning-plan.md](quest-learning-plan.md) for integration and privacy:
existing feed authentication does not filter the shared episode directory
by owner. Personalized transcripts/audio require owner-scoped RSS and direct
media access before publication. Keep the current nidra profile free of
recall prompts. Podcasts are extra listening beside the configurable
focused-study session estimate, initially thirty minutes and adjustable.

### Concept quality — v2 extraction

The v1 glossary produced noisy nodes; Reto's criteria (2026-07-15):

1. **Select** author-defined terms only (Schwartz-Hearst + an LLM
   "what does THIS paper introduce/define?"), scaled to the paper —
   drop raw KeyBERT keywords as a candidate source (they are the
   noise).
2. **Calibrate** against the user's likely knowledge (existing/
   mastered concepts, own drafts, well-reviewed cards, corpus
   prevalence) — skip the likely-known.
3. **Enrich** each survivor: 1–2 local corpus searches + optional
   perplexity lookup → a grounded definition.
4. **Relate** — extract key relationships among survivors → graph
   edges (extraction and edge-inference unify).
5. **Write** clean nodes + edges + provenance.

Sequencing: write the `precis-concept-craft` skill first; build v2
(bump `GLOSSARY_VERSION`, lazy re-derive); re-validate on the same 5
papers side-by-side; only then enable corpus-wide; then the meditation
walks a clean cohort. Cost stays a bounded trickle via selectivity +
calibration.

### Later / deferred

- **Learned transfer across prerequisites** — deferred until personal
  observations support conditional changes between related concepts.
  Corpus prerequisite edges alone do not change a learner's memory estimate.
- **Richer memory estimators** — the shipped default is still the shared
  scalar. Owner-scoped append-only evidence, decay, uncertainty and distinct
  recall/application/relationship projections are now foundation scope in
  [quest-learning-plan.md](quest-learning-plan.md). Exact estimators and learned
  transfer between graph neighbors wait for calibrated personal observations;
  a prerequisite edge must never certify a person's mastery automatically.
- **Guid-preserving in-place card edit** — deferred; v1 = always
  `delete + put` (curve resets; decision 2). Add only if mature-card
  churn proves costly.
- **Retirement threshold** (decision 6, postponed): mastered = keep
  ~forever (exponential intervals make a forever-deck nearly free);
  retirement only ever prunes the unlearned backlog. The "how stale"
  trip-wire waits for real backlog growth.
- **Graph-native phone client** (much later) — reads the graph
  directly; nothing built for the graph is wasted when Anki is
  replaced.
- **Booklet read-receipts** — not in v1 (decision 4: Anki review is
  the hard engagement signal; the morning audio stream is the
  delivery channel).

## Decided constraints

- Not every glossary term becomes a card (decision 5): triage promotes
  the worthwhile; the rest stay booklet-only reference. Cluster to
  *encode* (booklet/graph), interleave to *review* (Anki default);
  watch confusable clusters for interference.
- Slice 4's re-munge is experimental: observe-first posture
  (report → act autonomy dial), thresholds tuned from real behavior.
- Corpus-wide enablement is gated on v2 node quality — do not turn the
  extraction pass on corpus-wide until v2 nodes read clean.
