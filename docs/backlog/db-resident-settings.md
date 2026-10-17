---
status: idea
pillar: platform
title: DB-resident settings — deferred key migrations (move when touched)
---

# DB-resident settings — residuals

Slices 1–4 shipped (2026-08-22): see the `precis.settings` module
docstring and skill `precis-settings-help`.

Remaining, all deliberate move-when-touched:

- `precis.budget.settings` stays the unregistered generic-KV surface
  (live_config, dream_throttle, health_digest, …) by design; revisit
  whether any key deserves a registry entry when next touched.
- Behavior flags not moved yet (move when touched / when they bite):
  anki `fix/project/anki_enabled`, classify toggles, `kinds_disabled`
  (boot-shape semantics — decisions log in this file's git history),
  `default_tags`.
