---
status: idea
title: Agents can reach the prod web UI with the vaulted claude web user; guide-web --db prod boots read-only
pillar: platform
---

# Agent read-only access to the prod web UI

Bundled in the 2026-10-10 gripe triage. Both gripes ask for the same thing:
an agent can look at prod web pages, and the database cannot be written
through that path.

- **gr477803.** The prod web UI uses Basic auth against `web_users`, and
  agents have no credential they can fetch. Agents cannot dogfood `/graph`,
  `/eye`, `/rules`, `/builds`, NotFound→404 or the pcb argue box. Every web
  change is checked only on dev or by Reto.
- **gr461387.** `scripts/guide-web --db prod` is documented as read-only
  through `agent_ro`, yet at boot it upserted 55 kind rows. The boot
  read-only probe checks only the GUC, and the effective role may be
  `agent_rw`. Until this is fixed, do not restart guide-web against prod.

## Direction

1. guide-web against prod connects as a role that cannot write.
   `scripts/guide-web` already rewrites the DSN user with
   `sed 's#://agent_rw@#://agent_ro@#'`, which does nothing if the stored
   DSN names another user or form, and nothing checks the result. The
   launcher verifies the effective role (`current_user`, a test write that
   fails) and refuses to start otherwise. The boot kind-upsert is skipped
   in read-only mode instead of being attempted.
2. **Ruling (Reto, 2026-10-11): agents use the existing `claude` web user
   (`web_users` id 4) as-is, with full access.** precis-web has no roles;
   every `web_users` row is fully authorized (`src/precis_web/auth.py`
   docstring). No read-only agent credential is built. What remains: the
   plaintext is not stored (scrypt hash + vault pepper), so reset the
   password with the `precis users` CLI, vault it under a key like
   `PRECIS_WEB_PASSWORD:claude`, and document in the relevant runbook or
   skill how an agent fetches it and runs `curl -u claude:<pw> <url>`.
   Who: ops, S.

Related: [web-basic-auth-users.md](web-basic-auth-users.md).

## Acceptance

- `guide-web --db prod` boots with zero writes. A write attempt logs a
  refusal; it does not upsert.
- An agent follows the documented steps, fetches the vaulted password and
  gets `/graph` on prod with `curl -u`.
