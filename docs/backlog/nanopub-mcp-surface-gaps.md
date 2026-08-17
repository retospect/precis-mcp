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

## 1. Read-only mint-gate preflight (do this one)

`nanopub/gates.py::run_mint_gates` is callable only from
`mint.py::approve` and the CLI. Agents preparing approve payloads had
to **reimplement the gates locally** (verbatim-quote containment,
citation-marker regex, snip validity + uniqueness across body chunks,
pdf-sha pin, title-quantity checks) to pre-verify specs — that mirror
got batch B to 21/21 approvals with zero gate refusals, but a local
mirror silently rots when gates change (the 2026-08-16
citation-marker gate would have invalidated any older mirror).

Wanted: a pure-read MCP door that runs the real gates against a
candidate payload and returns the violation list —
`get(kind='finding', id='fi<id>', view='mint-preflight',
args={'payload': …})`, payload optional (falls back to the approve
prefill). No state change, no policy conflict with the human-only
approve line. CLI parity exists already (`nanopub preflight` covers
publish-time gates; this is the mint-time sibling).

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
