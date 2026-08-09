---
status: draft
title: Taproot — unify claim/fact nodes, resolve citations to a many-paper evidence graph with graded integrity
model: opus
---

# Taproot — claim/fact hub + citation resolution

**Taproot** is the evidence-grounded claim graph: it plumbs each claim
down its citation chain to the originating (seminal) source, grounds it
in the many papers that support it, and grades that support for integrity.
Named to pair with the existing `chase` verb — `chase` resolves one
citation edge; taproot is the graph it bottoms out into.

One spec, **phased build** (see [Build phasing](#build-phasing)) — the
design is one interlocking whole, so it lives in one document; only the
*delivery* is staged, gated on canonicalization. Today a "claim" is
spread across four half-overlapping representations and a citation points
at *one* chunk in *one* paper.
This unifies the claim into a single **hub node** that aggregates many
papers as typed, graded evidence edges, resolves citations (ours and
other papers') onto that hub, and derives which sources are *seminal*
from the citation graph we already hold. Written from a recon pass over
`handlers/{citation,finding,concept,provenance}.py`,
`workers/{chase,inbound_chase}.py`, `backfill/citation_lens.py`, ADRs
0047 / 0054, and the `{citation-chunk-grounding,argument-graph,
provenance-kind-plan}.md` (2026-07-28).

> **One spec, phased build.** The design is one interlocking whole (the
> shared claim model is the hard part; splitting the *document* would only
> invite drift), so it stays in one file. Delivery is staged — see
> [Build phasing](#build-phasing) — with **canonicalization as Phase 1,
> gating everything else.**

## Motivation / why

Two problems, one root cause.

1. **A claim has no home.** The same assertion — "Pd/C catalyzes Suzuki
   coupling at RT with mild base", "MOFs have tunable pore geometry" —
   is today recorded as a `citation` (claim→one quote, write-once), a
   `finding` (chased claim + chain to primary), or a `memory:kind:lemma`
   (argument-graph claim), depending on which code path minted it. None
   of them is *the* node that says "this fact is supported by these N
   papers, of which these 2 established it." So we cannot answer "who
   really showed this?" or "how many independent sources back it?" and we
   re-verify the same fact once per citing draft.

2. **Citations resolve to a chunk, not a claim.** When we cite, we point
   `\cite{}` at a paper (or, post-`885bd1ea`, a chunk). When we *read* a
   paper that says "MOFs are tunable [12]", `[12]` is a bib string until
   `chase` resolves it — and even then it resolves to a *chunk*, not to a
   shared claim other papers also support. So the corpus never compounds:
   fifty papers asserting the same fact stay fifty disconnected edges.

The substrate to fix both already largely exists (see
[Reuse map](#reuse-map--what-already-exists)); what's missing is a
*single claim node* and a *standing resolution pass* that ties the
`citation`/`finding` records to the `chase` verdict machinery and the
`provenance` integrity columns. This spec defines that node and the
resolution around it.

## The core model

**The claim is the hub. Reuse `finding` as the hub node — do not mint a
new `claim` kind.** Precedent: ADR 0054 built the entire argument graph
out of `memory` sub-kinds rather than a new kind; `finding` already
carries the claim text, `meta.scope` (the bound conditions), a
`derived-from` chain, and a `STATUS:tracing→established` lifecycle.
Promote it from "chase endpoint" to "the canonical claim," and hang a
**many-to-many, typed, graded evidence relation** off it:

### `finding` vs `memory:kind:lemma` — already unified, nothing retired

The code already treats these as **two flavors of one concept**
(`store/_argument_ops.py`: "`finding` — the grounded lemma, chase's chain
head"; `_is_premise()` is true for *either* `finding` or `kind:lemma`):

- **`finding` = a *grounded* lemma** — a claim standing on **evidence
  edges to papers**. ← this is the claim hub. Axis-A/B evidence edges
  attach **only here.**
- **`memory:kind:lemma` = a *derived* lemma** — a claim standing on
  `derived-from`/`entails` from other premises, no direct paper evidence.

They are the **empirical vs. inferred** split the argument graph was built
around; they do not compete. This spec extends `finding`'s *evidence*
side; ADR 0054 keeps the *inference* side. Both feed the one retraction
ripple. Rule: **evidence edges → `TAPROOT:claim` findings only; inference
edges → either.** (Only *grounded world-claim* findings are hubs — see
open #11 for the `TAPROOT:claim`/`TAPROOT:review` discriminator.)
(Resolves former incoherence #3.)

**Citation target (former incoherence #4).** `finding`'s contract
("never appears in `\cite{}`") is *kept*: you **cite the claim; the
exporter expands it to the `establishes` papers.** `\cite{fi123}` is
authoring sugar rendered at LaTeX/docx export as the originator paper(s)
in the bibliography. The finding never lands in the `.bib` — it is the
authoring target that expands to real paper cites. Cite the fact; print
the sources.

```
finding  "Pd/C catalyzes Suzuki coupling at RT with mild base"   ← hub (embedded, searchable)
   ├─ establishes  → paper A @chunk   support=yes   integrity=clean       (originator / seminal)
   ├─ establishes  → paper B @chunk   support=yes   integrity=clean       (originator / seminal)
   ├─ corroborates → paper C @chunk   support=yes   integrity=clean       (C cites A)
   ├─ corroborates → paper D @chunk   support=partial caveats=[aqueous only]
   └─ contradicts  → paper E @chunk   support=no    integrity=clean       (scope limit)
```

Each edge carries the verdict `chase` already emits (`supports:
yes/partial/no`, `support_reason`, `caveats[]`, `char_offset`) **plus** an
integrity status pulled from the target's `refs.retraction_*` columns.
`citation` does **not** go away — it becomes the *per-edge verified
record* (the quote-level artifact at `source_handle`); `finding` is the
*node those edges converge on*. `edge role` (`establishes` /
`corroborates` / `contradicts`) is **derived**, not hand-set (see
[Seniority](#seniority-is-derived-not-stored)).

### Two axes, not one failure list

"Is this a good citation" has orthogonal axes; the design must keep them
separate or it will conflate "the paper doesn't say this" with "the paper
was retracted":

| Axis | Question | Status |
|---|---|---|
| **A · Support** | Does the source actually assert the claim? | new pipeline, `chase` emits most verdicts |
| **B · Integrity** | Retracted / corrected / concerned — *and relevant to this claim*? | columns + `provenance` kind exist; **reason-relevance judgment missing** |
| **C · Currency** | Superseded / contradicted by later, stronger work? | lightly covered by `contradicts` edges + argument caveats; **not a focus here** |
| **D · Reliability prior** | Venue, preprint, n-of-3 | **out of scope by prior decision** (ADR 0054 rejected trust-derivation) |

## The crux — canonicalization = flat claim dedup

The gating problem (former open-question #1). **v1 does the simplest thing
that works: two claims are either the *same* (merge into one hub) or *not*
(separate hubs) — plus a `contradicts` flag when they assert opposites.**
No claim hierarchy. "MOFs are tunable" and "MOF-5 pore size scales with
linker length" are *both* valid claims; if they aren't the *same* claim
they simply become **separate hubs**. That's *under-merging*, the **safe**
direction — a spurious separate hub is harmless; a wrong *merge* lets a
retraction ripple across fused-but-distinct claims. Broader/narrower
subsumption between claims — the "lattice" — is a **navigation nicety
deferred to v2** (see [Non-goals](#non-goals--staying-out-of-the-formal-logic-pit)):
it is *not* needed for grounding claims or finding senior papers, which was
the actual goal.

A cascade mirroring the ADR-0047 classifier (cheap-and-wide → escalate
the hard bit):

1. **Canonical form.** Normalize the claim to a **claim sentence + a light
   scope note** (material / method / quantity / regime) — enough to tell
   claims apart, *not* a predicate-logic parse. (Splitting a
   multi-assertion chunk `X∧Y` into atoms is **deferred**: without it, a
   bundled `X∧Y` just fails to merge with a bare `X` — an under-merge,
   which the metric tolerates. Add it later only if under-merge hurts.)
2. **Block — no model.** Embed the claim; ANN-retrieve the top-K nearest
   existing hubs (reuse the `finding` card embeddings already indexed).
   Zero near neighbours ⇒ new hub, free.
3. **Dedup-judge — MEDIUM.** For each of the K, one bounded call returns
   one of **three** verdicts: `same` (→ merge into that hub) · `contradicts`
   (→ separate hub + a contradiction link) · `different` (→ separate hub).
   That is the whole judgment.
4. **Escalate — BIG/FRONTIER, rare.** Only when (3) proposes a `same`
   (merge) it isn't sure of — the single risky direction. The strong model
   confirms merge or keeps separate.

**Merge rule (the over-merge guard).** Return `same` **only** when the two
state the *same fact under the same conditions* (same material/method/
regime, differing only in wording). **Any** real difference ⇒ separate.
**Bias hard toward separate** — under-merge is recoverable (merge later);
over-merge is dangerous (a retraction ripples across fused-but-distinct
claims). Uncertain ⇒ separate.

**Eval fixture.** Primary metric = **over-merge rate → ~zero** (merges that
should have been separate), tolerating under-merge. Phase 1's `ready` bar.
The fixture carries richer 5-relation labels; v1 grades them **collapsed** —
`equivalent`→`same`, `broader`/`narrower`/`orthogonal`→`different`,
`contradicts`→`contradicts` — so the same fixture also serves a future v2
that restores the hierarchy.

> **v1 complete (2026-07-28)** — `tests/fixtures/taproot/` holds **238**
> pairs: 200 nearest-neighbour `citation`-claim pairs dual-labeled by
> Opus + Fable (blind), **92% inter-model agreement**, the 16
> disagreements adjudicated by three cluster rules **and human-signed-off**
> (`human_approved`); plus 8 corpus + 22 synthetic contradiction pairs
> (pairs 201–238) covering the `contradicts` edge. Two findings surfaced:
> multi-assertion chunks exist (motivated atomic-split, now **deferred** to
> keep v1 simple — see step 1) and the **`finding`-pollution** issue (open
> #11). v1 grades the fixture collapsed to `same`/`different`/`contradicts`
> (see the canonicalization section). The method — NN pairs + two blind
> labelers + human adjudication of only the disagreements, then targeted
> contradiction augmentation — is the repeatable recipe for growing it.

## Non-goals — staying out of the formal-logic pit

A claim graph is the trailhead to an open-ended formal-KR project. These
are the hard limits that keep "well enough" from sliding into a bottomless
description-logic build. Empirically the pragmatic path suffices: two
models hit **92% agreement with zero logic** — bounded pairwise judgment,
not inference. v1 stays deliberately flat:

1. **Deferred: broader/narrower subsumption between claims** (the
   "lattice"/DAG). v1 is flat — claims merge or stay separate. The
   hierarchy is a v2 navigation nicety, not core; without it you get more
   separate hubs (safe under-merge), never a wrong merge.
2. **Three-verdict judge** (`same` / `different` / `contradicts`) — no
   arbitrary predicates, no quantifiers, judged pairwise and graded
   against the fixture.
3. **Pairwise judgment, never a reasoner** — we never *compute* entailment
   or run a solver. No inference engine, no consistency checking. (With no
   hierarchy in v1, "transitive closure" isn't even a temptation.)
4. **Deferred: atomic-split** of multi-assertion chunks. If added later it
   is **one level** only (split `X∧Y` → `X`, `Y`, stop) — never recursive
   decomposition into logical constituents.
5. **Scope is a light note**, not an ontology.
6. **"Well enough" = the fixture metric, not a correctness proof** — once
   over-merge ~0 on the fixture, ship. The metric caps the ambition.

**Decision rule for any future feature:** if it needs quantifiers, an
entailment solver, consistency-enforcement, a maintained hierarchy, or
recursive decomposition — that is the pit; say no.

## Axis A — the support-resolution pipeline

Resolving one `(claim C, cited-source P)` pair runs four stages; each
stage has exactly one failure exit. This closed set *is* the edge-status
vocabulary — "review a paper" = walk its citations through this pipeline
and record the terminal state per edge.

0. **Identity** (silent, pre-stage) — is C a claim we already have?
   ([canonicalization](#the-crux--canonicalization--flat-claim-dedup), the
   gating risk — a wrong merge poisons everything downstream). The pipeline
   below *assumes* C is resolved to its hub.
1. **Acquire P** → fail **`UNACQUIRABLE`**: not held, no OA, no PDF.
   (`chase` stub-without-blocking already handles the mechanics.)
2. **Locate the passage in P** → fail **`UNLOCATED`**: whole-paper cite
   or too vague to pin to a chunk. (Draft-side warning already exists,
   `220bb674`.)
3. **Attribute** — does P assert C *as its own* (`ROLE3:own`) or repeat
   it from elsewhere (`ROLE3:background`, carrying its own `[k]`)? →
   **`NOT-ORIGINATOR`** (this is `chase`'s `cited_others[]`). **Not a
   failure** — it is the normal case and the seniority engine
   ([below](#seniority-is-derived-not-stored)).
4. **Support** — does the located passage back C? →
   **`SUPPORTS`** (edge `establishes`/`corroborates`) /
   **`PARTIAL`** (+ caveats) / **`NO-SUPPORT`** / **`CONTRADICTS`**.
   (`chase`'s `supports: yes/partial/no`.)

**A citation is an edge with two ends — either can be missing.** The
stages above catch the **source end** failing (`UNACQUIRABLE`,
`UNLOCATED`). The **claim end** can fail too: a paragraph that is *pure
pointer* — it cites a source but asserts nothing to ground ("See [12]", a
Related-Work sentence that is only "[1,2,3]"). Detected at **claim
extraction** (stage 1): if extraction yields **no** groundable claim (or
only a non-substantive meta-claim like "several studies exist"), the
citation has no host claim →

0'. **`NO-CLAIM`** (claim-end missing) — a dangling/orphan citation. Not a
   support failure; there is nothing to support. In our draft this is a
   hygiene defect (a citation must attach to an assertion); in an external
   paper it is a **pure navigation node** in the citation graph, never
   treated as evidence for anything.

Closed outcome enum: `NO-CLAIM · UNACQUIRABLE · UNLOCATED · NOT-ORIGINATOR
· PARTIAL · NO-SUPPORT · CONTRADICTS · SUPPORTS`.

## Axis B — integrity (retraction / correction), reason-relevant

Retraction is **orthogonal** to support: a paper can `SUPPORTS` a claim
*and* be retracted. So integrity is a parallel status on the edge, not a
pipeline exit. Machinery mostly exists — the **gap is relevance
judgment**.

**What exists:** `refs.retraction_status` (`retracted` / `corrected` /
`expression_of_concern`) + `retraction_reason` / `_url` / `_checked_at`
(`migrations/0001_initial.sql`); the `provenance` kind
(`handlers/provenance.py`) — a stateless Crossref checker that
write-through-ingests notices, attaches `retracts`/`corrected-by` links,
sets the status column; and the ADR-0054 ripple hook
(`store/_argument_ops.py`) that stamps `STALE:retracted-premise` on
dependents.

**The gap (your two-kinds distinction).** The ripple is **paper-level
and reason-blind**: any `retracts` edge marks dependents stale
regardless of *why*. A corrigendum fixing an author affiliation would
invalidate a claim exactly like a fabricated-data retraction. Needed: a
per-edge **integrity outcome**, judged from the retraction *reason*
against the *specific* claim:

- `CLEAN` — no notice.
- `RETRACTED-INVALIDATING` — reason undermines the result (fraud,
  unreproducible data, error in the finding). Poison the edge; if the
  edge is `establishes`, escalate to claim jeopardy.
- `RETRACTED-UNRELATED` — reason orthogonal (affiliation, authorship
  dispute, funding disclosure, text-plagiarism, image-dup in an
  *unrelated* figure). Administrative flag; **support stands.**
- `CORRECTED` — corrigendum/erratum → sub-judge: did the correction
  change the claimed value, or was it cosmetic?
- `CONCERN` — expression of concern, unresolved → soft flag, uncertain.

This needs `provenance` **Phase 3 (Retraction Watch reason codes)** —
today explicitly out of scope in `provenance-kind-plan.md` — as the
structured-reason input, plus a per-edge LLM adjudication of the same
shape as the support check.

## Recursion — how deep, and where it stops

Two *different* recursions with opposite answers:

**Vertical — chase to origin (in scope, bounded).** On `NOT-ORIGINATOR`,
hop P→Q→R *for the same claim* until the **originator** (asserts C as
`own`, cites no one further for it). Terminus is **data-defined** (the
originator), not a fixed depth; chains bottom out in ~1–3 hops.
`finding.meta.chain` already walks this.

**Horizontal — re-derive the origin's own argument (out of scope by
default).** The originator's result rests on *its* premises/methods.
Recursing here is unbounded ("verify the whole edifice") and is the
**argument-graph's** job (ADR 0054), opt-in per claim. Default: do not
recurse horizontally.

Within the vertical chase, all three sub-questions answer **yes**:

- **Re-cite the originator?** Our drafts → yes, cite the primary the
  chain surfaces (optionally keep the corroborator as "as reviewed in").
  External papers → no, record their chain, never rewrite them.
- **Ingest an unheld chain member?** Yes — the chase *drives
  acquisition* (`chase`/`inbound_chase` stub-without-blocking).
- **Integrity-check each chain member?** Yes — **and this is why
  vertical recursion is non-optional.** A clean corroborator citing a
  `RETRACTED-INVALIDATING` originator looks solid until you reach the
  root. You cannot certify a claim from its corroborators; you must
  integrity-check the *originator*. (Quest `169953`, "don't get
  bamboozled by a bad paper", stated precisely.)

**Governors** — this is a saliency-gated frontier, **not** transitive
closure:

1. Terminate at originator.
2. Hard depth cap (≈4) + visited-set cycle guard (citation loops exist).
3. Saliency gate — full vertical chase only for *load-bearing* claims
   (cited in a draft, in an active quest, flagged); everything else
   shallow/lazy. Mirrors `watch_poll`'s seminal-seed cap.
4. TTL — retraction re-check honors `retraction_checked_at`; S2 edges +
   edge verdicts are 30-day TTL-gated (`citation_lens.py`).

## Seniority is derived, not stored

The "1–2–3 papers that established it; the rest cite them" ordering is
**computable from the `links` citation graph we already materialize** —
no PageRank, no stored seniority score:

> Within a single claim's supporter set {A,B,C,D}, the **originators**
> are the ones the *other* supporters cite. Walk the `cites` edges
> *among that set only*; A,B cited by C,D ⇒ A,B `establishes`, C,D
> `corroborates`. Tie-break by earliest publication date.

`NOT-ORIGINATOR` (axis A stage 3) and this derivation are the **same
signal**: the corroborators are the not-originators, and following their
onward cites is *how you discover* the seniors. Fallback when the set has
no intra-edges held: global S2 citation count + earliest date — subject
to edge coverage (`citation_lens` only materializes edges to *held*
refs, see [open questions](#open-questions--incoherences)).

## Forward + backfill — one design, two triggers

- **Forward (on ingest)** — new PDF → resolve its outbound citations via
  `chase` (`PRECIS_CHASE_LLM`, dark). Each resolved edge attaches-or-mints
  the claim hub.
- **Backfill (review the held corpus — the "review existing papers"
  ask)** — `inbound_chase` (`PRECIS_INBOUND_CHASE_ENABLED`, dark) +
  source-backfill slices (`c1b00312`) sweep held papers, resolve the
  edges we skipped at ingest. **Gated on canonicalization being solid**
  (backfill floods the claim-matcher with the whole corpus at once) and
  on a **saliency-ordered rollout** (most-cited/most-salient first), not
  a flat corpus rescan.

## Response policies — one state machine, ownership picks the policy

Detection is identical for our drafts and external papers; only the
*response* differs.

| Outcome | Our draft (we own the text) → **fix** | External paper → **record** |
|---|---|---|
| `NO-CLAIM` | flag: "citation with no host claim — add the assertion or cut the cite" | mark chunk pure-pointer / non-evidential |
| `UNACQUIRABLE` | flag; soften or swap to a held source | edge: "cites unverifiable source" |
| `UNLOCATED` | whole-paper-cite warning → pin to chunk | low-confidence edge |
| `NOT-ORIGINATOR` | re-target `\cite` to the primary | follow onward; mark `corroborates` |
| `PARTIAL` | add caveat to prose, or narrow the claim | attach caveats to edge |
| `NO-SUPPORT`/`CONTRADICTS` | **drop/replace the cite** (we asserted what our own source doesn't back) | reliability signal → quest `169953` |
| `RETRACTED-INVALIDATING` | drop/replace; if seminal, claim jeopardy | poison edge; ripple per role |
| `RETRACTED-UNRELATED`/`CORRECTED-cosmetic` | administrative note only | flag, support stands |

## Model-tier table — smartness per decision call

Routes through the existing seam (ADR 0046 → **0066** capability tiers:
`FRONTIER / BIG / MEDIUM / SMALL`); `chase` already dispatches at
`Tier.MEDIUM`. Governing principle (ADR 0047): **cost = tier × volume —
push the fine/dangerous axis up, keep the hot axis free.** The two calls
worth FRONTIER are both rare *and* dangerous; every high-volume call is
SMALL or model-free.

| Decision call | Tier | Volume | Why |
|---|---|---|---|
| Attribution: own vs background | **SMALL / local** | very high (per chunk) | `role3` already does it **free** (ADR 0047) |
| Claim extraction → canonical form | **SMALL→MEDIUM** | high (per own-chunk) | structured extraction, local-capable |
| Blocking (nearest hubs) | **none — ANN** | high | vector search over existing embeddings |
| Support verdict (yes/partial/no) | **MEDIUM** | high (per edge) | `chase` today, ~$0.01/verify |
| Locate passage in target | **MEDIUM** (+ deterministic) | high | `chase:locate` is Tier.MEDIUM |
| Dedup-judge (`same`/`different`/`contradicts`) | **MEDIUM** | medium (K/candidate) | bounded 3-way pairwise call |
| Merge confirmation (a risky `same`) | **BIG → FRONTIER** | low (merges only) | a wrong merge fuses distinct claims — dangerous |
| **Integrity reason-relevance** | **FRONTIER / BIG** | low (retracted only) | false "unrelated" keeps fraud live; default-invalidating |
| Contradiction vs scope-mismatch | **BIG** | low | mislabel writes a false disagreement |
| Seniority ordering | **none — graph** | — | centrality over `links` |
| Draft-fix suggestion | **MEDIUM → BIG** | low | authoring judgment, human-confirmed |

## Reuse map — what already exists

| Piece | Where | State |
|---|---|---|
| Verified claim→quote record | `handlers/citation.py` | shipped; becomes per-edge artifact |
| Chased claim + chain | `handlers/finding.py` | shipped; **becomes the hub** |
| Own-claim chunk selector | ADR 0047, `quest/claims.py` | shipped (91% precision), extractor only |
| Outbound chunk resolver + support verdict | `workers/chase.py`, `_chase_llm.py` | built, **dark** (`PRECIS_CHASE_LLM`) |
| Inbound corpus sweep | `workers/inbound_chase.py` | built, **dark** (`PRECIS_INBOUND_CHASE_ENABLED`) |
| Corpus source-backfill lenses | `backfill/…`, `c1b00312` | slices 1–4 |
| S2 citation graph → `links` | `backfill/citation_lens.py` | shipped, 30d TTL, held-only |
| Retraction columns + Crossref checker | `refs.retraction_*`, `handlers/provenance.py` | shipped (Ph 1–2); **Ph 3 reason codes absent** |
| Retraction ripple | ADR 0054, `store/_argument_ops.py` | shipped, **reason-blind** |
| Argument graph (horizontal) | ADR 0054, `_argument_view.py` | shipped; the opt-in deep layer |

## In scope

- `finding`-as-claim-hub: the typed, graded, many-paper evidence
  relation; role (`establishes`/`corroborates`/`contradicts`) derived.
- Axis-A resolution pipeline + closed outcome enum, wired to `chase`.
- Axis-B integrity: 4-state enum + reason-relevance judgment +
  `provenance` Phase 3 reason codes.
- Vertical chase-to-origin with the four governors; seniority derivation.
- Forward + backfill triggers (turn on + finish `chase`/`inbound_chase`),
  saliency-ordered.
- Ownership-keyed response policies (draft fix vs external record).

## Explicitly NOT in scope

- **No new `claim` kind** — `finding` is the hub.
- **No horizontal recursion** into an originator's own argument by
  default — that stays the opt-in argument graph (ADR 0054).
- **No reliability/trust score** (axis D) — rejected by ADR 0054, not
  reopened here.
- **No currency/supersession engine** (axis C) beyond the `contradicts`
  edges that already exist.
- **No claim hierarchy** (broader/narrower subsumption between claims) in
  v1 — flat dedup only; the "lattice" is deferred to v2.
- **No claim↔concept reconciliation** — the `concept` kind is left
  untouched (loose end, open #13).
- **No full transitive closure** of the citation graph — governed
  frontier only.
- Backfill does **not** run corpus-wide until canonicalization is
  accepted and a saliency rollout is defined.

## Acceptance criteria

1. A `finding` renders (a new `view`) its evidence edges grouped by
   derived role, each showing support-outcome + integrity-state +
   caveats, with the originators marked.
2. Resolving a draft citation to a `NOT-ORIGINATOR` source surfaces the
   originator and offers the re-target; the draft outline shows the
   Axis-A/B outcome per cite.
3. A retraction whose reason is adjudicated `RETRACTED-UNRELATED` does
   **not** stale a claim it supports; one adjudicated
   `RETRACTED-INVALIDATING` on an `establishes` edge does, and the ripple
   reason is recorded.
4. Given two supporters where one cites the other, the cited one is
   derived `establishes` and the citer `corroborates`, from `links`
   alone.
5. Backfill on a saliency-ordered slice attaches edges to existing hubs
   (dedup) rather than minting near-duplicate hubs, measured on a labeled
   fixture (see canonicalization).
6. Every resolution attempt terminates in exactly one Axis-A outcome and
   one Axis-B state; none silently drops.

## Target + blast radius

**Storage — no migration.** Taproot is a tags-and-links overlay on tables
that already exist; **do not write a schema migration.** Verified:
- **Hubs** = `finding` refs (kind already exists) — reuse, don't create.
- **`TAPROOT:claim`/`review`** = rows in the existing **`ref_tags`** table;
  register the closed namespace in code (like `ROLE3`). No column.
- **Evidence + claim edges** (`establishes`/`corroborates`/`contradicts`)
  = rows in **`links`**. **Correction (Phase 2, ADR 0073):** `links.relation`
  is not free text — it has an **FK to `relations(slug)`**
  (`links_relation_fkey`, `0001_initial.sql`). So a *new* slug needs a
  **forward migration** seeding the `relations` row (plus the `Relation`
  Literal edit as the static typo hint + `store.valid_relations()` at runtime).
  In practice Phase 2 added **only** `establishes` (migration `0094`);
  `corroborates` (0085) and `contradicts` (0001) already existed and are
  reused. The "no SQL" claim originally here was wrong.
- **Edge metadata / integrity** = jsonb `meta` + the existing
  `refs.retraction_*` columns. Phase 1 (canonicalization) persists nothing.

**Write-path guard.** Because relation validity is enforced in code (not by
a DB CHECK), **every hub/edge write must go through the taproot handler** —
a raw `INSERT` bypasses `_VALID_RELATIONS` and a typo'd relation becomes a
silent junk edge (the exact error taproot exists to prevent). Single write
path; guard against non-process writes (open #16).

**Code touch points:** `handlers/{finding,citation,provenance}.py` ·
`workers/{chase,inbound_chase}.py` + `_chase_llm.py` ·
`backfill/citation_lens.py` + source-backfill · `store/_argument_ops.py`
(reason-graded ripple) · the `Relation` type + `ref_tags` writer · a new
`finding` evidence `view` + draft-outline hygiene surface · skills
`precis-finding-help`, `precis-citation-help`, `precis-provenance-help`,
`precis-argument-help` · env flags `PRECIS_CHASE_LLM`,
`PRECIS_INBOUND_CHASE_ENABLED`.

## Open questions / incoherences

Called out deliberately — several are **blocker-severity** (must resolve
before any `status: ready`).

1. **[BLOCKER — design in [canonicalization](#the-crux--canonicalization--flat-claim-dedup), simplified 2026-07-28]**
   **v1 is flat dedup** (canonical form → ANN block → 3-way dedup-judge
   `same`/`different`/`contradicts` → confirm risky merges) — the
   broader/narrower "lattice" was cut to v2 (self-inflicted complexity;
   not needed for grounding or seniority). Stays a blocker until it has a
   **sub-spec** and passes the fixture at **over-merge ~0**. Over-merge is
   the one dangerous direction. **Nothing downstream runs at corpus scale
   until it passes.**

2. **[RESOLVED — by the flat-dedup simplification]** Scope/granularity is
   no longer a problem to solve: claims that aren't the *same* just become
   **separate hubs** (safe under-merge). No need to pick "the right grain"
   or place claims in a hierarchy — that temptation (and the DAG /
   transitive-closure risk) is gone in v1.

3. **[RESOLVED — see [core model](#finding-vs-memorykindlemma--already-unified-nothing-retired)]**
   Nothing is retired. `finding` = grounded lemma (evidence edges);
   `memory:kind:lemma` = derived lemma (inference edges); the code
   already unifies them via `_is_premise()`. Evidence edges attach to
   findings only; inference edges to either; both feed one ripple.
   *Residual sub-question:* `citation`'s self-embedded card vs the hub's
   card — de-dup the embedding so search doesn't double-count N edges +
   the hub (mechanical; belongs in the `claim-hub-node` sub-spec).

4. **[RESOLVED — cite-the-claim, resolve at export time]** You author a
   reference to a **claim**, not a paper; the concrete citation is
   **materialized at export time** (LaTeX/docx), not at write time. The
   finding never enters the `.bib`. **Payoff — a living citation:** because
   the cite points at the claim, when the true originator is later
   discovered the *next export just improves on its own*; and a post-hoc
   claim merge re-points cites automatically at next export (no draft
   edit). *Blast radius:* the docx/latex resolvers (`93f4ff93`,
   `f2c72265`) learn claim→current-best-originators expansion.

5. **[BLOCKER → mostly deterministic + a small gate]** Reason-relevance.
   **Decided:** most of it is *not* an LLM call. RW reason codes cleave
   cleanly — a **static reason-code → default-severity table (tier-0,
   free)** handles the bulk: `data-fabrication`/`unreproducible`/
   `analysis-error` ⇒ INVALIDATING; `authorship`/`affiliation`/`funding`/
   `text-plagiarism`/`duplicate-publication` ⇒ UNRELATED. The FRONTIER
   call is reserved for the **ambiguous middle** ("error in Figure 3" →
   which claim?). Two hard rules: **uncertain ⇒ INVALIDATING** (fail
   safe); in **backfill, auto-apply INVALIDATING autonomously, queue
   probable-UNRELATED for human/FRONTIER review** (bounded worklist) — so
   backfill stays autonomous *and* errs safe. *Remaining:* build the
   reason-code table (needs RW Phase-3 data) + the ambiguous-residual
   gate. No longer a full blocker.

6. **[RESOLVED]** Seniority is a **post-chase derivation**: vertical chase
   *ingests* originators (as stubs if unfetchable) and `chase` writes
   `cites` edges to unheld **stub** targets, so an unheld originator is
   still visible to intra-set centrality as a stub node. Recompute as
   coverage improves (TTL-gated derived view). Order pinned:
   resolve/ingest → rank.

7. **[OPEN — needs a pilot number]** Backfill cost. **Decided shape:**
   dominant cost = MEDIUM calls × resolved edges (support + K
   dedup-judges); attribution is SMALL, integrity mostly tier-0 lookup,
   blocking free. Governance = saliency-ordered + hard per-run token valve
   (reuse tracked `cost_sources`) + "N most-salient unresolved
   papers/day" throttle. The **absolute number is genuinely unknown**
   until Phase 5 measures a pilot slice — this is the one item that
   *can't* be closed from the armchair.

8. **[RESOLVED — ties to #1]** Route the cited passage's claim through
   canonicalization *before* the contradiction verdict. If the dedup-judge
   calls it `different` from C, a "no support" is a **scope-mismatch**, not
   a contradiction. `CONTRADICTS` = **same claim, opposite-polarity** only.
   The dedup-judge is the disambiguator; you cannot label contradiction
   without first establishing it is the same claim.

9. **[TYPED — function deferred to P2]** Claim confidence is an **ordinal**
   (`established / supported / contested / undermined`), not a float
   (floats invite false precision). Dominated by *surviving established
   originators*; corroborators secondary with diminishing returns;
   `undermined` triggers on originator loss or same-scope contradictions
   outweighing support. The *type* is fixed now; the exact aggregation
   function is a Phase-2 detail.

10. **[RESOLVED]** Attribution is judged at **(chunk, claim)** grain.
    `ROLE3:own` is a *prior*, not the verdict; the call asks "does *this
    chunk* originate *this claim* or attribute it onward?", reading the
    claim sentence's inline `[k]` markers. A chunk can be `own` overall
    yet NOT-ORIGINATOR for a claim it cites prior art for.

11. **[RESOLVED — discriminator tag, don't split the kind]** The `finding`
    kind is **polluted**: many prod `finding` rows are editorial review
    notes ("acronym unexpanded", "riyaz25 cited but Pd not studied"), not
    grounded world-claims. **Decision** (repo-idiomatic — open-tag
    pattern, ADR-0054 precedent of *not* minting a kind): keep one
    `finding` kind, add a **closed discriminator tag** `TAPROOT:claim`
    (grounded world-claim) vs `TAPROOT:review` (editorial/manuscript note).
    **The taproot hub + evidence edges attach only to `TAPROOT:claim`
    findings**; review notes stay findings but are excluded from the claim
    graph. Backfill-classify existing findings with a cheap SMALL/local
    pass (mirrors the `ROLE3` cascade). Must land before `finding` becomes
    the hub in Phase 2. (The fixture sidestepped this by drawing from
    `citation`, which is clean.) **Built 2026-07-29 (Phase-2 slice 2a):**
    `data/axes/taproot.yaml`, a ref-level `axis_pass` classifier (`axis:taproot`,
    default-OFF); `TAPROOT` registered in `_CLOSED_VOCAB`. Fail-open (no
    `default_unknown`).

12. **[COVERED — via synthetic, corpus is thin]** A targeted Opus scan of
    all 422 claims found only **1 genuine** contradiction + 7
    apparent-but-orthogonal negatives — the held corpus is genuinely thin
    on real contradictions (restatements + different-scope opposites
    dominate). Closed with **22 synthetic scope-matched negations** + 8
    scope-shifted hard-negatives (pairs 209–238, `needs_adjudication`),
    giving the `contradicts` edge real coverage (n=23). *Caveat:*
    coverage is now mostly synthetic — tests judge *logic*, not evidence
    of real literature disputes.

13. **[OPEN — loose end from the simplification pass]** **`concept` is
    unaddressed.** There are three claim-ish node types: the taproot hub
    (`TAPROOT:claim` finding), `memory:kind:lemma` (derived lemma), and
    `concept` (learner term + mastery). We reconciled the first two by rule
    (grounded vs derived, #3), but never drew the claim↔concept boundary. A
    concept ("catalytic activity") is a *term/topic*, not an *assertion* —
    probably orthogonal to a claim — but the overlap was never pinned. Not
    a v1 blocker (taproot doesn't touch `concept`); decide before any
    cross-wiring.

14. **[RESOLVED — canonical text = best synthesis]** A hub's claim
    sentence is the **best synthesis** of its supporters (not first-writer,
    not most-general), **re-synthesized as evidence accrues**. Combined
    with export-time resolution (#4), the hub is a *living* claim: both its
    wording and its cited originators improve over time without touching
    any draft.

15. **[RESOLVED — only paper-sourced claims become hubs; drafts insulated]**
    A hub is minted only from a claim **grounded in the corpus** (a paper
    chunk). A draft's own *novel* assertions stay **draft-local** — they do
    **not** enter the shared claim graph, so concurrent drafts can't
    cross-contaminate each other's in-progress "made-up" claims. You may
    still cite a paper, a chunk, or an existing claim directly.

16. **[RESOLVED — risky merges are todos; guard the write path]** A
    low-confidence `same` (merge) is **not auto-applied** — it files a
    `kind='todo'` requesting human adjudication (an over-merge in prod is
    the dangerous error). And **all hub/edge writes go through the taproot
    handler** — never a raw insert; a non-process write bypasses relation
    validation and is a defect to guard against (single write path).

**Fully-subsumed?** No — taproot *partitions*, it does not swallow
everything. It absorbs the **evidence-grounding** cluster (`citation` →
per-edge artifact, `finding`-as-hub, the `chase` engine, `provenance`
integrity) into one hub, but **coexists with** the argument graph
(`memory:kind:lemma`, the *inference* axis) and leaves `concept` untouched
(#13). That boundary is deliberate, not an oversight — but it is a
partition, not total unification.

**Status after the 2026-07-28 pass:**
#2,#3,#4,#6,#8,#10,#11,#12,#14,#15,#16 resolved · #1,#5 designed-with-a-gate ·
#9 typed, function deferred · #7 needs a Phase-5 pilot measurement · #13
(concept boundary) open, not a v1 blocker. **Simplified 2026-07-28:**
canonicalization cut from a 5-relation subsumption lattice to **flat dedup**
(`same`/`different`/`contradicts`); broader/narrower deferred to v2. New
outcome: **`NO-CLAIM`** (dangling cite). Storage = **tags+links overlay, no
migration**. Citations resolve at **export time** (living citation, #4);
canonical text = **best synthesis** (#14); only **paper-sourced** claims
become hubs (drafts insulated, #15); risky merges → **todos** (#16).
**Phase 1 SHIPPED** (flat canonicalizer, `src/precis/taproot/canon.py`;
gate passed at over-merge 0/238, 2026-07-29). **Phase 2 SHIPPED except
slice 2d** (hub node + evidence vocab + view + export expansion — see
`taproot-phase2-hub-node.md` for the 2d remainder).

## Build phasing

**One spec, one shared model, staged delivery.** Not five proposals — the
model is cross-cutting and splitting the document would invite drift. The
*builds* are ordered, gated on canonicalization. Each phase is a
self-contained branch off this spec; the phase boundary is where we stop
and validate before widening blast radius.

1. **Phase 1 — canonicalization (the gate). SHIPPED** (2026-07-29,
   `src/precis/taproot/canon.py`, over-merge 0/238; build ticket in git
   history of `taproot-phase1-canonicalization` (git-only)).
2. **Phase 2 — hub node. SHIPPED except slice 2d** (`finding`-as-hub,
   evidence vocab migration `0094`, `hub.py` write door, evidence view,
   `\cite{}`→originators export). Remainder — the `citation`-card dedup
   (open #3 residual) — is
   [`taproot-phase2-hub-node.md`](./taproot-phase2-hub-node.md).
3. **Phase 3 — forward resolution.** Turn on + finish `chase`, wire the
   Axis-A pipeline to edges, draft-side response policies. First user
   value; runs on ingest only, bounded volume. **Slice W1 (the forward
   bridge) landed dark** (`chase` mints/attaches through `hub.py` on an
   established finding's terminal verdict, `PRECIS_TAPROOT_CHASE_ENABLED`);
   further slices open (S2 global-citation-count seniority fallback, …).
4. **Phase 4 — integrity axis.** `provenance` Phase 3 reason codes +
   Axis-B reason-relevance adjudication + reason-graded ripple (open #5).
   Independent of Phase 3 — can proceed in parallel once Phase 2 lands.
5. **Phase 5 — backfill.** `inbound_chase`/source-backfill over the held
   corpus, saliency-ordered rollout, cost valve (open #7). Last, because
   it is the highest-volume, highest-cost, and most-stresses
   canonicalization — gated on Phases 1–4 being solid.

This whole spec stays `status: draft` — it is the shared model + decisions
log, not a fixer pick-up. It graduates to an **ADR** when the phases land
(the durable "why"), reconciling with ADR 0054 at that point. It should
*not* pass `/ready` as one unit — the phases are the shippable grain, and
Phase 1 cannot even start `ready` until canonicalization is designed to
the fixture bar. That is the correct state, not a defect.
