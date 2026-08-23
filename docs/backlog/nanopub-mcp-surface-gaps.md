---
status: draft
title: "nanopub MCP surface: mirror/publish status read view; approve door (policy call)"
model: sonnet
---

# Nanopub MCP surface gaps (from the nanobud campaign, 2026-08-17)

The 124-hub nanobud campaign stress-tested the agent surface. Hub
authoring via MCP is complete and behaved well: `put(supporters=)`
mint/convergence, `link(rel=)` chunk-granular idempotent evidence
attach (no 502 double-write hazard, unlike the web `evidence/add`
door), `edit(title=)` reword with dup detection, `view='nanopub'`/
`view='evidence'` reads. What was missing:

## 0. Mirror/publish status read view (measured; merged from
## nanopub-status-read-kind, 2026-08-21)

62h of local session mining found **1,063** Bash `psql`/`scripts/prod-psql`
calls concentrated in nanopub-mint verification workflows polling
`nanopub_mirror` / `nanopub_publish` — tables with no precis-kind read path.
Agents aren't routing around MCP friction; the surface has a hole. Add a read
view (a `view=` on the nanopub-adjacent kind, or a thin `nanopub-status` kind)
covering the polled questions: mirror/publish state per claim, counts by
status, recent failures. Test: the mint-verification recipe (see the
`precis.nanopub` package docstring) completes via MCP reads only. Pattern to
institutionalize: measured detour → kind/view, not new verbs (general form:
`mcp-aggregate-surface-gaps.md`).

## 2. MCP approve door — Reto's policy call, not a default

Approve itself stayed web/human-only by design; the campaign ran it
via user-authorized curl to the web door, paced. If that pattern
recurs, a feature-flagged MCP approve (allowlist/authorization-token
gated, server-side pacing) would remove the curl scaffolding — but it
moves a line that is currently deliberately drawn. Decide before
building. Sign/signoff/anchor/publish stay human-only regardless.

## 3. Minor

- Paper soft-delete is web-only (`POST /papers/<id>/delete`); no MCP
  equivalent (campaign needed it for dup/un-import cleanup).
- Session-MCP process wedged ~30 min mid-campaign (cost the tier-2
  agent a timeout loop) — reliability bug, separate from features;
  gripe when reproduced.
