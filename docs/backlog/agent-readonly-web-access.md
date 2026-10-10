---
status: idea
title: Agents can read the prod web UI through an enforced read-only role and credential
pillar: platform
---

# Agent read-only access to the prod web UI

Bundled in the 2026-10-10 gripe triage. Both gripes ask for the same thing:
an agent can look at prod web pages, and the database cannot be written
through that path.

- **gr477803.** The prod web UI uses Basic auth against `web_users`, and no
  read-only agent credential exists. Agents cannot dogfood `/graph`,
  `/eye`, `/rules`, `/builds`, NotFound→404 or the pcb argue box. Every web
  change is checked only on dev or by Reto.
- **gr461387.** `scripts/guide-web --db prod` is documented as read-only
  through `agent_ro`, yet at boot it upserted 55 kind rows. The boot
  read-only probe checks only the GUC, and the effective role may be
  `agent_rw`. Until this is fixed, do not restart guide-web against prod.

## Direction

1. guide-web against prod connects as a role that cannot write. The
   launcher verifies the effective role (`current_user`, a test write that
   fails) and refuses to start otherwise. The boot kind-upsert is skipped
   in read-only mode instead of being attempted.
2. A `web_users` row (or token) for agents that maps to the read-only
   path. Write routes refuse it. It is stored in the vault like the other
   agent credentials.

Related: [web-basic-auth-users.md](web-basic-auth-users.md).

## Acceptance

- `guide-web --db prod` boots with zero writes. A write attempt logs a
  refusal; it does not upsert.
- An agent can fetch `/graph` on prod with its credential, and a POST to a
  write route returns 403.
