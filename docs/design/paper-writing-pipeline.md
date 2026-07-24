# The paper-writing pipeline

How precis writes and maintains a long document (a `draft`) by absorbing the
corpus into it. Extends **ADR 0060** (topic dossiers) — 0060 owns the classify +
track + digest spine; this doc specifies **claims**, **scaffold**, the
**section-batch integrate loop**, the **memoized review ledger**, and the
**view × model routing** that lets most of it run on a non-frontier model.

Core idea: **the pipeline is a set of content-keyed cache tables over the
corpus, each filled by a cheap background model, each invalidated only by a
change.** So maintenance cost is proportional to *change*, not to document
*size* — the property a perpetual living document needs.

Status: design, grounded against code. Decisions in §Decisions; residual
sub-questions at end. _Verified @ b3c2136f._

## Framing

A document is a `quest` (a perpetual striving — "keep the definitive living
review of X current", never `done`). The quest owns the document as its
**dossier** `draft` (`dossier-of`) and a WORM **logbook** (`quest_log`). The
coordinator loop, dossier link, typed logbook, and weekly recurring tick are
reused (`src/precis/quest/`, `precis-quest-help`). The paper-writing tick is a
**new tick body** with a **phase state machine** —
`vocabulary → scaffold → integrate → maintain` — because a tick that runs the
wrong phase for its inputs **dry-spins** (the catpath failure mode). A phase
with unmet inputs parks on a **`blocked-by` todo + `auto_check` leaf**
(`tag_present`/`discord_reply_received`) that auto-clears when the input lands
(`workers/auto_check.py` flip + `dispatch.py` re-admission) — **no new quest
state.** Caveat: evaluators read the DB, not the filesystem, so the vocabulary
gate watches a DB signal (`tag_present(topic:X)`), not "the YAML file exists."

Two regimes, one loop:

- **Make** — no structure; placement has no anchor.
- **Maintain** — solid structure + embedded chunks; search surfaces placement.

**Make is the degenerate case where the residual is 100% of the corpus** — the
scaffold's body sections come from clustering that residual, so one integrate
machinery serves both.

## The memoization spine

Every expensive judgment is cached against the `content_sha`/version that
produced it, and only invalidated when that content changes. Same principle as
`scripts/test --impacted` (testmon) or a build system.

| Memo | Keyed on | Invalidated by | Filled by |
|---|---|---|---|
| `topic:` classification | `TOPICCASCADE:<version>` marker | version bump | classify worker |
| paper claims | paper `content_sha` | re-ingest | background haiku |
| placement (paper→section) | paper + section shas | either changes | cheap model |
| review verdict (per checker) | chunk `content_sha` | any edit | the reviewer |

**Local vs global.** The cache is exact for **local** checks — a verdict keyed
on *one* chunk's sha (flow, citation-faithfulness, human approval). It cannot
express **global** validity — cross-section coherence depends on *many*
sections, so a single-sha watermark would **stale-pass**. That split *is* the
per-weave-vs-deep-tier reviewer split: local checks memoize per-chunk; global
coherence lives in the whole-doc deep tier and is recomputed, not cached.

## The relevance question — resolved

"Is this paper relevant to my document?" ≠ a per-draft binary classifier — that
re-creates the folksonomy drift ADR 0047 **measured** here (52% singleton OPEN
tags). Instead:

> **relevance-to-my-document ≡ carries my document's `topic:` tag.**

One topic = one quest = one dossier. The classifier is 0060's shared,
multi-label, closed-top-level `topic:` cascade (`workers/classify_topics.py`); a
paper spanning topics gets both tags. "Unintegrated for X" is a live query:
`search(tags=['topic:X'])` minus `integrated-into → dossier:X`.

## Claims — the composition unit

**A "claim" already exists: it is the `citation` kind.** `handlers/citation.py`
stores claim text + `source_handle` (a `pc<id>` chunk handle) + `source_quote` +
`verifier_confidence`/`caveats`, links `cites → paper`, and **embeds the claim**
(a `card_combined` vector via `upsert_card_combined`). What's missing is only the
**extractor**: today citations are minted client-side while authoring; nothing
extracts a paper's own assertions into rows.

The extractor:
- **Sparse, by signal** — runs only over `ROLE3:own` chunks (ADR 0047 already
  tags "the paper's own contribution"). A handful of claims/paper, high-value.
- **Demand-scoped** — only papers tagged `topic:X` earn claims (a paper's ~180
  per topic, not the 8000-paper corpus).
- **Background + memoized** — a worker pass shaped like `classify`, cheap model,
  cached per paper `content_sha`; reused across every weave/placement/coverage
  query.
- **Build path: v0 inline → v1 table.** v0 extracts claims *inline at weave
  time* for the batch being woven, no new infra — tests whether
  claim-composition beats abstract-composition before the table is built. v1
  promotes to the background pass.

What claims buy:
- **Weave composes pre-extracted claims**, not raw abstracts → the frontier
  model's job shrinks (the tier-drop lever).
- **Clustering** (cosine over claim vectors, reuse `toc_db.cluster_blocks`):
  near-identical claims across papers → **one woven point citing all N**
  (semantic dedup, mechanical); a disagreeing cluster → the **contradiction**
  signal.
- **A richer paper-eye** — fisheye a paper → its claims, not raw keyword TOC.

**You still cite the paper, not the claim.** The claim is the intermediate the
weave arranges; it *carries* its `pc` handle so the rendered sentence grounds on
the source. Export builds the bibliography from the `cites → paper` set (already
wired, below).

## Integrate — the tick body

**The unit is `(section, batch-of-placed-papers)`, not `(paper, section)`.**
Per-paper weaving produces accretive list-prose ("X does A [1]. Y does B [2].").
A section is (re)composed from all its placed claims at once.

Draft chunks are embedded (ADR 0033): **sections are centroids; place = nearest
section; residual = papers far from every centroid = missing centroids.**

1. **Place** — search dossier chunks with each unintegrated paper's gist → top-k
   sections above a floor (multi-place allowed). None clears → **residual**.
2. **Weave (section-batch)** — for each section with placed papers, hand the
   model the section at `fisheye+1hop` + its papers' **claims** → recompose
   (merge duplicates, one argument, transitions), mint `citation`s, link each
   paper `--<disposition>--> dossier`, log a `result`. Edits are
   **section-scoped `dc` chunk edits** — not the quest's whole-dossier rewrite
   (`quest/dossier.py` rewrites one chunk; a 40-section review can't be
   whole-rewritten per tick).
3. **Residual → section** — cluster deferred papers (gist embedding + KeyBERT
   labels, deterministic); the model judges a **digest** (label + 3–5 exemplar
   titles), not raw titles; skip clustering when residual <~15. Each surviving
   cluster → candidate section → `scaffold_sections` → re-enter Place.

**Batch = regime-scoped.** Make: the whole residual per section (one big weave).
Maintain: **the week's arrivals** (accumulate placed-but-unwoven papers, weave
each touched section once at the maintain tick) — or a manual
`precis quest weave <qid>` trigger. Never per-ingested-paper, or Maintain pays a
frontier weave + reviews per paper.

### The integration ledger (the reviewable list)

`integrated-into` is **not a new table** — the refs↔refs link set (many-to-many).
Disposition rides the edge as its relation (edge-scoped; optional section = the
chunk-level `element`).

| Relation | Meaning |
|---|---|
| `cited-in` | woven + a `citation` |
| `corroborates` | supports an existing point, grouped |
| `superseded-in` | subsumed by a later/review paper |
| `off-topic-for` | misclassified; also drop the `topic:` tag |

**Objective = disposition-to-zero, not cite-everything.** Weekly gap review =
`topic:X` minus `integrated-into`. `off-topic-for` volume is the classifier's
error signal. Surface: **`view='integration'`** (paper × section × disposition ×
date).

## Review — the memoized approval ledger

A per-**(chunk, checker)** watermark, keyed on the chunk's `content_sha` (same
shape as the classifier's existing `chunk_claims` lease):

```
chunk_review(chunk_id, checker, approved_sha, verdict, at)
   checker ∈ {human, cites, flow, structure, adversarial, …}
```

- **"requires review by C"** = `current content_sha ≠ the sha C last passed` — a
  derived query, not a loop. A weave bumps the sha → the chunk goes dirty for
  every checker; reviewers only run on dirty chunks (incremental, cheap).
- **The human is a checker** (`checker='human'`) — the approval you actually
  want. Export gates on "sections human-passed at current sha."

This one mechanism subsumes three failure modes:
- **Thrash** — convergence = a weave whose sha every checker passes without
  forcing an edit; a per-chunk `review_attempts` counter escalates to `human`
  after N rounds (soft gate).
- **Disposition gate** — prose always lands (sha updates); `cited-in` = "the
  `cites` checker passed at this sha." "Provisional section" = "has dirty
  checkers." No separate gate; the prose never stalls the tick.
- **Clobbering human edits** — a re-weave that touches a human-passed chunk
  marks it dirty-for-human → surfaces for re-approval (visible), never frozen.

**Diff-since-approval.** `chunk_events.prev_text` already retains old content per
edit (schema `0031`) — a reconstructable version chain. Net-new: record the
approved sha (the ledger) + a renderer walking the chain to show
approved→current (nothing reads `prev_text` today).

**Engine + personas exist — this is wiring.** A review-todo (`meta.review=<lens>`,
`meta.anchor='dc<id>'`) flips the planner into reviewer mode
(`workers/planner_prompt.py`), renders the section at `fisheye+1hop`, emits
anchored change-request todos that auto-close on resolution
(`auto_check_evaluators/all_child_findings_resolved.py`). Personas cover the
failure axes:

| Lens (skill) | Catches | Checker | Tier |
|---|---|---|---|
| `precis-review-paragraph-flow` | list-prose / accretion | `flow` | per-weave (local) |
| `precis-review-citation-faithfulness` | hallucinated cites | `cites` | per-weave (local) |
| `precis-review-section-structure` | intro↔body↔conclusion arc | `structure` | weekly |
| `precis-review-paper-help` | unsupported claims, counterargument | `adversarial` | deep (global) |

Net-new wiring: the trigger minting review-todos at each T; **whole-draft
orchestration + auto-aggregation** (`precis-polish-paper` aggregates manually —
the `review.py` `Reviewer` driver is the template); teach
`precis-review-paragraph-flow` to read `dc` chunks (it reads `tex` today).

## Freshness — what's new

No document-freshness view exists (draft views = outline/toc/wordcount/links/
backfill; the ADR 0051 decay ladder is context-side eye-TTL). Data is free
(chunk `created_at` + `ref_events` + edge timestamps). Add **`since=` /
`view='recent'`** (colors chunks + citations in the window) and a regenerated
dated **"What's new" appendix** inside the dossier; the same synthesis
broadcasts as the 0060 §5 digest cast.

## View × model routing

| Sub-task | Eye / context | Tier |
|---|---|---|
| Classify paper→topic | title + abstract (+ gist fallback) | **local** |
| Extract claims | `ROLE3:own` chunks | **local/haiku** |
| Place paper→section | paper gist + `view='toc'` + search top-k | **local** |
| Cluster residual / claims | embeddings + KeyBERT | **no model** |
| Weave (section-batch) | section `fisheye+1hop` + placed claims | **frontier** (measure down later) |
| Residual → section | cluster digest + outline | **frontier**, batched |
| Review (per-weave/weekly) | anchored section `fisheye+1hop` | **mid**, per persona |
| Deep review | whole-doc + `view='integration'` | **frontier**, rare |

The bet (ADR 0051: haiku edited fisheye-rendered sections at **0 tool uses**):
prep — fisheye, pre-extracted claims, pre-clustered residual — buys the
tier-drop. **Code does geometry (retrieve/cluster/dedup/diff); the model
composes and judges.**

## Failure modes (red-team)

*Guard* = a wired reviewer catches it; *Fix* = design change; *Solved* = a
mechanism above eliminates it.

1. **Per-paper weave → list-prose.** *Fix:* section-batch unit. *Guard:* `flow`.
2. **Whole-rewrite dossier won't scale.** *Fix:* section-scoped `dc` edits.
3. **Hallucinated citations.** *Guard:* `cites` per-weave (ledger-gated).
4. **Review↔weave thrash.** *Solved:* the ledger + `review_attempts` soft gate.
5. **Gate stalls the tick.** *Solved:* gate the disposition, not the prose.
6. **Maintain pays per-paper.** *Fix:* weekly-window batching.
7. **Cost blind.** *Mostly solved (corrected 2026-07-24):* the per-quest
   allocator breaker meters on **char-count**, not dollars (gr162594), so it is
   live regardless of null `cost_usd`; the global $20 dollar breaker
   deliberately excludes OAuth/subscription transports (`budget/meter.py`
   `OAUTH_TRANSPORTS`) because those draw down rate-limit quota, gated in
   `budget/quota.py`. So `quest_tick cost=null` is **cosmetic** (status shows
   $0), *not* an inert breaker — **not a weave pre-req.** *Optional:* a
   `cost_from_tokens` fallback in `router.result_from_claude_p` for a dollar
   estimate (needs token counts out of `claude -p`, absent today).
8. **Dry-spin at T0–T1.** *Fix:* phase machine + `auto_check` gate.
9. **Accretion without re-org.** *Fix:* split/prune in the deep tier + wire
   `contradicts`; executing a split is itself a place→weave on the subtree.
10. **Classifier under-covers abstract-less refs (patents).** *Fix:* gist fallback.
11. **Placement floor** — regime-dependent (looser in Make, tighter in Maintain).

Through-line: **retrieval/placement is sound; composition needs the section-batch
weave + section-scoped edits; the reviewers that police composition exist; the
ledger makes verification incremental.**

## Gap analysis

| Piece | Have | Need |
|---|---|---|
| Paper→topic classifier | `classify_topics.py` (dark) | enable + ingest-trigger + gist fallback + tier-2 |
| Claim primitive | `citation` (embedded, source-linked, confidence) | — |
| Claim extractor | `ROLE3:own` tags the signal | **background pass minting citations from `ROLE3:own`, demand-scoped** |
| `integrated-into` rels + `view='integration'` | link infra | relation vocab + the view |
| **Section-batch weave** | draft `dc` edits, fisheye, claims | **the core net-new loop** |
| Review ledger + diff renderer | `chunk_events.prev_text` (history), reviewer engine + 4 personas + `chunk_claims` pattern | **`chunk_review` table + approved-sha + diff renderer + trigger + auto-aggregate** |
| Scaffold-by-class | `_SCAFFOLDS`, `scaffold_sections` | MCP-expose + `book`/`summary` + corpus-shape via residual→section |
| Reference-list / BibTeX export | `export/latex.build_bib` + `\printbibliography`; docx References | works |
| Figures (author/paste) | draft figure chunks (blobs) + `third_party` reuse | works |
| Figures (lift a corpus paper's) | marker extracts at ingest | persist figure binaries (`db_writer` drops them) |
| `get(kind='draft', project=…)` | `draft-of` link | reverse lookup (`backlog_draft_by_project`) |
| Freshness `since=` + appendix | chunk timestamps, `ref_events` | the view + regenerated appendix |
| Phase gate (blocked-on-human) | `blocked-by` + `auto_check` | wire the gate (DB signal) |
| Dedup pre-placement | exact (`probe_existing`); trigram sweep | mostly subsumed by claim-clustering; opt. read-only `near_duplicates(paper)` |
| Cost attribution | char-based per-quest breaker (live, gr162594); $ breaker excludes OAuth by design | optional cosmetic $ estimate — **not** a weave pre-req |
| Digest cast + quiet lane | `briefing_cast.py` | 0060 §5 |
| Per-draft binary classifier | — | **do not build** — resolved as topic-tag |

## Topics to quest on

Multi-label. Per-domain separate; tooling topics cover **all** tooling (AI + non-AI).

| Topic slug | Scope | Note |
|---|---|---|
| `molelec` | single-molecule transistors, atomically-precise wires | 0060 seed |
| `healthspan` | life-span / anti-aging | 0060 seed (one big dossier) |
| `mof` | metal-organic frameworks | new |
| `nanobuds` | fullerene-nanotube hybrids, carbon nanostructure | new |
| `carbon-cad` | carbon-structure design/assembly tooling — CAD, AI, **+ script/pip interfaces for nanotube assembly** | new; tooling |
| `mof-tools` | MOF design tooling (AI + scripts) | new; tooling |
| `catalysis-tools` | catalyst design tooling (AI/ML + scripts) | new; ≠ the ammonia discovery quest |
| — | NO→NH₃ ammonia catalysis | already a catalyst-discovery quest (`catpath`) |

`llm-improvements` is also a 0060 seed.

## Document classes

Existing `_SCAFFOLDS`: paper, review(=survey), report, patent, manufacturing
(`article` has an empty brief; `proposal` is deliberately scaffold-less — the
linked CFP dictates its sections). Add **`book`** (multi-chapter) and
**`summary`/`brief`** (short digest, distinct from the comprehensive `review`).

## Build order

Each rung standalone-useful:

1. **classify_topics: enable + ingest-trigger + gist fallback + backfill** — live
   relevance lists on the corpus.
2. **`integrated-into` rels + `view='integration'` + minus-query** —
   "unintegrated" visible before any writing.
3. **`chunk_review` ledger + human checker + diff renderer** — the memoization
   spine; formalizes "does this chunk need review," standalone-useful for any
   existing draft.
4. **MCP-expose scaffold (+`book`/`summary`) + `draft(project=…)`.**
5. **Claims v0 (inline at weave)** → measure → **v1 background extractor.**
   v0 is inline *at weave*, so it lands **with** rung 6, not before it; the v1
   background table waits on the v0 measurement.
6. **Section-batch weave over `dc` edits + phase machine + per-weave reviewers
   (flow + cites, ledger-gated)** — the core; weave without its guards produces
   the garbage. (Cost-attribution is **not** a blocker — the per-quest breaker
   already meters on chars; see failure-mode 7.)
7. **Weekly + deep review wiring + auto-aggregation; weekly-window batching.**
8. **Freshness view + appendix + digest; contradiction + re-org.** Follow-ons:
   coverage matrix, figure-binary persistence, `near_duplicates`.

## Decisions (settled 2026-07-24)

1. **Objective** — disposition-to-zero; the list is `view='integration'`.
2. **Weave unit** — `(section, batch)`, section-scoped `dc` edits; batch is
   regime-scoped (Make = residual, Maintain = weekly window + manual trigger).
3. **Claims = `citation` + a demand-scoped background extractor over
   `ROLE3:own`**; build v0-inline before the v1 table.
4. **Review = a per-(chunk, checker) memoized ledger**; human is a checker;
   reuse the existing reviewer engine + 4 personas; per-weave gates the
   disposition, not the prose; soft-escalate after N attempts.
5. **Reviewers fire per-weave (local) / weekly / deep (global)** — mirrors the
   memoization local/global split.
6. **Tooling topics** — per-domain, AI + non-AI: `carbon-cad`, `mof-tools`,
   `catalysis-tools`.
7. **Weave tier** — frontier-first, measure down later.
8. **Doc classes** — add `book`, `summary`.

### Residual sub-questions

- Cluster algorithm/threshold for residual + claims (HDBSCAN vs agglomerative-k;
  reuse `toc_db.cluster_blocks` vs a paper/claim-level util).
- Whole-draft review orchestration: own driver vs the `review.py` `Reviewer`
  pattern.
- The DB signal marking the vocabulary/scaffold gate cleared (evaluators can't
  read files).
- Placement floor values per regime + `since=` window controls.
- Disposition relation names; section on the edge vs via the `citation`.
- Cost-attribution fix scope (`quest_tick cost=null`).
