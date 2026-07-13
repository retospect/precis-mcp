# Secrets vault — DB-resident secrets with a shareable dump

> Design-of-record. Decisions log is [ADR 0055](../decisions/0055-secrets-vault.md).
> Status: **v1 implemented** (2026-07-13) — migration `0059`, resolver
> `src/precis/secrets.py`, CLI `precis secret`, web `/secrets`.
>
> **v1 ships WITHOUT the per-service roles** described below (ADR 0055
> Amendment 1). The role split / per-name ACL sections (`precis_secrets` /
> `precis_web` / `asa`) are the *designed next step*, not what shipped: v1 grants
> the verbs to `PUBLIC` (the DB connection is the boundary) and instead makes the
> DSN parameter-only by popping `PRECIS_DATABASE_URL` from `os.environ` at boot,
> so subprocesses don't inherit it. Read the role sections as the future tightening,
> not current behaviour.

## Problem

Secrets live scattered as direct `os.environ` reads at the point of use
(`PERPLEXITY_API_KEY`, `WOLFRAM_APP_ID`, `EPO_OPS_CLIENT_KEY/SECRET`,
`ORCID_CLIENT_ID/SECRET`, `CLAUDE_CODE_OAUTH_TOKEN`, the DB DSNs, plus the
`~/.secrets/pw/*` files). There is no secret abstraction. Two consequences:

1. **Ambient env leakage.** A worker that spawns `claude -p` / ansible / ruff
   passes the *whole* environment to the child by default — every key is
   visible in `/proc/<pid>/environ`, a stray `env` dump, a crash log. This is
   the dominant real-world leak vector.
2. **All-or-nothing.** Env can't express "this role may read the SendGrid key
   but not the OAuth token." A future untrusted extension or sandbox that gets
   *any* foothold gets everything.

## Goal

- One place to hold, rotate, and audit secrets — the Postgres cluster that is
  already the system's center of gravity.
- **A `pg_dump` that is safe to share** (ciphertext only; the key never lands
  in the dump).
- **Postgres is the policy enforcement point** — access decided by role
  `GRANT EXECUTE`, not by any client behaving well.
- A migration path that breaks nothing: env-override-wins, adopt call sites
  one at a time.

Non-goal (v1): defending against **hostile in-process code**. In-process code
runs with precis's privileges and can call the resolver, read the live
connection, or read `os.environ` — Python has no in-process sandbox. v1 trusts
first-party extensions (see *Extensions*); the untrusted tier is deferred to
the out-of-process broker below.

## The crypto split — why the dump is shareable

`pg_dump` emits table **data** and object **source** (function bodies
included). So the key must live somewhere that is neither: **server config.**

- Encrypt values with `pgcrypto` (`pgp_sym_encrypt`) — only ciphertext is
  stored, so only ciphertext is dumped. The PGP format carries a fresh random
  salt per encryption, so identical secrets encrypt to different bytes and the
  dump leaks nothing by comparison.
- Hold the passphrase in `ALTER SYSTEM SET app.secret_key = '…'` →
  `postgresql.auto.conf`, a server file `pg_dump` structurally never touches.
- Decrypt inside `SECURITY DEFINER` functions whose *body* references
  `current_setting('app.secret_key')` — a **name**, not the value. The dumped
  function source is safe.

Result: `pg_dump precis_prod` contains ciphertext rows + function text naming a
GUC. Shareable.

### Two caveats, stated honestly

- **The GUC is world-readable within the DB.** Any connected session can
  `SELECT current_setting('app.secret_key')`. This protects the *dump*, not a
  low-priv role with a live connection. If a connected low-priv role must also
  be blind to the key, put the key in a `REVOKE`-locked `vault._keyring` table
  and exclude it from the dump with `pg_dump --exclude-table-data=vault._keyring`
  — protects both, but only for logical dumps whose flags you control.
- **Physical backups leak.** `pg_basebackup` / WAL / streaming replicas copy
  the whole data dir, including `postgresql.auto.conf`. "Dump is safe to share"
  holds for **logical `pg_dump` only**. Confirm what caspar backs up with; if
  physical, hold the key in a config file *outside* `PGDATA`.

Encryption-at-rest and RBAC solve **different** problems and compose:
encryption → opaque bytes at rest + shareable dump; RBAC → who may decrypt live.
You want both.

## Schema + the three verbs

The DB exposes exactly three functions — three privilege tiers:

| Verb | Returns | Tier | Typical grantee |
|---|---|---|---|
| `vault.list()` | `(name, hint, updated_at)` — **never plaintext** | lowest | web, inventory |
| `vault.mask(name)` | single masked hint | lowest | web |
| `vault.reveal(name)` | plaintext of **one named** secret | guarded | precis core, asa (ACL'd) |

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA vault;

-- key: ALTER SYSTEM SET app.secret_key = '<long random>'; SELECT pg_reload_conf();
-- lives only in postgresql.auto.conf — never in a dump.

CREATE TABLE vault.secrets (
  name       text PRIMARY KEY,
  ciphertext bytea       NOT NULL,   -- pgp_sym_encrypt output; the ONLY thing dumped
  hint       text        NOT NULL,   -- masked preview, stamped at write time (no plaintext)
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE vault.events (          -- tamper-resistant audit; only definer funcs write it
  at    timestamptz NOT NULL DEFAULT now(),
  who   text        NOT NULL,        -- session_user
  verb  text        NOT NULL,        -- 'reveal' | 'set' | 'delete'
  name  text        NOT NULL
);

-- masking: at most min(3, len/5) chars per side; fully masked under ~12 chars.
CREATE FUNCTION vault._hint(v text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN length(v) < 12 THEN repeat('•', 6) || ' (' || length(v) || ')'
    ELSE left(v, least(3, length(v)/5)) || '…' || right(v, least(2, length(v)/5))
  END;
$$;

CREATE FUNCTION vault.list()
  RETURNS TABLE(name text, hint text, updated_at timestamptz)
  LANGUAGE sql SECURITY DEFINER SET search_path = vault AS $$
    SELECT name, hint, updated_at FROM vault.secrets ORDER BY name;
$$;

CREATE FUNCTION vault.mask(p_name text) RETURNS text
  LANGUAGE sql SECURITY DEFINER SET search_path = vault AS $$
    SELECT hint FROM vault.secrets WHERE name = p_name;
$$;

CREATE FUNCTION vault.reveal(p_name text) RETURNS text
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault AS $$
  DECLARE v text;
  BEGIN
    -- optional per-name ACL enforced here (see below)
    SELECT pgp_sym_decrypt(ciphertext, current_setting('app.secret_key'))
      INTO v FROM vault.secrets WHERE name = p_name;
    IF v IS NULL THEN RETURN NULL; END IF;
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'reveal', p_name);
    RETURN v;
  END;
$$;

CREATE FUNCTION vault.set_secret(p_name text, p_value text) RETURNS void
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = vault AS $$
  BEGIN
    INSERT INTO vault.secrets(name, ciphertext, hint)
    VALUES (p_name,
            pgp_sym_encrypt(p_value, current_setting('app.secret_key')),
            vault._hint(p_value))
    ON CONFLICT (name) DO UPDATE
      SET ciphertext = EXCLUDED.ciphertext, hint = EXCLUDED.hint, updated_at = now();
    INSERT INTO vault.events(who, verb, name) VALUES (session_user, 'set', p_name);
  END;
$$;
```

**Deliberately no bulk-plaintext function.** `reveal` takes exactly one name
and returns one value. Even a caller with EXECUTE on `reveal` must exfiltrate
one name at a time, and each call is one `vault.events` row. This is the
structural "can't accidentally (or quietly) dump the whole thing" guard.

## Lock it down — the grants are the boundary

```sql
REVOKE ALL ON ALL TABLES    IN SCHEMA vault FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vault FROM PUBLIC;

GRANT EXECUTE ON FUNCTION vault.list, vault.mask   TO precis_web;      -- list/mask ONLY
GRANT EXECUTE ON FUNCTION vault.list, vault.mask,
                          vault.reveal, vault.set_secret TO precis_secrets;
-- asa: list + reveal, but ACL-scoped to its own keys (below)
GRANT EXECUTE ON FUNCTION vault.list, vault.reveal TO asa;
-- sandbox / arbitrary code: NO grants at all.
```

**The make-or-break rule: the broad role arbitrary/sandbox code already holds
(`agent_rw`) gets ZERO vault grants.** The vault has its own dedicated roles.
Otherwise the boundary is theater — anyone with the widely-held `agent_rw` DSN
could `SELECT vault.reveal('anything')`.

- `precis_web` → `list` + `mask`. **Cannot reveal.** So even a fully
  compromised web process leaks only hints — enforced by Postgres, not Python
  discipline. (The web page never sees ciphertext or the key either.)
- `precis_secrets` → the full set; this is the role precis core connects as
  for secret reads (a *third* DSN, distinct from `agent_rw`/`agent_ro`).
- `asa` → `list` + `reveal`, narrowed by a per-name ACL so it can reveal only
  `ASA_*` keys.

### Optional per-name ACL (the "real good" tier)

```sql
CREATE TABLE vault.acl (role_name text, name_glob text);  -- e.g. ('asa','ASA_%')
-- inside reveal(), before decrypt:
--   IF NOT EXISTS (SELECT 1 FROM vault.acl
--                  WHERE role_name = session_user AND p_name LIKE name_glob)
--   THEN RAISE insufficient_privilege; END IF;
```

Lets a role with EXECUTE on `reveal` still only decrypt secrets it's ACL'd for —
DB-level scoping env can never express.

## Python is a thin wrapper — nothing more

`precis.secrets.get_secret(name)` resolves in a fixed order:

1. **explicit env override** (`os.environ[name]`) — bootstrap, tests, and the
   migration safety net (env-override-wins means adopting a call site never
   breaks it);
2. **`vault.reveal(name)`** over the `precis_secrets` DSN, cached in-process,
   invalidated by `pg_notify('precis.secrets')` (rotation is one `set_secret` +
   one NOTIFY, cluster-wide);
3. `~/.secrets/pw/<name>` file fallback (retirement ramp).

All *authorization* lives in the DB (grants + ACL + audit). Python holds **no
policy** — it's transport + caching + invalidation. `KindSpec.requires_env`
becomes `requires_secret` and checks the same resolver, so kind-gating keeps
working. `ensure_oauth_token` generalizes into the file-fallback tier.

The **one** place Python does more than wrap the DB verb is the extension
capability broker — because the DB can't construct an HTTP client.

## Extensions — first-party now, out-of-process later

In-process = inside the trust boundary. v1 trusts first-party plugins and
gives them **bound capabilities, not credentials**: core calls `reveal`, builds
the client (Perplexity HTTP client, Discord session), and hands the plugin the
*client* — the plugin calls `client.query(...)` and never sees the string.
Scope by a manifest (`requires_secret=(…)`) mirroring `requires_env`. This
stops accidental exposure and sloppy logging (≈90% of real leaks); it does not
stop malicious introspection, which is fine for code we wrote.

**Deferred (untrusted tier):** arbitrary plugins run **out-of-process** on the
existing `sandbox_run`/`claude_docker` substrate (separate container,
allowlisted env, no vault DSN) and request secret-backed *operations* through a
broker ("do X with secret Y") — precis performs the call and returns only the
result; the secret never crosses the boundary. The verb split (`reveal` vs. a
future `use-capability` broker call) is designed now so callers don't rework
when we tighten. Keep `reveal` and the broker distinct from day one.

### Supply chain — why a package allowlist is not the boundary

The obvious instinct is "keep a list of allowed pips (asa, …) that may see
secrets, so a trojaned `matplotlib` I install unaudited can't exfiltrate." Keep
the list — but understand what it is and isn't.

**A package-name allowlist cannot stop a hostile in-process dependency.** Code
loaded into the process holding the `precis_secrets` DSN runs with full process
privileges. A Python-level caller check ("is the calling module allowlisted?")
is bypassed trivially by same-interpreter code: read `os.environ`, run
`SELECT vault.reveal(...)` on the live connection, `import` and call
`get_secret` directly, walk `gc.get_objects()` for the plaintext, or monkeypatch
the broker. Python has no tamper-proof caller authentication.

**Sharper still: `import matplotlib` runs matplotlib's `__init__` in the trusted
process — a trojan's payload executes at *import time*, before any secret call.**
So "only allowlisted callers may call `get_secret`" is defeated before the first
call. The out-of-process render sandbox protects *execution of user plot code*,
not the parent's `import`.

The real defenses (neither is a name allowlist):

1. **Hash-pinned, audited deps** — `uv.lock` + `--require-hashes`; no unaudited
   `pip install` into the secret-holding process. A swapped package fails the
   hash. This is the primary control: don't admit the trojan.
2. **Keep the `precis_secrets` DSN out of any process that loads an untrusted or
   large dependency graph.** The TCB for secrets = every package imported into
   the DSN-holding process. Push heavy/untrusted code out-of-process (the
   sandbox substrate) with an allowlisted env and no DSN, and *lazy-import*
   those packages only inside the subprocess so their `__init__` never runs in
   core.

**What the allowlist IS for:** a manifest + review gate + audit anchor. It
declares which first-party extensions may request secrets
(`requires_secret=(…)`), makes "add a package" the moment you audit it, catches
*honest* misuse, and maps onto the DB ACL (an allowlisted extension ⇒ a
role/`vault.acl` entry). Enforcement stays at the DB `GRANT`/ACL; the list
documents intent. It is **not** a wall against a malicious dependency — for
that, the DSN-holding process must run only pinned, audited code.

## The web page

`/secrets` (behind precis-web auth). Reads **`vault.list()` only** — the page
never decrypts, never holds ciphertext, cannot reveal even if compromised.

- One row per secret: `name`, masked `hint`, `updated_at`.
- **Write-only update.** Each row's input is empty, placeholder = the hint.
  Blank on submit = unchanged; only a non-empty value calls `set_secret`. The
  form can never round-trip existing values, so a stray "Save" is a no-op
  across the board — you must type into one field to change that one secret.
- Per-row edit toggle + confirm. No bulk op, no export affordance exists.
- Reuse the `_redact` / sensitive-name convention already in
  `precis_web/routes/env.py`.

## asa

asa is already a precis *client* (long-lived `precis serve` MCP subprocess +
a direct DB connection for `pg_listen`). For secrets it becomes a **vault
consumer**, not a repo merge: `ASA_DISCORD_TOKEN` moves into the vault; asa
fetches it via `vault.reveal` over the ACL-scoped `asa` role (or a `secrets`
MCP verb). This is independent of — and mildly *reduces* the pressure for — the
standing fold-asa-into-precis question.

## Build order

1. Migration `0059_secrets_vault.sql` — extension, schema, tables, `_hint`,
   the three verbs + `set_secret`, grants, roles. (Key set out-of-band via
   `ALTER SYSTEM` on each DB host — an ops step, not a migration.)
2. `precis.secrets.get_secret` resolver (env-override-wins + cache + notify).
3. Migrate a first call site (`PERPLEXITY_API_KEY`) off raw `os.environ`;
   `requires_env` → `requires_secret`.
4. `/secrets` web page (list + write-only set).
5. asa → vault consumer for `ASA_DISCORD_TOKEN`.
6. Retire `~/.secrets/pw/*` once every call site is migrated.

Ships dark: nothing changes behavior until a secret is actually written to the
vault, because env-override wins.
