# 0047 — Controlled chunk tagging: closed faceted vocabulary, one machine tagger, offline curation

- **Status**: accepted — **implemented 2026-07-04**.
- **As-built**: [`docs/design/chunk-classifier-cascade.md`](../design/chunk-classifier-cascade.md)
  (architecture) + [`scripts/classify/EVAL_RESULTS.md`](../../scripts/classify/EVAL_RESULTS.md)
  (measured numbers + model finding). Worker:
  `src/precis/workers/classify.py` (`run_classify_pass`), gated
  `PRECIS_CLASSIFY_ENABLED`; gold sets + eval harness in `scripts/classify/`.
- **Key change from this proposal.** The free local model cannot do the
  11-way `role:` *attribution test* (72% accept-aware — `related-work`
  recall 10/39). So the shipped writer is a **cascade**: a `junk` gate →
  the **3-way `ROLE3:` collapse** (own / background / furniture — 88%
  accept-aware, **91% own-claim precision**, the citation-grounding
  filter), with a stronger model reserved for the `own` residual and the
  full 11-way `role:` kept as an optional refinement. Human inter-annotator
  agreement is ~89%, so ~85–90% is the ceiling; the `accept:` sets + the
  query-time agent absorb the rest. `material`/`transport` ref axes pass on
  the free model; the ref-axis production runner is not yet built.
- **Extended by**: [ADR 0060](./0060-topic-dossiers.md) lifts this cascade
  pattern one level, from chunks to papers (`topic:` tags, multi-label).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0007 — derived queue, no block jobs](./0007-derived-queue-no-block-jobs.md)
    — a chunk's facet tags are one more *derived artifact* (like its
    embedding, summary, keywords): filled by an idempotent, versioned,
    content-addressed background pass; no todo, no job.
  - `migrations/0045_chunk_claims.sql` — the shared lease table this
    pass claims through, exactly as `llm_summarize` does.
  - `src/precis/data/axes/*.yaml` — the existing machine-readable
    paper-axis system (7 closed ref-level axes, versioned, gated,
    prompt-carrying). This ADR **extends that schema to chunk level**
    and adds the curation lifecycle it was missing; it does not invent
    a parallel taxonomy. NB: as of 2026-07-02 those 7 axes have
    **never been applied on prod** (0 of 9,374 papers carry
    `meta.processing`; no `domain:`/`studytype:`/`scale:` tags exist)
    — the external classifier script was designed but never run at
    scale, which is itself an argument for the worker-pass mechanism
    below.

## Context

We want dense, *controlled* metadata on chunks: the RAG literature is
consistent that metadata-annotated chunks (facets + entities alongside
the embedding) beat bare-text chunks on retrieval precision — but only
when the vocabulary is consistent. Folksonomy tagging is the opposite
regime, and our own corpus demonstrates both failure modes the
literature predicts:

- **Folksonomy drift is live in prod.** The `OPEN` tag namespace has
  2,742 distinct values; **1,430 (52%) are used exactly once**. Facet
  mixing is real: `interest:molecular-computing` (350 uses) and
  `topic:molecular-computing` (82 uses) coexist as distinct tags for
  the same concept.
- **Extracted keywords are recall material, not tags.** Top
  `chunks.keywords` values include `doi`, `http`, `refhub`, single
  letters, author surnames (`zhang`, `zhao`, `xu`), and uncollapsed
  morphology (`catalyst`/`catalysts`/`catalysis`,
  `simulation`/`simulations`). Embeddings collapse the synonyms, but
  inconsistent granularity and mixed facets pollute any *filtering*
  surface built on them. (Surface-form fragility in numbers:
  `monte carlo` has 9 keyword hits although GCMC is ubiquitous in the
  MOF-adsorption literature — the forms are `gcmc`, `grand
  canonical`, …)

**The retrieval intents** (drive the vocabulary — there are two, and
they pull in opposite directions):

1. **Citation-grounding**: write papers rapidly from primary sources.
   The queries that matter are *what did this paper actually do*,
   *how*, *why* — and, symmetrically, *excluding* the chunks that
   only recap other papers (lit-review) or are publisher furniture
   (copyright boilerplate). A `\citequote` must be grounded in a
   paper's **own contribution**, never in its summary of someone
   else's. This intent tunes for **precision**.
2. **Experiment-planning**: plan new experiments; continue where
   previous work left off. The material is scattered across
   rhetorical roles — explicit future-work, limitations, negative
   results (which classify as `result`), "beyond the scope" asides,
   and "no study has yet addressed X" chunks sitting in the
   *introduction* — so it gets its own cross-cutting binary axis
   (`open-question:`) rather than overloading `role:`. This intent
   tunes for **recall**: a false-positive lead costs seconds of
   reading, a false negative is an invisible lost thread. It also
   composes into the continuation workflow: paper X's
   `open-question:yes` chunks, each searched against `role:result`
   in papers newer than X = "has anyone closed this gap yet?" as a
   query.

What already exists (and shapes the design):

- `chunk_tags` (chunk_id, tag_id, set_by) + the `(namespace, value)`
  vocabulary table, with `v_chunk_tags_all` / `v_ref_tags_all` giving
  **inheritance both ways**. The chunk pipeline already writes one
  closed system axis at chunk level (`DENSITY:*`), and search verbs
  already filter chunk-level tags via
  `build_tag_filter(block_level=True)`.
- The `data/axes/` YAML schema: per-axis `id`, `version`, `question`,
  closed `values`, `default_unknown`, `applies_when` gate, `prereq`,
  `prompt` with per-value definitions.
- The `chunk_claims` lease + three-phase loop (`llm_summarize`) — the
  proven shape for a slow per-chunk LLM pass with no long transaction.
- **Every chunk already has a bge-m3 vector** (`chunk_embeddings`) and
  a one-sentence llm-v1 gist (`chunk_summaries`) — both are reused
  below (student classifier; compressed context).
- Corpus scale: **1.54M indexable chunks (1.46M paper, 8,673 papers
  with chunks, ~168 chunks/paper)** — full-corpus LLM passes are
  weeks-scale trickles on the shared local 80B.
- Tags carry **no definition anywhere in the DB** — semantics live
  only in code and skill docs.
- The `clusterize` SOM (`cluster_cells`/`cluster_assignments`) — a
  corpus-density map with c-TF-IDF tile labels, warm-started daily.

## Decision

### 1. Two channels, strict division of labour

- **Embedding** = the recall channel (unchanged).
- **Closed faceted tags** = the precision channel: small, curated,
  filterable axes written to `chunk_tags` / `ref_tags`.
- **Keywords** stay a display/clustering artifact and a *mining
  substrate* for vocabulary candidates. Never promoted to tags
  directly.

### 2. The vocabulary is a versioned artifact in git, not an emergent set

Every curated axis is one YAML file under `data/axes/` (same schema,
new fields `level`, `applies_to_kinds`, `select`, `aliases`,
`context`):

```yaml
id: role
level: chunk                 # ref | chunk
applies_to_kinds: [paper, cfp]
version: 1
question: "What rhetorical role does this chunk play in the document?"
select: one                  # one | multi(3)
values: [motivation, related-work, method, result, interpretation,
         limitation, future-work, data, boilerplate, unknown, n-a]
default_unknown: unknown
context: [section_path, position, title, ref_tags, neighbor_gists_1]
prompt: |
  ...one definition line per value, with include/exclude notes...
aliases:                     # entity axes: surface form → canonical
  dft-run: dft
  density functional theory: dft
```

Rules that keep it consistent and finite:

- **One axis = one question.** Values within an axis are mutually
  exclusive (`select: one`) or bounded (`select: multi(3)` for entity
  axes). The axis *questions* are the stable API (restructuring an
  axis after filters/docs reference its prefix is the one expensive
  operation); the *values* are the evolving vocabulary.
- **Every value has a definition** — one line plus include/exclude
  notes, living in the axis `prompt` (classifier and human read the
  same text). The tag handler loads the YAML at boot so
  `get(kind='tag', id='role:method')` surfaces the definition.
- **Hard finiteness cap = the prompt budget.** The classifier makes a
  forced choice among *all* values of an axis in one call (splitting
  a vocabulary across prompts breaks mutual exclusivity), so the
  whole axis — values *and* definitions — must fit one prompt:
  ≤ ~30 values/axis in practice, both for token cost (paid per chunk
  per call) and forced-choice accuracy. An axis that needs more is
  two facets wearing one name (split it) or wants hierarchy (a gated
  child axis via `applies_when`/`prereq` — two cheap ~10-value calls
  beat one bad 60-value call).
- **`unknown` and `n-a` are mandatory on every axis and mean
  different things.** `n-a` = the question doesn't apply (confident,
  terminal — a references-list chunk has no rhetorical role).
  `unknown` = the question applies but the text/model can't answer
  (epistemic; also the coercion target for out-of-vocabulary
  classifier output). They behave oppositely downstream: `n-a` is
  done; `unknown` feeds the miner (vocabulary-gap signal) and is the
  priority re-tag population on version bumps. **Neither is written
  to `chunk_tags`** — they live only in the envelope; informative
  values become tag rows.
- **`aliases:` kills synonym drift.** Surface forms map to one
  canonical value at mining and validation time; only canonical
  values reach `tags`.
- **The context packet is part of the axis contract** (`context:`
  field): changing what the classifier sees changes what the tags
  mean, so a packet change is a version bump like a vocabulary edit.

### 3. Level split and the axis roster

Ref-level facets stay ref-level — chunks inherit them through
`v_chunk_tags_all`; duplicating per chunk is pure waste. Chunk axes
exist only for what varies *within* a document.

**Ref-level (existing 7, to be applied for the first time):**
`domain`, `studytype`, `scale`, `dim`, `transport`, `material`,
`property`. The `material` vocabulary is visibly tuned to an older
carbon-nanostructure focus (no zeolite/SAC/perovskite) — it gets
*extended* by mining rather than duplicated by a new chunk axis
(the `interest:`/`topic:` lesson applied to ourselves: one facet,
one axis).

**Chunk-level:**

| axis | select | values / sketch |
|---|---|---|
| `role:` (v1) | one | motivation, related-work, method, result, interpretation, limitation, future-work, data, boilerplate, unknown, n-a |
| `open-question:` (v1) | one | yes, no, unknown, n-a — cross-cutting experiment-planning flag, recall-biased; rides `role:`'s combined call. Named to avoid colliding with the `role:future-work` *value* (same token as axis name and value = the facet confusion we're purging) |
| `method:` (v2) | multi(3) | ~20 mined: dft, md, gcmc, ml-potential, synthesis, xrd, xps, electron-microscopy, vibrational-spectroscopy, nmr, electrochemical, sorption-measurement, … |
| `move:` (v1.5, ref-level, dreams) | one | analogy, transfer, contradiction, extension, critique, other, unknown, n-a — the cognitive move a DREAM-tagged memory makes; vocabulary read off the live dream corpus (consistent template: isomorphism claim → variable mapping → testable implication). Dream bodies live in `refs.title`, so dream axes classify at REF level (envelope on `refs.meta->'tagging'`); needs a new `tags_any` gate predicate |
| `process:` (v3, gated) | multi(2) | ~12 if mining supports it: co2-reduction, nox-reduction, her, oer, water-splitting, ammonia-synthesis, photocatalysis, … — gated (`applies_when`) on the catalysis/DFT population |

`role:` is provenance-oriented by design: the load-bearing boundary
is **`result` vs `related-work`** ("what *they* did" vs "what they
say others did") — it gets the most careful include/exclude lines and
the most gold rows, because it is what protects the citation-verifier
workflow. `boilerplate` is an explicit value (not `n-a`): positively
detecting publisher furniture both excludes it from retrieval and
feeds ingest cleaning. Candidate later refinements follow corpus
density (see §7): a `subfield:` axis gated on
`domain in (chemistry, materials)` (catalysis / porous-materials /
computational-methods / 2d-materials / …) — the bio corner stays
coarse until it grows.

### 4. Exactly one writer: teacher–student, behind one worker pass

A new system-profile ref-pass `chunk_tag` (worker rotation, env-gated
like `llm_summarize`):

- **Claim** — `chunk_claims` lease, `artifact='chunk-tags'`,
  three-phase loop copied from `llm_summarize` (short claim txn →
  unlocked classification → short write txn; stale-lease reclaim;
  `attempts` cap with terminal marker).
- **Done-ness** — a versioned envelope at `chunks.meta->'tagging'`:
  per-axis `{value(s), confidence, classifier, version}` +
  `content_sha` + the gate inputs the axis was conditioned on (so a
  parent-axis re-tag cascades: child claim predicate re-fires when
  its recorded gate value no longer matches the parent envelope).
  Claim predicate mirrors `chunk_keywords`: envelope missing, axis
  version distinct, or `content_sha` distinct → re-claim. Bumping a
  version lazily re-tags; no backfill migration ever.
- **Context packet** (per `context:` field) — chunk text +
  `section_path` + relative position + ref title + inherited ref
  tags + **±1 neighbor *gists*** (the llm-v1 one-liners: ~90 extra
  tokens ≈ 20% overhead, vs ~5× for ±2 raw neighbor chunks). The
  gold-set ablation (§6) picks the cheapest packet that clears the
  gate. Multi-axis chunks get **one combined call** (chunk text paid
  once, one JSON object over all applicable axes) with per-axis
  envelope versions preserved. Prompts are assembled
  **cache-consciously**: llama.cpp reuses the KV cache of the longest
  common prefix, so the pass chooses which repetition to exploit by
  ordering — axis-major for a corpus sweep (system + vocabulary as
  the shared prefix, chunk as suffix) and chunk-major for gated
  multi-axis interrogation (system + context + chunk as prefix, each
  axis question as a rewound suffix — gated follow-ups like "ask
  `process:` only if `method:` said dft" are conditional questions
  against the same cached prefix).
- **Teacher–student economics.** The LLM (same litellm surface as
  `llm_summarize`; the task is forced-choice with definitions
  provided, so a 4–14B model likely clears the gate — the eval
  decides) is the **teacher and referee**: its prompt is the
  semantics; it labels the gold set and a 10–50k silver set via the
  normal trickle, and adjudicates the low-confidence band. The
  **workhorse is a distilled linear head over the existing bge-m3
  chunk vectors** (chunk-level `role:` is a classic sequential-
  sentence-classification task — PubMed-RCT/CSAbstruct lineage;
  supervision finds the faint rhetoric-correlated directions that
  topic-dominated variance hides from clustering): training is
  minutes of numpy, sweeping 1.4M chunks is a matrix multiply, and
  low-confidence chunks escalate back to the LLM (cascade). The
  cascade is **asymmetric for the planning-critical rare values**
  (`role:future-work`, `role:limitation`, `open-question:yes` — a
  few chunks per ~168-chunk paper, where the head is weakest):
  low-confidence rare-class candidates escalate preferentially, and
  their tag rows are written at a *lower* confidence threshold than
  precision-tuned values like `role:method`. A vocab
  bump re-labels the silver set through the new prompt, retrains the
  head, re-sweeps. The envelope's `classifier` field keeps
  provenance (`llm-v1` vs `head-v1`), like `keywords_meta.embedder`.
- **Output validation** — strict JSON; a value outside the vocabulary
  is coerced to `default_unknown`, **never inserted**. Values at/above
  the confidence threshold become `chunk_tags` rows
  (`set_by='system'`, canonical `<AXIS>:<value>` in the axis's own
  namespace); everything, including sub-threshold and
  `unknown`/`n-a`, lands in the envelope for audit.
- **Curated axes get their own DB namespaces, not OPEN.** A curated
  tag is stored as `tags(namespace=upper(axis id), value)` —
  `ROLE:result`, `SCALE:10nm`, `STUDYTYPE:computational` — the same
  grammar as the existing closed axes (`STATUS:done`), with the
  validation table loaded from the axis YAML at boot instead of the
  hardcoded `_CLOSED_VOCAB` dict (so a vocabulary edit is still a
  YAML PR, never a code change). Two reasons this beats riding OPEN:
  `parse_strict` already routes uppercase prefixes through closed
  validation (no `ROLE:banana`, from any actor — finiteness is
  structural, no third parse path needed), and it makes **OPEN
  nukeable**: the 2,742-value folksonomy can be culled or dropped
  wholesale without machine-curated tags standing in the blast
  radius. The ref-level paper axes adopt the same scheme in Round 0
  (they were speced as lowercase OPEN tags but never applied, so the
  move is free). CAVEAT before nuking OPEN: several OPEN prefixes
  are **written by live code paths** (`project:`, `tier:`,
  `source:`, `published:`, `alert-source:`, `agentlog-source:`,
  `user:` …) — inventory and migrate those to proper namespaces
  first; only the human/agent folksonomy dies.

### 5. Curation lifecycle: mining is online, minting is offline

The tagger can never create a value. The vocabulary changes only by
a PR — reviewed, versioned, evaluated:

1. **Candidate mining** (background, weekly cadence): three inputs —
   (a) per-axis `unknown` envelopes; (b) high-support keywords not
   covered by any value after alias normalization; (c) **the cluster
   map**: SOM tiles collapse surface forms for free (embedding-space
   grouping), and a tile arrives as a candidate with support (chunks
   assigned) and spread (distinct refs) pre-computed; new tiles in
   the warm-started daily rebuild are an emerging-topic feed. Tiles
   mix facets by construction (a "mof · adsorption · gcmc" tile fuses
   material + process + method — embeddings cluster by topical
   co-occurrence, not by axis), so **clusters propose, prompts
   dispose**: a human assigns the candidate to a facet and writes the
   definition; the tile label is descriptive statistics, the
   definition is a decision.
2. **Proposal, not action**: the miner files one `kind='gripe'` per
   candidate with support, spread, and sample chunks.
3. **Admission criteria**: support across **≥ 30 distinct refs**
   (paper-spread, ~0.3% of corpus — NOT raw chunk count: at ~168
   chunks/paper a chunk-count bar is satisfiable by a single long
   review); fits exactly one axis; definition writable in ≤ 2 lines
   with include/exclude notes; not an alias of an existing value
   (tag-embedding cosine ≥ ~0.85 → alias proposal instead); axis
   stays under prompt budget (the *real* finiteness mechanism — it
   forces candidates to be ranked against each other, the floor only
   keeps noise out of the ranking).
4. **Admission mechanics**: PR edits the YAML (value + definition +
   aliases), bumps `version` → lazy re-tag. Gold set gains ~10
   hand-adjudicated rows for the new value; eval ≥ gate blocks merge.
5. **Removal / merge**: near-zero-support values get deprecated at
   version review — dropped from `values`, kept in `aliases` pointing
   at a replacement (merge) or nothing (removal); cleanup deletes
   retired `chunk_tags` rows where `set_by='system'`.
6. **Drift monitoring** (nursery-style SQL → alerts): per-axis
   `unknown` rate trending up (vocabulary gap); sharp distribution
   shifts; ~zero-support values; envelope-version stragglers.

### 6. Gold set + eval

- **Sampling**: stratified on structure proxies (top-level
  `section_path` bucket × relative-position quintile × parent
  `studytype`/`domain` × **has-vs-lacks section_path** × length),
  topical spread via cluster tiles (the existing `sample-gold`
  convention, moved to chunk grain). **~250 chunks for `role:`**
  (~25/value; CI ±4–5% — 30 rows would give ±13%, too loose for an
  85% gate at chunk grain). Rare planning-critical classes
  (`future-work`, `limitation`, `open-question:yes`) are
  **deliberately oversampled** — random draws barely catch 1–3
  chunks/paper classes — seeded lexically ("future work", "remains
  to be", "further studies", "outlook") and positionally
  (conclusion-section chunks), with the seeds used only to surface
  *candidates* and adjudication done blind so the gold set doesn't
  just learn the seed phrases.
- **Protocol**: a strong model (different from the production tagger)
  pre-labels with value + rationale; the human adjudicates —
  corrections are ground truth. ~20s/chunk adjudicating vs cold
  labeling; in practice the human reviews the flagged contested band
  (~10–15%) plus a ~25-row random audit slice (~30 min total). The
  human is the referee, not the labeler — and irreplaceably owns the
  *definitions* (they encode retrieval intent) and disputed
  boundaries. LLM–LLM agreement overstates the ceiling (shared blind
  spots); the human-vs-model audit slice is the number that bounds
  honest classifier performance.
- **IAA slice**: ~50 rows double-labeled independently; kappa < ~0.8
  means the definitions are broken — fix include/exclude lines before
  blaming any model. Labeling sessions are definition-debugging
  sessions: every hesitation becomes a disambiguation line; gold and
  prompt co-evolve and freeze together at each version.
- **Addressing**: gold rows keyed by `(ref slug, ord, content_sha)` —
  survives re-ingest; sha mismatch flags the row stale instead of
  scoring against changed text. Rows carry a text snippet for
  readability. Stored in git next to the axes.
- **Eval harness**: runs the **exact production context packet**;
  reports overall accuracy vs the ≥85% gate, a **per-value confusion
  matrix** with a per-value recall floor (~60% default — 85% overall
  can hide a value that's never predicted — raised to **~80% for the
  recall-tuned planning values** `future-work`, `limitation`,
  `open-question:yes`, where a miss is the expensive error), and
  **confidence calibration**
  (the cascade threshold keys on it). Also runs the packet ablation
  (chunk-only → +structure → +neighbor gists) to ship the cheapest
  packet that clears the gate. Confusion patterns feed curation
  (persistent result↔interpretation confusion = sharpen or merge).
- **Growth by rule**: +10 rows per admitted value; adjudicated
  production disputes and miner hard cases accrete where the
  boundaries are.

### 7. Iterative rollout: axis resolution follows corpus density

Deployment is **rounds over weeks-to-months**, coarse → dense, each
round gated by the previous round's tags and informed by the miner +
cluster map. Adding an axis later is additive and isolated (new YAML
+ gold + eval + its own trickle; independent envelope versions); the
steady state *is* the loop, not a finished taxonomy.

- **Round 0 (days)** — the 7 existing **ref-level** axes on 9.4k
  papers (title+abstract; an afternoon of local-model compute).
  First real application ever; shakes down envelope / versioning /
  eval / write-boundary at 9k scale before anything touches 1.5M
  chunks. Immediate retrieval payoff: the axes compose
  multiplicatively (`studytype:experimental × scale:nano ×
  property:electrical` cuts 9k papers to a shortlist).
- **Round 1 (weeks)** — chunk `role:`: gold set → teacher trickle
  labels silver → distilled head sweeps the corpus → cascade
  escalation. The provenance filter (`result` vs `related-work` vs
  `boilerplate`) is the headline retrieval win.
- **Round 2** — `method:` gazetteer, mined from the now-tagged corpus
  + cluster tiles; alias/lexical matching covers the bulk, LLM breaks
  ties.
- **Round 3+ (months, repeating)** — gated refinements where the
  corpus is dense: `process:` on the catalysis/DFT population
  (`applies_when` on round-1/2 tags — e.g. co2-reduction vs
  nox-reduction vs her *only* on method:dft/electrocatalysis chunks:
  small population, small vocabulary, cheap round), `subfield:` under
  chemistry/materials, `material` vocabulary extension. Each round's
  population is a fraction of the corpus, its vocabulary is informed
  by real distributions, and its gold set is drawn from the gated
  population only. The cluster map is the density chart that says
  where the next round goes; the bio corner stays coarse until it
  earns refinement.

### 8. Discovery: definitions in the embedding

Today `tag_embeddings` embeds only the slug string; `search(kind=
'tag')` is hybrid lexical+semantic over that. For curated axes the
pass embeds **slug + definition line** (one `TAG_EMBEDDINGS_VERSION`
bump, lazy re-embed): "grand canonical simulations" then lands on
`method:gcmc` with zero surface overlap — the alias problem solved on
the discovery side too. Flow stays two-step: discover the tag
(`search(kind='tag')` / skill doc), then *filter* chunks with
`tags=[...]` — tags are a WHERE clause, not a similarity channel.
Query-time LLMs read the same definitions the tagger used (YAML
served as a skill + via the tag handler), closing the write/read
semantics loop.

### 9. Extension beyond papers

Nothing above is paper-specific: gripe bodies, memories, conv, news
are chunks, and `applies_to_kinds` is the gate. New kinds adopt by
adding an axis YAML — no code, no migration. The prod facet mess
(`interest:` vs `topic:`) is an early curation target: fold both into
one curated axis and let a version bump do the migration.

**Dreams are the first non-paper adopter** (round 1.5), on evidence
from reading the live corpus: dreams follow a consistent template —
an isomorphism claim across anchored sources, the variable mapping
(often noting the same variable wears different names in two
fields), and a testable implication. Three consequences: (a)
`open-question:` extends to `memory` — nearly every dream ends in an
untested lead, so `OPEN-QUESTION:yes` + the `DREAM:speculative`
opt-in is a queryable idea backlog (the fence stays orthogonal:
topical tags never un-fence speculative content); (b) a
dream-specific `move:` axis classifies the cognitive move (dreams
have no paper anatomy — `role:` is never stretched across corpus
roles); (c) the miner watches for **recurring dream-coined concepts**
("two-filter architecture", "kinetic-trap encoding") propagating by
handle citation across dreams — the system's emergent vocabulary is
a candidate source no keyword count or cluster tile surfaces.
Mechanics note: dream bodies live in `refs.title` (their only chunk
is the `card_combined` card), so memory-kind classification runs at
**ref level** with the envelope on `refs.meta->'tagging'`.

## Alternatives considered

- **Folksonomy / agents mint tags freely** — refuted by our own data
  (52% singletons, `interest:`/`topic:` split). Rejected.
- **Promote KeyBERT keywords to tags** — citation noise, surname
  noise, morphology drift; keywords stay the mining substrate.
  Rejected.
- **Embedding-only, no tags** — leaves no precision/filter channel;
  facet questions ("methods chunks only", "exclude lit-review") are
  exactly what vectors blur. Rejected.
- **Label the embedding clusters instead of classifying chunks** —
  works as a *prior* for topical facets, but clusters are
  facet-mixed composites, and intent-defined axes (`role:`) are
  near-orthogonal to embedding topology (topic dominates variance; a
  methods chunk and a results chunk about the same MOF are
  neighbors). Clustering answers "what varies most"; classification
  answers "what do we care about". Kept as mining input only.
- **Duplicate ref axes onto chunks** — redundant with inheritance;
  ~168× write volume for zero filter power. Rejected.
- **Curated axes as prefixed values inside OPEN** (the initial
  draft) — no schema friction, but it couples the curated tags to
  the folksonomy namespace we intend to cull, and needs a third
  validation path (prefix-gated OPEN). Superseded by per-axis
  uppercase namespaces with the closed-vocab table loaded from YAML
  — hard boundary enforcement *without* the code-change-per-edit
  cost that made the original `_CLOSED_VOCAB` route unattractive.
- **One umbrella `CLASSIFIER` namespace** (values like
  `role:result`) — also separates from OPEN, but double-prefixed
  slugs (`CLASSIFIER:role:result`), and per-axis metadata/sibling
  queries need LIKE-parsing inside values. Per-axis namespaces fit
  the existing `STATUS:done` grammar with zero new parsing.

## Consequences

- Retrieval gains a precision channel composing with existing search
  (`tags=` + `block_level` filtering already work end-to-end).
- The vocabulary is finite by construction (prompt-budget cap +
  write-boundary validation + offline minting) and every tag is
  defined where classifier, human, and discovery embedding all read
  the same text.
- New moving parts: one worker pass, one miner cadence, YAML loading
  in the tag handler/validator, drift monitors, a numpy training
  script. No new tables — envelope on `chunks.meta`, claims on
  `chunk_claims`, tags on `chunk_tags`.
- Costs: Round 0 is an afternoon; `role:` teacher trickle is
  weeks-scale but the distilled head collapses the sweep to hours;
  human bandwidth is bounded (adjudication ~30 min per axis version,
  proposals weekly).
