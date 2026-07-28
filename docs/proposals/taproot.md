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
0047 / 0054, and `docs/design/{citation-chunk-grounding,argument-graph,
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
ripple. Rule: **evidence edges → findings only; inference edges → either.**
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

## The crux — canonicalization = lattice placement, not flat dedup

The gating problem (former open-question #1). The reframe that makes it
tractable: **do not try to pick the "right" grain.** "MOFs are tunable"
and "MOF-5 pore size scales with linker length" are *both* valid claims;
forcing them onto one node is the mistake. Instead place every claim at
its own grain and link them by broader/narrower edges — a **subsumption
lattice**. The scope problem (former #2) dissolves into this: a too-broad
claim becomes a **parent**; specific ones are **children** carrying their
own evidence. #1 and #2 are one problem, solved together.

A cascade mirroring the ADR-0047 classifier (cheap-and-wide → escalate
the hard bit):

1. **Canonical form (linchpin).** A **normalized claim sentence + a
   structured scope object** (`material`, `method`, `quantity`, `regime`)
   — *not* full predicate-logic tuples (brittle over open science). The
   scope object is what makes broader/narrower judgeable. You cannot
   block or compare claims stated at inconsistent grain, so this is the
   prerequisite that makes 2–4 work; identity and scope fuse here.
2. **Block — no model.** Embed the canonical form; ANN-retrieve the top-K
   nearest existing hubs (reuse the `finding` card embeddings already
   indexed). Zero near neighbours ⇒ new hub, free.
3. **Pairwise lattice-judge — MEDIUM.** For each of the K, one bounded
   call emits *one* relation: `equivalent` (merge) · `broader`/`narrower`
   (subsumption edge) · `orthogonal` (separate) · `contradicts`
   (contradiction edge). These five verdicts **are** the lattice edges —
   the same call dedups, builds the hierarchy, and records disagreement.
   The lattice is a **DAG, not a tree** (a claim can be narrower-than
   several parents).
4. **Escalate on conflict — BIG/FRONTIER, rare.** Only when (3) is
   ambiguous, or a proposed merge would fuse claims with divergent scope
   or opposite retraction implications (the dangerous over-merge). The
   strong model decides merge / keep-separate / create-parent.

**Merge-vs-subsume rule (the over-merge guard).** `equivalent` (one node)
**only** when scope objects match modulo paraphrase; **any** scope
difference ⇒ a `narrower`/`broader` **edge**, never a merge. **Bias hard
toward not-merging** — under-merge is recoverable (merge later),
over-merge is dangerous (a retraction ripples across fused-but-distinct
claims). Uncertainty defaults to keep-separate + link.

**Eval fixture (required before backfill).** ~200 human-labeled claim
pairs across real corpus domains (MOFs, Pd catalysts, NOx), tagged with
the five relations. Primary metric = **over-merge rate → target ~zero**,
tolerating under-merge. This is Phase 1's `ready` bar. Cheap blocking (2)
is what keeps the expensive judges (4) rare — load-bearing for cost, not
just recall.

## Axis A — the support-resolution pipeline

Resolving one `(claim C, cited-source P)` pair runs four stages; each
stage has exactly one failure exit. This closed set *is* the edge-status
vocabulary — "review a paper" = walk its citations through this pipeline
and record the terminal state per edge.

0. **Identity** (silent, pre-stage) — is C one claim, or two fused? A bad
   answer here poisons every downstream verdict. This is
   [canonicalization](#the-crux--canonicalization--scope), the gating
   risk; the pipeline below *assumes* C is well-formed.
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

Closed outcome enum: `UNACQUIRABLE · UNLOCATED · NOT-ORIGINATOR ·
PARTIAL · NO-SUPPORT · CONTRADICTS · SUPPORTS`.

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
| Pairwise lattice-judge | **MEDIUM → BIG** on ambiguity | medium (K/candidate) | bounded pairwise; escalate only if unsure |
| Merge/split over-merge | **BIG → FRONTIER** | low (conflicts only) | fuses distinct claims — dangerous |
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

`handlers/{finding,citation,provenance}.py` · `workers/{chase,
inbound_chase}.py` + `_chase_llm.py` · `backfill/citation_lens.py` +
source-backfill · `store/_argument_ops.py` (reason-graded ripple) ·
`refs.retraction_*` consumers · a new `finding` evidence `view` +
draft-outline hygiene surface · skills `precis-finding-help`,
`precis-citation-help`, `precis-provenance-help`, `precis-argument-help`
· env flags `PRECIS_CHASE_LLM`, `PRECIS_INBOUND_CHASE_ENABLED`.

## Open questions / incoherences

Called out deliberately — several are **blocker-severity** (must resolve
before any `status: ready`).

1. **[BLOCKER — design now in [canonicalization](#the-crux--canonicalization--lattice-placement-not-flat-dedup)]**
   The lattice approach (canonical form → ANN block → pairwise
   lattice-judge → escalate-on-conflict) replaces the earlier
   hand-waving, but it stays a blocker until it has its **own sub-spec +
   a labeled eval fixture** measuring under/over-merge. Over-merge is
   still the dangerous direction (a retraction rippling across fused-but-
   distinct claims). **Nothing downstream runs at corpus scale until the
   fixture passes.**

2. **[BLOCKER — folded into #1]** Scope/granularity is no longer a
   separate problem: the lattice places broad claims as *parents* and
   specific ones as *children*, and the **canonical-form** step binds
   claim + conditions so the matcher compares like with like. Remains a
   blocker only insofar as #1 does (same sub-spec, same fixture).

3. **[RESOLVED — see [core model](#finding-vs-memorykindlemma--already-unified-nothing-retired)]**
   Nothing is retired. `finding` = grounded lemma (evidence edges);
   `memory:kind:lemma` = derived lemma (inference edges); the code
   already unifies them via `_is_premise()`. Evidence edges attach to
   findings only; inference edges to either; both feed one ripple.
   *Residual sub-question:* `citation`'s self-embedded card vs the hub's
   card — de-dup the embedding so search doesn't double-count N edges +
   the hub (mechanical; belongs in the `claim-hub-node` sub-spec).

4. **[RESOLVED — cite-the-claim-expand-the-sources]** `finding`'s
   "never in `\cite{}`" contract is kept: `\cite{fi…}` is authoring sugar
   the exporter expands to the `establishes` papers at LaTeX/docx build.
   The finding never enters the `.bib`. *Blast radius to verify:* the
   docx/latex citation resolvers (`93f4ff93`, `f2c72265`) must learn the
   finding→originators expansion.

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
   lattice-judges); attribution is SMALL, integrity mostly tier-0 lookup,
   blocking free. Governance = saliency-ordered + hard per-run token valve
   (reuse tracked `cost_sources`) + "N most-salient unresolved
   papers/day" throttle. The **absolute number is genuinely unknown**
   until Phase 5 measures a pilot slice — this is the one item that
   *can't* be closed from the armchair.

8. **[RESOLVED — ties to #1]** Route the cited passage's claim through
   canonicalization *before* the contradiction verdict. If it is
   `orthogonal`/`narrower` to C, a "no support" is a **scope-mismatch**,
   not a contradiction. `CONTRADICTS` = **same-scope, opposite-polarity**
   only. The lattice-judge is the disambiguator; you cannot label
   contradiction without first establishing same scope.

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

**Status after the 2026-07-28 pass:** #3,#4,#6,#8,#10 resolved · #1,#5
designed-with-a-gate · #9 typed, function deferred · #7 the only item
needing a measurement (a Phase-5 pilot), not more design.

## Build phasing

**One spec, one shared model, staged delivery.** Not five proposals — the
model is cross-cutting and splitting the document would invite drift. The
*builds* are ordered, gated on canonicalization. Each phase is a
self-contained branch off this spec; the phase boundary is where we stop
and validate before widening blast radius.

1. **Phase 1 — canonicalization (the gate).** Canonical-form extractor +
   ANN blocking + pairwise lattice-judge + escalation, **plus the labeled
   eval fixture** (five-relation pairs) measuring under/over-merge. Ships
   and is validated *before anything writes edges at scale.* Resolves
   open #1/#2. **Everything else waits on this.**
2. **Phase 2 — hub node.** `finding`-as-hub, the typed graded evidence
   relation + lattice edges, the evidence `view`, the `citation`-card
   dedup (open #3 residual), the `\cite{}`→originators export expansion
   (open #4 residual). The schema/vocab foundation the rest writes to.
3. **Phase 3 — forward resolution.** Turn on + finish `chase`, wire the
   Axis-A pipeline to edges, draft-side response policies. First user
   value; runs on ingest only, bounded volume.
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
