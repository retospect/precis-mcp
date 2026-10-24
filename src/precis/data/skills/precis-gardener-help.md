---
id: precis-gardener-help
family: memory
title: precis — the graph gardener (proposals-only graph upkeep)
summary: scheduled, bounded maintenance over graph nodes/edges/tags that files review-ledger proposals; a consumer step prunes dangling edges and has Opus apply or reject merges
answers:
  - what is the graph_garden job and what does it change?
  - where do gardener merge/split/relink/prune/tag proposals land?
  - how do I review or act on a gardener proposal?
  - what do `precis garden review` and `precis garden stats` do?
  - how do I turn the gardener on?
  - what is a polysemy finding and when does it trigger a split proposal?
  - why did two sibling taxa get a merge proposal?
applies-to: get/search (kind='taxon'/'concept'/'memory'/'finding'), reviews ledger
tags: [workflow]
kinds: [taxon, concept, memory, finding, job]
status: active
---

# precis-gardener-help — the graph gardener

`graph_garden` is a job_type that keeps the knowledge graph honest —
proposals only. It never merges, splits, prunes, relinks or retags on
its own. Every accepted candidate lands as one row in the reviews
ledger: `verdict='proposed'`, `actor='graph-garden'`, the machine
payload JSON in `note` (`garden.action`, `garden.evidence`,
`garden.payload`, `garden.rationale`). A row stays *current* while its
`content_sha` matches the target now; an edited target makes the
proposal stale by itself.

## What it scans

One bounded tick per minted job (`job_inproc` executor), `params.pass`
selects the detector subset (`merge|split|relink|prune|tag-sweep|
siblings|paper-identity`, one name or a list, default `all`):

- **merge** — same-kind node pairs that look like one term minted
  twice. taxon/concept compare their `card_combined` embeddings and
  normalized names/aliases. A memory compares its `memory_body`
  embedding plus a body hash (byte-identical bodies), never its title:
  memory cards are the title cut to 80 chars and dream memories share
  an opening line, so titles put distinct memories at distance 0. Taxon
  pairs with a dimension clash (both `dimension_kind` set and different,
  or both `si` with different `si_vector`) are never candidates —
  different units are not dupes. A taxon/concept pair's evidence keeps
  names and definitions apart: `name_overlap`, `definition_overlap`,
  `definition_template` (both definitions are one placeholder with a
  different name in front) and `siblings_under` (the shared parents).
- **paper-identity** — duplicate papers; see *Paper identity* below.
- **relink** — a `taxon` reaching no start node gets a
  `specialises`-edge proposal naming the nearest start node by card
  embedding as candidate parent — but only when that parent sits past
  the `+recall` distance floor (`RELINK_MIN_DISTANCE` = 0.6): inside
  it the index already lists the start node, so the proposal would
  restate what retrieval finds on demand. An orphan with no card
  vector is always kept (the index has nothing to offer it).
- **prune** — a link from a live ref to a retired one (`dangling_edge`);
  a `specialises` 2-cycle (proposes deleting the younger edge); a node
  untouched, unlinked and unviewed for `stale_days` (default 180); and a
  machine `related-to` edge (`trust_origin` `generated`/`computed`)
  whose endpoints are mutual top-5 card neighbours inside
  `PRUNE_MAX_DISTANCE` — the edge duplicates the index, so the proposal
  is removal. `dangling_edge` skips an edge whose both endpoints are
  retired (invisible to live reads), a provenance relation
  (`PROVENANCE_RELATIONS`: supersedes, derived-from, fixes, retracts,
  corrects, cites and their inverses — still true after a retire), and
  a retired `todo`/`agentlog`/`job` endpoint (history of how the live
  node was made). `redundant_link` probes neighbours in batches,
  closest pairs first, and stops at `limit` candidates or a 40 s probe
  budget, returning what it found. A `prune` proposal is never applied
  by code: apply stays merge-only, the row is for human follow-through.
- **tag-sweep** — a ref of a covered kind missing a closed namespace
  its live siblings already use (`DOMAIN:`, `TOPIC:` …). The proposal
  carries the namespace; the value is the judge's call.
- **split** — a node with an *open polysemy finding* linked to it (a
  `finding` with `meta.annotation='polysemy'`; `meta.uses` names the
  incompatible senses) yields a split proposal naming those uses.
  A node reachable to two different start nodes also surfaces — marked
  `hint: true`, weaker evidence.
- **siblings** — two taxa under one parent on one *exclusive* axis with
  a concrete confusion trace: a shared classified instance, or an
  instance whose `instance-of` edge carried `meta.run='r1'` into sibling
  A and `run='r2'` into sibling B. The proposal offers
  merge/sharpen/re-axis/re-parent. Similar labels alone are never a
  candidate — disjoint siblings are legitimate.

Every proposal names its evidence (distances, shared terms, instance
ids, run ids) so a judge can verify it without trusting the model. The
proposer's prompt also carries an excerpt of each named ref's own text
(memory body, else card, else title; up to 600 chars each), so it
judges content, not titles.

## Paper identity

The `paper-identity` detector pairs live main-text papers whose
normalized titles are equal (lowercase, punctuation collapsed; titles
under 20 chars such as "Editorial" are skipped; supplements and
front/back matter are skipped). The higher ref_id is the proposed loser.
Never a candidate when both papers carry different DOIs, years more
than one apart, or different first-author surnames. Evidence lists
DOIs, years, first authors and whether the PDF hash matches. Proposal
only: a paper merge is a human decision.

## Scan windows and budget

**Scan windows.** The wide scans (merge, tag-sweep, redundant_link,
paper-identity) read one window per kind per run, starting past a
cursor saved on the run's `meta.garden.cursors` (`dup_pair:<kind>`,
`untagged:<kind>`, `redundant_link`, `paper_identity`). The next run
starts where the last stopped and wraps at the end, so successive runs
cover every kind; a run over fewer kinds carries the other cursors
forward. The merge window shrinks as a kind grows (each window ref is
compared with every live ref of its kind, about `scan_cap`² comparisons
per kind). A detector skipped on its deadline keeps its old cursor.

**Budget.** `max_proposals` is shared round-robin across the detectors
that found candidates: each gets `max_proposals // n` first, and a
share a detector leaves unspent goes to the others.
`meta.garden.proposals_by_detector` records the split.

**Freshly reviewed targets are skipped.** A candidate whose target was
`considered` inside the last `considered_days` (default 30) is dropped
before the proposer — provided the node is unchanged: the consider
act's `expected_sha` must still equal the node's content sha and the
stamp must still read `considered` (a demote or a content edit re-opens
it). Reviewers stamp with the structured `kg-review:<model>:
<YYYY-MM-DD>:ok|fixed` reason on `edit(mode='trust')` — the stamp's
date decides freshness (a backdated review stays old), absent it the
act's recording time. The tick reports the drop count as
`meta.garden.skipped_considered`. `considered_days: 0` disables the
skip.

**Review coverage.** `get(kind='taxon', id='/coverage',
args={'days': 30})` reports per kind: live refs, how many were
considered inside the window on their current sha, the share, and the
breakdown by reviewing model — the answer to "how much of the graph has
a reviewer actually looked at, and who".

## The ledger row

Pending proposals are readable by operator CLI (`precis garden
proposals`) and by SQL on `reviews` — filter `actor='graph-garden'`,
`verdict='proposed'`. A later `approved`/`rejected` row with the same
`garden.key` resolves it. The same key is never re-proposed while
pending, nor after a `rejected` verdict while the target is unchanged
(its sha still matches the rejected row; counted in
`meta.garden.skipped_rejected`). Editing the target, or an `approved`
verdict, re-opens the key.

A candidate the proposer turns down also gets a `rejected` row
(`actor='graph-garden'`, `garden.proposer_reject: true`, `garden.prompt`
= the proposer's prompt version), so the next run skips it instead of
asking again. It blocks only under the same prompt version: a new
prompt re-asks once. These rows answer no proposal and are not in
`precis garden proposals`.

## The consumer (after every run)

Reto's 2026-10-23 ruling (gr506044): each `graph_garden` run ends with
a consumer step (`params.consume`, default on; `review_limit` Opus
verdicts per run, default 10):

- **Auto-prune** — a pending `dangling_edge` proposal whose link still
  meets the detector predicate (exactly one retired endpoint, retired
  kind not a history kind, not provenance) is applied: the link is
  deleted and the proposal resolved `approved` with
  `garden.applied='prune'` and the deleted row in `garden.link`.
  Prunes are not undoable by tool. Predicate failures close as
  `not_dangling`; a human-authored edge closes as `human_edge`.
- **Opus review** (`graph_garden:consume`, FRONTIER, bounded by
  `review_limit` *attempts*, stopping after 3 consecutive errors) of
  pending merge and relink proposals. Merge proposals must carry
  `payload.shas` (both endpoints' content shas, pinned by the
  detectors) — unpinned ones close `stale` without a call. Approve on
  a merge re-verifies the pins and the dup floor inside the apply
  transaction, then applies through the merge
  door below (`actor='garden-consumer'`, undoable). Approve on a
  relink applies through `apply_relink` (gr507641): the resolution row
  lands first in the same transaction and the edge — child → parent,
  `specialises` or another structural relation — goes through
  `store.add_link` keyed on that approved review row (the one door the
  machine-edge gate admits for structural rels), carrying
  `meta.garden_relink` so `precis garden undo <relink_id>` deletes it.
  Relink proposals approved before the door existed (the recorded
  `garden_review` rows) are applied by the consumer each tick, bounded
  by `review_limit`. Reject writes a
  terminal `rejected` row with `garden.reason`: `distinct_terms`,
  `specialisation`, `placeholder_definition`, `wrong_target`,
  `insufficient_evidence` or `other`.
- **Short-sibling rule** — a taxon merge between two siblings under
  one parent, each named in at most two content words, sharing no name
  word (Pedal/Wheel, Height/Width, Hose/Pipe) is rejected without a
  model call, reason `sibling_distinct_names`, both in the run's
  proposer step and by the consumer.

- **Paper identity gate** — a merge of two papers needs positive
  evidence: a shared DOI, the same `pdf_sha256`, or body-text shingle
  Jaccard ≥ 0.8. A supplement, corrigendum, or a preprint id on one
  side against a published record on the other (no shared DOI) closes
  `rejected` with reason `supplement`, `corrigendum` or `version_pair`.
  An ambiguous pair (`version_unclear`, `venue_differs`) or a
  title/author-only one (`title_author_only`) is held for a human
  (consumer `garden_review` row, `held=<reason>`), never applied by the
  consumer. Checked before the model call and again inside the apply
  transaction.

`precis garden stats` lists reject reasons per detector. The ledger
has no `applied` verdict, so applied = `approved` + `garden.applied`;
every consumer decision row also carries `garden.outcome`
(`applied`/`rejected`), `loser`/`winner`, `detector`, `why`, `by_model`
and the proposal's `evidence` (plus `identity` on a paper merge).

## Applying a proposal by hand

The explicit door is `precis garden apply <review_id>`
— merge only for now: it repoints every link off the loser onto the
winner (each repointed edge keeps `meta.garden_merge`), folds the loser's
name into the winner's aliases, stamps `meta.superseded_by`, retires the
loser, and records an `approved` review row. `precis garden undo
<merge_id>` restores both nodes and every repointed edge — the loser's
`meta.garden_merge` carries the full reversal record; `precis garden
undo <relink_id>` deletes the edge a reviewed relink wrote
(`links.meta.garden_relink`). Split, prune and tag proposals today are
for human follow-through (the apply paths are
deliberately narrower than the proposal space).

## The sampled reviewer

`precis garden review` burns down the open-proposal backlog: up to
`--per-day` (default 20) pending, still-current proposals older than
`--min-age-days` (default 3), oldest first, global over detectors. Each
sampled proposal gets one routed Opus verdict (`graph_garden:review`,
steerable via `llm.op.*`) — approve/reject plus a one-line why —
recorded as a NON-TERMINAL reviews row (`actor='garden-reviewer'`,
payload under `note.garden_review`, never `note.garden`). The first
`--fable-per-day` (default 5) of the day's sampled proposals also get
an independent Fable re-review (`graph_garden:review_fable`,
`actor='garden-reviewer-fable'`, same payload shape) so Opus's verdicts
get a cross-check. Everything stays open for a human: a sampled merge
proposal is never resolved or applied by either path, and a proposal
is sampled at most once.

`precis garden stats` reports the outcome per detector: the pending
count, the sampled count, and two agreement rates side by side —
proposer–Opus (the share of sampled proposals Opus approved) and
Opus–Fable (the share of paired proposals where Fable matched Opus).
They are the proposer's quality bar (a detector Opus keeps rejecting
is noise; a systematic Opus–Fable split means the bar itself needs a
look); reporting only, no gate. No scheduler change: `review` runs
when an operator runs it.

## Scheduling

The gardener is driven by an operator-seeded **recurring todo** under
Watches — a `meta.schedule` (`{'every': '2h'}`) plus
`meta.executor='job_inproc'` and `meta.job_type='graph_garden'`, the
job-mode automation shape (`precis-recurring-help`,
`precis-automations`). Each due tick spawns a subtask the dispatch pass
mints the job from; the spawner's `spawned_for_tick` stamp makes a tick
idempotent and its collision-skip holds the next tick while the
previous child is still open, so runs can't pile up.

The two gates are evaluated **per run**, inside the job's dispatch —
not by the schedule:

- **dark** — no `service_config` row for `graph_garden` with
  `prio >= 1` on any host: the run writes `meta.garden.skipped='dark'`
  and exits. To turn the gardener on, ask a human operator to light the
  `graph_garden` service.
- **cooled** — after `DRY_RUNS_ESCALATE` (36) consecutive
  zero-proposal runs the pass cools for `COOL_RUNS` (84) runs
  (`meta.garden.skipped='cooled'`): the old "dry rest", counted in runs
  rather than days. A productive or failed run resets the streak.

`watchdog:cadence/graph_garden` watches the recurring's spawn events
(`ref_events(source='schedule')`) plus the newest job: more than one
missed tick, a spawn with no job after it, or a newest job
`STATUS:failed` all read stale.

The proposer's model is steerable via the operation row
`llm.op.graph_garden:propose` — a tier or chain override flips where
judging runs without a redeploy.

Job params: `pass` (a name or a list), `limit` (candidates per
detector, default 50), `scan_cap` (refs per kind per window, default
2000), `max_proposals` (writes per tick, default 20, shared across
detectors), `stale_days`, `considered_days` (fresh-review skip window,
default 30, 0 disables), `kinds`, `tag_namespaces`, `tag_kinds`,
`exclusive_axes`.

## The machine-edge write rule

Any write the gardener (or another model-backed pass) makes through
`store.add_link` under a machine-writer context is gated at the link
admission — see `precis-link-help` §Machine-written edges for the full
rule. In short: no `related-to`/`see-also`, no direct structural writes
(those stay proposals), stance/provenance edges plus any other rel need
`meta.evidence` chunk pins and the `meta.{model,prompt_version,run_id,
why}` annotation, and non-stance rels must join endpoints past the
`+recall` distance floor. Merge repointing (`precis garden apply`/`undo`)
is exempt — it moves existing edges, it does not mint new ones — and a
relink apply mints one structural edge admitted on the approved review
row the proposal resolved under (`meta.garden_relink.review`).

## If you land on a garden proposal while working

Treat `verdict='proposed'` rows as pending evidence, not truth. To
dispute one, `precis garden reject <review_id>` retires it (a `rejected`
row with the same `garden.key`), or leave it for the review queue. Do
not apply its payload by hand outside `precis garden apply` — the merge
bookkeeping is what makes undo possible.
