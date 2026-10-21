---
id: precis-gardener-help
family: memory
title: precis — the graph gardener (proposals-only graph upkeep)
summary: scheduled, bounded maintenance over graph nodes/edges/tags that files review-ledger proposals — never auto-applies
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
siblings`, default `all`):

- **merge** — same-kind node pairs whose `card_combined` embeddings or
  normalized names collide. Taxon pairs with a dimension clash (both
  `dimension_kind` set and different, or both `si` with different
  `si_vector`) are never candidates — different units are not dupes.
- **relink** — a `taxon` reaching no start node gets a
  `specialises`-edge proposal naming the nearest start node by card
  embedding as candidate parent — but only when that parent sits past
  the `+recall` distance floor (`RELINK_MIN_DISTANCE` = 0.6): inside
  it the index already lists the start node, so the proposal would
  restate what retrieval finds on demand. An orphan with no card
  vector is always kept (the index has nothing to offer it).
- **prune** — a link whose endpoint ref is retired; a `specialises`
  2-cycle (proposes deleting the younger edge); a node untouched,
  unlinked and unviewed for `stale_days` (default 180); and a machine
  `related-to` edge (`trust_origin` `generated`/`computed`) whose
  endpoints are mutual top-5 card neighbours inside
  `PRUNE_MAX_DISTANCE` — the edge duplicates the index, so the proposal
  is removal. A `prune` proposal is never applied by code: apply stays
  merge-only, the row is for human follow-through.
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
ids, run ids) so a judge can verify it without trusting the model.

## The ledger row

Pending proposals are readable by operator CLI (`precis garden
proposals`) and by SQL on `reviews` — filter `actor='graph-garden'`,
`verdict='proposed'`. A later `approved`/`rejected` row with the same
`garden.key` resolves it; the same key is never re-proposed while
pending, and a stale key (target edited since) may be re-proposed.

## Applying a proposal

Never automatic. The explicit door is `precis garden apply <review_id>`
— merge only for now: it repoints every link off the loser onto the
winner (each repointed edge keeps `meta.garden_merge`), folds the loser's
name into the winner's aliases, stamps `meta.superseded_by`, retires the
loser, and records an `approved` review row. `precis garden undo
<merge_id>` restores both nodes and every repointed edge — the loser's
`meta.garden_merge` carries the full reversal record. Split, prune and
relink proposals today are for human follow-through (the apply paths are
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

The `graph_garden` scheduler cadence mints at most one job per UTC day
(`idem_key='garden:<date>'`). It stays dark — mints nothing — until a
`service_config` row for `graph_garden` carries `prio >= 1` on any host.
After three consecutive zero-proposal runs the mint backs off to a
weekly interval (the "dry rest" — a healthy empty graph should cost
nothing). The proposer's model is steerable via the operation row
`llm.op.graph_garden:propose` — a tier or chain override flips where
judging runs without a redeploy.

Job params: `pass`, `limit` (candidates per detector, default 50),
`scan_cap`, `max_proposals` (writes per tick, default 20), `stale_days`,
`kinds`, `tag_namespaces`, `tag_kinds`, `exclusive_axes`.

## The machine-edge write rule

Any write the gardener (or another model-backed pass) makes through
`store.add_link` under a machine-writer context is gated at the link
admission — see `precis-link-help` §Machine-written edges for the full
rule. In short: no `related-to`/`see-also`, no direct structural writes
(those stay proposals), stance/provenance edges plus any other rel need
`meta.evidence` chunk pins and the `meta.{model,prompt_version,run_id,
why}` annotation, and non-stance rels must join endpoints past the
`+recall` distance floor. Merge repointing (`precis garden apply`/`undo`)
is exempt — it moves existing edges, it does not mint new ones.

## If you land on a garden proposal while working

Treat `verdict='proposed'` rows as pending evidence, not truth. To
dispute one, `precis garden reject <review_id>` retires it (a `rejected`
row with the same `garden.key`), or leave it for the review queue. Do
not apply its payload by hand outside `precis garden apply` — the merge
bookkeeping is what makes undo possible.
