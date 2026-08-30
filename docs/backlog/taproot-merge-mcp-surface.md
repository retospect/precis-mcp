---
status: draft
title: "expose taproot merge on the MCP surface — with a guard the CLI does not need"
---

# Merge from the web

`precis taproot merge --loser … --winner …` (shipped 2026-08-20) is CLI-only, so
collapsing a duplicate pair means an operator with a prod DSN. The dedup sweep is
a read-review-act loop over pairs a human judges one at a time, and that loop
belongs where the claims are already being read — the web reader — not in a
terminal. Reto's ask, 2026-08-20.

## Why this is not just "wire the verb up"

The CLI is reachable only by someone who already has a prod DSN in hand. The MCP
surface is reachable by **every agent in the cluster**, and merge is the most
destructive door taproot has:

- It **hard-DELETEs** redundant `links` rows. `links` has no `retired_at`; there
  is no undo.
- It **soft-deletes a claim hub**, changing what the corpus asserts.
- It is exactly the **over-merge** direction `eval_canon`'s live gate exists to
  hold at zero — and an automated judge is most likely to over-merge on the very
  bands (numeric near-misses, narrower-vs-broader restatements) where merging is
  wrong. Two of the nine pairs in the first real cohort were do-not-merge, and
  one of those looked like a duplicate until its sources were read.

So the MCP verb must not be a thin passthrough.

## Shape

- **Dry-run is the default.** `put(kind='finding', mode='merge', …)` returns the
  plan — edges to repoint, edges dropped as redundant and which existing edge each
  collides with, publish-state check — and writes nothing. A separate explicit
  confirmation applies it. The plan is the reviewable artifact; the CLI already
  produces it.
- **Refuse past `candidate`**, as the CLI does: merging changes `pub_id`, and a
  reviewed or signed artifact cannot be retroactively re-identified.
- **Decide whether apply is a human door.** `approve`/`sign`/`signoff`/
  `publish --live` are human-only today. Merge is not obviously less consequential
  than approve — it can silently delete a claim. Leaning toward: agents may
  produce and read plans freely; applying one requires the same human door. That
  is a product call, not a technical one.
- **Record who applied it.** `set_by` already exists; make sure the web door
  passes the acting identity rather than a generic `agent`.

## Also worth having

A **list-candidates** read verb — the all-pairs cosine scan over
`finding_body` embeddings, banded — so the sweep is discoverable from the reader
instead of requiring hand-written SQL. That is the half of the loop the CLI does
not cover at all, and it is read-only, so it carries none of the above risk.
Note the strict hub predicate (`claim_hub_predicate_sql`) or it will surface
chase-tree findings as merge candidates.
