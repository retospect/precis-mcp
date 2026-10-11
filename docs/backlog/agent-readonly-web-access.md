---
status: idea
title: Agents can reach the prod web UI with the vaulted claude web user
pillar: platform
---

# Agent read-only access to the prod web UI

Agents need to look at prod web pages.

- **gr477803.** The prod web UI uses Basic auth against `web_users`, and
  agents have no credential they can fetch. Agents cannot dogfood `/graph`,
  `/eye`, `/rules`, `/builds`, NotFound→404 or the pcb argue box. Every web
  change is checked only on dev or by Reto.

## Direction

1. **Ruling (Reto, 2026-10-11): agents use the existing `claude` web user
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

- An agent follows the documented steps, fetches the vaulted password and
  gets `/graph` on prod with `curl -u`.
