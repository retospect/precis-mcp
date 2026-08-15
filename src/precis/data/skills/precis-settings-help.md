---
id: precis-settings-help
title: precis — DB-resident settings (config that isn't a secret)
summary: precis.settings REGISTRY — DB row → env var → compiled default, DB wins; precis settings CLI, /settings web page, drift visibility via the condition registry
applies-to: precis.settings (get_str/get_float/get_int/get_bool/resolve/set_setting/clear_setting); precis settings CLI; /settings web page; KindSpec.requires_setting
status: active
---

# precis-settings-help — non-secret config that lives in the DB

`precis.settings` holds the fleet's non-secret configuration knobs — spend
caps, polite-pool contact identities (Crossref/Unpaywall/EDGAR/Wikipedia
User-Agents), and anything else a `KindSpec.requires_setting` gate checks.
It is the non-secret sibling of `precis.secrets` (the vault): same shape
(registry + typed getters + a short TTL cache), **opposite precedence**.

## Precedence — DB row wins

```
DB row (app_settings) → registered env var → compiled default
```

This is the *reverse* of `precis.secrets` (env-override-wins there, so a
call site can adopt the vault with zero behaviour change). Settings
inverts it on purpose: **one DB write repairs every host at once.** A
stale env var baked into an old ansible template or a launchd/systemd
unit would otherwise keep silently winning over a fleet-wide fix — the
class of incident that motivated this module (2026-08: asa lost the paper
kind, an API caller broke for want of its polite-pool email — both
traced to one host's env chain silently not carrying a value).

A ~60s TTL cache means a DB write propagates fleet-wide within a minute,
no restart needed. A read of a key not in `REGISTRY` warns once and falls
back to `env(name) → the call's default` — no DB tier, because that needs
the registry's declared env-var name and compiled default.

## What's registered

```bash
precis settings list                 # every REGISTRY key: value, layer, env var, updated_at/by
precis settings get budget.hourly_usd
precis settings set contact.polite_email ops@example.org
precis settings clear contact.polite_email   # revert to env/compiled default
```

Or the web editor at `/settings` — same validation, same CLI-shared
`coerce_for_write` so a bad value is refused before it writes (never
stored as junk a typed getter later warns about and drops).

Current keys (see `precis.settings.REGISTRY` for the authoritative list —
this drifts as keys are added, code doesn't): `budget.hourly_usd`,
`budget.daily_usd`, `budget.quota_ceiling_pct`, `budget.resume_until`
(DB-only, no env — a one-shot manual override), `contact.crossref_mailto`,
`contact.polite_email`, `contact.edgar_user_agent`,
`contact.wikipedia_ua`.

## What never moves here

Bootstrap config stays env forever: `PRECIS_DATABASE_URL`,
`db_connect_retry_seconds`, boot log level — anything needed before or
without the DB. Per-host topology facts (`PRECIS_ROOT`, `corpus_dir`,
`python_roots`, `embedder_url`) stay in the ansible inventory —
declarative, versioned, per-host by construction. Test knobs
(`embedder="mock"`) stay env — tests must not need a DB row. A
semantic change to what a key *means* mints a **new** key, never
repurposes one (mixed-version fleet discipline, same rule as the SQL
migrations this sits next to).

`precis.budget.settings` remains the generic, unregistered `app_settings`
KV surface (`live_config`, `dream_throttle`, `health_digest` markers, …)
— arbitrary keys that don't fit a static registry entry. Only the
budget-cap keys that motivated `app_settings` in the first place are
promoted into `REGISTRY`.

## Gating a kind on a setting

```python
KindSpec(..., requires_setting=("contact.polite_email",))
```

Parallel to `requires_env` / `requires_secret` — the kind registers only
when `precis.settings.is_available(key)` is true (non-`None`, non-empty,
and for a bool key, actually `True`).

## Registering a new key

```python
from precis.settings import SettingSpec, register

register(
    SettingSpec(
        key="mypkg.some_flag",
        type="bool",
        env_var="PRECIS_SOME_FLAG",
        default=False,
        doc="One line: what flips, what it's for.",
    )
)
```

Call `register` at import time (plugin packages like `precis_bio`/
`precis_chem` own their keys this way, registering before their
`KindSpec`s reach the availability gate). Idempotent for an identical
re-registration; a *conflicting* respec raises — two owners disagreeing
about a key is a bug, not a race to win.

## Seeing drift across the fleet

The condition registry (`precis.workers.conditions`, evaluated hourly)
carries a `settings-env-shadowed` row: each host's heartbeat advertises
which registered env vars it still has set locally, and the probe flags
any host where that env var is now shadowed by a DB row — the ansible
template still sets it, but it no longer does anything, and is safe to
remove. `info` severity — it's cleanup visibility, not a fault. See
`get(kind='skill', id='precis-status')`'s Database section for a single
process's live resolution, or `precis settings list` for the full
registered inventory with `updated_at`/`updated_by`.

## See also

- `precis.settings` module docstring — the full why, precedence
  argument, and never-moves-here boundary.
- `docs/backlog/db-resident-settings.md` — build history across slices.
- `get(kind='skill', id='precis-status')` — per-process runtime facts
  (build, DB connection, migration state).
