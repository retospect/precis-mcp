# Rotating the `agent_rw` DB password (and `OPENROUTER_API_KEY`)

> `agent_rw` is the prod DB role every precis daemon, the MCP, and the web app
> authenticate as. Rotating it touches nine render sites and needs a short
> coordinated window — Postgres roles hold exactly one password, so there is no
> overlap period. This is the ordered procedure. `OPENROUTER_API_KEY` is at the
> bottom and is much simpler.

## When

- A credential reached somewhere it shouldn't (an agent context, a shell
  history, a pasted log).
- Routine hygiene, or an operator leaving.

**Pick the window deliberately.** The planner is the heaviest DB client; a
rotation during a `plan_tick` storm means many in-flight jobs fail at once. The
cheapest window is while dispatch is frozen — check first:

    scripts/prod-psql "SELECT round(sum(cost_usd)::numeric,2) FROM llm_call_log
                       WHERE ts > now() - interval '24 hours'"

Over `PRECIS_DAILY_COST_CEILING` ⇒ the planner is parked and the window is free.

## The constraint that shapes everything

`pgbouncer` runs `auth_type = md5` against a **static** `userlist.txt` that
holds the password in cleartext (`roles/pgbouncer/templates/userlist.txt.j2`,
via `postgres_roles[].password_var`). So the password lives in two places that
must move together:

1. the Postgres role itself (`ALTER ROLE`), and
2. pgbouncer's `userlist.txt` (re-rendered + reloaded).

Change one without the other and **every** connection through `:6432` fails
instantly. There is no graceful overlap — plan for a ~1–2 minute auth gap while
the playbooks converge, and roll forward rather than trying to be clever.

## Where the secret is rendered

All nine come from the single vault var `vault_pg_agent_rw_pass` in the
gitignored `deploy/inventory` overlay. Nothing is hand-placed:

| Site | What it feeds |
|---|---|
| `roles/pgbouncer/templates/userlist.txt.j2` | pgbouncer client auth (**the gate**) |
| `roles/pgpass/templates/pgpass.j2` | `~deploy/.pgpass` + `~hermes/.pgpass` (0600) — the password-free DSN path |
| `roles/asa_bot/templates/config.yaml.j2` | asa's `ACATOME_PG_PASSWORD` |
| `roles/asa_bot/templates/com.asa.bot.plist.j2` | asa's inline DSN (`127.0.0.1:5433`) |
| `roles/asa_slack/templates/config.yaml.j2` | asa-slack's `ACATOME_PG_PASSWORD` |
| `roles/extract_watch/templates/extract-watch.plist.j2` | acatome-meta's `ACATOME_PG_*` |
| `playbooks/22-papers-sync.yml` | papers-sync `config.toml` |
| `run-reconcile.yml`, `run-fix-metadata.yml`, `run-migrate-refs.yml` | one-shot ops DSNs |

`roles/asa_bot/templates/claude_mcp.json.j2` is deliberately **not** on this
list: since 2026-08-07 the rendered `~/.claude/mcp.json` carries no credential
at all (`PRECIS_DATABASE_URL` is password-free, `PGPASSFILE` points at
`.pgpass`). Don't reintroduce an inline password there.

## Procedure

**1 — Mint the new password.** Avoid `@ : / ?` so it stays safe in a DSN:

    LC_ALL=C tr -dc 'A-Za-z0-9_.-' < /dev/urandom | head -c 40; echo

**2 — Update the vault.** In the `deploy/inventory` overlay:

    ansible-vault edit group_vars/all/vault.yml

Set `vault_pg_agent_rw_pass` to the new value. If `postgres_roles` names a
different `password_var` for `agent_rw`, update that var instead — the userlist
resolves through it.

**3 — Change the role in Postgres.** Direct to the data node on **5432**, not
through pgbouncer (you are about to invalidate the pooler's copy):

    ssh caspar "psql -h 127.0.0.1 -p 5432 -U admin -d precis_prod \
      -c \"ALTER ROLE agent_rw PASSWORD '<new>';\""

From here until step 4 finishes, connections through `:6432` fail. Expected.

**4 — Re-render and reload, pooler first.**

    ansible-playbook playbooks/02-postgres.yml    # userlist.txt + pgbouncer reload
    ansible-playbook playbooks/19-pgpass.yml      # ~deploy/.pgpass, ~hermes/.pgpass
    scripts/deploy                                # daemons + web pick up new DSNs
    ansible-playbook playbooks/31-asa-bot.yml     # asa (SSH_AUTH_SOCK= — see runbook)
    ansible-playbook playbooks/27-extract-watch.yml

**5 — Verify** — through the pooler, as the app does:

    scripts/prod-psql "SELECT current_user, now()"

Then confirm nothing is auth-looping:

    ssh caspar 'tail -50 /usr/local/var/log/pgbouncer.log' | grep -i "auth\|error"
    ssh melchior 'tail -30 /var/log/precis-worker-agent.log'

A `fe_sendauth: no password supplied` on a *sandboxed* job is a different,
known defect (gr196677 — `PGPASSFILE` pinning), not a rotation failure.

**6 — Sync the overlay to melchior** so its checkout matches, or the next
in-place render there re-writes the OLD password:

    git -C <deploy/inventory> push deploy-helper main

**7 — Purge the old value** from anywhere it leaked: shell history on the
touched hosts (`~/.zsh_history`), and any agent transcript you can reach. Treat
transcripts as unpurgeable — that is why the value is being rotated.

## `OPENROUTER_API_KEY`

Much simpler — no coordinated window, since the old key stays valid until you
revoke it:

1. Mint a new key at `openrouter.ai/keys`.
2. `ansible-vault edit` the overlay; set the OpenRouter key var.
3. `scripts/deploy` (every host reads it from the vault-injected env).
4. Confirm traffic flows on the new key (`llm_call_log` gains rows, or the
   OpenRouter dashboard shows the new key active).
5. **Only then** revoke the old key in the OpenRouter dashboard.

Because step 5 is last, this rotation has no outage — do it whenever.

## Related

- `docs/conventions/container-ops.md` — `scripts/prod-psql` / `scripts/db`
- `scripts/hooks/guard-secret-read.py` — denies wholesale reads of the files
  above; `ALLOW_SECRET_READ=1` for a deliberate rotation read
- memory `melchior_overlay_sync`, `asa_bot_oauth_and_deploy`
