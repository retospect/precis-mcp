---
status: ready
prio: high
---

# Two gripe clusters that no thread ranks

Found 2026-09-30 while sweeping the 39 gripes the fix_gripe lane had parked at
`in_review` behind branches that never existed (gr458326). Every programme in
`threads/INDEX.md` — pcb, se, serving, knowledge, platform — has an owner and a
ranking. These eleven gripes fall between them, so nothing decides when they
get done, and a gripe nobody ranks is a gripe nobody does.

This is a **coverage** item, not a work item: the ask is to give each cluster a
home, not to fix the eleven bugs.

## Cluster 1 — ingest / fetch pipeline (6 gripes)

| gripe | what |
|---|---|
| gr228652 | ingest silently destroys μ/Greek from publisher PDFs that lie about font encoding |
| gr228699 | corpus PDFs lose Greek/micro at the text layer; no OCR cheaply recovers it |
| gr453859 | the OA fetch backlog has no "no OA copy exists" state, so paywalled stubs are indistinguishable from untried ones and the count lies |
| gr453860 | a paper can carry `pdf_sha256` and zero body chunks indefinitely, and nothing records or counts that state |
| gr453862 | ref 448193 sat as `no_oa_version` while its arXiv copy was one GET away |
| gr453913 | arxiv_html markup ingest loses the arXiv identifier, so every arXiv-HTML fold fails at `make_paper_id` |

The `knowledge` programme covers taxonomy, quests and papers — the layer that
*consumes* this pipeline, not the pipeline. Nothing owns extraction and fetch.

Two of these are silent-corruption bugs (gr228652, gr228699): they do not fail,
they produce a corpus that is quietly wrong, and everything downstream —
embeddings, findings, cites — inherits it. That is the worst shape a bug can
have in a research corpus, and it has been open since roughly 2026-08-21.

Why it matters beyond the six: this is the pipeline that feeds pre-search,
condensation and KG additions, which is where the local-compute plan expects to
spend its capacity.

## Cluster 2 — job lifecycle / unpark (5 gripes)

| gripe | what |
|---|---|
| gr452203 | doctor asks never dedup: 161 open, 161 unique ask keys, `seen_count=1` on every one |
| gr452384 | fix_gripe agent runs hit the bare `max_turns=20` default and feed child-failed-parked with no escalation |
| gr454480 | fix_gripe reports a clean exit with no commits as a failure, so already-fixed gripes burn retries and park |
| gr454792 | neither documented path unparks a child-failed-final leaf; the only working recipe is undocumented |
| gr456240 | unpark attempts are consumed by infrastructure failures, permanently latching leaves for reasons unrelated to their fix |

Nearest to `platform`, which owns deploy and monitors, but the nursery/job
substrate is not in any thread's Do-next. These compound: gr456240 latches
leaves for infra reasons, gr454792 means nobody can unlatch them, and gr452203
buries the evidence in undeduped asks.

Note two of the five (gr452384, gr454480) are about the fix_gripe lane, which
Reto has ruled stays inert (gr458326). They are not dead — the diagnosis in
each still describes a real defect in the job substrate around it — but they
should be re-read in that light before anyone spends effort on them.

## The ask

Decide an owner for each cluster. Three shapes, and this wants a choice rather
than a default:

1. **Fold into an existing programme** — cluster 1 into `knowledge` (widening
   it from "papers" to "papers and the pipeline that makes them"), cluster 2
   into `platform`. Cheapest; risks burying eleven items under threads that
   already have ranked queues.
2. **A new thread each.** Honest about the fact that these are distinct
   concerns with distinct code, and gives each a Do-next. Costs two more
   thread files to keep current.
3. **Rank them nowhere and close what is stale.** Several are weeks old; some
   may be obsolete or already fixed in passing. A triage pass that closes the
   dead ones might shrink eleven to a handful small enough for (1).

Recommend (3) then (1): find out how many are still real before deciding they
need a structure.

## Status 2026-09-30 (product-plan review, same day)

Both clusters were ranked while this item was in flight, which answers
"nowhere" but not "whose":

- **Cluster 1** sits as one Parked entry in `threads/local-compute.md`
  ("ingest pipeline — no thread owns it yet"), because pillar 3
  (`docs/roadmap.md`) consumes this pipeline. That is a holding position,
  not an owner. The choice above is still open; the review's lean is your
  (3) then (1), with `local-compute` rather than `knowledge` as the fold
  target since the ingest cost lands on local capacity.
- **Cluster 2** is ranked: gr452203 in `threads/monitors-that-go-quiet.md`
  Do next; the fix_gripe four (gr452384, gr454480, gr456240 + gr458326) as
  one Parked entry there with Reto's inert ruling as the unpark condition;
  gr454792 in `threads/roadmap-quest.md` Horizon. Nothing further for
  cluster 2 here.
