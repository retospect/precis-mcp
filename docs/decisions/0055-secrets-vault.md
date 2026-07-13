# ADR 0055 — Secrets vault: DB-resident secrets, shareable dump, DB-enforced access

- **Status**: accepted — **implemented** (2026-07-13, worktree `secrets`).
  Migration `0059_secrets_vault.sql`; resolver `src/precis/secrets.py`; CLI
  `precis secret`; web `/secrets`. Design-of-record:
  [`docs/design/secrets-vault.md`](../design/secrets-vault.md) — this ADR
  records the *decisions*; the schema, functions, grants, and build order live
  there.
- **Deciders**: Reto + agent

> **Amendment 1 (v1 scope, 2026-07-13).** Decisions 3–5 below describe the full
> role-split design. **v1 ships without the roles** (Reto's call): everything
> already connects as one role (`agent_rw`), so per-service roles add complexity
> for minimal gain while the DSN itself is the boundary. What v1 ships instead:
> the vault verbs are `GRANT`ed to `PUBLIC` (anyone who can connect), the tables
> stay `REVOKE`d so `vault.reveal` remains the only audited decrypt path, and
> the **DSN is made parameter-only** — `build_runtime` pops `PRECIS_DATABASE_URL`
> from `os.environ` after connecting, so default-inheriting subprocess spawns
> (`claude -p`, `plan_tick`, shell-outs) no longer receive it. Net bootstrap
> surface: the DB password (already deployed) + one new `app.secret_key`
> (`ALTER SYSTEM` on caspar via ansible-vault). The role split + per-name ACL of
> decisions 3–4 remain the designed next step and layer on without reworking
> callers (`reveal` vs. a future broker verb stay distinct).

## Context

Secrets are scattered as direct `os.environ` reads at the point of use, plus
`~/.secrets/pw/*` files. Two problems: (1) ambient env leakage — a worker that
spawns `claude -p`/ansible/ruff hands the child the whole environment; (2)
all-or-nothing — env can't say "this role reads the SendGrid key but not the
OAuth token." Postgres is already the system of record (ADR 0010); make it the
secret store too.

## Decision

1. **DB-resident, encrypted.** A `vault.secrets(name, ciphertext, hint,
   updated_at)` table; values encrypted with `pgcrypto` (`pgp_sym_encrypt`).
   Only ciphertext is stored, so only ciphertext is dumped.

2. **Key in server config, not in the DB objects.** The passphrase lives in
   `ALTER SYSTEM SET app.secret_key` (`postgresql.auto.conf`), a file `pg_dump`
   structurally never emits. `SECURITY DEFINER` functions decrypt via
   `current_setting('app.secret_key')` — their dumped *source* names the key,
   never holds it. **Result: a logical `pg_dump` is safe to share.** (Physical
   backups still carry the key — out of scope; hold the key outside `PGDATA` if
   prod backs up physically.)

3. **Postgres is the policy enforcement point.** Three verbs, three tiers:
   `vault.list()` / `vault.mask(name)` (masked, no plaintext) and
   `vault.reveal(name)` (one named plaintext, audited). Access is decided by
   role `GRANT EXECUTE`, not by client behaviour. **There is deliberately no
   bulk-plaintext function** — reveal is one-name-at-a-time and each call writes
   a `vault.events` row. An optional per-name `vault.acl` narrows a
   reveal-capable role to specific keys.

4. **The broad role stays out.** `agent_rw` (held by sandbox/arbitrary code)
   gets **zero** vault grants. The vault has dedicated roles: `precis_web` →
   list/mask only (so a compromised web process leaks only hints);
   `precis_secrets` → full (precis core's third DSN); `asa` → list + reveal,
   ACL-scoped to `ASA_*`.

5. **Python is a thin wrapper.** `precis.secrets.get_secret(name)` resolves
   env-override → `vault.reveal` (cached, `pg_notify`-invalidated) → file
   fallback. All authorization is in the DB; Python holds no policy.
   `requires_env` → `requires_secret` over the same resolver. Env-override-wins
   means call sites migrate one at a time and it **ships dark** until a secret
   is actually written to the vault.

6. **Extensions: first-party now, out-of-process later.** v1 trusts first-party
   plugins and injects **bound capabilities, not credentials** (core reveals,
   builds the client, hands the plugin the client). A package/manifest
   allowlist (asa included) is a **review gate + audit anchor mapping to the DB
   ACL — not a boundary against hostile code**: in-process code bypasses any
   Python caller check, and a trojaned dependency runs its payload at
   `import` time. The real supply-chain controls are hash-pinned/audited deps
   (`uv.lock --require-hashes`) and keeping the `precis_secrets` DSN out of any
   process that loads an untrusted/large dependency graph. The untrusted tier
   (out-of-process broker on the `sandbox_run` substrate, no DSN) is deferred;
   `reveal` and a future `use-capability` broker verb are kept distinct now.

## Consequences

- One place to hold/rotate/audit secrets; rotation is `set_secret` + one NOTIFY,
  cluster-wide. Shareable logical dumps. RBAC and per-name scoping env can't
  express.
- Deepens DB-availability coupling (a down DB means no secrets) — keep the
  genuinely bootstrap-critical secrets (the DSN itself) as files, move only leaf
  keys into the vault.
- The secret-holding process's pinned dependency set becomes the trusted
  computing base — a reason to push heavy/untrusted deps out-of-process over
  time.

## Builds on

- [ADR 0010 — Postgres/pgvector as system of record](./0010-postgres-pgvector-system-of-record.md)
- [ADR 0005 — Forward-only migrations](./0005-greenfield-migrations.md) — the
  vault arrives as migration `0059_secrets_vault.sql`.
