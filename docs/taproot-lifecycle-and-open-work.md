---
status: snapshot
title: "the claim lifecycle, and every open thread in it — a hand-off for compaction"
---

# The claim lifecycle, and what is missing from it

**This is a working snapshot, not durable truth.** It exists to be read
whole, compacted, and streamlined. Once that is done, the lifecycle
description belongs in `src/precis/taproot/__init__.py`'s docstring and
each open thread belongs in its own `docs/backlog/` item; this file
should then be deleted. Do not let it become a done-log
(`docs/README.md`).

---

## Part 1 — the flow

### Two loops

The system has an **inward loop** and needs an **outward loop**. Only the
inward one is built.

```
INWARD (built)                          OUTWARD (mostly absent)

  paper lands                             hub exists
      ↓                                       ↓
  chunk + embed                          retrieve near chunks corpus-wide
      ↓                                       ↓
  agent cites [pc] in a draft            judge: does this support it?
      ↓                                       ↓
  backfill converts cite → hub           attach new evidence
      ↓                                       ↓
  extract → place → ground               re-weigh independence
      ↓                                       ↓
  hub with evidence                      surface opposition → adjudicate
```

The inward loop's entry condition is **someone wrote about it**. That is
the ceiling. A paper sitting in the corpus that directly bears on a claim
is invisible to that claim unless a human or agent happened to cite it in
prose.

### The stages, in order

| # | stage | what it decides | state |
|---|---|---|---|
| 1 | **Extract** | is this sentence a claim at all? | live |
| 2 | **Admit** | falsifiable · self-contained · method-attributed · single assertion | live (advisory at mint, blocking at approve) |
| 3 | **Place** | is this the same claim as one we hold? | live — *just repaired* |
| 4 | **Ground** | which passage supports it, and how strongly | live |
| 5 | **Widen** | who else in the corpus speaks to this? | **built, dark** — `workers/hub_refine.py` |
| 6 | **Weigh** | how much independent support is there? | partial — count shipped, display-only, gates nothing |
| 7 | **Oppose** | what conflicts with this? | **found but discarded** — `hub_refine` verifies contradictors and memoes them as rejected instead of writing the edge |
| 8 | **Adjudicate** | is the conflict real, and who wins? | **absent** |
| 9 | **Gate** | is it publishable? | live (admissibility only) |
| 10 | **Publish** | mint, sign, anchor | live — human doors |

### What each missing stage would be

**5 — Widen. This is built and switched off, not missing.**
`workers/hub_refine.py::run_hub_refine_pass` is the stage, end to end:
claim a due-set → discover → filter → LLM-verify → attach → stamp. Its
docstring states the gap in the exact terms above — *"neither ever looks
at an **existing** hub and asks 'what else in the corpus supports this
claim?'"*. It ships with 46 tests, a zero-write tuning harness
(`taproot/slice_refine_eval.py`), and an enablement runbook
(`docs/runbooks/taproot-chase-enablement.md`).

It also carries a second discovery source nobody asked for and everybody
wants: `_citation_candidates` follows the hub's own grounding chunks →
`chunk_citations` → the cited paper, and verifies the claim against what
that citation *actually says*. A `no` becomes a queryable red flag in
`meta['citation_misses']` — automated detection of miscitation.

The incremental trigger is `workers/chase_trigger.py`, a **reverse** ANN:
instead of sweeping ~1.9M paper chunks per hub, it embeds the small
(~1,524) hub set and probes *that* per newly-embedded chunk, tagging near
hubs `TAPROOT_DUE`. Cheap by construction.

**The query-design question is already settled, and settled correctly.**
`hub_refine` anchors on the **claim sentence** (`_refine_one_hub`,
`embed_query(embedder, claim_sentence)`), not on existing supporters. The
reason is stronger than the independence argument: a fixed claim vector
means pass N+1 searches the same semantic point as pass 1, so the
rejection memo and the attached-source precheck can actually *drain* the
candidate set. A supporter-seeded query mutates on every attach, the
candidate set never stabilizes, and the bounded-spend guarantee collapses.
Seeding on supporters would also be rich-get-richer on phrasing, and
phrasing correlates with same-lab, same-lineage work — selecting against
the independence that carries the most weight.

The real concern behind "search from what we already have" — that one
claim vector misses confirmations phrased differently — is genuine. The
fix is **query expansion anchored on the claim**: `search_blocks_multi`
(`store/_blocks_ops.py`) already does reciprocal-rank fusion over N legs,
takes `kinds` plural, and offers `per_paper` as a diversity cap. Feed it
the claim plus a few LLM paraphrases in deliberately different
vocabularies.

**A concrete defect found in the reading.** `_refine_one_hub` fetches
`topk` hits and *then* discards already-attached and already-rejected
sources, but `search_blocks` accepts `exclude_ref_ids` and `hub_refine`
does not pass it. So known and rejected sources burn the 8-slot budget —
worst on hubs that already have several supporters, which are exactly the
hubs where widening pays most. Small fix, direct leverage.

Two invariants that still need deciding:

- **Widening currently may only add support** — it always writes
  `corroborates`; `establishes` is derived over the citation graph rather
  than guessed at write time. That is the right default.
- **Idempotency is handled in the writer, not the schema.** The pass has
  a precheck, a rejection memo, a TTL'd lease and a sha-reopen. But
  `links` still has no unique constraint on `(src, dst, relation)`, so
  the guarantee lives entirely in application code — and the memo write is
  a read-modify-write on `meta`, which is *why* the runbook forbids
  running two instances.

**7 — Oppose.** Today the only path that can produce a hub↔hub
disagreement is `place()` at ingest, which compares a new claim against
retrieved candidates. It has produced **zero** edges in the corpus's
lifetime. A standing pass over near-neighbour hub pairs is the missing
piece — but note that embedding proximity measures *topical* similarity,
not propositional opposition. "X enhances Y" and "X has no significant
effect on Y" can sit far apart in embedding space while two paraphrases
sit at 0.03. Any retrieval-based opposition finder is structurally biased
against the disagreements most worth finding, so its recall is a floor,
never an estimate.

**8 — Adjudicate.** A statement about two claims is itself a claim. The
right carrier is not a link but a **hub whose sentence is about two other
hubs**, minted through the normal path and therefore itself citable,
reviewable and disputable. That gives recursion for free: an adjudication
can be disputed like anything else. The cheap link flag and the
adjudication hub are two ends of one lifecycle, not competitors.

### The ratchet problem

Every stage above promotes. Almost nothing demotes. A claim accumulates
support and never re-opens when contradicting evidence lands later. The
dark `chase_trigger` pass (marks a hub `TAPROOT_DUE` when a near
paper/patent chunk arrives) is the closest existing thing to a
re-opening mechanism. **A flow that can only ratchet upward will report
increasing confidence in a claim the literature has moved against.**

---

## Part 2 — open work

### A. In flight, this branch (34 files, staged, not yet shipped)

| # | change | risk |
|---|---|---|
| A1 | `block()` retrieval repaired: `card_combined` (12.3% coverage) → `finding_body` (100%) | **activates `place()` at scale for the first time** |
| A2 | `place()` gates `contradicts` on confidence; sub-threshold mints unlinked | low — all consumers already group `new` with `new_contradicts` |
| A3 | notation + scope lints surfaced at mint (CLI stderr, MCP response) | none — advisory only |
| A4 | independent-supporter count (union-find over authors) | low — read path only |
| A5 | `precis-taproot-help` split into orientation / mint / backfill | none — content move |

**A1 is the one to watch.** It has been dark by accident: `place()` almost
never saw a candidate, so neither the contradiction path *nor the merge
path* ran. Deploying it switches on auto-attach (`same` at high
confidence → attach to existing hub) across 1,524 hubs. That is the
intended repair — it is how duplicate hubs converge — but the first large
run should probably be a dry run reviewed by a human before it writes.

### B. Corpus repair — the nanobuds campaign

| # | item | state |
|---|---|---|
| B1 | 456 hubs needing notation normalization | dry-run clean, **not applied** |
| B2 | 156 hubs with prose `scope` values | term choices, not mechanical; **changes `pub_id`**, so must run before re-approval and may collapse hubs |
| B3 | 297 hubs where title ≠ body chunk | unstarted |
| B4 | 2 duplicate pairs to merge (`fi191179`/`fi191260`, `fi191192`/`fi191262`) | identified; both forked on `scope`, not sentence |
| B5 | 6 acquisition markers | remediation blocker 3 |
| B6 | nanobud draft `dr173020` residuals | open |
| B7 | re-approve the nanobud cohort | **human door — last step** |
| B8 | the boxel document's `dr` id | **never obtained; blocks its per-document cohort pass** |

### C. Mechanism

| # | item | note |
|---|---|---|
| C1 | Split the `contradicts` vocabulary | one slug currently carries ≥4 unrelated relationships; the case rests on **naming, not volume** — it holds at zero rows |
| C2 | Deduplication pass using the repaired index | 23 hub pairs under 0.10 cosine look like failed convergences; **now the highest-value use of A1** |
| C3 | Evidence widening (stage 5) | investigation of `hub_refine` / `chase_trigger` / `PRECIS_TAPROOT_CHASE_ENABLED` pending — may be switch-on rather than build |
| C4 | Adjudication-as-nanopub (stage 8) | two-tier model specced; open question whether `pub_id` must hash the two claim ids |
| C5 | `contradiction_confirm` at BIG tier | A2 added a threshold, not a confirmation; `same` and `contradicts` still asymmetric |
| C6 | `links` unique constraint | `docs/backlog/links-no-unique-edge-constraint.md`; **must resolve "can one paper support one claim via two passages?" first** |
| C7 | Hub `scope` has no edit door | pre-existing; interacts badly with B2 since `scope` is in the identity hash |
| C8 | Patent assignee ≠ author | the independence count over-reports for patents, in the flattering direction |

### D. Ops / hygiene

| # | item |
|---|---|
| D1 | `/opt/mcps/venv` (this session's MCP) runs `precis-mcp 8.30.2` and serves a skill corpus two revisions stale — root-owned, needs a user-run reinstall, then an MCP reload |
| D2 | memory reconsolidation overdue (last 2026-08-17) |
| D3 | backlog-lint: one item marked done but still present in `docs/backlog/` |

---

## Part 3 — the concerns worth arguing about

1. **Admissible is not true, and we have no truth-bearing mechanism.**
   Every gate checks well-formedness, sourcing, traceability. The only
   mechanism that could bear on truth is claim-versus-claim disagreement,
   and it has never run. An external review reached this independently:
   the corpus is *"impeccably traced but epistemically flat."*

2. **Retrieval-shaped epistemics.** Both the widening and the opposition
   finder are ANN-retrieval-first. That structurally favours claims phrased
   like their neighbours and misses both independent confirmation and
   genuine contradiction expressed in different vocabulary. Any coverage
   number either produces is a floor.

3. **Scale changes the risk profile of a wrong judge.** At 6 hand-written
   edges, a bad LLM verdict is a nuisance. At 1,524 hubs × k candidates,
   the same error rate is a corpus-wide event. Every automated writer
   needs a confidence floor, an idempotency story, and a dry-run mode
   before its first large run — A2 is one instance of this pattern, not a
   one-off.

4. **The measurement keeps overturning the plan.** In this session alone:
   a lint we were about to build already existed; the diagnosis of the
   `contradicts` count was wrong; the count itself was wrong; a
   "definitely the backfill job" code pointer was two different services.
   Three of four dispatched tasks came back reporting the task was
   mis-specified. **When a plan item is cheap to check against the code or
   the DB, check it before building it** — this has paid off every single
   time it was done and cost real work every time it was skipped.
