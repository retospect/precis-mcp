---
status: in-progress
pillar: platform
---

OPS-ONLY (2026-10-17): Reto runs these rotations by hand; never auto-pick this for a builder. All four rotations are still owed and the credentials are leaked, so it's urgent. The agent_rw rotation needs a parked-planner pause window.

# Rotate agent_rw, OPENROUTER_API_KEY, Claude OAuth token, anki password

All have leaked (transcripts, shell history, five on-disk copies since July,
cleartext `PRECIS_ANKI_PASSWORD` in melchior's worker plist). `agent_rw`
needs a deliberate cluster pause: pgbouncer's static md5 userlist and the
Postgres role must move together (1–2 min auth gap; pick a parked-planner
window) — full ordered procedure:
`docs/runbooks/rotate-agent-rw-credential.md`. `OPENROUTER_API_KEY` is NOT
blocked on the pause (new-key → vault → deploy → confirm → revoke is
zero-outage) — do it independently. The vaulted Claude OAuth token is the
same one exposed on disk since July; the vault is now the single rotation
point. Ops (Reto).

Evidence gr186753: cleartext DB + 3rd-party creds outside §L pgpass —
sortie renders a literal DB password; `asa_bot`/`asa_slack`/`extract_watch`
env.

test: `scripts/prod-psql "SELECT current_user"` succeeds post-rotation with
no pgbouncer auth loop.
