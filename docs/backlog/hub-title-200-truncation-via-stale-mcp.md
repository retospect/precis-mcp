---
status: draft
title: claim-hub refs.title truncated at 200 chars mid-word — stale local MCP, not the deployed path
model: sonnet
---

# Hub titles are being cut at exactly 200 characters again

Observed 2026-08-19 while minting claim hubs for smartdraft dr43029
through **this session's local `precis` MCP server**. Hubs minted today
carry a `refs.title` of exactly 200 characters, severed mid-word:

| hub | `length(title)` | `length(finding_body)` | tail of title |
|---|---|---|---|
| fi218561 | 200 | 224 | `…nd when the twist angle deviat` |
| fi218562 | 200 | 244 | `…honon mode at the graphene K p` |
| fi218649 | 200 | 225 | `…t overestimation of the WSe₂ v` |

The `finding_body` chunk (`ord=0`) holds the **full** sentence. Only
`refs.title` is truncated, so the damage is display/scan surfaces and any
consumer that reads `refs.title` as the claim.

## This is not the deployed path

- `refs.title` is unbounded `text` in prod (`information_schema` reports
  `character_maximum_length` NULL).
- 9d0b9206 (2026-08-17, "drop finding-title [:200] cap") removed the cap;
  it touched `handlers/finding.py`, `taproot/hub.py`, `nanopub/mint.py`,
  `precis_web/claim_render.py`.
- The **deployed** melchior sha is 5d1ef498, whose
  `taproot/hub.py` writes `title=claim.sentence.strip()` unbounded. The
  only surviving `[:200]` slices there are on todo-creation paths
  (`backfill.py`, `taproot_migrate.py`, `directed.py`) — not the hub door.
- The current worktree likewise has no cap on the finding/hub write path.

So the truncation is coming from the **session-local MCP server process**,
which per `CLAUDE.md` runs local code against `precis_prod`. That process
is serving a build from before 9d0b9206 and is writing truncated titles
into production.

## Why it matters beyond cosmetics

1. **A retro-normalization pass must not read `refs.title`.** Rewriting a
   hub from its truncated title would persist the mid-word cut as the
   canonical sentence *and* recompute `pub_id` from it — turning a display
   bug into an identity corruption. Any such pass must source the sentence
   from the `finding_body` chunk. (This bit the notation-canon pass in
   `claim-dedup-and-notation-canon.md`; that pass was scoped to
   sentences ≤200 chars for exactly this reason.)
2. **Unclear whether `pub_id` was derived from the full or truncated
   sentence.** `seed_claim_hub` computes it from `claim.sentence`, so it
   depends on whether the stale handler truncated before or after
   constructing `CanonicalClaim`. Worth confirming — if before, today's
   long-sentence hubs carry an identity that no future correctly-built
   mint of the same claim will converge onto.
3. The `edit(kind='finding', title=…)` path may carry the same stale cap,
   so retitling a long hub through this MCP could re-truncate.

## Resolved 2026-08-19 — root cause, blast radius, repair

**Root cause.** `precis-mcp-dev-stdio.sh` bind-mounts the *primary
checkout* read-only at `/app` (`-v ~/work/projects/code/precis-mcp:/app:ro`)
and re-reads it at every MCP start. That checkout sat on `main` at
`59b8cb07`, four days stale and predating 9d0b9206, so the MCP served the
capped handler while melchior served the fixed one. Fixed by pulling the
primary and reconnecting `/mcp` (a dependency change in the same range
also required an image rebuild).

**Identity was never corrupted — the important finding.** All 306
non-frozen truncated hubs' stored `pub_id` recomputes exactly from the
**full** sentence (`make_pub_id(make_taproot_hub_paper_id(full, scope))`),
0 from the truncated one. So the handler truncated the title *after*
constructing `CanonicalClaim`. The `finding_body` chunk was likewise
always full. Only `refs.title` — a display/scan field — was damaged, and
a future correct re-mint of the same claim still converges.

**Blast radius.** 332 live hubs, created 2026-07-30 → 2026-08-19 (≈3
weeks, not one session). 306 non-frozen, 26 with a live `nanopub_publish`
row. Zero frozen rows had a truncated `approved_title`, so nothing
contaminated the signing path.

**Repaired.**
- 306 non-frozen: `refs.title` restored from the `ord=0` chunk.
- 26 frozen: `refs.title` set to `nanopub_publish.approved_title` — the
  invariant 9d0b9206's approve-time sync establishes. Restoring from the
  chunk would have been *wrong* for the 21 of them whose reviewer
  deliberately reworded the claim at approval.

Verified: 0 hubs remain with `length(title)=200` over a longer body.

## Still open

- **77 of 139 live `nanopub_publish` rows have `refs.title !=
  approved_title`**, so `gates.check_drift` (gate #14, computed off
  `refs.title`) fails and they cannot be signed. Only 26 came from this
  truncation; the other ~51 were approved *before* 9d0b9206 added the
  sync, so it never ran for them. Backfilling `refs.title =
  approved_title` for those rows replays the fix retroactively and clears
  a false drift signal — needs a human call, since the gate's own remedy
  text says "re-approve".
- Assert in `mint_hub`/`refine_claim_sentence` that the persisted title
  round-trips equal to the sentence, so a stale caller fails loudly
  instead of silently truncating. This bug was invisible for three weeks.
- The launcher has a `--check` preflight for *dependency* drift but
  nothing warns that `/app` is N commits behind `origin/main`. A staleness
  banner there would have caught this on day one.
