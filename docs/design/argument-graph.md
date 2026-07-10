# Design — the argument graph (reasoning shadow beside a draft)

Slice of record for [ADR 0054](../decisions/0054-argument-graph-lemmas-inferences-reasoning-shadow.md).
This doc is the build plan + the **skill sketches**. The ADR owns the
*decisions*; this owns *what ships and in what order*.

## The shape, concretely

Author is writing draft `dc123`, cites `pc893` and `pc999`.

```
                 memory:kind:lemma  "pc893 (Nature, unretracted) claims X"
                   │  cites pc893          TRUST:high
                   │
   derived-from ───┤
                   │
                 memory:kind:lemma  "pc999 claims Y"
                     cites pc999           TRUST:medium

   memory:kind:inference  "from X ∧ Y, Z"
     meta.rule="and-intro"                  ← the two lemmas derived-into here
     meta.warrant="both hold under the same ambient, so Z is well-posed"
     │
     │ entails
     ▼
   memory:kind:lemma  "Z"    ← reusable; becomes a premise for the next inference

   dc123 (draft chunk)  ── see-also ──▶  the inference   (writer's aide, never \cited)
```

Reader-facing prose still cites the **primary sources** (`[pc893]`,
`[pc999]`). The inference is the shadow layer.

## Primitives used (no schema migration)

| Concept | Realised as |
|---|---|
| Empirical lemma (one claim, one source) | `finding` (chase + `verifier_confidence` for free) |
| Judgment / composite lemma | `memory` tagged `kind:lemma` |
| Inference step | `memory` tagged `kind:inference`, `meta.rule` + `meta.warrant` |
| Conclusion (reusable) | `memory` tagged `kind:lemma`, `entailed-by` the inference |
| Premise → inference | `derived-from` (reuse) |
| Inference → conclusion | **`entails`** (new relation) |
| Caveat / limitation (rebuttal) | `memory` tagged `kind:caveat` |
| Caveat → claim it bounds | **`qualifies`** (new relation) |
| Evidential agree/disagree | `supports` / `contradicts` (reuse) |
| Source trust | `TRUST:` closed tag axis (new) |
| Retraction trigger | `retracts` / `raises-concern-about` (reuse) |
| Free-form edge nuance | `meta.note` on a `related-to` edge (no new relation) |
| Draft → reasoning | `see-also`-class link from `dc…` to the inference |

**Relations stay closed** (behavioral contract; admission test = *does
code branch on it?*). Author-minted labels are rejected — open nuance
rides in edge `meta`. See ADR 0054 §2.1.

## Build order

1. **Relations `entails`/`entailed-by` + `qualifies`/`qualified-by`.**
   Add both mirrored pairs to the closed vocab (relation registry +
   `precis-relations` skill). Smallest, contract-level, unblocks
   everything. Auto-mirror like the other directed relations. **No open
   labels** (ADR 0054 §2.1).
2. **`TRUST:` closed tag axis.** Register the prefix + allowed values
   (`high|medium|low|retracted`; no-tag = implicitly unverified); wire
   per-kind gating so it is allowed on `memory` + `finding` only.
   Tag-verb prefix validation makes the set finite (ADR 0047 pattern).
3. **`meta.rule` + `meta.warrant` on inference memories.** No storage
   change — `memory.meta` already JSONB. Write path: accept them on
   `put`/`edit`; render them in the node view.
4. **`view='argument'` on `memory`.** Walk `derived-from` (in) +
   `entails` (out) from the node, render begat-style (model on
   `finding` detail render, `_render_bibliography`). **Two flag passes,
   both pure graph walks — no text reading:**
   - *stale-premise*: any premise whose cited paper has an inbound
     `retracts`/`raises-concern-about` edge, or is tagged
     `TRUST:retracted`.
   - *inherited-caveat*: any caveat reachable via a premise's
     `qualified-by` edge, listed as *"inherited — confirm still
     addressed."*
5. **Corpus report** — "arguments resting on retracted/concerned
   sources" (+ "… carrying unaddressed caveats"). A search-side view or
   a small CLI report. Exhaustive by construction (SQL walk).
6. **(phase 2)** Retraction-ripple worker: on a new retraction edge,
   walk `entailed-by` transitively, tag downstream
   `STALE:retracted-premise`. Plus an optional edge-scoped `addresses`
   relation for per-argument caveat discharge.

Steps 1–5 are the v1 slice; each is independently testable against
`PRECIS_TEST_PG_URL`. Step 6 is deferred (named in ADR §5/§7).

## What is code vs skill vs free (ADR 0054 §8)

The LLM reads and judges; the code remembers and routes. Nothing here
tries to read text smartly.

| Part | Where |
|---|---|
| When to make a lemma; phrasing; publication boundary; when *not* to | **skill** (`precis-argument-help`) |
| `kind:lemma` / `kind:inference` / `kind:caveat` sub-kinds | **free** (open tags — zero code) |
| Creating nodes, linking with existing relations, autolink | **free** (works today) |
| The 2 relation pairs + inverses | **code** (relation registry) |
| `TRUST:` closed axis + per-kind gating | **code** (tag validation) |
| `view='argument'` render + the two flag walks | **code** (no migration) |
| `meta.rule` / `meta.warrant` accepted on put/edit | **code** (schema-free) |

**The value-bearing 20% (propagation, ripple, the view) is exactly the
code part.** "Just skills" delivers the ~80% that already works and none
of what earns the feature.

## Skills we'll have

Three deliverables: one new skill, two edits to existing skills. Sketched
below at the fidelity they'd ship (house style: intent-first `##`
headings that mirror how an agent phrases the need).

### NEW — `precis-argument-help`

```
---
id: precis-argument-help
title: precis — build a defensible argument as a reusable lemma/inference graph
summary: the reasoning shadow beside a draft — state lemmas, chain inferences, keep it out of the published prose
applies-to: put / get / link / tag (kind='memory', kind='finding')
status: active
---
```

Sections:

- **What the argument graph is (and is not).** A shadow layer of small,
  individually-defensible steps beside a draft. NOT published, NOT a
  proof checker — you *assert* the logic, precis *records and audits* it.
- **State a lemma.** `put(kind='memory', text='pc893 (Nature,
  unretracted) claims X', tags=['kind:lemma','TRUST:high'],
  link='pc893', rel='cites')`. When it's one empirical claim from one
  source, prefer a `finding` (pointer to `precis-finding-help`).
- **Chain an inference.** Create the `kind:inference` node, `derived-from`
  each premise, set `meta.rule` + `meta.warrant`, then `entails` the
  conclusion. Worked `and-intro` example ending in a reusable `Z` lemma.
- **The operator vocabulary** (`meta.rule`): `modus-ponens`,
  `and-intro`, `or-elim`, `abduction`, `statistical`, `analogy`,
  `generalisation`. Free-text allowed; these are the scannable defaults.
- **Read the argument.** `get(kind='memory', id=<inference>,
  view='argument')` — the proof tree + stale-premise flags.
- **Recursion.** A conclusion lemma is a premise for the next step; how
  the graph deepens without new machinery.
- **The publication boundary.** Lemmas/inferences never `\cite`; the
  draft cites the *primary source*; link the draft chunk to the
  inference with `see-also` for the writer's-aide trail.
- **When NOT to use it.** First-time claims, opinions, rhetoric — those
  are prose, not lemmas. Over-producing lemmas is a smell (mirrors the
  finding spin-breaker guidance).
- **See also**: `precis-finding-help`, `precis-citation-help`,
  `precis-relations`, `precis-provenance-help` (retraction).

### EDIT — `precis-relations`

- Add the `entails` / `entailed-by` row to the relation table:
  *"A logically yields B (asserted, not proven). Inference node →
  conclusion lemma; premises attach with `derived-from`."*
- Add a worked block under a new heading **"Record a reasoning step
  (argument graph)"** showing `derived-from` premises + `entails`
  conclusion, cross-linking `precis-argument-help`.
- Note it auto-mirrors like the other directed relations.

### EDIT — `precis-memory-help`

- Document the `kind:lemma` / `kind:inference` sub-kinds and the
  `meta.rule` / `meta.warrant` fields.
- Document `view='argument'`.
- Point at `precis-argument-help` as the workflow skill.

Add a **Caveats** section to `precis-argument-help`:

- **State a caveat.** `put(kind='memory', text='validated only for n <
  100', tags=['kind:caveat'], link='<claim handle>', rel='qualifies')`.
- **What propagates.** `view='argument'` surfaces every inherited caveat
  on the conclusion; you confirm or neutralise it in the inference's
  `meta.warrant`. precis never auto-decides — it only refuses to let you
  forget.
- **Caveat vs scope.** Scope = the setup a claim *holds under*; caveat =
  where it *breaks / is unproven*. Different fields; don't conflate.

### EDIT — `precis-relations` (add row)

`qualifies` / `qualified-by`: *"A limits/caveats B. Caveat node → the
claim it bounds; surfaced (never auto-discharged) by `view='argument'`."*

### (phase 2) — `precis-argument-audit-help`

Deferred with step 6: how the retraction-ripple worker flags
`STALE:retracted-premise`, the edge-scoped `addresses` discharge, and how
to triage a tainted argument. Sketched only; not part of the v1 slice.

## Open questions

**Resolved in this round:**

- ~~Generic vs typed link labels?~~ **Closed vocabulary.** Relations are
  a behavioral contract; open labels re-import ADR 0047 drift and break
  auto-mirroring. Nuance rides in edge `meta` (ADR 0054 §2.1).
- ~~Do we need a caveat sidecar, or is it the tarpit?~~ **Add it** — the
  soft sibling of retraction. Not the tarpit *because it propagates by
  display, never by logic* (ADR 0054 §7).
- ~~Code, or just skills?~~ **Both, split by ADR 0054 §8.** The code does
  bookkeeping (traversal, persistence, guaranteed surfacing, exhaustive
  retrieval), not reading. "Just skills" is not this feature.
- ~~`TRUST:` values.~~ **4 explicit + implicit default:**
  `high|medium|low|retracted`; **no tag = unverified** (no fifth value).

**Still open (carry into review / build):**

- **`premise-of` refinement?** v1 uses `derived-from` for premise edges.
  A distinct load-bearing-premise relation is deferred, not rejected —
  revisit if the reified-conjunction node proves too coarse.
- **Edge-scoped `addresses` (phase 2).** A caveat discharged in one
  argument but not another needs an edge, not a global tag. Confirm the
  shape when step 6 is sliced.
- **Blackboard hook (0051).** Should a `contradicts` edge between two
  lemmas auto-surface on the convergence blackboard, or is that 0051's
  pull? Defer to 0051's slice.
