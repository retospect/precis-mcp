---
id: precis-gardener-help
family: memory
title: precis — the graph gardener (reviewed graph upkeep)
summary: the graph gardener files review-ledger proposals over nodes/edges/tags and prunes dangling edges; what a proposal means, how to audit an applied merge, and what to do on meeting one
answers:
  - what is memory gardening / the graph gardener?
  - what is the graph_garden job and what does it change?
  - where do gardener merge/split/relink/prune/tag proposals land?
  - how do I list pending gardener proposals over MCP?
  - how do I reject a gardener proposal over MCP?
  - how do I tell whether the gardener is on?
  - what should I do when I meet a gardener proposal?
  - what is a polysemy finding and when does it trigger a split proposal?
  - why did two sibling taxa get a merge proposal?
  - does a memory record when it was last gardened?
  - how do I audit a merge the gardener applied?
applies-to: get/search (kind='taxon'/'concept'/'memory'/'finding'/'job'), reviews ledger
tags: [workflow]
kinds: [taxon, concept, memory, finding, job]
status: active
---

# precis-gardener-help — the graph gardener

`graph_garden` is a job_type for graph maintenance over nodes, edges and
tags. It files proposals; a consumer step prunes dangling edges and has a
reviewer apply or reject merges and relinks. Every accepted candidate lands
as one row in the reviews ledger: `verdict='proposed'`, `actor='graph-garden'`.
A proposal stays *current* while the target's content is unchanged; editing
the target makes it stale by itself.

Memory judgments share [[precis-memory-help]] §"Memory reconciliation policy"
with [[precis-make-memory]]. Memory detectors and reviewed memory actions:
[[precis-gardener-memory-help]].

## Investigate repeated detector timeouts

A partial `graph_garden` job can succeed while a detector times out.
Inspect `meta.garden.skipped_detectors` and `detector_skip_streaks` on the
job. After three consecutive attempted timeouts, the warning alert
`graph_garden:detector_timeout/<detector>` opens. Repeated timeouts refresh
that alert; a completed scan resets the counter and resolves it. Unrelated
passes, dark/cooled runs and unavailable evidence preserve the counter.
Inspect the named detector's queries and scan bounds before increasing
its deadline.

## What it proposes

Every proposal names its evidence (distances, shared terms, instance ids,
run ids) for a judge to verify.

- **merge** — same-kind node pairs that look like one term minted twice.
  Taxon pairs with a dimension clash (different `dimension_kind`, or both
  `si` with different `si_vector`) are never candidates: different units are
  not dupes. A memory pair is judged on its body, never its title.
- **paper-identity** — duplicate papers with equal normalized titles. Never
  a candidate when the DOIs differ, years are more than one apart, or
  first-author surnames differ. Proposal only: a paper merge is a human
  decision, and needs positive evidence (shared DOI, same `pdf_sha256` or
  near-identical body text); a supplement, corrigendum or preprint-versus-
  published pair is rejected.
- **memory** — memory-body smells (dangling mentions of retired handles,
  thread shape, anchor rot and more). Null replacement text needs
  evaluation; it is never a deletion instruction. Detail:
  [[precis-gardener-memory-help]].
- **memory-review** — proposal-only read-only checks on live memory bodies:
  review debt (`review_due`), title shape (`title_shape`), undated absence
  claims (`negative_undated`) and notation-canon violations (`notation` —
  one anchored fix per memory per pass, skipping quoted spans, code and
  math). Detail: [[precis-gardener-memory-help]].
- **relink** — a `taxon` reaching no start node gets a `specialises`-edge
  proposal naming the nearest start node as candidate parent, only when that
  parent sits past the `+recall` distance floor: inside it the index already
  lists the start node.
- **prune** — a link from a live ref to a retired one (`dangling_edge`); a
  `specialises` 2-cycle; a node untouched, unlinked and unviewed for a long
  while; a machine `related-to` edge that merely duplicates the index.
  `dangling_edge` skips an edge whose both endpoints are retired, a
  provenance relation (supersedes, derived-from, fixes, retracts, corrects,
  cites and their inverses), and a retired `todo`/`agentlog`/`job` endpoint.
  Only a dangling edge is deleted by the consumer; other prune proposals stay
  for human follow-through, and a pruned edge is not undoable by tool.
- **tag-sweep** — a ref of a covered kind missing a closed namespace its live
  siblings already use (`DOMAIN:`, `TOPIC:` …). The proposal carries the
  namespace; the value is the judge's call.
- **split** — a node with an *open polysemy finding* linked to it (a
  `finding` with `meta.annotation='polysemy'`; `meta.uses` names the
  incompatible senses) yields a split proposal naming those uses. A node
  reachable to two different start nodes also surfaces, marked `hint: true`,
  weaker evidence.
- **siblings** — two taxa under one parent on one *exclusive* axis with a
  concrete confusion trace: a shared classified instance, or an instance
  whose `instance-of` edge carried `meta.run='r1'` into sibling A and
  `run='r2'` into sibling B. The proposal offers
  merge/sharpen/re-axis/re-parent. Similar labels alone are never a
  candidate: disjoint siblings are legitimate. A merge between two siblings
  each named in at most two content words that share no word (Pedal/Wheel,
  Height/Width) is rejected outright.

## Freshly reviewed targets are skipped

A candidate whose target was `considered` within the last 30 days is
dropped before the proposer, provided the node is unchanged: the consider
act's `expected_sha` must still equal the node's content sha and the stamp
must still read `considered` (a demote or a content edit re-opens it).
Expiry-driven memory review and reader complaints (`griped`) are exempt.
Reviewers stamp with the structured `kg-review:<model>:
<YYYY-MM-DD>:ok|fixed` reason on `edit(mode='trust')`; the stamp's date
decides freshness (a backdated review stays old).

**Review coverage.** `get(kind='taxon', id='/coverage',
args={'days': 30})` reports per kind: live refs, the share considered in
the window on their current sha, and the breakdown by reviewing model.

## When was a memory last gardened?

There is no per-memory `last_gardened_at`. A clean scan leaves no per-node
trace, so nothing proves a memory's last scan time. Review currency is a
`consider` trust act on the current content (`expected_sha` matching,
`trust_confidence='considered'`, review date from the `kg-review` stamp);
a proposal alone is not a completed review. An applied decision records an
actual change; a merge also names loser and winner. Content changes
invalidate review currency.

## Audit a merge

An applied merge retires the loser, so its handle redirects to the
winner — a plain `get` can no longer show what was folded. Two MCP
doors close that gap:

- `get(kind='job', id='/garden/merges', args={'since': '30d'})` — the
  merge slice of the review ledger, folded by `garden.key`: merge id,
  loser → survivor handles, verdict, proposed/decided times, the
  proposal's title/rationale/evidence, and `undone` (the merge was
  reversed).
- `get(kind='<kind>', id='<loser handle>', view='retired')` — the
  tombstone itself, never the redirect: remaining identifiers, its own
  body chunks (count + first chunk), the links the merge repointed
  (the `meta.garden_merge` reversal record), `superseded_by`, and the
  merge id. `args={'retired': True}` spells the same opt-in. A merged
  paper's re-homed cite key resolves to the survivor — address the
  tombstone by its `pa<id>` handle, which the merges list prints.

## Pending proposals and rejections over MCP

`get(kind='job', id='/garden/proposals', args={'action': 'merge',
'limit': 20})` lists the proposals still awaiting a verdict: per row the
review id, action, target handle, a `stale` flag when the target moved
since the proposal, title, rationale, and any consumer hold with its
next step. `action` and `limit` are optional.

`put(kind='job', mode='garden-reject', args={'review_id': <id>,
'reason': '<why>'})` closes one pending proposal: the terminal
`rejected` row records your reason and actor, and the key stays decided
while the target is unchanged. Rejecting an `apply_error` or
`unresolved` hold instead re-opens the key — that is the retry door
once the held error is fixed. There is no MCP apply or undo: they
repoint the live graph and stay operator-held.

`get(kind='job', id='/garden')` reports `gardener: on|dark` plus
`cooling: yes|no` — whether a `service_config` row lights the pass and
whether a dry-run streak has cooled it.

## The machine-edge write rule

Any link the gardener (or another model-backed pass) writes is gated at
link admission — see `precis-link-help` §Machine-written edges for the full
rule. In short: no `related-to`/`see-also`, no direct structural writes
(those stay proposals), stance/provenance edges plus any other rel need
`meta.evidence` chunk pins and the `meta.{model,prompt_version,run_id,
why}` annotation, and non-stance rels must join endpoints past the
`+recall` distance floor. Merge repointing is exempt (it moves existing
edges), and an approved relink mints one structural edge.

## If you land on a garden proposal while working

For a `project:memory` reader complaint, use
[[precis-fix-memory-gripe-help]]: replay its routes, classify the
disagreement, apply through the ledger, then replay before closing.

Treat `verdict='proposed'` rows as pending evidence, not truth. Do not
apply a proposal's payload by hand: the merge bookkeeping is what makes
undo possible.
