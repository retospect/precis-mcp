---
status: draft
pillar: memory-graph
title: Local models keep the mesh — measured per action against two bars (auto-apply, reviewed by a bigger model), one reviewed-by ledger an edit makes stale, and a revision log off a stable head
prio: high
---

# Local models keep the mesh

Reto, 2026-10-03 (via the review session): "evaluate what local compute
can do for mesh upkeep: fix up the mesh (dedupe and merge nodes,
categorise), add links, and add findings." The target is independent
operation, or at least output good enough that a bigger model can review
it cheaply. Co-owned by knowledge-mesh (this file, the ledger, the task
sets) and local-compute (the candidates, serving, the feed's capacity
side).

## Motivation / why

The local box is about to have capacity: castor runs gpt-oss 120B at 290
out tok/s over 32 streams (Slice 0, 2026-10-03), and the graph always has
more upkeep work than anyone does. Nothing measures whether a local model
does that work well enough, and nothing records who checked the result.
Three pieces exist and are not joined:

- **An eval harness that can compare placements.**
  `precis.llm_eval.harness.compare` runs two models over one gold set with
  strict `placement_a`/`placement_b` (a local arm that ran on the cloud
  raises `PlacementMismatch`). Scorers are a registry
  (`llm_eval/scorers.py::SCORERS`); gold sets built from prod live in the
  gitignored `scripts/llm_eval/gold_set/local/` (Reto 2026-10-02: they
  never enter the public repo); `build_summarize_gold.py` is the builder
  pattern.
- **One test set for one action.** The taxonomy agreement test
  (knowledge-mesh-7/8): 100 hubs, cross-run folded-key agreement, bar
  0.711. It lives in session scratch (`norr-her-meta/compare_runs.py`), not
  in the harness, and it measures self-agreement, not correctness.
- **Review state, in four incompatible shapes.** Read in prod 2026-10-03:
  - `chunk_review` (chunk, checker, approved_sha, verdict): 166 rows,
    5 by a human. The only shape with a content sha.
  - `links.meta.verified_by`: 2,352 edges, free strings. `hub-refine` 2160,
    `opus-5/retro-verify` 93, `agent:ga3-grounding-audit-step3` 60, and one
    value that is a sentence about a Reto ruling. No model on the
    `hub-refine` stamps; no version on any.
  - `refs.meta.last_refined_sha/_version` on claim hubs (hub_refine): an
    edit or a `REFINE_VERSION` bump makes the hub due again. No reviewer
    identity.
  - `refs.human_verified_by`: 27 papers, nothing else.

  `links.set_by` is `system` 153k, `agent` 83k, `user` 5. Authorship has
  no model either, so "a local model proposed this, opus approved it" is
  not answerable today.

**Gold is thin where it is human.** Human-approved items number in the
tens: 5 user links, 5 human chunk reviews, 16 anchored or published hubs,
11 recorded hub merges. Machine-verified items number in the thousands.
So most gold below is a frontier model's verdict, which measures
"would the bigger model accept this", the question the reviewed bar
asks. The auto-apply bar needs a small human holdout on top (§1).

## In scope

### 1. EVAL — one graded task set per action

Four task sets, each built read-only from prod by a builder script into
`gold_set/local/`, each with a scorer in `SCORERS`:

| action | task | gold (prod, today) | scorer |
|---|---|---|---|
| categorise | name the measurand key for each mention in a hub | the taxonomy set: the frontier packed run's folded keys on the same 100 hubs; extend to the 300-row run's hubs | folded-key agreement (port `compare_runs.py` into the harness) |
| link proposal | given a node and its top-k embedding neighbours, propose typed edges | the 2,352 verified edges as positives; neighbours with no edge, judged by the frontier, as negatives | precision and recall on (dst, relation) |
| finding extraction | given a paper chunk, state the claims it supports | hubs whose edge to that chunk is verified (`hub-refine` supported); anchored and published hubs weighted up | claim match, graded by the frontier judge on a sample (the grading spend is the reviewer cost below) |
| node merge | given a hub pair, same or different | the 11 recorded merges (`refines` from a retired hub) plus hub-duplicate-reconcile's hand-merged twins as positives; non-merged near neighbours as negatives | accuracy, with false-merge counted apart |

**Human holdout.** 20 items per action (80 in all), labelled by Reto
once, held out and never used to tune a prompt. The shape is
`llm-judge-reliability.md` §4. Without it the auto-apply bar measures
agreement with the frontier, not correctness.

**Candidates.** castor gpt-oss 120B and Nemotron 3 Super (vLLM; Slice 0
throughput done, quality not), melchior glm-4.7-flash (llama-swap, slot
cap 4). The frontier arm is the cloud chain each action runs on today.
The taxonomy task is local-compute's Slice 0 quality check ("km-8
taxonomy first", local-compute Do-next 4b), so it runs once for both
threads.

**What is measured, per action and candidate:**

- acceptance rate: the share of local outputs the reference accepts;
- frontier self-agreement on the same set (test-retest), the ceiling a
  local model can be held to;
- reviewer cost per accepted item: the frontier review spend over all
  local outputs, divided by the accepted count;
- transport errors (any error fails the run, as in the summariser gate).

**Two bars** (proposed; Reto sets the numbers on the first result):

- **Auto-apply.** Agreement with gold ≥ the frontier's own test-retest
  agreement on that set, and the human holdout shows no more errors than
  the frontier makes on it. Only for reversible actions whose prior is
  recorded: categorise and link add. Merge never auto-applies
  (`graph-gardener.md`: "probably not ever for merge and split").
- **Reviewed by a bigger model.** Local plus review beats the frontier
  alone: (local cost + review cost) per accepted item < frontier
  generation cost per accepted item. Local marginal cost is about zero,
  so this reduces to the review being cheaper than the generation, at
  the measured acceptance rate.
- An action below both bars stays on the frontier.

### 2. REVIEW LEDGER — one reviewed-by stamp for chunks, refs and links

**Proposal: one table, not per-kind stamps.** `chunk_review` already has
the right shape and covers chunks only. Generalise it:

```
reviews(target_kind  chunk|ref|link,
        target_id    bigint,
        actor        text  → actors.slug   -- 'reto', 'hub-refine', 'mesh-upkeep'
        model        text  NULL            -- 'openai/gpt-oss-120b' (NULL = human)
        version      text                  -- prompt/rules version, e.g. REFINE_VERSION
        content_sha  text                  -- the target's sha when reviewed
        verdict      text                  -- proposed | approved | rejected
        note         text NULL,
        at           timestamptz)
```

- **Authorship is a row too.** A machine write records
  `verdict='proposed'` with its actor, model and version. "Local proposed,
  opus approved, Reto spot-checked" is then three rows on one target.
- **Edit marks it dirty by sha, not by trigger.** A review is current
  while its `content_sha` equals the target's sha now, and its `version`
  is the current one for that actor. That is hub_refine's rule (sha or
  version mismatch ⇒ due) and curation-gate's stale-if-sha-changed rule,
  applied everywhere. No write path has to remember to invalidate.
- **Per-kind sha, one function registry.** Each target kind supplies its
  sha:
  - chunk: the text hash. `chunks.content_sha` is NULL on body chunks,
    but they are append-only, so the chunk id stands for the content.
  - ref: the existing `claim_sha` for findings; title plus scope for
    others.
  - link: src, dst, relation, chunk ends and the non-bookkeeping meta
    keys. A relation change is remove-then-add today, so a new `link_id`
    starts unreviewed.
  - `links` rows are hard-deleted, so their reviews cascade with them.
- **Requeue is a query.** "Targets with no current approved review" is
  the review lane's input in §3. An edit therefore requeues by changing
  the sha.
- **Folds in what exists.** `chunk_review` rows migrate as
  `target_kind='chunk'`. `links.meta.verified_by` and hub_refine's
  `last_refined_*` are backfilled as rows: actor from the string, model
  NULL where it was never recorded, version `0`. Their writers switch to
  the ledger.

**Why one table over per-kind:** the four shapes above exist because each
kind grew its own. None records model and version, and "who reviewed
what" across them is four queries with three vocabularies. What does
differ by kind is what "the same content" means, and that lives in the
sha registry, not in separate tables.

The migration and its backfill go to the orchestrator as a branch,
never qland (round contract).

### 2b. VERSION HISTORY — every revision keeps the prior state

Reto's add-on (knowledge-mesh-10, 2026-10-03). On every revision, keep
the prior state, with a reason for the change, in an auditable chain off
a stable head. Links always point at the head, and a reviewer can diff
the version they reviewed against the current one. He left the mechanism
open if a better one has the same properties.

**Chosen: a `revisions` log keyed on the head, not snapshot refs.**

```
revisions(target_kind  ref|link,
          target_id    bigint,          -- the head; never changes
          at           timestamptz,
          event        edited|retired|restored|deleted|merged-into,
          actor        text → actors.slug,
          model        text NULL,
          reason       text NOT NULL,
          prev_sha     text,            -- sha of the state below
          new_sha      text,            -- sha after the change
          prev_state   jsonb)           -- the full prior row, plus the
                                        -- replaced body chunks' text
```

**How each property holds:**

| property | how it holds |
|---|---|
| the prior state is kept | `prev_state` is the whole prior row. A body replacement also stores the replaced chunks' text there. |
| an auditable chain | the target's rows in `at` order, linked by `prev_sha` → `new_sha`. `get(..., view='history')` renders each entry as a "previous version" line: when, who, which model, the reason. |
| head id stable, links point at the head | the head is never copied, so no link has anywhere else to point |
| a reason on each entry | `reason` is NOT NULL. `edit` gains a `reason=` arg (finding's `motivation=` is a different field, the hypothesis motivation). Without one, the store writes the verb and actor, e.g. `edit(kind='finding') by mesh-upkeep`. |
| diff reviewed against current | the review's `content_sha` names a `new_sha` in the chain. `view='diff', args={'since': <sha>}` renders that state against now. |

**What counts as a revision is what the sha covers.** A row is written
exactly when the target's content sha changes (§2's sha registry), so:

- bookkeeping meta (`verified_*`, `last_refined_*`, counters) makes no
  history;
- every change a review could be invalidated by makes exactly one entry.

**Written by a trigger, not by each write path.** Two numbers make the
case: 153k system and 83k agent links. Too many paths write refs and
links to trust each one to log.

- An `AFTER UPDATE OR DELETE` trigger on `refs` and `links` writes the
  row when a covered column changed. It compares columns and meta minus
  the bookkeeping keys; one SQL list holds those keys, and a test pins it
  to the sha registry's list.
- The trigger reads the reason, actor and model from
  `current_setting('precis.reason', true)` and its siblings. The store
  sets these with `SET LOCAL`, which is transaction-scoped and safe
  under pgbouncer (never a session `SET`).
- A covered write with no reason set still logs, with
  `reason='(unrecorded)'`. A nightly count of those rows names the write
  paths to fix.

**Why not snapshot refs tagged `history`** (the first form of the
add-on):

- **Every reader would have to skip them.** A snapshot is a ref, so
  search, fisheye rings, embeddings, kind counts and graph-health
  metrics would each need to learn the tag. Any reader that misses it
  shows a duplicate, which is the problem hub-duplicate-reconcile exists
  to remove.
- **Links have no ref to snapshot.** Their revisions would need a second
  mechanism anyway.
- **"Previous version" edges would land in the ring.** They would add
  history to the head's fisheye and link counts.

The log keeps every property and adds no node.

**Fit with what exists:**

- **`chunk_events` is this mechanism already, for draft chunks.**
  - It holds a stable handle, an in-place edit, and an `edited` row with
    `content_sha` and `prev_text`.
  - `revisions` extends the same design to refs and links.
  - Chunk history stays in `chunk_events`, which drives the
    embed/summary cascade; moving it would put that cascade at risk for
    no new property.
  - A single reader, `revision_at(target, sha)`, serves the diff for all
    three target kinds.
- **The append-only body-chunk rule is untouched.**
  - A body revision is still DELETE + INSERT on `chunks`, so the cascade
    re-runs.
  - The ref-level `revisions` row keeps the replaced text in
    `prev_state`.
  - The rule forbids updating a body row in place; it does not forbid
    keeping a copy.
- **Greenfield schema review (knowledge-mesh-6).**
  - A greenfield schema would have one `revisions` and one `reviews`
    table across all targets.
  - Built this way, today's gap is just `chunk_events` being separate.
    That is recorded there as a gap row (fold `chunk_events` into
    `revisions`, after the cascade reads a view), not done here.

### 3. FEED — into the maintenance queue, out through the review lane

- **Work in.** `graph-maintenance-queue.md` is the producer. Each queued
  unit is one action on one target (categorise hub X, propose links for
  node Y). The eval decides which actions are local-eligible and at which
  bar; an action below both bars is never queued for local.
- **Capacity.** `llm-dispatch-feedback-controller.md` pulls maintenance
  units when interactive load leaves headroom; maintenance never overflows
  to the cloud.
- **Output.** Every local result is a `proposed` ledger row.
  - Auto-apply actions apply at once, recording the prior; the review
    still samples them at a set rate.
  - Reviewed actions apply only on an `approved` row.
- **Review lane.** A budgeted cloud consumer of "proposed, or approved
  but stale". The cloud spend is its own line, separate from the
  local-only maintenance lane. Reviewer cost per accepted item is
  reported per action, so the bars can be re-read from production
  numbers, not only from the eval.
- **Human sampling.** Reto sees a fixed sample of approved items per
  week as `waiting-for:reto`, not every proposal.

## Explicitly NOT in scope

- Choosing the serving model or server (local-compute Slice 0) or
  building the controller and queue (their items).
- The gardener's pass logic (`graph-gardener.md`). This item supplies its
  eval, its stamp and its review path.
- Per-run curation (`curation-gate.md`). That gate reviews one run's
  write set; this ledger is what its verdict should write per target.
- Body-chunk prose review (drafts keep `view='review'`, now reading the
  ledger).
- Auto-apply for merge or split, at any score.
- Folding `chunk_events` into `revisions`. That is a greenfield-review
  gap row, done after the cascade reads a view.

## Acceptance criteria

1. Four task sets and their scorers exist; `llm eval --compare` prints,
   per action, acceptance rate, frontier test-retest, reviewer cost per
   accepted item and transport errors, for each of the three candidates.
   No set is committed to the repo.
2. The taxonomy task reproduces the scratch `compare_runs.py` number on
   the two existing frontier runs (0.794 folded on the packed pair) before
   a local run is read.
3. A table states, per action, which bar each candidate clears, and the
   80-item human holdout is labelled and scored.
4. One query answers "who reviewed this target, with which model and
   version, and is it current" for a chunk, a ref and a link.
5. Editing a reviewed link's relevant meta, or a finding's title, makes
   its review not current without any extra write. A bookkeeping-only meta
   change does not.
6. `chunk_review`, `links.meta.verified_by` and hub_refine's stamps are
   backfilled as ledger rows. The hub_refine due rule reads the ledger,
   and its test still passes.
7. A local maintenance result lands as `proposed` and applies only on
   `approved` (reviewed actions). An auto-apply action records its prior
   and can be undone in one call.
8. Editing a finding's title, a memory's body or a link's covered meta
   writes exactly one `revisions` row with the prior state and a reason.
   A bookkeeping-only change (a `verified_at` bump) writes none.
9. Given a review's `content_sha`, `view='diff'` renders that version
   against the current one. The head's id, its links and its fisheye ring
   are unchanged by any number of revisions.
10. A write that sets no reason still logs, as `(unrecorded)`, and the
    nightly count reports it. Hard-deleting a link logs `deleted` with
    its last state.

## Target + blast radius

`src/precis/llm_eval/` (scorers, harness report) and new builders in
`scripts/llm_eval/`. One migration (`reviews` plus the backfill). The
`chunk_review` readers (`executors/claude_inproc.py` review tick, draft
`view='review'`). `workers/hub_refine.py` (due rule, verdict stamp). The
`links.meta.verified_by` writers (hub_refine, verify-edges, the grounding
audit). The `revisions` trigger runs on every UPDATE and DELETE of `refs`
and `links`. That makes it the widest blast radius here: it must never
fail a write, and a trigger error has to log and pass. The maintenance queue and review lane (with local-compute).
Skills: `precis-review-help`, `precis-gardener-help` when it exists.

## Slices

0. **Eval, no schema.** Port the taxonomy scorer, build categorise and
   merge sets first (gold exists), then links and findings. Run on castor
   when Slice 0 serves. Costs only the frontier grading sample. Castor's
   bench server binds 127.0.0.1 and has no `resource_slots` row, so
   `llm_eval` cannot reach it. local-compute opens a serving window (a LAN
   bind plus a temporary slot row, or the harness run on castor) after its
   above-64-stream load test, and pings when it is open.
1. **Ledger and history.** One migration for `reviews`, `revisions` and
   its trigger, plus the sha registry, the backfill, hub_refine on the
   ledger, `edit(reason=)`, and `view='history'`/`'diff'`. A branch to
   the orchestrator.
2. **Feed.** Proposals into the queue, the review lane, the bars applied
   per action. Waits on the queue and the controller existing.

## Open questions / decisions log

- **[decided 2026-10-03, Reto knowledge-mesh-10]** Accepted all three:
  - one `reviews` ledger;
  - the two bars, with auto-apply only for categorise and link-add;
  - the 80-item human holdout.

  The weekly sample replaces per-proposal todos, so `graph-gardener.md`'s
  `waiting-for:reto`-per-proposal rule now applies only to passes no
  bigger model reviews.
- **[decided 2026-10-03, Reto knowledge-mesh-10 add-on]** Version history
  on every revision, with a reason, off a stable head (§2b). Reto left the
  mechanism open. Chosen: a `revisions` log written by a trigger, over
  snapshot refs tagged `history`; the reasons are in §2b.
- **[open]** Whether `reviews` should also replace `refs.human_verified_*`
  (27 rows). Leaning yes, as `target_kind='ref'`, `model` NULL.
