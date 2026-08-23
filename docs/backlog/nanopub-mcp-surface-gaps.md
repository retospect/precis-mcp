---
status: draft
title: "nanopub MCP surface: read-only mint-gate preflight; approve door (policy call)"
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

## 1. Read-only mint-gate preflight — SHIPPED

`get(kind='finding', id='fi<id>', view='mint-preflight',
args={'payload': …})` runs the real `nanopub/gates.py::run_mint_gates`
and returns the violation list. Payload optional: falls back to the
frozen envelope, else an agent's parked proposal, else sentence-only.
No state change. Owner:
`src/precis/handlers/_finding_mint_preflight.py`; test
`tests/test_finding_mint_preflight_view.py`. Retires the hand-rolled
local gate mirror the nanobud campaign had to carry.

## 1b. Hypothesis proposal door — SHIPPED

`put(kind='finding', hypothesis=True, motivation=…, testable_by=…,
motivated_by=[…])` lets an agent originate the one artifact type it
honestly can: a conjecture, with no evidence by type, motivated by ≥2
artifacts across ≥2 source papers. It mints the hub, writes
chunk-granular `motivated-by` edges (migration 0135 — motivation, NOT
evidence; `hub_refine`/`chase_trigger` exclude these hubs so widening
can never manufacture support for a guess), and parks the prepared
envelope on `refs.meta` so the human's approve form comes pre-filled.
It creates no `nanopub_publish` row and touches no door in §2. Owner:
`src/precis/handlers/_finding_hypothesis.py`.

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
