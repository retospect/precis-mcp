---
status: idea
title: DB-resident settings — deferred key migrations (move when touched)
---

# DB-resident settings — residuals

Slices 1–4 shipped: registry/CLI/web/`requires_setting` (see the
`precis.settings` module docstring), doctor `settings-env-shadowed` row,
`precis-settings-help` skill, and the ansible diet (2026-08-22:
`contact.polite_email` / `contact.crossref_mailto` seeded in precis_prod;
env templating removed from `group_vars/all.yml`'s identity block, the
operator overlay, and asa's `claude_mcp.json.j2` — whose
`UNPAYWALL_EMAIL`/`CROSSREF_MAILTO` names matched no registered env var
anyway).

Remaining, all deliberate move-when-touched:

- `precis.budget.settings` stays the unregistered generic-KV surface
  (live_config, dream_throttle, health_digest, …) by design; revisit
  whether any key deserves a registry entry when next touched.
- Behavior flags not moved yet (move when touched / when they bite):
  anki `fix/project/anki_enabled`, classify toggles, `kinds_disabled`
  (boot-shape semantics — decisions log in this file's git history),
  `default_tags`.
