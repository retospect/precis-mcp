---
status: idea
title: hardening residuals from the hub-title-200-truncation incident
---

# Title round-trip assert + MCP staleness banner

Two hardening residuals from the 2026-08-19 truncated-title incident
(root cause: `precis-mcp-dev-stdio.sh` served a four-days-stale `/app`
bind-mount predating the `[:200]`-cap removal; repaired, see `git log`).

1. Assert in `mint_hub`/`refine_claim_sentence`
   (`src/precis/taproot/hub.py`) that the persisted `refs.title` round-trips
   equal to the claim sentence, so a stale caller fails loudly instead of
   silently truncating. This bug was invisible for three weeks.
2. `precis-mcp-dev-stdio.sh` has a `--check` preflight for *dependency*
   drift but nothing warns that `/app` is N commits behind `origin/main`.
   A staleness banner there would have caught this on day one.
