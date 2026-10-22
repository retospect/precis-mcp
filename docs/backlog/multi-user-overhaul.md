---
status: draft
title: Multi-user overhaul — four roles, tiered Postgres roles, per-name vault ACL, authenticated human actor
pillar: platform
prio: high
---

# Multi-user overhaul

Reto 2026-10-21 (via chat-99): "inspect and plan for a comprehensive
multi-user overhaul ... Keep role sprawl minimal (read only, normal user,
admin, maybe development agent). Compile a comprehensive plan that is
secure but not arduous to maintain, and to run." Inspection only; nothing
below is built. Spec of record for the role model; the items it links keep
their own slices until they ship.

Links: `identity-and-access.md` (visibility filter, per-transaction
identity, RLS phase 2 — decided design, reused here),
`multiuser-separation.md` (fail-open gap list, absorbed by S6),
`mcp-session-user-identity.md` (per-user MCP token, S5),
`email-per-user-credentials.md` (S7), `per-user-library-link.md` (S7),
`web-basic-auth-users.md` (residuals), `db-role-enforce-pgbouncer.md`
(answered by S1's per-transaction role), `review-container-readonly-role.md`
(S1), `rotate-credentials.md` (the agent_rw pause window S1 rides),
`graph-node-trust-state.md` §2b rulings Q2 (S4), `slullama-hpc-placement.md`
D1/D2 (S1, S2), thread `threads/security-hardening.md`,
`out-of-box-setup.md` (the admin users screen needs S3),
`mcp-seen-state-dedup.md` (needs S10.0 + S10b's per-agent key).

## Motivation / why

One real user, one credential tier. Every daemon, every agent and every web
login holds the same authority: `agent_rw` reads every secret, writes every
setting, edits every row, and asserts any actor string. Accepted on
2026-10-21 for HPC D1/D2 and trust Q2 as "risk accepted for now; revisit in
the multi-user overhaul". This is that revisit, plus the roster roles the
out-of-box admin screen is blocked on (`cli/users.py` docstring refuses a
web create-user affordance while every account is fully authorised).

## Inventory — single-trusted-user assumptions

Verified against the tree on 2026-10-21. One row per assumption; the
`Closed by` column names the slice below.

| # | Anchor | Assumption | Risk once multi-user | Closed by |
|---|---|---|---|---|
| 1 | `src/precis_web/auth.py` module docstring; `migrations/0131_web_users.sql` | every `web_users` row is fully authorised; no role column, no per-route ACL | any login is admin | S3 |
| 2 | `src/precis_web/routes/secrets.py` (writes via `vault.set_secret`/`delete_secret`) | any login replaces or deletes any fleet secret | credential tampering by a non-admin | S3, S1 |
| 3 | `src/precis_web/routes/settings.py::set_setting`, `routes/budget.py`, `routes/factory.py` (`set_setting` callers) | any login edits app settings, spend caps, cloud enable, chain config | spend and routing changed by a non-admin | S2, S3 |
| 4 | `src/precis_web/routes/console.py` docstring | every verb incl. `put`/`delete`, as `web:owner` | full-write console for any login | S3, S6 |
| 5 | `src/precis_web/routes/env.py`, `routes/agentlogs.py` | operator introspection (agent env, prompts, logs) readable by any login | prompt/config disclosure | S3 |
| 6 | `src/precis_web/routes/account.py` (`discord_user_id` form), `src/asa_bot/identity.py::resolve_speaker` | a login self-claims any Discord snowflake; asa then attributes that Discord user's turns to the claimant | identity squat; writes attributed to the wrong account | S9 |
| 7 | `deploy/roles/precis_web/defaults/main.yml::precis_web_funnel` | Basic auth is the whole boundary when funnelled to the public internet | with #1, any account is an internet-reachable admin | S3 |
| 8 | `src/precis_web/deps.py::human_write_context` | web identity is attribution (`revisions.actor`) only, never authorisation | — (the seam S3/S6 build on) | S3 |
| 9 | `src/precis/server.py::_install_token_auth` | one install-wide bearer token on the HTTP MCP transport; no principal | every network session is the same anonymous caller | S5 |
| 10 | `src/precis/acting_user.py::install_from_env` | identity on stdio comes from the spawner's env; ignored on the network transport | shared-server sessions all act as `agent` | S5 |
| 11 | `src/precis/tools/core.py::_dispatch` | no per-caller authorisation at the verb layer (audit log only) | reader/user cannot be distinguished from admin | S6 |
| 12 | `src/precis/handlers/profile.py::_check_write`, `precis.profiles::acting_login` | the only ownership check; `None` identity passes (gr490266 gaps in `multiuser-separation.md`) | unidentified callers bypass the one ACL | S6 |
| 13 | `src/precis/handlers/_numeric_ref.py` (edit/delete) | no ownership check on memory, todo, quest, gripe, draft, finding, conv | any caller edits or deletes anyone's rows | S6 |
| 14 | `delete` leaves inbound `qualifies` edges (gr503031) | a deleted node's edges stay traversable | a private node's existence leaks through dangling links | S6 (precondition) |
| 15 | `src/precis/todo/guards.py` (`$PRECIS_SOURCE` verdict) | owner-vs-worker from a process-wide, self-declared env var; unknown → owner | any process claims owner | S6 |
| 16 | `src/precis/workers/envelope.py::disallowed_tools` | tool-level deny lists are profile-dependent (gr503441) | an envelope that reads as read-only is not | S1 (DB role is the backstop) |
| 17 | `deploy/roles/postgres/tasks/main.yml` ("APPLICATION roles only") | `agent_rw` = MCP + workers + web; `agent_ro` exists but is rarely bound | no tier between a worker and a human's browser session | S1 |
| 18 | `src/precis/store/pool.py::_apply_db_role` | session-level `SET ROLE`, incorrect under pgbouncer transaction pooling | the read-only guarantee is unenforceable on prod DSNs | S1 (per-transaction `SET LOCAL ROLE`) |
| 19 | `review-container-readonly-role.md` | review containers still connect as `agent_rw` | a "read-only" agent can write | S1 |
| 20 | `migrations/0059_secrets_vault.sql` grants; `0111_vault_events_client_identity.sql`; `precis.secrets` docstring ("per-name ACL ... deferrable") | `vault.list/mask/reveal/set_secret/delete_secret` granted to PUBLIC | every DSN holder reveals every secret (HPC key, OAuth token, mailbox) | S1 |
| 21 | `vault.events.who = session_user` (0059), client identity self-reported (0111) | audit says `agent_rw` for every reveal | leaks are untraceable to a principal | S1, S5 |
| 22 | `precis.users::feed_token_secret_name` (`NAME:<login>` convention), `/secrets` excluding `:` names | per-user secrets are a naming convention | any DSN holder reveals another user's tokens | S1, S7 |
| 23 | `precis.mail.account::default_secret_name`; `email_account` has no owner (migration 0075) | mailbox credentials are cluster-scoped | any caller reads any mailbox | S7 |
| 24 | `slullama-hpc-placement.md` D1 | HPC SSH key and passphrase are vault refs every DSN holder can reveal | key exfiltration by any process | S1 |
| 25 | `src/precis/settings.py::set_setting`; `migrations/0125_app_settings_updated_by.sql` | `app_settings` writable by `agent_rw`; `updated_by` is a self-reported summary | D2: SSH host/port/user/host-key pins repointed by any writer | S2 |
| 26 | `migrations/0185_reviews_and_revisions.sql` (`reviews.actor text`, `model NULL = a human`) | review actor is whatever the caller states (trust 2b Q2 lax) | a machine asserts a human verdict; admit gate trusts it | S4 |
| 27 | `src/precis/store/revision_context.py` (`set_config('precis.actor', ..., true)`) | the actor GUC is caller-set; attribution, not authentication | any DSN holder writes as any user | S4, S6 |
| 28 | `migrations/0164_refs_owner_login.sql` | `owner_login` written by profile and anki only; NULL = commons; `ON DELETE SET NULL` | deleting a user turns their private rows public (`identity-and-access.md`) | S6 |
| 29 | worker claim loop (`src/precis/workers/dispatch.py`, `envelope.py`) | jobs carry an envelope, not a requesting user; workers run with system authority | a user-originated job runs wider than its user | S6 |
| 30 | `src/asa_bot/config.py::allowed_channels` (empty = all), `asa_bot/identity.py` | unmapped Discord speakers write as `agent` with full reach | any channel member is an unidentified full-write agent | S9 |
| 31 | `src/precis/handlers/memory.py` (`SPACE:` tags, `user:<handle>` tag) | memory spaces are namespaces, not ACLs; tags are mutable metadata | a user reads or retags another's memory | S6 |
| 32 | `src/precis/cli/users.py` | roster CLI has no role; docstring refuses a web create-user screen | blocks the admin screen in `out-of-box-setup.md` | S3 |

Postgres roles today: `agent_rw` (MCP, workers, web; the migrating role),
`agent_ro` (read-only; `GRANT agent_ro TO agent_rw` applied on prod),
`prometheus_reader` (exporters, out-of-tree). Ownership columns today:
`refs.owner_login` (0164), `ref_favorites.login` (0211),
`drive_*.user_id`/`owner_user_id` (0220), `web_users.discord_user_id`
(0196). Not a hole, a decision: fetched kinds (paper, patent, web, cache
kinds) are a shared commons (`identity-and-access.md`, decided).

## Role model

Exactly four user-facing roles, one app-level column, Postgres roles by
authority tier (never per human — decided in `identity-and-access.md`).

### The boundary, stated once

A process that holds a DSN is at least `dev-agent` at the DB level: it can
set any GUC, call any PUBLIC function and `RESET ROLE`. `reader` and `user`
are therefore enforced by the daemons that authenticate humans (web, the
shared HTTP MCP server), and backstopped at the DB by what the daemon's
*own* role may do. Human identity is trustworthy only when asserted by an
ingress daemon's role. RLS (`identity-and-access.md` phase 2) is the later
DB-level backstop for `user`; every slice below keeps identity
per-transaction so RLS stays a transcription.

### Roles

| Role | Who | Postgres role (connection) | Web | MCP verbs | Secrets |
|---|---|---|---|---|---|
| `reader` | guests, reviewers, read-only containers | `agent_ro` (or `agent_rw` + `SET LOCAL ROLE agent_ro` per transaction) | GET only; `/account` self | `get`, `search`, `more` | none |
| `user` | a normal human | ingress daemon's role, principal = `web_users.id` in the transaction GUC | all content pages; POST on own rows and commons; no `/secrets`, `/settings` (operator keys), `/budget`, `/factory`, `/env`, `/console` writes | all seven verbs; private kinds scoped to owner; commons kinds writable | `user/<id>/*` only |
| `admin` | operators | same as `user`; admin routes re-authenticated | everything, incl. `/admin/users` | all verbs, all rows | all names via the vault editor (write-only); reveal only through the probe |
| `dev-agent` | stdio `precis serve` (Claude Code sessions, scripts), cluster agents with write envelopes, the install-token MCP client | `agent_rw` | none | all verbs, all rows; cannot assert a human actor | API keys (`svc/api/*`); never `svc/infra/*` or `user/*` |

Service principals (not user-facing): workers, `precis migrate`, cron →
`precis_system`; the web daemon and the shared HTTP MCP server →
`precis_ingress`. Workers are not a fifth role because they never
authenticate a human and must read every row (embedding, synthesis); the
only authority they need beyond `dev-agent` is `svc/infra/*` secrets and
operator settings, which is exactly what `precis_system` adds. Folding them
into `admin` would hand every worker the HPC key.

Postgres roles after the overhaul: `agent_ro`, `agent_rw`, `precis_ingress`
(member of `agent_rw`), `precis_system` (member of `agent_rw`). Four roles,
created by deploy (`deploy/roles/postgres`) and by `precis setup --roles`
(`out-of-box-setup.md`), never by a migration (`agent_rw` has no
`CREATEROLE`).

### Mechanisms

- **Role column.** `web_users.role text NOT NULL DEFAULT 'user' CHECK (role
  IN ('reader','user','admin'))`. `dev-agent` is not a roster row: it is the
  role of any principal that is not a web user (stdio serve, install token).
  Backfill: existing rows → `admin` (today's roster is the operator).
- **Principal.** One frozen dataclass `Principal(user_id, login, abbrev,
  role, source)` resolved at the trusted boundary (web middleware, MCP token
  check, stdio env) and carried in a ContextVar; `precis.profiles::acting_login`
  becomes `principal().login`. Set per transaction alongside the existing
  `precis.actor` GUC (`revision_context.py`), never per session.
- **Per-transaction DB role.** `pool.py::_apply_db_role` moves from session
  `SET ROLE` to `SET LOCAL ROLE` inside each transaction (valid under
  pgbouncer transaction pooling); answers `db-role-enforce-pgbouncer.md`.
  A guard against bugs, not against a hostile DSN holder (see boundary).
- **Per-name vault ACL.** `vault.acl(pattern text, grantee regrole)`;
  `vault.reveal` checks `pg_has_role(session_user, grantee, 'MEMBER')` for
  the first matching pattern; no matching row → allow (slice 1) → deny
  (slice 1b, once every name is classed). Name classes: `svc/infra/*`
  (`precis_system`), `svc/api/*` (`agent_rw`), `user/<id>/*`
  (`precis_ingress`, checked against the transaction principal inside the
  function). Migration renames existing names into classes with a
  compatibility view for one release.
- **Authenticated human actor.** `reviews` rows with `model IS NULL` and
  any `revisions.actor LIKE 'user:%'` are accepted only through
  `assert_human_actor(abbrev)` — SECURITY DEFINER, granted to
  `precis_ingress` only, which sets `precis.actor` and a
  `precis.actor_asserted` GUC the triggers check. A `dev-agent` stating a
  human actor is recorded as `agent` with the claimed string in `reason`.
- **Settings write scopes.** `precis.settings` registry gains `scope:
  state | operator`. `state` keys (worker cursors, throttles) stay open to
  `agent_rw`; `operator` keys (budget caps, cloud enable, chains, HPC host/
  port/user/host keys) write only via `set_operator_setting()` SECURITY
  DEFINER granted to `precis_ingress` (behind the admin role + re-auth) and
  `precis_system`.
- **Ownership and visibility.** Adopt `identity-and-access.md` as is:
  `KindSpec.visibility: commons | private`, `build_visibility_filter`
  injected at the user-reachable read sites, write check at `_dispatch`,
  derived artifacts inherit the source owner, user-originated jobs run as
  their user. Key new ownership on `web_users.id` (Reto 2026-10-06);
  migrate `refs.owner_login` → `owner_id` when S6 touches it. Fail closed
  for a `user` principal with no identity; `dev-agent`/`admin`/system pass
  (today's behaviour). Row ownership stays per-ref; `shares` is deferred
  until a second real user exists.

## Migration path — independently shippable slices

Order: S1 → S3 → S2 → S4 → S5 → S10 → S6 → S7 → S9 → S8. S1 and S2 close
the accepted HPC risks, S4 closes trust Q2; S3 unblocks the admin screen.
S10.0 (an experiment, no code) can run any time; `mcp-seen-state-dedup.md`
needs only S10.0 and S10b, not the rest of this spec.
Cost = builder days (Sonnet/Opus) excluding Reto's windows.

| Slice | Delivers | Cost | Blast radius | Closes |
|---|---|---|---|---|
| **S1** vault ACL + `precis_system` | migration: `vault.acl`, `vault.reveal` check (default allow), name classes; deploy: create `precis_system`, workers + migrate switch DSN; `pool.py` `SET LOCAL ROLE`; review containers bind `agent_ro`; ACL rows: HPC key+passphrase, Claude OAuth token, anki password, mailbox → `precis_system` | 3 d + one agent_rw pause window (ride `rotate-credentials.md`) | every `get_secret` path; `/secrets` probe becomes presence-only for `svc/infra/*`; worker DSN rotation | HPC D1 (full: add a controller-only ACL row once a second worker host exists), #16–#22, #24 |
| **S1b** ACL default-deny | flip the no-match default once `vault.list()` shows every name classed | 0.5 d | any unclassed name stops resolving (loud, named) | #20 |
| **S2** settings scopes | registry `scope`, `set_operator_setting()`, trigger refusing operator keys from plain `agent_rw`; `/settings`, `/budget`, `/factory` writes gated on admin (needs S3) | 2 d | every `set_setting` caller (37 sites; state keys unchanged) | HPC D2, #3, #25 |
| **S3** roster role + web gate | migration `web_users.role` (backfill admin); `precis users add --role`, `precis users role`; `auth.py` route classes (admin prefixes, reader GET-only); admin re-auth (sudo window, current password); `/console` read-only verbs for `user`/`reader` | 3 d | every web route; CLI roster | #1, #2, #4, #5, #7, #32; unblocks `out-of-box-setup.md` O6 |
| **S4** `precis_ingress` + human actor | deploy: `precis_ingress`, web + shared MCP server DSN; migration: `assert_human_actor()`, triggers on `reviews`/`revisions`; `human_write_context` calls it | 2 d | trust 2b admit gate (human approvals must come via web or S5); `revisions.actor` semantics | trust Q2, #26, #27 |
| **S5** MCP principal | `mcp-session-user-identity.md` as specced; install token → `dev-agent`; per-user token → that user's role; `requesting_user()` → `principal()` | 3 d | `server.py` auth, `/account`, shared server config | #9, #10, #21 |
| **S6** verb authorisation + ownership | `identity-and-access.md` phase 1 + `multiuser-separation.md` gaps; `_dispatch` role gate (reader read-only); `owner_id` migration; delete unlinks inbound edges (gr503031) first; `todo/guards.py` reads the principal | 8 d | search/get family, every mutate verb, worker claim loop, synthesis passes | #11–#15, #28, #29, #31 |
| **S7** per-user secrets | `user/<id>/*` class live; `email-per-user-credentials.md`, `per-user-library-link.md` | 3 d | email handler + mail workers, `/secrets`, `/account` | #22, #23 |
| **S8** RLS backstop | `identity-and-access.md` phase 2 | 3 d | every table with `owner_id` | DB-level `user` enforcement |
| **S10** agent caller identity | see [§S10](#s10--agent-caller-identity-from-the-plumbing): S10.0 experiment, S10a launcher credentials, S10b subagent stamp, S10c no-identity → `reader` | 0.1 + 2 + 1 + 0.5 d | every launcher's MCP config, `server.py` auth, `_dispatch` | #9 for launched agents; per-agent attribution in `tool_ledger` |
| **S9** Discord binding | link confirmed from the Discord side (asa DM challenge) or by an admin; unmapped speakers → `reader` | 1 d | `asa_bot` turn attribution | #6, #30 |

Each slice ships with its tests, its skill/docstring update
(`precis_web/auth.py`, `precis.secrets`, `precis.settings`,
`precis-overview` refusal hints) and deletes the backlog items it closes.
Migrations go through `/go` (migration rule), never `/qgo`.

## S10 — agent caller identity from the plumbing

Reto 2026-10-22: identity of an agent caller comes from the code that
launches or hosts it, never from the model. This replaces the earlier idea
of an "agent cookie" the model passes in each call (not built, see below).
Same rule as Q5: the code knows, the LLM does not.

### Problem

S5 identifies a *human* behind a connection. It says nothing about which
*agent* is calling: every Claude Code session, each of its subagents, every
`claude -p` worker, every Devin and every Agent SDK loop on the shared HTTP
server presents the same install bearer token
(`server.py::_install_token_auth`). Two consequences: `tool_ledger` cannot
attribute a call to an agent (`session_id` there is one row per MCP
connection, and a subagent shares its parent's connection), and the server
cannot keep per-agent state. That second gap is why the per-session skill
serve ledger was removed (gr477798, `serve_ledger.py` docstring): a subagent
on the parent's connection got stubs for skills only its parent had seen.
`mcp-seen-state-dedup.md` needs the per-agent key this slice supplies.

### Design

**Two sources of identity, both plumbing:**

1. **Launched agents get their own credential.** The launcher mints a
   credential per agent and writes it into that agent's MCP connection
   config: the `--mcp-config` file `utils/claude_agent.py` passes, Agent SDK
   `mcpServers` per `query()`, the fleet tree's MCP config written by
   `scripts/devin-tree` / `scripts/fleet-wave`, and the job envelope for
   cluster quest/job agents. It travels as the bearer token in the HTTP
   `Authorization` header. The model never sees it in its prompt, tool
   results or errors. Accepted risk (Reto): an agent with a shell can read
   its own env or config file.
2. **Claude Code subagents are stamped by a hook.** A subagent shares its
   parent's MCP connection, so it cannot have its own header. A PreToolUse
   hook on `mcp__precis__.*` reads the hook input's `session_id`,
   `agent_id` and `agent_type`, and writes them into the call as
   `args._caller` via `hookSpecificOutput.updatedInput`. The hook overwrites
   any `_caller` the model wrote. `_dispatch` strips `_caller` before
   handlers run and binds a `caller()` ContextVar (`session_id`, `agent_id`
   or `main`, `agent_type`). A forged stamp (hook not installed, model
   wrote `_caller`) can only narrow rights or split seen-state, never widen
   (rule below), so it is harmless.

**Credential store.** Table `agent_credentials`: `handle_sha256` (the
handle itself is 128 random bits, shown once to the launcher, stored as a
digest), `parent_id`, `principal_user_id` (NULL = service), `role`
(`reader | user | dev-agent`; never `admin`), `label` (launcher + job id),
`expires_at`, `revoked_at`. A handle is opaque: it decodes to nothing and
is useless without the server row. Mint API is in-process code
(`precis.agent_identity.mint(parent=, role=, ttl=, label=)`), not a verb.

**Rights rule.**

- No identity → `reader` (least privilege by absence). Enforced by S10c,
  once every launcher mints.
- A child's role is `min(parent role, requested role)`. Rights never widen:
  `mint` refuses a role above the parent's; a stamped subagent carries the
  connection's principal and role.
- Subagents share the parent's identity for now (Reto, Q3 ruling relayed
  2026-10-22: "pin and close for now"). `agent_id` is recorded for
  attribution and seen-state only; narrowing a subagent by its definition's
  tool list is open question 11.

### Slices

| Step | Delivers | Acceptance |
|---|---|---|
| **S10.0** experiment (15 min, no code shipped) | In a scratch Claude Code session with a logging PreToolUse hook on `mcp__precis__.*` and one subagent dispatch, record: (a) main-session payloads omit `agent_id`, subagent payloads carry it (`edit-delegation-nudge.py` already assumes this, unverified for MCP tools); (b) `updatedInput` on an MCP tool call reaches the server (see `_caller` in `tool_ledger.arg_sample`); (c) a subagent whose definition has an inline `mcpServers` entry opens its own connection (distinct `tool_ledger.session_id`) with its own headers; (d) whether SessionStart `source=compact` or PreCompact fire for a *subagent's* compaction, and with which `agent_id`. Write the four answers into this section. | Four recorded answers with Claude Code version. Fallback if (b) fails: subagents that need their own key get an inline `mcpServers` entry in `.claude/agents/*.md` (only if (c) holds); built-in subagents (Explore, general-purpose, fork) stay unkeyed and are treated as the parent. |
| **S10a** launcher credentials | migration `agent_credentials`; `precis.agent_identity` (`mint`, `resolve`, `revoke`); `_install_token_auth` accepts a handle as bearer and binds the principal + role; the install token keeps working as `dev-agent`; `claude_agent.py`, the worker envelope and the fleet launch scripts mint one handle per agent with `ttl` = job budget; `tool_ledger` gains `agent_credential_id` | Two concurrent launched agents show distinct `agent_credential_id` on every ledger row; a revoked handle gets 401 on its next request; a canary handle never appears in any prompt, transcript, tool result or log (reuse the Q5 canary test); `mint` above the parent's role refuses. |
| **S10b** subagent stamp | `scripts/hooks/mcp-caller-stamp.py` (stdlib, every doubt passes the call unchanged), wired PreToolUse in `.claude/settings.json` and the global copy; `_dispatch` strips `_caller`, binds `caller()`; `tool_ledger` gains `caller_session_id`, `agent_id`, `agent_type` | A main-loop call logs `agent_id=main`; a subagent call logs its `agent_id`; a handler never receives `_caller`; a call whose model-written `_caller` names a wider role runs at the connection's role. |
| **S10c** no-identity → `reader` | the install token maps to `reader`; interactive Claude Code sessions get a per-machine `dev-agent` handle from `scripts/mcp-http-install`; stdio keeps its env identity | A request with the bare install token can `get`/`search`/`more` and is refused on `put` with a hint naming `scripts/mcp-http-install`; trigger in open question 12. |

### Not built

- **In-call agent cookies** (the model passes a token in each call's args):
  the model would see and could copy the credential, and every prompt would
  need to carry it. Replaced by S10a/S10b.
- A proxy that hides launched agents' credentials from their own shell
  (accepted risk, as in Q5).
- Per-subagent rights narrowing (open question 11).

## In scope

The role model, the four-tier Postgres role set, the vault ACL, settings
scopes, the authenticated human actor, the principal seam, and the slice
order above.

## Explicitly NOT in scope

- Groups, sharing (`shares`), share links, OIDC/SSO, Tailscale-header auth
  (web stays Basic auth; `identity-and-access.md` open decisions stay open).
- Per-human Postgres roles or DSNs (rejected, `identity-and-access.md`).
- Rewriting worker SQL sites for visibility (workers stay system authority).
- Budget per user (`budget-guardrails.md`), spend attribution per user.
- The admin users screen itself (`out-of-box-setup.md` O6).
- In-call agent cookies (replaced by S10's plumbing identity).

## Acceptance criteria

- `vault.reveal('svc/infra/hpc_ssh_key')` from an `agent_rw` connection
  refuses; from `precis_system` succeeds and audits (S1).
- `set_setting` on an operator key from `agent_rw` refuses with the admin
  route named; from `/settings` as admin after re-auth succeeds (S2).
- A `reader` login gets 403 on every POST except `/account`; a `user` login
  gets 403 on `/secrets`, `/settings` operator keys, `/budget`, `/factory`,
  `/env`, `/console` write verbs; `precis users list` shows the role (S3).
- A `reviews` row with `model IS NULL` cannot be inserted from a stdio
  serve; the same verdict via the web lands with `actor='user:<abbrev>'`
  (S4).
- Two per-user MCP tokens in interleaved sessions never see each other's
  principal; the install token is `dev-agent` (S5).
- A `user` principal: `get` by id of another user's private ref is absent,
  `edit`/`delete`/`tag` refused; a job the user dispatched reads and writes
  as that user; deleting a memory removes its inbound edges (S6).
- Two launched agents and two subagents of one session each show their own
  identity on every `tool_ledger` row; no credential handle reaches model
  context; a bare install token cannot write after S10c (S10).
- Fresh single-user install: one admin row, everything behaves as today.
- `tests/test_deploy_tree_no_secrets.py` green; migrations forward-only.

## Target + blast radius

`src/precis_web/auth.py` · `src/precis/users.py` + `cli/users.py` ·
`src/precis/secrets.py` + `migrations/` (vault.acl, role column, settings
scope, human-actor functions) · `src/precis/settings.py` ·
`src/precis/store/pool.py`, `revision_context.py` · `src/precis/profiles.py`
· `src/precis/tools/core.py::_dispatch` · `src/precis/server.py` ·
`src/precis/todo/guards.py` · `src/asa_bot/identity.py` ·
`deploy/roles/postgres`, `deploy/roles/precis_web`, `deploy/mcp-http` ·
skills `precis-overview`, `precis-status`.

## Decisions (Reto 2026-10-21, batch 11)

Q1–Q10 approved as recommended, with two changes:

- **Q5, changed: the LLM never sees secret values; code may read them.**
  Reto: "the code that starts the agent needs to know, but the agents
  should not be able to see", refined the same day: "The code can access
  the keys, but the LLM should not see the keys; we don't want to make it
  super weird or hard just yet." The `dev-agent` secrets column stands as
  proposed (`svc/api/*` readable by the code, never `svc/infra/*` or
  `user/*`); handler code in a stdio session's own process may read it, and
  a launched agent may keep a key in its environment. Rule: no secret value
  enters model context. No verb result, skill, error message, log line,
  prompt or job transcript carries one; the vault editor stays write-only
  and the probe reports status, never the value. Acceptance: a test that
  sets a known canary secret, exercises the paid-call paths, error paths
  and `precis doctor`, and greps every agent-facing output for the canary.
  Accepted risk for now (same lax trust as Q2): an agent with a shell can
  still read its own process environment or the config file; no proxy or
  routing through the shared daemon is built for this.
- **Q8, extended: Discord is private too.** Discord-sourced content (asa
  turns, `message`/`conv` rows from Discord) defaults private to its
  bound speaker, alongside memory, draft, conv, profile, anki and email.
  Unmapped speakers stay `reader` (S9).

## Open questions for Reto (each with a recommendation)

1. **Boundary statement.** Accept that `reader`/`user` are daemon-enforced
   and a DSN holder is at least `dev-agent` until RLS (S8)? Recommendation:
   yes; it matches the friendly-academic threat model and keeps the Postgres
   role count at four.
2. **Two new Postgres roles** (`precis_ingress`, `precis_system`) or one?
   Recommendation: two. Ingress is what makes a human assertion trustworthy
   (Q2); system is what keeps the HPC key out of every daemon (D1). One
   role would give the web daemon the HPC key.
3. **Backfill role.** Existing roster rows → `admin`, new accounts default
   `user`? Recommendation: yes; a fresh single-user install then needs no
   role flag.
4. **Admin re-auth shape.** Current password on every admin POST, or a
   signed 10-minute sudo cookie after one re-entry? Recommendation: sudo
   window, 10 min, for `/admin/users`, `/secrets`, operator settings;
   password change at `/account` keeps its per-POST check.
5. **`svc/infra/*` membership.** HPC key+passphrase, Claude OAuth token, DB
   credentials, anki password, mailbox passwords; API keys stay `svc/api/*`
   (readable by `dev-agent`, since session handlers call paid APIs).
   Recommendation: as listed.
6. **HPC D1 full closure.** `precis_system` for all workers now, or a
   controller-only role immediately? Recommendation: `precis_system` now;
   add the controller-only ACL row when a second worker host runs jobs
   that are not ours.
7. **`/console` for non-admins.** Remove, or read-only verbs?
   Recommendation: read-only (`get`/`search`/`more`) for `user` and
   `reader`; full for `admin`.
8. **Quests, todos, gripes visibility.** Commons (all users read, owner or
   admin writes) or private by default? Recommendation: commons for quest,
   todo, gripe, job, finding, paper-derived kinds; private for memory,
   draft, conv, profile, anki, email. Reuse `KindSpec.visibility`.
9. **Slice order.** S1 first (independent, closes D1) and S3 before S2
   (S2's gate needs the role column)? Recommendation: yes, as listed; S4 can
   run parallel to S3 since it touches deploy and migrations only.
10. **`owner_login` → `owner_id`.** Migrate in S6 (first slice touching it)
    per the 2026-10-06 ruling? Recommendation: yes, with a one-release
    compatibility column.

### S10 (2026-10-22)

11. **Subagent rights.** Ruled (Reto 2026-10-22): a subagent keeps its
    parent's identity unless the parent asks for a narrower one (stretch
    goal: parent-requested narrowing via a child credential, rights never
    widen).
12. **When S10c flips the bare install token to `reader`.** Ruled (Reto
    2026-10-22): after seven days with no write call from a connection
    carrying only the shared install token (the one bearer token every
    client on a machine presents today), except the interactive sessions
    S10c re-issues handles to.
