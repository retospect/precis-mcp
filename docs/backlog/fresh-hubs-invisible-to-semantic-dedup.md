---
status: draft
title: "a just-minted claim hub is invisible to semantic dedup until the next hourly embed_batch — the dedup-before-mint rule has a ~1 hour blind window"
---

# Fresh hubs are invisible to semantic dedup for up to an hour

Found 2026-08-22, right after minting fi237847.

## The gap

Ref-level search gained a semantic leg in `b634155c`, which is what finally
made the standing rule — *search for a proximate hub before minting; strengthen,
don't duplicate* — enforceable. But the semantic leg reads
`chunk_embeddings`, and chunk embedding is **demand-driven**, not eager:
`embed:bge-m3` is manual-only (`cli/worker.py`), and prod drains the queue
through the demand materializer's `embed_batch` / `job_inproc` path.

Observed cadence of `embed_batch` on 2026-08-22: 09:53, 10:17, 11:20, 12:09,
13:01, 14:14 — roughly hourly, all `succeeded`. fi237847's `finding_body` chunk
was created 14:50 and was still unembedded at 16:47.

So a hub minted at T is not semantically findable until the next batch, up to
~1 h later (longer if a deploy bounce delays the materializer — see
`deploy-drain-wait-is-a-silent-noop.md`). During that window:

- `search(kind='finding')` finds it only if the **title-lexical** leg matches,
  which is the AND-over-title behaviour the hybrid work existed to stop relying
  on. A paraphrase query will not find it.
- Two agents minting near-duplicate claims inside the same window cannot see
  each other's hub semantically — exactly the duplicate-hub failure the rule
  exists to prevent, and the mechanism behind existing pairs like
  fi191259/fi191268.

Confirmed live: `"how does neck length affect the conductance plateau in a
nanobud"` — the claim fi237847 states almost verbatim — returned three other
hubs and not fi237847.

## Why it isn't just "wait an hour"

The dedup gate is the one place where staleness is not benign. Everywhere else
a missing embedding costs recall, and the answer arrives late. Here it costs
**correctness of the corpus**: the miss is silent, and its consequence (a
duplicate hub with its own `pub_id` and its own evidence edges) is expensive to
undo — merging hubs is a manual adjudication, which is why the remediation
backlog has a dedup pass at all.

## Options

1. **Embed hub chunks synchronously at mint.** A claim hub is one short
   sentence — a single embed call, not a batch. `mint_hub` /
   `refine_claim_sentence` already write inside one transaction; enqueueing a
   priority embed (or calling the embedder directly, degrading to the queue on
   failure) closes the window entirely for the kind that needs it most. Note
   the standing rule that ingest must not call `fill_embeddings` — this would
   need to be a deliberate, hub-only carve-out via the worker/job path, not a
   general loosening.
2. **Prioritize `finding_body` in the materializer** so hub chunks jump the
   queue, shrinking but not closing the window.
3. **Make the blind window explicit at the mint door.** Have `direct-mint` (and
   the `put(kind='finding')` hub path) report how many candidate chunks are
   unembedded, or refuse to claim "no matching candidate" when the hub's own
   cohort has pending embeddings — an honest "dedup ran degraded" beats a
   confident false negative.

(1) is the real fix; (3) is cheap and worth doing regardless, since it makes
the failure loud instead of silent.

## Verification

Mint a hub, immediately search its own claim sentence, and assert it returns
itself. That test fails today.
