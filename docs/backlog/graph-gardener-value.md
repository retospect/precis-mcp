---
status: draft
pillar: memory-graph
title: Gardener value — a link is written only when retrieval cannot recompute it, every machine edge carries a fixed annotation, and a per-quest retrieval eval decides whether a pass stays on
prio: high
model: opus
---

# Gardener value: write only what RAG cannot recompute, and measure it

Reto, 2026-10-21: "If a fisheye finds things already with a RAG call, will
a specific link actually help? What is the value add of the graph
gardener, have we done this right?" Then: run the gardener on castor
(openai/gpt-oss-120b) for a few days, make the changes below, and find
what else is worth doing. Builds on [graph-gardener](graph-gardener.md)
(v1 shipped, lit on prod 2026-10-21: `service_config` melchior
`graph_garden` prio 1, proposer routed to castor by the
`llm.op.graph_garden:propose` local chain),
[knowledge-mesh](knowledge-mesh.md) §4 (the per-quest mesher) and
[local-mesh-upkeep](local-mesh-upkeep.md) (per-action bars, reviews
ledger).

## The argument (binding)

A stored edge adds value only when it holds something retrieval cannot
recompute on demand:

1. **Stance** — `supports`, `disputes`, `explains`, `qualifies`. Two
   papers that disagree on a barrier embed almost identically; similarity
   cannot tell agreement from contradiction.
2. **Distant connection** — a mechanism in one field that explains a
   result in another usually sits below the retrieval cutoff, so RAG never
   co-retrieves the pair.
3. **A settled judgment** — who checked the edge, against which content,
   when. Without it every agent re-derives the same judgment from raw
   chunks, in tokens, possibly differently.
4. **Provenance** — `derived-from`, `establishes`: not inferable later.
5. **Identity** — merging duplicates removes retrieval noise; this is
   most of what gardener v1 does.

A `related-to` edge between embedding neighbours adds nothing: it
duplicates the index and makes the fisheye (which renders links) noisier.

## Slices

1. **Machine-edge write rule + annotation.** One gate every machine
   writer passes (gardener apply, mesher, scout, any `set_by='agent'`
   write with a model): the edge is refused unless
   - `rel` is not `related-to` / `see-also` (those stay human-only), and
   - it names at least one evidence chunk (`pc…`) in `meta.evidence`, and
   - either the rel is a stance/provenance rel, or the endpoints' card
     cosine is below the retrieval cutoff the fisheye's `+recall` rung
     uses (so RAG would not co-retrieve them).
   The annotation is NOT a new shape: it is the decided trust contract
   of [graph-node-trust-state](graph-node-trust-state.md) (origin,
   grounding, review, confidence, plus writer model/version/run
   identities). A machine edge writes `origin=generated`, writer model +
   prompt version + run id, `grounding=sourced` pinned to the evidence
   chunk(s), `review=unreviewed`, `confidence=hunch` (considered needs a
   recorded act, never a model's own score). The only addition is one
   `links.meta.why` sentence, the reason a reader cannot get from the
   pin alone. No numeric confidence (the contract forbids inferring
   considered from a probability). Tests: each refusal by name; an
   accepted stance edge round-trips its trust fields and `why`.
2. **Retrieval eval (the value measure).** `precis.llm_eval` gains a
   `retrieval` task: for one quest, a gold set of its rubric questions
   with the chunks that answer them (built from prod, gitignored like the
   other gold sets). Arms: plain `search` (RAG), RAG + `fisheye+1hop`,
   and the same after a gardener/scout pass. Scores: recall@k of gold
   chunks and tokens consumed to reach them. Pilot quest: qu500066
   (kinetics). A pass stays on only if it moves recall or tokens.
3. **Gardener v1 corrections.**
   - Drop `relink`'s nearest-embedding `specialises` proposal for
     taxa whose nearest start node the index already ranks first (it
     restates RAG); keep it where the orphan reaches nothing.
   - New `prune-redundant` detector: machine `related-to` edges whose
     endpoints are mutual top-k neighbours in the index → propose
     removal (the fisheye gets quieter, nothing is lost).
   - Ledger burn-down: a reviewing model (Opus/Fable tier, sampled)
     records `approved`/`rejected` on proposals older than 3 days; the
     agreement rate per detector is castor's bar per
     local-mesh-upkeep. Merge stays the only auto-apply path.
4. **Per-quest scout on castor** (knowledge-mesh §4 made concrete): one
   tick per active quest reads `fisheye+2hop` around the quest, picks the
   thinnest rubric axis, and does one of: widen (paper search), read
   (queue the reader on held papers for that axis), stance (for chunk
   pairs on the axis that are near in the index but state different
   numbers → a `disputes`/`supports` proposal), bridge (for a held
   finding, search other quests' chunks below the cutoff for a mechanism
   that explains it → an `explains` proposal), finding (mint a `finding`
   from a chunk with `establishes` + measure). All writes pass slice 1;
   findings and stance edges stay proposals until the reviewer bar is
   met.

## Other passes worth having (ranked, not yet sliced)

1. **Numeric conflict → dispute** (knowledge-mesh §3): the same
   measurand stated with incompatible values in two chunks is the
   highest-value stance edge and is mostly deterministic (unit-normalised
   comparison); the model only writes the `why`.
2. **Hub summaries per cluster the scout keeps landing in**, linked
   `derived-from`, so a quest's fisheye shows a handful of summaries
   instead of 181 papers (quest-graph-as-dossier). Measured by slice 2's
   token score.
3. **Paper identity**: duplicate paper refs (preprint vs journal, stub vs
   held) merged through the gardener's merge path; directly cuts
   retrieval noise.
4. **Fisheye ranking, not more edges**: render evidential and reviewed
   edges first, collapse unreviewed generated ones to a count. Cheaper
   than pruning and immediately visible.
5. **Question-driven gaps**: from each quest's rubric, list the questions
   slice 2's eval cannot answer from the graph; those become the scout's
   widen/read targets instead of the thinnest-axis heuristic.

## Watch (running now)

A 6-hourly check in the chat desk session reads gardener runs, proposals
and `graph_garden:propose` call stats, and files gripes per class: model
confusion, skill confusion, API, flow, token efficiency. Findings feed
slice 3.

## Explicitly NOT in scope

Auto-applying anything but merge; new relation types; the paper
full-text acquisition problem (someone else's); cloud fallback for the
castor chain (failures should be visible while castor is on trial).

## Target + blast radius

`src/precis/links` write path (the gate), `src/precis/graph/garden/`
(detectors, reviewer pass), `src/precis/llm_eval/` (retrieval task),
a new scout pass beside the gardener service. Skills:
`precis-gardener-help`, `precis-relations` (machine-edge rule),
`precis-link-help`.

## Open questions (Reto)

- Q1 Where does the gate live relative to trust-state slice 1/2a
  (trees `mesh-trust`, `trust-2a`): in the same link-write admission, or a
  separate machine-writer check? Recommended: same admission path, so
  there is one door.
- Q2 Reviewer tier for the burn-down: Opus sample of 20/day, or Fable?
</content>
</invoke>
<invoke name="Bash">
<parameter name="command">grep -n "## \|^[0-9]\." docs/backlog/threads/knowledge-mesh.md | head -30