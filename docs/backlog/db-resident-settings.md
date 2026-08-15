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

## Slice 4 — shipped this round

- **Doctor/status visibility**: `precis.workers.conditions` gained a
  `settings-env-shadowed` row (info severity — cleanup visibility, never a
  gripe/page) — `precis.settings.advertised_env_presence()` self-reports,
  via `host_heartbeat.meta.settings_env_present`, which registered env vars
  a host still has set locally; the probe flags any that a DB row now
  shadows on that same host. Acceptance met: a host with a stale env var
  behind a DB row surfaces on `/alerts` + the digest within one hourly
  window.
- **Operator skill**: `precis-settings-help` (sibling of `precis-status-help`)
  — precedence, the registered-key inventory, CLI/web editors, registering a
  new key, and the drift-visibility row above.

## Remaining: slice 4 — ansible diet (needs live prod access, not code)

- Seed the prod DB rows for the `contact.*` keys, verify resolution
  fleet-wide (`precis settings list` / the new condition row should show
  every host's shadow-env cleared), then shrink the ansible-templated env
  surface (launchd/systemd templates, asa's stdio MCP `env` block —
  `deploy/roles/asa_bot/templates/claude_mcp.json.j2` sets `UNPAYWALL_EMAIL`/
  `CROSSREF_MAILTO`, names that don't even match the registered
  `PRECIS_UNPAYWALL_EMAIL`/`PRECIS_CROSSREF_MAILTO` env vars — worth a look
  when this is picked up) to bootstrap + host facts. Acceptance (the
  original incident's repro): a `requires_setting` kind registers in asa's
  spawned `precis serve` with no env plumbing in asa's MCP config — verified
  live on the asa host. Out of scope for a code-only session: needs a prod
  DB write + a live asa-host check.

## Residuals (small, from the build)

- ~~`/budget`'s cap writes still go through `precis.budget.settings.set_float`
  and do not record `updated_by`~~ — done: `/budget`'s cap/resume writes
  (`src/precis_web/routes/budget.py`) now go through
  `precis.settings.set_setting`/`clear_setting` directly.
- ~~`precis.budget.quota._ceiling_pct` still hand-rolls DB → env →
  default~~ — done: delegates to `precis.settings.get_float`.
- `precis.budget.settings` remains the unregistered generic-KV surface
  (live_config, dream_throttle, health_digest, …) by design; revisit whether
  any of those keys deserve registry entries when next touched.
- Behavior flags deliberately not moved yet (move when touched / when they
  bite): anki `fix/project/anki_enabled`, classify toggles, `kinds_disabled`
  (boot-shape semantics — see decisions log in git history of this file),
  `default_tags`.
