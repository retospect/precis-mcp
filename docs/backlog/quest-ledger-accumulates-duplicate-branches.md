# Quest attempt ledger accumulates near-duplicate branches

> Found 2026-08-13 reading dossier 202546's live ledger. Not a data-loss bug —
> the tree keeps everything. The defect is that it keeps the *same thing*
> several times.

## Symptom

The live ledger on dossier 202546 holds ~23 items, of which a substantial
fraction are restatements of each other at different nesting depths:

- "NrfA active-site structural dissection (distal heme pocket residues,
  substrate channel, proton-transfer network)"
- "Identify NrfA active-site residues, heme coordination, and proton-transfer
  network that trap NO intermediates and enforce six-electron selectivity"
- "NrfA active-site structure and mutagenesis (distal heme pocket,
  proton-transfer network)"
- "NrfA active-site structural/mechanistic dissection"
- "Obtain NrfA active-site structural details (distal heme pocket, proton
  network) from literature"

and separately, four variants of "whole-cell / immobilized-enzyme NO conversion
at exhaust-relevant conditions".

These are one attempt each, phrased five ways across five ticks.

## Why it matters

`ledger_do_not_repropose` exists to stop the quest re-proposing what it already
tried. It matches on the ledger's item text. When the same attempt is present
under five different phrasings, a sixth phrasing does not match any of them, so
the guard does not fire — the ledger is *causing* the re-proposal it exists to
prevent. The tree grows, the prompt budget it consumes grows, and its
suppressive value falls.

## Why slice 3 does not fix it

The ledger→chunks migration (shipped in the same thread) makes each attempt node
an individually addressable chunk with an `ATTEMPT:` closed-axis tag. That is
the **precondition** for de-duplication — you can now point at a node, tag it,
supersede it, link it — but nothing in that change compares two nodes for
sameness. The duplicates will simply become duplicate chunks.

## Shape of a fix

Do not reach for a string-similarity threshold first; these restatements are
semantically identical but lexically quite different, and a threshold tuned to
catch them will also collapse genuinely distinct sibling attempts.

Better candidates, roughly in order of cost:

1. **Make `add_attempt` ask.** Before minting a node, show the model the
   existing sibling set and require it to either name the existing node it is
   refining or assert it is new. Cheapest, and it puts the judgment where the
   semantics are.
2. **Embed and cluster.** Nodes are chunks now, so they get embeddings for free
   via the `embed:bge-m3` worker. Flag high-cosine siblings for merge at read
   time rather than blocking the write.
3. **`supersedes` links.** The relation is already seeded
   (`migrations/0007_dreaming.sql`). A refinement links to what it refines and
   the renderer collapses the chain. Preserves history, which matters given the
   ledger's do-not-re-propose role.

Option 1 and option 3 compose well and are probably the right pair.

## Related

- `docs/backlog/dossier-present-tense-refinement.md` — the "work the learning
  in rather than restate it" contract. This ledger is the same failure mode one
  layer down: the narrative was being restated wholesale each tick, and so was
  the attempt tree.
