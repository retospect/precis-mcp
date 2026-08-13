---
status: ready
title: DB-resident settings — slice 4 (visibility + ansible diet) + residuals
prio: medium
---

# DB-resident settings — slice 4 (visibility + ansible diet) + residuals

Slices 1–3 shipped: `precis.settings` (registry, DB → env → compiled-default,
TTL cache, `updated_by` via migration 0125), `precis settings` CLI +
`/settings` web page, `KindSpec.requires_setting` (bio/chem re-gated), and
the incident-proven identity strings (`contact.*` keys). The why, the
precedence argument, and the never-moves-here boundary live in the
`precis.settings` module docstring — read that first.

## Remaining: slice 4 — visibility + ansible diet

- A doctor/status line (self-healing spine condition registry is the natural
  home) reporting, per host, each registered key and which layer it resolved
  from — drift becomes a report, not a mystery. Acceptance: a deliberately
  drifted host (stale env var shadowed by a DB row) is visibly reported.
- Seed the prod DB rows for the `contact.*` keys, verify resolution
  fleet-wide, then shrink the ansible-templated env surface (launchd/systemd
  templates, asa's stdio MCP `env` block) to bootstrap + host facts.
  Acceptance (the original incident's repro): a `requires_setting` kind
  registers in asa's spawned `precis serve` with no env plumbing in asa's
  MCP config — verified live on the asa host.
- Author the operator skill (`precis-settings-help`, sibling of the secrets
  workflow docs) — skills are the runtime channel.

## Residuals (small, from the build)

- `/budget`'s cap writes still go through `precis.budget.settings.set_float`
  and do **not** record `updated_by`; a write via `precis.settings` /
  `/settings` / the CLI does. Consolidate `/budget`'s write path (needs
  `commit()`/`rollback()` added to a few test fakes).
- `precis.budget.quota._ceiling_pct` still hand-rolls DB → env → default for
  `budget.quota_ceiling_pct` — delegate to `precis.settings.get_float`.
- `precis.budget.settings` remains the unregistered generic-KV surface
  (live_config, dream_throttle, health_digest, …) by design; revisit whether
  any of those keys deserve registry entries when next touched.
- Behavior flags deliberately not moved yet (move when touched / when they
  bite): anki `fix/project/anki_enabled`, classify toggles, `kinds_disabled`
  (boot-shape semantics — see decisions log in git history of this file),
  `default_tags`.
