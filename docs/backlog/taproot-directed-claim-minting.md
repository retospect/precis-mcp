---
status: draft
title: Taproot directed claim minting — argue a proposed claim against a passage, then into the tree
model: opus
---

# Directed claim minting (the "argue-out" mint)

Decided direction (Reto, 2026-08-14): paragraph-driven minting must be
**directed** — a specific, qualified claim we need is argued out so it fits
the para *and* the tree. Never an undirected harvest: a para contains many
micro-claims, and mining them all floods the graph with unowned claims at
the wrong grain. Demand-driven minting (backfill over authored prose) stays
the default; this adds the missing front door for "we need to be able to
say X — does this source license it?"

## Shape — two argument steps, only the first is new

**Step 1 — qualify against the passage (new).** Input: a *proposed* claim
(from the quest/draft layer — the demand carries the "why") + the target
passage. The LLM negotiates, not extracts: rewrite the proposal into the
strongest version the passage actually licenses — adding the qualifiers
(material/method/regime/comparator/conditions, i.e. the existing scope
fields) that make it supportable — or return **unsupported** (no honest
version survives; the demand-side NO-CLAIM). Output: qualified sentence +
grounding quote. Direction of fit is one-way: the claim bends to the
evidence, never the reading of the evidence to the claim. This is
`hub_refine`'s support check run pre-mint and made generative
(rewrite-to-supportable instead of pass/fail) — same shape as the
migration phase-2 per-atom evidence verify.

**Step 2 — fit the tree (already built).** The qualified atom runs the
standard `block → dedup_judge → place` cascade and the
`apply_extraction`/`attach_evidence` write door: attach (tree already says
it), `refines` a broader hub (common — the qualified version is usually
more specific), new, new+contradicts, or needs_review. Same convergence
gates, idempotence, and published-hub hard-stop as every other mint.

## Requirements

- **Provenance of demand**: record the demanding quest/draft/todo in the
  minted hub's meta so directed mints are never unowned.
- **Strict dispatch posture**: use the strict extraction/dispatch pattern
  (see `extract_claim_strict`, docs/backlog/
  taproot-backfill-llm-outage-silent-noclaim.md) — an LLM outage must
  surface as infra failure, never as "unsupported".
- **Tier — BIG (Reto, 2026-08-14)**: qualify is genuine judgment (spotting
  what the passage does NOT license and finding the strongest honest
  weakening — exactly where small models over-agree), and directed mints
  are demand-paced, not bulk, so BIG is affordable. SMALL stays right for
  `extract_claim` (bulk normalization); the two tiers encode the different
  jobs. Eval on the extraction-fixture pattern (`tests/fixtures/taproot/`)
  still applies.
- The cheap-half prompt slot (a `focus` param on `_EXTRACT_PROMPT`) is NOT
  this feature and should not be built as a shortcut — it steers topic but
  keeps harvest semantics.

## Sequencing

**v1 built now (Reto, 2026-08-14)**, as a CLI (`precis taproot direct-mint`)
+ Python API (`precis.taproot.directed.qualify_claim` /
`directed_mint`), dry-run by default — ahead of the compound-hub migration
(docs/backlog/taproot-atomic-claims.md) rather than after it, per Reto's
call. The tree is still pre-migration (compound hubs live): a directed
mint's cascade can place a qualified atom against an existing compound
hub, same as any other forward mint — `place`'s `attach` onto a compound
downgrades to `needs_review` (`apply_placement`'s existing guard, docs/
backlog/taproot-atomic-claims.md step 2) rather than silently attaching
evidence to a bundle, so no new compound-hub hazard is introduced; the
existing cascade guards handle it exactly as they do for backfill/chase.
The MCP-verb surface (a `directed_mint` tool call, vs. today's CLI-only
front door) is deferred — natural next consumers (quest gap-filling, draft
authoring, the nanopub negative-results pathway) can call the CLI or the
Python API directly until that's built.
