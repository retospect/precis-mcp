---
status: draft
title: Taproot — unify claim/fact nodes, resolve citations to a many-paper evidence graph with graded integrity
model: opus
---

# Taproot — claim/fact hub + citation resolution

**Taproot** is the evidence-grounded claim graph: it plumbs each claim
down its citation chain to the originating (seminal) source, grounds it
in the many papers that support it, and grades that support for
integrity. `chase` resolves one citation edge; taproot is the graph it
bottoms out into. One spec, phased build — the shared claim model is
cross-cutting, so it stays in one document; only delivery is staged.

Shipped: **Phase 1** (flat canonicalizer, `src/precis/taproot/canon.py`,
gate passed at over-merge 0/238) and **Phase 2 except slice 2d**
(`finding`-as-hub, evidence vocab migration `0094`, `hub.py` write door,
evidence view, `\cite{}`→originators export; the 2d `citation`-card
dedup remainder is [`taproot-phase2-hub-node.md`](./taproot-phase2-hub-node.md)).
Build tickets + the fixture story live in git history.

## Motivation / why

Two problems, one root cause: (1) **a claim has no home** — the same
assertion is recorded as a `citation`, a `finding`, or a
`memory:kind:lemma` depending on which path minted it, so we can't
answer "who really showed this?" and re-verify the same fact once per
citing draft; (2) **citations resolve to a chunk, not a claim**, so
fifty papers asserting the same fact stay fifty disconnected edges.

## The core model (decided)

**The claim is the hub. Reuse `finding` — no new `claim` kind**
(ADR 0054 precedent). `finding` = the *grounded* lemma (evidence edges
to papers) and is the hub; `memory:kind:lemma` = the *derived* lemma
(inference edges). Evidence edges attach only to `TAPROOT:claim`
findings; inference edges to either; both feed one retraction ripple.

```
finding  "Pd/C catalyzes Suzuki coupling at RT with mild base"   ← hub
   ├─ establishes  → paper A @chunk   support=yes   integrity=clean  (originator)
   ├─ corroborates → paper C @chunk   support=yes   integrity=clean  (C cites A)
   ├─ corroborates → paper D @chunk   support=partial caveats=[aqueous only]
   └─ contradicts  → paper E @chunk   support=no    integrity=clean
```

- `citation` stays as the *per-edge verified record*; `finding` is the
  node the edges converge on; edge role is **derived**, never hand-set.
- **Cite the claim, resolve at export time** — `\cite{fi123}` expands
  to the current-best originator papers at LaTeX/docx export; the
  finding never enters the `.bib`. Payoff: a *living citation* — a
  later-discovered originator or a post-hoc merge improves the next
  export with no draft edit.
- **Canonical text = best synthesis** of the supporters, re-synthesized
  as evidence accrues.
- **Only paper-sourced claims become hubs** — a draft's own novel
  assertions stay draft-local (concurrent drafts can't
  cross-contaminate).

**Two axes, not one failure list:** A · Support (does the source assert
the claim?) — the new pipeline; B · Integrity (retracted/corrected *and
relevant to this claim*?) — reason-relevance judgment missing;
C · Currency — not a focus beyond existing `contradicts` edges;
D · Reliability prior — out of scope (ADR 0054 rejected
trust-derivation).

## Canonicalization (Phase 1, shipped — the standing constraints)

v1 is **flat claim dedup**: `same` (merge) / `different` (separate hub)
/ `contradicts` (separate + link). No claim hierarchy — the
broader/narrower "lattice" is deferred to v2. Cascade: canonical form
(claim sentence + light scope note) → ANN block over finding-card
embeddings → 3-way MEDIUM dedup-judge → BIG/FRONTIER confirmation of
risky merges only. **Merge rule:** `same` only for the same fact under
the same conditions; bias hard toward separate — under-merge is
recoverable, over-merge lets a retraction ripple across
fused-but-distinct claims; uncertain ⇒ separate. The 238-pair fixture
(`tests/fixtures/taproot/`) grades collapsed 3-way and stays the
regression bar; **nothing downstream runs at corpus scale until the
over-merge rate holds ~zero.** Atomic splitting of multi-assertion
chunks **is live** (migration 0126, `conjunct-of` relation,
`hub.apply_extraction`, compound hubs — see `taproot-compound-migration.md`
for the corpus-wide migration of existing hubs).

## Axis A — support-resolution pipeline (Phase 3, open)

Stages: acquire → locate → attribute → support. Closed outcome enum:
`NO-CLAIM · UNACQUIRABLE · UNLOCATED · NOT-ORIGINATOR · PARTIAL ·
NO-SUPPORT · CONTRADICTS · SUPPORTS`. `NO-CLAIM` is the claim-end
missing (a pure-pointer cite — hygiene defect in our drafts, a
navigation node in external papers). `NOT-ORIGINATOR` is not a failure
— it is the seniority engine. `CONTRADICTS` = same claim (per the
dedup-judge), opposite polarity only; a "no support" on a `different`
claim is a scope-mismatch. Attribution is judged at (chunk, claim)
grain — `ROLE3:own` is a prior, not the verdict.

## Axis B — integrity, reason-relevant (Phase 4, open)

Exists: `refs.retraction_*` columns, the `provenance` Crossref checker,
the ADR-0054 ripple. **Gap: the ripple is paper-level and
reason-blind** — a corrigendum fixing an affiliation invalidates like
fabricated data. Needed per-edge outcomes: `CLEAN` /
`RETRACTED-INVALIDATING` / `RETRACTED-UNRELATED` / `CORRECTED`
(sub-judge: did the value change?) / `CONCERN`. Decided mechanics: a
static reason-code → default-severity table handles the bulk (tier-0,
free); FRONTIER reserved for the ambiguous middle; **uncertain ⇒
INVALIDATING** (fail safe); in backfill, auto-apply INVALIDATING,
queue probable-UNRELATED for review. Needs `provenance` Phase 3
(Retraction Watch reason codes) as the structured input.

## Recursion — vertical only, governed

Vertical chase-to-origin is in scope and bounded: hop for the *same
claim* until the originator; chains bottom out in ~1–3 hops.
Integrity-check every chain member — a clean corroborator citing a
fraudulent originator looks solid until you reach the root
(quest `169953`). Horizontal recursion into an originator's own
argument is the argument-graph's job (ADR 0054), opt-in, never default.
Governors: terminate at originator; depth cap ≈4 + visited-set cycle
guard; saliency gate (full chase only for load-bearing claims);
TTL-gated re-checks (30-day S2/edge TTL, `retraction_checked_at`).

## Seniority is derived, not stored

Within a claim's supporter set, the originators are the ones the other
supporters cite — walk `cites` edges *among that set only*; tie-break
by earliest date. Chase ingests unheld originators as stubs so they
stay visible to intra-set centrality; recompute as coverage improves.
Fallback: global S2 citation count + earliest date.

## Forward + backfill — one design, two triggers

- **Forward (Phase 3):** new PDF → resolve outbound citations via
  `chase`; each resolved edge attaches-or-mints the hub. Slice W1 (the
  forward bridge) landed dark (`PRECIS_TAPROOT_CHASE_ENABLED`); open
  slices include the S2 global-citation-count seniority fallback.
- **Backfill (Phase 5):** `inbound_chase` + source-backfill sweep held
  papers — **gated on canonicalization staying solid** and a
  saliency-ordered rollout (most-cited first), never a flat rescan.

## Response policies — ownership picks the policy

Detection is identical; response differs. Our draft (we own the text)
→ **fix**: flag `NO-CLAIM`, pin `UNLOCATED`, re-target `\cite` to the
originator on `NOT-ORIGINATOR`, add caveats on `PARTIAL`, drop/replace
on `NO-SUPPORT`/`CONTRADICTS`/`RETRACTED-INVALIDATING`. External paper
→ **record**: mark the edge (pure-pointer / unverifiable /
low-confidence / `corroborates` / caveats / reliability signal /
poisoned + ripple per role). `RETRACTED-UNRELATED` / cosmetic
corrections: administrative note only, support stands.

## Model-tier discipline (ADR 0047/0066)

Cost = tier × volume — push the fine/dangerous axis up, keep the hot
axis free. High-volume calls are SMALL or model-free (attribution =
`role3`, blocking = ANN, seniority = graph); support/locate/dedup-judge
= MEDIUM; merge confirmation and integrity reason-relevance = BIG/
FRONTIER (rare and dangerous — a wrong merge fuses distinct claims, a
false "unrelated" keeps fraud live).

## In scope

Typed, graded, many-paper evidence relation on `finding`; the Axis-A
pipeline + closed enum wired to `chase`; Axis-B reason-relevance +
`provenance` Phase 3; vertical chase with the four governors; derived
seniority; forward + saliency-ordered backfill; ownership-keyed
response policies.

## Explicitly NOT in scope

- No new `claim` kind; no horizontal recursion by default; no
  reliability/trust score (axis D); no currency engine (axis C); no
  claim hierarchy in v1; no claim↔concept reconciliation (open #13);
  no transitive closure; backfill does not run corpus-wide until
  canonicalization is accepted and a saliency rollout is defined.

## Acceptance criteria

1. A `finding` renders its evidence edges grouped by derived role with
   support-outcome + integrity-state + caveats, originators marked.
2. Resolving a draft cite to a `NOT-ORIGINATOR` source surfaces the
   originator and offers the re-target; the draft outline shows the
   Axis-A/B outcome per cite.
3. `RETRACTED-UNRELATED` does not stale a supported claim;
   `RETRACTED-INVALIDATING` on an `establishes` edge does, with the
   ripple reason recorded.
4. Given two supporters where one cites the other, roles derive from
   `links` alone.
5. Backfill on a saliency-ordered slice attaches to existing hubs
   rather than minting near-duplicates, measured on the fixture.
6. Every resolution attempt terminates in exactly one Axis-A outcome
   and one Axis-B state; none silently drops.

## Target + blast radius

Tags-and-links overlay: hubs = `finding` refs; `TAPROOT:claim`/`review`
= closed `ref_tags` namespace (classifier axis shipped,
`data/axes/taproot.yaml`, default-OFF); evidence edges = `links` rows —
note `links.relation` has an FK to `relations(slug)`, so a *new* slug
needs a forward migration (Phase 2 added only `establishes`, 0094).
**Write-path guard:** every hub/edge write goes through the taproot
handler — a raw INSERT bypasses relation validation (open #16
decision). Code touch points: `handlers/{finding,citation,provenance}`,
`workers/{chase,inbound_chase}` + `_chase_llm`,
`backfill/citation_lens`, `store/_argument_ops` (reason-graded ripple),
the finding evidence view + draft-outline hygiene surface, the
`precis-{finding,citation,provenance,argument}-help` skills, env flags
`PRECIS_CHASE_LLM` / `PRECIS_INBOUND_CHASE_ENABLED`.

## Open questions

Resolved items (#2,#3,#4,#6,#8,#10,#11,#12,#14,#15,#16) are folded into
the body above; full argument in git history. Still open:

- **#5 residual** — build the RW reason-code → severity table (needs
  `provenance` Phase-3 data) + the ambiguous-residual FRONTIER gate.
- **#7 backfill cost [needs a pilot number]** — dominant cost = MEDIUM
  calls × resolved edges; governance = saliency order + per-run token
  valve + N-papers/day throttle. The absolute number is unknowable
  until Phase 5 measures a pilot slice.
- **#9** — claim confidence is an ordinal
  (`established/supported/contested/undermined`), dominated by
  surviving established originators; the exact aggregation function is
  deferred.
- **#13** — the claim↔concept boundary was never pinned (a concept is
  a term/topic, not an assertion — probably orthogonal). Not a v1
  blocker; decide before any cross-wiring.

## Build phasing

1. **Phase 1 — canonicalization. SHIPPED** (gate: over-merge ~0).
2. **Phase 2 — hub node. SHIPPED except slice 2d**
   ([`taproot-phase2-hub-node.md`](./taproot-phase2-hub-node.md)).
3. **Phase 3 — forward resolution.** Turn on + finish `chase`, wire the
   Axis-A pipeline to edges, draft-side response policies. First user
   value; ingest-only, bounded volume. W1 landed dark; further slices
   open.
4. **Phase 4 — integrity axis.** `provenance` Phase 3 reason codes +
   Axis-B adjudication + reason-graded ripple. Parallel to Phase 3.
5. **Phase 5 — backfill.** Highest-volume, highest-cost; gated on
   Phases 1–4 being solid.

This spec stays `status: draft` — it is the shared model + decisions
log, not a fixer pick-up; the phases are the shippable grain. It
graduates to an ADR when the phases land, reconciling with ADR 0054.
