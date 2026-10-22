---
status: draft
title: Out-of-box setup — `precis setup`, `precis doctor`, first-run page, onboarding guide, admin users screen, clean-container CI
pillar: platform
prio: high
---

# Out-of-box setup

Reto 2026-10-21 (via chat-99): "When a user follows the git instructions,
it should be easy to get started. Maybe the pip has a setup command that
generates the appropriate databases and installs the right modules if
missing. There is no way to add a user, so if a site is just stood up with
an empty db we should have an onboard new user guide. Also a create new
user screen under admin (password protected)." Inspection only; nothing
below is built. Role precondition: `multi-user-overhaul.md` S3.

## Motivation / why

Today's path is `docs/setup-single-machine.md`: `pip install
'precis-mcp[all]'`, `createdb`, `CREATE EXTENSION vector`, `precis migrate`,
pick an embedder, `precis worker`, `precis serve`, `precis users add`,
`precis web`, `precis secret set`. Eight manual steps, three of which fail
late: a missing extra surfaces as a traceback at call time (gr503047:
`ModuleNotFoundError: rapidfuzz` from a tree synced without `paper`), a
missing account surfaces as a 503 on every web page (`precis_web/auth.py`),
and a missing `app.secret_key` surfaces as a dark vault. `precis users`
(`src/precis/cli/users.py`) exists but every account is fully authorised, so
its docstring refuses a web create-user screen; roles come first.

## In scope

### O1 `precis doctor` — landed (oobe-o1)

New command `src/precis/cli/doctor.py`, read-only, exit non-zero on any
FAIL, one line per check with the next command on failure. Checks: Python
version; DSN resolves and connects; `vector`, `pg_trgm`, `btree_gist`
present; migration ledger at head (`precis.store.migrate`); vault key set
(`SHOW app.secret_key` via `vault.list()` succeeding); pepper present
(`precis.users.resolve_pepper`); roster non-empty and, after S3, at least
one `admin`; embedder: `mock` ok, `bge-m3` model cached, `remote` URL
answers; extras vs kinds: the `InitError` list the server would log at
boot, each with its extra name; worker heartbeat age (`precis heartbeat`
data); web port bound; tiered roles present when `--roles` was used.
Secrets are never printed; `--probe` runs `precis.secret_status` provider
probes (default presence-only). `/status` shows the same table for admins.

### O2 `precis setup`

New command `src/precis/cli/setup.py`, idempotent (every step checks before
acting; re-running on a finished machine prints "ok" per step), flags
`--database-url`, `--embedder mock|bge-m3|remote`, `--admin-login`,
`--admin-abbrev`, `--password-stdin`, `--install-extras`, `--roles`,
`--yes`, `--env-file PATH`. Steps, in order:

1. Preflight = O1's static checks (Python, `psql`/`pg_isready` on PATH).
2. Extras: `importlib.util.find_spec` on one sentinel module per extra
   (`sentence_transformers`→`embed`, `marker`/`rapidfuzz`→`paper`,
   `fastapi`→`web`). Missing: print the exact install line for the detected
   installer (`uv tool install --with`, `uv pip install`, or `pip install
   'precis-mcp[web]'`); run it only with `--install-extras`.
3. Database: connect to the maintenance DB of the given DSN; `CREATE
   DATABASE` if missing; `CREATE EXTENSION IF NOT EXISTS vector` (the
   migrate bootstrap adds `pg_trgm`, `btree_gist`); `ALTER DATABASE ... SET
   app.secret_key` from a generated 32-byte value when unset (the vault's
   key; `precis.secrets` docstring) — the only step needing the DB owner.
4. `--roles`: create `agent_ro`, `agent_rw`, `precis_ingress`,
   `precis_system` (`multi-user-overhaul.md`) with generated passwords
   written to the env file (0600), never printed. Default off: the
   single-machine install uses the invoking DB login as the app role, as
   today.
5. `precis migrate` in-process (`precis.cli.migrate`).
6. Embedder: write `PRECIS_EMBEDDER` to the env file; `bge-m3` warns about
   the ~2 GB first-use download and does not fetch it.
7. First admin: if `web_users` is empty, prompt login + abbrev, password
   on a no-echo prompt or `--password-stdin` (`cli/users.py::_read_password`,
   same floor as the web form); `ensure_pepper`; role `admin` once S3
   exists (fully authorised before). `--yes` never skips the password.
8. Write the env file (`~/.config/precis/precis.env`, 0600: DSN, embedder,
   role DSNs) and print the `export`/`source` line; print next steps:
   `precis worker`, `precis serve` + the `mcp.json` block from README §Run,
   `precis web`, `precis secret set NAME --prompt`, `precis doctor`.

Never: print a password or DSN with a password, bind web off loopback,
install anything without `--install-extras`, create an account from an
unauthenticated surface.

### O3 Extra-gated import refusal (closes gr503047)

`src/precis/cli/main.py` wraps command import/run: a `ModuleNotFoundError`
whose module is in the extras map (one dict, shared with O2's step 2)
becomes a one-line refusal naming the extra and the install line, exit 2,
no traceback. `scripts/prod-precis` passes the extras the command needs.

### O4 First-run state + onboarding guide

`precis_web/auth.py` already returns 503 with the `precis users add` line
when the roster is empty. Change: a rendered HTML page (no credentials
accepted, still 503) with the three lines — `precis setup`, `precis users
add <login> --abbrev <ab>`, `precis doctor` — and a link to the guide. No
create-admin form, ever: an unauthenticated route that mints credentials
is the surface the gate exists to remove (`cli/users.py` docstring).

Guide: new `docs/getting-started.md`, the 10-minute path: install →
`precis setup` → `precis doctor` → connect an agent (`mcp.json`) → first
`put`/`search` → `precis web` → secrets. README §Install and
`setup-single-machine.md` point at it; the latter keeps the manual steps as
the "what setup does" reference. `precis setup` prints the guide's path.

### O5 Clean-container CI job

`check.yml` job `oobe` (Linux, pgvector service, nightly `plan.full` plus
on changes to `src/precis/cli/{setup,doctor,migrate}.py`, `pyproject.toml`,
`docker/`, `docs/getting-started.md`): `uv build`; inside
`python:3.13-slim` (`--network host`): `pip install dist/*.whl[web]` (no
checkout, no dev deps), `precis setup --yes --database-url ... --embedder
mock --admin-login ci --admin-abbrev ci --password-stdin < secret`,
`precis doctor` exit 0, `precis web` boots and `/healthz` is 200 while `/`
is 401 (not 503), `precis eval "get(kind='skill', id='precis-status')"`
answers over stdio. Fails the nightly, not the gate, until it has run
green for a week.

### O6 Admin users screen (blocked-by `multi-user-overhaul.md` S3)

`src/precis_web/routes/users.py`, prefix `/admin/users`, admin role +
re-auth (sudo window from S3) on every POST; `check_same_origin` CSRF
already covers it. List (login, abbrev, name, role, state, last login);
add (login, abbrev, name, email, role; password generated server-side,
shown once, user changes it at `/account`); set role; disable/enable;
reset password (generated, shown once). No delete (`precis users rm` stays
CLI), cannot disable or demote yourself or the last admin, lockout counters
reset on password reset. Each action logs one INFO line with actor and
target login, no secrets. `cli/users.py` docstring updates: the refusal
becomes "behind the admin role".

## Explicitly NOT in scope

- Docker Compose quickstart (postgres + precis in one file) — separate
  item if wanted; the image entrypoint already self-migrates.
- A config-file loader in `PrecisConfig` (env stays the input; the env file
  is sourced by the user or a unit file).
- Service units (launchd/systemd) — `deploy/` owns those for the cluster.
- Password reset by email (refused in `cli/users.py`; unchanged).
- Any role enforcement — `multi-user-overhaul.md`.

## Acceptance criteria

- On a machine with Postgres and nothing else: `pip install precis-mcp
  && precis setup` ends with `precis doctor` all-OK and one admin row, in
  one terminal session, with the password never echoed.
- Re-running `precis setup` changes nothing and prints "ok" per step.
- A tree without `paper`: `precis resolve-metadata` prints the one-line
  extra refusal, exit 2, no traceback (gr503047).
- Empty roster: every web page is a 503 HTML page with the three commands;
  no form accepts input.
- O5 green on the nightly for a week before it joins the gate.
- O6: a `user` login gets 403 on `/admin/users`; an admin without a fresh
  re-auth is redirected to re-auth; the last admin cannot be disabled.
- `docs/getting-started.md` is the path a new reader lands on from README
  §Install.

## Target + blast radius

New `src/precis/cli/setup.py`, `cli/doctor.py`, `precis_web/routes/users.py`,
`docs/getting-started.md`; edits `src/precis/cli/main.py` (extras refusal),
`cli/registry.py`, `precis_web/auth.py` (503 page), `README.md`,
`docs/setup-single-machine.md`, `.github/workflows/check.yml`,
`scripts/prod-precis`. Reuses `precis.cli.migrate`, `precis.users`,
`precis.secret_status`, `precis.store.migrate`.

## Slices

| Slice | Cost | Blast radius | Depends on |
|---|---|---|---|
| O1 doctor | 1.5 d | new command; `/status` admin panel | — |
| O3 extras refusal | 0.5 d | `cli/main.py` import path, `scripts/prod-precis` | — |
| O2 setup | 3 d | new command; `migrate` reuse; env file | O1 (shares checks) |
| O4 first-run page + guide | 1 d | `auth.py` 503 body, README, setup doc | O2 (commands it names) |
| O5 CI job | 1 d + ~6 CI min/run | `check.yml` nightly | O2 |
| O6 admin users screen | 2 d | new admin route; `cli/users.py` docstring | `multi-user-overhaul.md` S3 |
| O7 `setup --roles` | 0.5 d | role creation + env file | `multi-user-overhaul.md` S1, S4 |

## Decisions (Reto 2026-10-21, batch 11)

Q11–Q18 approved as recommended (`precis setup`; extras only with
`--install-extras`; `~/.config/precis/precis.env`; vault key via
`ALTER DATABASE`; CI in a real python:3.13-slim container; generated
passwords shown once; admin screen disables, `rm` stays CLI; Compose
quickstart after O2). Secret values never reach model context (multi-user Q5
ruling): `precis setup` never prints one.

## Open questions for Reto (each with a recommendation)

1. **Name.** `precis setup` or `precis init`? Recommendation: `setup`;
   `init` reads as "this directory" (git, npm), and setup acts on a machine
   and a database.
2. **Installing extras from inside a running Python.** Only on
   `--install-extras`, by printing and running the detected installer's
   line? Recommendation: yes; silent installs change the environment the
   user did not ask to change, and `uv tool` installs need `--with`, which
   cannot be done from inside the tool's own venv.
3. **Env file vs config loader.** Write `~/.config/precis/precis.env` and
   tell the user to source it, or teach `PrecisConfig` to read a file?
   Recommendation: env file now (zero code in the hot path); revisit if
   service units for single machines are wanted.
4. **Vault key in setup.** Set `app.secret_key` on the database (`ALTER
   DATABASE ... SET`) rather than the server (`ALTER SYSTEM`)? Recommendation:
   database-level — needs the DB owner, not a superuser, and the key
   still never appears in a logical dump. Confirm `vault._key()` reads it
   (it reads `current_setting`, so yes).
5. **CI container.** Honest `python:3.13-slim` container with
   `--network host`, or a fresh venv on the runner? Recommendation:
   container; a venv on the runner inherits system packages and the
   checkout's cwd imports.
6. **Admin screen password handling.** Server-generated, shown once, or an
   admin-typed password? Recommendation: generated and shown once; an
   admin never knows a user's chosen password and the user changes it at
   `/account`.
7. **Delete from the screen.** Disable only, `rm` stays CLI?
   Recommendation: yes; delete cascades `ref_favorites` and nulls
   `refs.owner_login`, which is an operator decision, not a click.
8. **Docker Compose quickstart.** File it as its own item?
   Recommendation: yes, after O2 lands — compose then wraps `precis setup`
   rather than duplicating its steps.
